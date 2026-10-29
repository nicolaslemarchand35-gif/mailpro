"""Moteur metier : synchronisation incrementale, analyse, taches, brouillons, classement.

Fiabilite :
- verrou distribue Firestore avec bail d'expiration (`jobs/sync`) ;
- curseurs SyncFolderItems par dossier, enregistres APRES l'ecriture des messages
  (au moins une fois + identifiants deterministes = pas de doublon) ;
- budget de temps par passage (lots courts) : le travail restant est repris au
  passage suivant, rien n'est perdu si le processus est arrete ;
- aucune operation d'envoi ; deplacements et brouillons journalises (`audit`).
"""
import re
import uuid
from datetime import datetime, timedelta, timezone
import time

from . import ai, config
from . import settings as settings_mod
from .ews import EwsBusy, EwsClient, EwsError, stable_hash
from .store import utcnow

SCHEMA_VERSION = 1
FOLDER_REFRESH = timedelta(hours=6)
LOCK_COL, LOCK_ID = 'jobs', 'sync'
SYSTEM_FOLDERS = ('msgfolderroot', 'sentitems', 'drafts', 'deleteditems', 'junkemail')
OPEN_TASK_STATUSES = ('proposee', 'a_faire', 'en_cours', 'en_attente')
ACTIVE_DRAFT_STATUSES = ('creation_en_cours', 'enregistre')


class UserError(Exception):
    """Erreur presentable a l'utilisateur (message en francais, sans donnee sensible)."""


def iso_day(value):
    return value.date().isoformat() if isinstance(value, datetime) else value


def compute_view(doc):
    if doc.get('direction') == 'sent':
        return 'envoye'
    treatment = doc.get('treatment') or 'ouvert'
    if treatment in ('traite', 'ignore'):
        return 'traite'
    category = doc.get('category_override') or (doc.get('analysis') or {}).get('categorie') or 'a_traiter'
    if category == 'a_savoir':
        return 'archive' if treatment == 'vu' else 'a_savoir'
    if category == 'ignorer':
        return 'ignore'
    return 'a_traiter'


class Engine:
    def __init__(self, store, client_factory=EwsClient, analyzer=None, now=None, budget=None):
        self.store = store
        self.client_factory = client_factory
        self.analyzer = analyzer or ai.analyze_with_ai
        self._now = now or utcnow
        self.budget = budget if budget is not None else config.tick_budget_seconds()
        self._deadline = None
        self._client = None
        self._projects = None  # cache le temps d'une operation

    # -- utilitaires --------------------------------------------------------
    def now(self):
        return self._now()

    def remaining(self):
        return self._deadline - time.monotonic() if self._deadline else 3600

    def settings(self):
        return settings_mod.merged(self.store.get('settings', 'main'))

    def projects(self):
        if self._projects is not None:
            return self._projects
        rows = self.store.query('projects')
        self._projects = sorted(((pid, p) for pid, p in rows if p.get('active', True)),
                                key=lambda r: (r[1].get('name') or '').lower())
        return self._projects

    def folders_meta(self):
        return self.store.get('meta', 'folders') or {'folders': [], 'distinguished': {}}

    def client(self):
        if self._client is None:
            self._client = self.client_factory()
        return self._client

    def close(self):
        self._projects = None
        if self._client is not None:
            self._client.close()
            self._client = None

    def audit(self, op, **fields):
        return self.store.add('audit', {'op': op, 'at': self.now(), **fields})

    def watched_folder_ids(self, cfg=None, meta=None):
        cfg = cfg or self.settings()
        meta = meta or self.folders_meta()
        dist = meta.get('distinguished') or {}
        return {dist.get(ref, ref) for ref in cfg['watched_folders']}

    def folder_path(self, folder_id, meta=None):
        meta = meta or self.folders_meta()
        for f in meta.get('folders') or []:
            if f['id'] == folder_id:
                return f.get('path') or f.get('name')
        return None

    # -- passage programme ---------------------------------------------------
    def tick(self, manual=False):
        now = self.now()
        cfg = self.settings()
        if not manual and not settings_mod.in_window(now, cfg):
            return {'status': 'skipped', 'reason': 'outside_work_hours'}
        owner = uuid.uuid4().hex
        if not self.store.acquire_lease(LOCK_COL, LOCK_ID, owner, self.budget + 90, now):
            return {'status': 'busy'}
        self._deadline = time.monotonic() + self.budget
        counters = {'created': 0, 'removed': 0, 'analyzed': 0, 'ai_calls': 0, 'moved': 0,
                    'drafts': 0, 'purged': 0}
        result = {'status': 'ok'}
        error = None
        try:
            meta = self.refresh_folders()
            backlog = False
            for ref in cfg['watched_folders']:
                backlog |= not self.sync_folder(ref, 'received', cfg, counters)
            backlog |= not self.sync_folder('sentitems', 'sent', cfg, counters)
            self.reconcile_drafts(counters)
            backlog |= not self.analyze_pending(cfg, meta, counters)
            self.housekeeping(cfg, counters)
            result['backlog'] = backlog
        except EwsBusy:
            error = 'ExchangeOccupe'
        except EwsError as exc:
            error = exc.code
        except Exception as exc:  # jamais de detail (pourrait contenir des donnees)
            error = type(exc).__name__
        finally:
            self.close()
            finished = self.now()
            state = {'last_tick_at': finished, 'schema_version': SCHEMA_VERSION,
                     'last_counters': counters, 'last_manual': manual}
            if error:
                state.update({'last_error': error, 'last_error_at': finished})
                result = {'status': 'error', 'error': error}
            else:
                state.update({'last_success_at': finished, 'last_error': None,
                              'backlog': result.get('backlog', False)})
            self.store.set('sync', 'state', state, merge=True)
            self.store.release_lease(LOCK_COL, LOCK_ID, owner)
            self._deadline = None
        result['counters'] = counters
        return result

    # -- dossiers -----------------------------------------------------------
    def refresh_folders(self, force=False):
        meta = self.folders_meta()
        refreshed = meta.get('refreshed_at')
        if not force and refreshed and self.now() - refreshed < FOLDER_REFRESH and meta.get('folders'):
            return meta
        client = self.client()
        dist = client.distinguished_ids()
        folders = client.list_folders()
        by_id = {f['id']: f for f in folders}
        names = {dist.get('inbox'): 'Boîte de réception'}
        for f in folders:
            parts, cur, guard = [], f, 0
            while cur and guard < 20:
                parts.append(names.get(cur['id']) or cur['name'])
                cur = by_id.get(cur.get('parent_id'))
                guard += 1
            f['path'] = ' / '.join(reversed(parts))
            f['system'] = f['id'] in {dist.get(k) for k in SYSTEM_FOLDERS}
            f['is_inbox'] = f['id'] == dist.get('inbox')
        folders.sort(key=lambda f: f['path'].lower())
        meta = {'folders': folders, 'distinguished': dist, 'refreshed_at': self.now()}
        self.store.set('meta', 'folders', meta)
        return meta

    def folder_choices(self, meta):
        """Dossiers proposables a l'IA : identifiants courts stables pour ce passage."""
        choices, mapping = [], {}
        for f in meta.get('folders') or []:
            if f.get('system') or f.get('is_inbox'):
                continue
            short = 'f' + stable_hash(f['id'])[:6]
            choices.append((short, f['path']))
            mapping[short] = f['id']
            if len(choices) >= 150:
                break
        return choices, mapping

    # -- synchronisation -------------------------------------------------------
    def cutoff(self, cursor, cfg):
        base = self.now() - timedelta(days=cfg['history_days'])
        last = cursor.get('last_complete_at')
        if cursor.get('initial_done') and last:
            # Rattrapage apres une absence (vacances, service endormi) : rien n'est ignore.
            return min(base, last - timedelta(days=1))
        return base

    def sync_folder(self, ref, direction, cfg, counters):
        """Synchronise un dossier jusqu'au budget. Renvoie True si a jour."""
        key = stable_hash('cursor', ref)
        cursor = self.store.get('sync_cursors', key) or {'ref': ref}
        client = self.client()
        reset_done = False
        while self.remaining() > 6:
            try:
                changes, state, last = client.sync_items(ref, cursor.get('state'), 100)
            except EwsError as exc:
                if exc.code in ('ErrorInvalidSyncStateData', 'ErrorInvalidSyncStateDataType') and not reset_done:
                    cursor = {'ref': ref, 'reset_at': self.now()}
                    reset_done = True
                    continue
                if exc.code == 'ErrorFolderNotFound' and direction == 'received':
                    cursor['error'] = exc.code
                    self.store.set('sync_cursors', key, cursor)
                    return True  # dossier suivi supprime : signale dans le statut
                raise
            cutoff = self.cutoff(cursor, cfg)
            creates = [c['id'] for c in changes if c['type'] == 'create'
                       and (c.get('received_at') is None or c['received_at'] >= cutoff)]
            if creates:
                details = client.get_items(creates, with_body=direction == 'received')
                for item_id in creates:
                    item = details.get(item_id)
                    if item:
                        self.upsert_message(item, direction, cfg)
                        counters['created'] += 1
            if direction == 'received':
                for change in changes:
                    if change['type'] == 'delete':
                        counters['removed'] += self.mark_left_folder(change['id'])
            cursor.update({'state': state, 'last_page_at': self.now(), 'error': None})
            if last:
                cursor.update({'initial_done': True, 'last_complete_at': self.now()})
            self.store.set('sync_cursors', key, cursor)
            if last:
                return True
        return False

    def message_doc_id(self, item, direction):
        imid = item.get('internet_message_id')
        return stable_hash(direction, 'imid', imid) if imid else stable_hash(direction, 'ews', item['id'])

    def upsert_message(self, item, direction, cfg):
        doc_id = self.message_doc_id(item, direction)
        existing = self.store.get('messages', doc_id)
        now = self.now()
        location = {'ews_id': item['id'], 'folder_id': item.get('folder_id'), 'in_watched': True,
                    'updated_at': now}
        if existing:
            # Reapparition (deplacement manuel puis retour, doublon...) : on conserve l'analyse.
            self.store.set('messages', doc_id, location, merge=True)
            return doc_id
        sender = item.get('from') or {}
        doc = {
            **location,
            'direction': direction,
            'internet_message_id': item.get('internet_message_id'),
            'conversation_id': item.get('conversation_id'),
            'subject': item.get('subject'),
            'from_name': sender.get('name'), 'from_email': sender.get('email'),
            'to': [r['email'] or r['name'] for r in item.get('to') or []][:20],
            'cc': [r['email'] or r['name'] for r in item.get('cc') or []][:20],
            'received_at': item.get('received_at') or item.get('sent_at') or now,
            'has_attachments': item.get('has_attachments', False),
            'created_at': now,
            'treatment': 'ouvert',
            'replied_at': None,
        }
        if direction == 'sent':
            doc.update({'state': 'envoye', 'view': 'envoye'})
            self.store.set('messages', doc_id, doc)
            self.mark_replied(doc)
            return doc_id
        ctx = self.base_context(cfg, None)
        provisional = ai.rules_analysis(item, ctx)
        bulk = ai.is_bulk(item)
        use_ai = cfg['ai_enabled'] and ai.ai_configured() and (not bulk or cfg['ai_on_bulk'])
        doc.update({
            'analysis': provisional, 'is_bulk': bulk,
            'state': 'en_attente' if use_ai else 'analyse',
            'attempts': 0, 'analyzed_at': None if use_ai else now,
        })
        doc['replied_at'] = self.find_reply_time(doc)
        doc['view'] = compute_view(doc)
        self.store.set('messages', doc_id, doc)
        if not use_ai:
            self.after_analysis(doc_id, doc, cfg, auto=False)
        return doc_id

    def find_reply_time(self, doc):
        conv = doc.get('conversation_id')
        if not conv:
            return None
        times = [m.get('received_at') for _, m in self.store.query('messages', 'conversation_id', '==', conv, limit=50)
                 if m.get('direction') == 'sent' and m.get('received_at') and m['received_at'] > doc['received_at']]
        return max(times) if times else None

    def mark_replied(self, sent_doc):
        conv = sent_doc.get('conversation_id')
        if not conv:
            return
        for doc_id, m in self.store.query('messages', 'conversation_id', '==', conv, limit=50):
            if m.get('direction') == 'received' and m.get('received_at') and \
                    m['received_at'] < sent_doc['received_at'] and \
                    (not m.get('replied_at') or m['replied_at'] < sent_doc['received_at']):
                self.store.set('messages', doc_id, {'replied_at': sent_doc['received_at']}, merge=True)

    def mark_left_folder(self, ews_id):
        rows = self.store.query('messages', 'ews_id', '==', ews_id, limit=5)
        for doc_id, m in rows:
            # L'etat de traitement est conserve : un mail range peut rester a traiter.
            self.store.set('messages', doc_id, {'in_watched': False, 'updated_at': self.now(),
                                                'classement': None if m.get('classement') == 'propose'
                                                else m.get('classement')}, merge=True)
        return len(rows)

    # -- analyse ---------------------------------------------------------------
    def base_context(self, cfg, meta):
        choices, mapping = self.folder_choices(meta) if meta else ([], {})
        projects = self.projects()
        # Les dossiers cibles des projets sont exprimes en identifiants courts pour l'IA.
        reverse = {v: k for k, v in mapping.items()}
        return {
            'settings': cfg, 'projects': projects, 'folder_choices': choices,
            'folder_map': mapping, 'folder_reverse': reverse,
            'today': settings_mod.local_today(cfg, self.now()).date(),
            'user_email': config.env('OVH_EMAIL'),
        }

    def ai_budget_left(self, cfg):
        usage = self.store.get('sync', 'ai_usage') or {}
        today = settings_mod.local_today(cfg, self.now()).date().isoformat()
        used = usage.get('count', 0) if usage.get('day') == today else 0
        backoff = usage.get('backoff_until')
        if backoff and backoff > self.now():
            return 0
        return max(0, cfg['ai_daily_limit'] - used)

    def count_ai_call(self, cfg, quota_error=False):
        usage = self.store.get('sync', 'ai_usage') or {}
        today = settings_mod.local_today(cfg, self.now()).date().isoformat()
        count = (usage.get('count', 0) if usage.get('day') == today else 0) + 1
        update = {'day': today, 'count': count}
        if quota_error:
            update['backoff_until'] = self.now() + timedelta(minutes=15)
            update['last_quota_error_at'] = self.now()
        self.store.set('sync', 'ai_usage', update, merge=True)

    def analyze_pending(self, cfg, meta, counters):
        """Analyse IA des mails en attente, les plus recents d'abord. True si plus rien en attente."""
        if not (cfg['ai_enabled'] and ai.ai_configured()):
            return True
        pending = self.store.query('messages', 'state', '==', 'en_attente', limit=200,
                                   order_by='received_at', descending=True)
        if not pending:
            return True
        ctx = self.base_context(cfg, meta)
        for doc_id, doc in pending:
            if self.remaining() < 12 or self.ai_budget_left(cfg) <= 0:
                return False
            self.analyze_one(doc_id, doc, cfg, ctx, counters)
        return self.remaining() >= 12

    def analyze_one(self, doc_id, doc, cfg, ctx, counters, force_ai=True):
        details = self.client().get_items([doc['ews_id']], with_body=True)
        item = details.get(doc['ews_id'])
        if item is None:
            self.store.set('messages', doc_id, {'state': 'analyse', 'ai_error': 'MessageIntrouvable',
                                                'analyzed_at': self.now()}, merge=True)
            return
        local_ctx = dict(ctx)
        local_ctx['thread'] = self.thread_context(doc_id, doc)
        local_ctx['replied_at'] = doc['replied_at'].isoformat() if doc.get('replied_at') else None
        local_ctx['rule_project'] = ai.match_project(item, ctx['projects'], item.get('body'))[0]
        counters['ai_calls'] += 1
        try:
            timeout = max(5, min(25, int(self.remaining()) - 4))
            analysis = self.analyzer(item, local_ctx, timeout=timeout)
            self.count_ai_call(cfg)
        except ai.AiQuotaError as exc:
            self.count_ai_call(cfg, quota_error=True)
            self.store.set('messages', doc_id, {'ai_error': exc.code}, merge=True)
            return
        except ai.AiError as exc:
            self.count_ai_call(cfg)
            attempts = int(doc.get('attempts') or 0) + 1
            update = {'attempts': attempts, 'ai_error': exc.code}
            if attempts >= 3:
                # Abandon visible : l'analyse par regles reste affichee, relance manuelle possible.
                update.update({'state': 'analyse', 'analyzed_at': self.now()})
            self.store.set('messages', doc_id, update, merge=True)
            return
        if analysis.get('dossier_id'):
            analysis['dossier_id'] = ctx['folder_map'].get(analysis['dossier_id'])
        update = {'analysis': analysis, 'state': 'analyse', 'analyzed_at': self.now(),
                  'ai_error': None, 'attempts': int(doc.get('attempts') or 0) + 1}
        # Relecture : l'utilisateur a pu marquer le mail traite pendant l'appel IA.
        current = self.store.get('messages', doc_id) or doc
        current.update(update)
        update['view'] = current['view'] = compute_view(current)
        self.store.set('messages', doc_id, update, merge=True)
        counters['analyzed'] += 1
        self.after_analysis(doc_id, current, cfg, auto=True, counters=counters)

    def thread_context(self, doc_id, doc):
        conv = doc.get('conversation_id')
        if not conv:
            return []
        rows = [m for mid, m in self.store.query('messages', 'conversation_id', '==', conv, limit=30)
                if mid != doc_id]
        rows.sort(key=lambda m: m.get('received_at') or datetime.min.replace(tzinfo=timezone.utc))
        out = []
        for m in rows[-6:]:
            out.append({
                'date': m['received_at'].isoformat() if m.get('received_at') else None,
                'sens': 'envoye_par_utilisateur' if m.get('direction') == 'sent' else 'recu',
                'de': m.get('from_email') or m.get('from_name'),
                'objet': m.get('subject'),
                'resume': (m.get('analysis') or {}).get('resume') if m.get('direction') != 'sent' else None,
            })
        return out

    def after_analysis(self, doc_id, doc, cfg, auto=False, counters=None):
        analysis = doc.get('analysis') or {}
        project_id = analysis.get('projet_id')
        projects = dict(self.projects())
        # Proposition de classement : dossier IA, sinon dossier cible du projet.
        target = analysis.get('dossier_id') or (projects.get(project_id) or {}).get('folder_id')
        update = {}
        if target and doc.get('in_watched') and target != doc.get('folder_id') and \
                doc.get('classement') not in ('fait', 'refuse'):
            update['classement'] = 'propose'
            update['classement_target'] = target
        if project_id:
            update['project_id'] = project_id
        if update:
            self.store.set('messages', doc_id, update, merge=True)
            doc.update(update)
        self.propose_tasks(doc_id, doc)
        if analysis.get('reponse_necessaire') and analysis.get('brouillon') and not doc.get('replied_at'):
            existing = self.store.get('drafts', doc_id)
            # Une proposition rejetee ou deja enregistree dans Exchange n'est jamais ecrasee.
            if not existing or existing.get('status') == 'proposee':
                self.store.set('drafts', doc_id, {
                    'message_id': doc_id, 'subject': doc.get('subject'),
                    'to_hint': doc.get('from_email'), 'body': analysis['brouillon'],
                    'reply_all': bool(analysis.get('repondre_a_tous')),
                    'checks': analysis.get('points_a_verifier') or [],
                    'status': 'proposee', 'version': (existing or {}).get('version', 0),
                    'created_at': (existing or {}).get('created_at') or self.now(),
                    'updated_at': self.now(),
                }, merge=True)
        if not auto:
            return
        if cfg['auto_draft'] and doc.get('view') == 'a_traiter':
            draft = self.store.get('drafts', doc_id)
            if draft and draft.get('status') == 'proposee' and self.remaining() > 10:
                try:
                    self.create_draft(doc_id, by='auto')
                    if counters is not None:
                        counters['drafts'] += 1
                except (UserError, EwsError):
                    pass
        if cfg['auto_move'] and update.get('classement') == 'propose' and project_id:
            project = projects.get(project_id) or {}
            rule_project = ai.match_project(
                {'from': {'email': doc.get('from_email')}, 'subject': doc.get('subject')},
                [(project_id, project)], '')[1]
            if project.get('auto_move') and project.get('folder_id') == target and rule_project > 0 and \
                    float(analysis.get('confiance') or 0) >= cfg['auto_move_min_confidence'] and \
                    self.remaining() > 8:
                try:
                    self.move_message(doc_id, target, by='auto')
                    if counters is not None:
                        counters['moved'] += 1
                except (UserError, EwsError):
                    pass

    def propose_tasks(self, doc_id, doc):
        analysis = doc.get('analysis') or {}
        if analysis.get('source') != 'ia':
            return
        open_tasks = self.store.query('tasks', 'status', 'in', list(OPEN_TASK_STATUSES), limit=500)
        for task in analysis.get('taches') or []:
            norm = ai.normalize(task['titre'])
            project_id = analysis.get('projet_id')
            duplicate = next((tid for tid, t in open_tasks if t.get('title_norm') == norm and
                              t.get('project_id') == project_id), None)
            if duplicate:
                t = dict(open_tasks)[duplicate]
                sources = list(dict.fromkeys((t.get('source_message_ids') or []) + [doc_id]))
                self.store.set('tasks', duplicate, {'source_message_ids': sources,
                                                    'updated_at': self.now()}, merge=True)
                continue
            task_id = stable_hash('task', doc_id, norm)
            if self.store.get('tasks', task_id):
                continue
            data = {
                'title': task['titre'], 'title_norm': norm, 'project_id': project_id,
                'priority': task['priorite'], 'status': 'proposee',
                'due_at': task['echeance'], 'due_source': task['echeance_source'],
                'source_message_ids': [doc_id], 'source_subject': doc.get('subject'),
                'notes': '', 'created_by': 'ia', 'created_at': self.now(), 'updated_at': self.now(),
            }
            self.store.set('tasks', task_id, data)
            open_tasks.append((task_id, data))

    def reanalyze(self, doc_id):
        doc = self.store.get('messages', doc_id)
        if not doc or doc.get('direction') != 'received':
            raise UserError('Message introuvable.')
        cfg = self.settings()
        if not (cfg['ai_enabled'] and ai.ai_configured()):
            raise UserError("L'IA n'est pas configurée (API_OPENROUTER) ou est désactivée.")
        if self.ai_budget_left(cfg) <= 0:
            raise UserError('Quota IA du jour atteint ou fournisseur saturé : réessayez plus tard.')
        self._deadline = time.monotonic() + 40
        counters = {'ai_calls': 0, 'analyzed': 0, 'drafts': 0, 'moved': 0}
        try:
            ctx = self.base_context(cfg, self.folders_meta())
            doc['attempts'] = 0
            self.analyze_one(doc_id, doc, cfg, ctx, counters)
        finally:
            self.close()
            self._deadline = None
        doc = self.store.get('messages', doc_id)
        if counters['analyzed'] == 0:
            raise UserError(f"Analyse IA impossible ({doc.get('ai_error') or 'erreur'}).")
        return doc

    # -- brouillons ----------------------------------------------------------
    def create_draft(self, doc_id, body=None, reply_all=None, by='user'):
        msg = self.store.get('messages', doc_id)
        if not msg or msg.get('direction') != 'received':
            raise UserError('Message introuvable.')
        draft = self.store.get('drafts', doc_id) or {'message_id': doc_id, 'version': 0,
                                                     'created_at': self.now(), 'subject': msg.get('subject')}
        if draft.get('status') in ACTIVE_DRAFT_STATUSES or draft.get('status') == 'modifie_dans_outlook':
            raise UserError('Un brouillon existe déjà dans Exchange pour ce mail.')
        body = body if body is not None else draft.get('body')
        if not body or not body.strip():
            raise UserError('Le brouillon est vide.')
        reply_all = draft.get('reply_all', False) if reply_all is None else bool(reply_all)
        cfg = self.settings()
        version = int(draft.get('version') or 0) + 1
        correlation = f'{doc_id}:{version}'
        draft.update({'status': 'creation_en_cours', 'version': version, 'correlation': correlation,
                      'intent_at': self.now(), 'body': body, 'reply_all': reply_all,
                      'updated_at': self.now(), 'error': None})
        self.store.set('drafts', doc_id, draft)
        client = self.client()
        try:
            found = client.find_by_correlation(correlation)
            if found:
                draft_id, change_key = found[0]['id'], found[0]['change_key']
            else:
                fresh = client.get_items([msg['ews_id']]).get(msg['ews_id'])
                if fresh is None:
                    raise UserError("Le mail d'origine est introuvable dans Exchange (déplacé ?). "
                                    'Lancez une synchronisation puis réessayez.')
                full_body = body.rstrip() + ('\n\n' + cfg['signature'].strip() if cfg['signature'].strip() else '')
                draft_id, change_key = client.create_reply_draft(fresh['id'], fresh['change_key'],
                                                                 full_body, reply_all)
                try:
                    client.set_correlation(draft_id, change_key, correlation)
                except EwsError:
                    draft['correlation_missing'] = True
            saved = client.get_items([draft_id]).get(draft_id) or {}
        except UserError as exc:
            self.store.set('drafts', doc_id, {'status': 'erreur', 'error': str(exc)}, merge=True)
            raise
        except EwsError as exc:
            # Etat incertain : la reconciliation verifiera Exchange avant toute recreation.
            self.store.set('drafts', doc_id, {'error': exc.code}, merge=True)
            raise
        draft.update({
            'status': 'enregistre', 'ews_draft_id': draft_id,
            'modified_at': saved.get('modified_at'),
            'to': [r['email'] or r['name'] for r in saved.get('to') or []],
            'cc': [r['email'] or r['name'] for r in saved.get('cc') or []],
            'saved_at': self.now(), 'by': by,
        })
        self.store.set('drafts', doc_id, draft)
        self.audit('brouillon', message_id=doc_id, subject=msg.get('subject'), result='ok', by=by)
        return draft

    def reconcile_drafts(self, counters):
        """Verifie les brouillons : creation interrompue, modification dans Outlook, envoi."""
        rows = self.store.query('drafts', 'status', 'in', list(ACTIVE_DRAFT_STATUSES), limit=100)
        if not rows or self.remaining() < 8:
            return
        client = self.client()
        saved = [(d, r) for d, r in rows if r['status'] == 'enregistre' and r.get('ews_draft_id')]
        current = client.get_items([r['ews_draft_id'] for _, r in saved]) if saved else {}
        for doc_id, draft in saved:
            item = current.get(draft['ews_draft_id'])
            if item is None:
                self.store.set('drafts', doc_id, {'status': 'envoye_ou_supprime', 'checked_at': self.now()},
                               merge=True)
            elif draft.get('modified_at') and item.get('modified_at') and item['modified_at'] != draft['modified_at']:
                self.store.set('drafts', doc_id, {'status': 'modifie_dans_outlook', 'checked_at': self.now()},
                               merge=True)
        for doc_id, draft in rows:
            if draft['status'] != 'creation_en_cours' or self.remaining() < 6:
                continue
            found = client.find_by_correlation(draft['correlation'])
            if found:
                item = client.get_items([found[0]['id']]).get(found[0]['id']) or {}
                self.store.set('drafts', doc_id, {'status': 'enregistre', 'ews_draft_id': found[0]['id'],
                                                  'modified_at': item.get('modified_at'),
                                                  'reconciled_at': self.now()}, merge=True)
                continue
            intent = draft.get('intent_at')
            if intent and self.now() - intent > timedelta(minutes=5):
                msg = self.store.get('messages', doc_id) or {}
                orphan = next((i for i in client.find_recent('drafts', 20)
                               if not i.get('correlation') and i.get('conversation_id') and
                               i['conversation_id'] == msg.get('conversation_id') and
                               i.get('created_at') and i['created_at'] >= intent - timedelta(seconds=30)), None)
                if orphan:
                    ck = client.set_correlation(orphan['id'], orphan['change_key'], draft['correlation'])
                    item = client.get_items([orphan['id']]).get(orphan['id']) or {}
                    self.store.set('drafts', doc_id, {'status': 'enregistre', 'ews_draft_id': orphan['id'],
                                                      'modified_at': item.get('modified_at'), 'change_key': ck,
                                                      'reconciled_at': self.now()}, merge=True)
                else:
                    self.store.set('drafts', doc_id, {'status': 'erreur',
                                                      'error': 'Création non confirmée dans Exchange'}, merge=True)

    def check_draft(self, doc_id):
        self._deadline = time.monotonic() + 20
        try:
            self.reconcile_drafts({})
        finally:
            self.close()
            self._deadline = None
        return self.store.get('drafts', doc_id)

    def replace_draft(self, doc_id, body, reply_all=None):
        """Remplace un brouillon de l'assistant NON modifie dans Outlook."""
        draft = self.store.get('drafts', doc_id)
        if not draft:
            raise UserError('Brouillon introuvable.')
        body = body if body is not None else draft.get('body')
        if not body or not body.strip():
            raise UserError('Le brouillon est vide.')  # verifie AVANT de toucher a Exchange
        if draft.get('status') == 'enregistre':
            client = self.client()
            item = client.get_items([draft['ews_draft_id']]).get(draft['ews_draft_id'])
            if item is not None:
                if item.get('modified_at') != draft.get('modified_at'):
                    self.store.set('drafts', doc_id, {'status': 'modifie_dans_outlook'}, merge=True)
                    raise UserError('Ce brouillon a été modifié dans Outlook : il ne sera pas écrasé.')
                if item.get('correlation') != draft.get('correlation'):
                    raise UserError("Brouillon non reconnu comme créé par l'assistant : non modifié.")
                client.trash_draft(draft['ews_draft_id'])
            self.audit('brouillon_remplace', message_id=doc_id, result='ok', by='user')
        elif draft.get('status') in ('creation_en_cours', 'modifie_dans_outlook'):
            raise UserError('Ce brouillon ne peut pas être remplacé automatiquement.')
        self.store.set('drafts', doc_id, {'status': 'proposee', 'ews_draft_id': None}, merge=True)
        try:
            return self.create_draft(doc_id, body=body, reply_all=reply_all)
        finally:
            self.close()

    # -- classement ------------------------------------------------------------
    def move_message(self, doc_id, folder_id, by='user'):
        msg = self.store.get('messages', doc_id)
        if not msg or msg.get('direction') != 'received':
            raise UserError('Message introuvable.')
        meta = self.folders_meta()
        target = next((f for f in meta.get('folders') or [] if f['id'] == folder_id), None)
        if not target or target.get('system'):
            raise UserError('Dossier de destination invalide.')
        if msg.get('folder_id') == folder_id:
            raise UserError('Le mail est déjà dans ce dossier.')
        audit_id = self.audit('deplacement', message_id=doc_id, subject=msg.get('subject'),
                              from_folder_id=msg.get('folder_id'),
                              from_path=self.folder_path(msg.get('folder_id'), meta),
                              to_folder_id=folder_id, to_path=target['path'], result='en_cours', by=by)
        try:
            new_id = self.client().move_item(msg['ews_id'], folder_id)
        except EwsError as exc:
            self.store.set('audit', audit_id, {'result': 'echec', 'error': exc.code}, merge=True)
            if exc.code == 'ErrorItemNotFound':
                raise UserError('Mail introuvable à son emplacement connu (déjà déplacé dans Outlook ?).')
            raise
        watched = self.watched_folder_ids(meta=meta)
        self.store.set('messages', doc_id, {
            'ews_id': new_id or msg['ews_id'], 'folder_id': folder_id, 'in_watched': folder_id in watched,
            'classement': 'fait', 'updated_at': self.now(),
        }, merge=True)
        self.store.set('audit', audit_id, {'result': 'ok'}, merge=True)
        return audit_id

    def undo_move(self, audit_id):
        entry = self.store.get('audit', audit_id)
        if not entry or entry.get('op') != 'deplacement' or entry.get('result') != 'ok':
            raise UserError('Déplacement non annulable.')
        msg = self.store.get('messages', entry['message_id'])
        if not msg or msg.get('folder_id') != entry['to_folder_id']:
            raise UserError("Le mail n'est plus à l'emplacement où l'assistant l'avait rangé.")
        if not entry.get('from_folder_id'):
            raise UserError("Dossier d'origine inconnu.")
        try:
            new_id = self.client().move_item(msg['ews_id'], entry['from_folder_id'])
        finally:
            self.close()
        watched = self.watched_folder_ids()
        self.store.set('messages', entry['message_id'], {
            'ews_id': new_id or msg['ews_id'], 'folder_id': entry['from_folder_id'],
            'in_watched': entry['from_folder_id'] in watched, 'classement': 'refuse', 'updated_at': self.now(),
        }, merge=True)
        self.store.set('audit', audit_id, {'result': 'annule', 'undone_at': self.now()}, merge=True)
        self.audit('annulation', message_id=entry['message_id'], subject=entry.get('subject'),
                   from_path=entry.get('to_path'), to_path=entry.get('from_path'), result='ok', by='user')

    def create_folder(self, parent_id, name):
        name = (name or '').strip()
        if not name or len(name) > 120 or '/' in name:
            raise UserError('Nom de dossier invalide.')
        meta = self.folders_meta()
        parent = next((f for f in meta.get('folders') or [] if f['id'] == parent_id), None)
        if parent_id and (not parent or (parent.get('system') and not parent.get('is_inbox'))):
            raise UserError('Dossier parent invalide.')
        try:
            folder_id = self.client().create_folder(parent_id or 'msgfolderroot', name)
            self.refresh_folders(force=True)
        finally:
            self.close()
        self.audit('dossier_cree', to_path=self.folder_path(folder_id), result='ok', by='user')
        return folder_id

    def project_parent(self, cfg=None, meta=None):
        """Dossier parent des projets : reglage, sinon un dossier nomme « Affaire(s) »."""
        cfg = cfg or self.settings()
        meta = meta or self.folders_meta()
        folders = [f for f in meta.get('folders') or [] if not f.get('system')]
        chosen = cfg.get('project_parent_folder')
        if chosen:
            return next((f for f in folders if f['id'] == chosen), None)
        # « Affaires », « AFFAIRES », « 01 - Affaires », « Affaires en cours »...
        candidates = [f for f in folders if re.search(r'(?<![a-z])affaires?(?![a-z])', ai.normalize(f['name']))]
        # Nom exact d'abord, puis le moins profond (ex. « Boite de reception / Affaires »).
        candidates.sort(key=lambda f: (ai.normalize(f['name']) not in ('affaire', 'affaires'),
                                       f.get('path', '').count(' / '), len(f['name'])))
        return candidates[0] if candidates else None

    def create_project_folder(self, project_id):
        """Cree (ou reutilise) le dossier du projet dans le dossier parent et le lie au projet."""
        project = self.store.get('projects', project_id)
        if not project:
            raise UserError('Projet introuvable.')
        name = ' '.join((project.get('name') or '').replace('/', '-').replace('\\', '-').split())[:120]
        if not name:
            raise UserError('Le projet doit avoir un nom.')
        try:
            meta = self.refresh_folders(force=True)
            parent = self.project_parent(meta=meta)
            if not parent:
                raise UserError('Aucun dossier dont le nom contient « Affaires » n’a été trouvé : choisissez '
                                'le dossier parent dans Réglages → Projets → « Dossier parent des projets ».')
            existing = next((f for f in meta['folders'] if f.get('parent_id') == parent['id'] and
                             ai.normalize(f['name']) == ai.normalize(name)), None)
            if existing:
                folder_id, created = existing['id'], False
            else:
                folder_id = self.client().create_folder(parent['id'], name)
                created = True
                self.refresh_folders(force=True)
        finally:
            self.close()
        self.store.set('projects', project_id, {'folder_id': folder_id, 'updated_at': self.now()}, merge=True)
        path = self.folder_path(folder_id)
        if created:
            self.audit('dossier_cree', to_path=path, result='ok', by='user')
        return {'folder_id': folder_id, 'path': path, 'created': created}

    # -- maintenance -----------------------------------------------------------
    def housekeeping(self, cfg, counters):
        if self.remaining() < 4:
            return
        now = self.now()
        cutoff = now - timedelta(days=cfg['retention_days'])
        for doc_id, _ in self.store.query('messages', 'received_at', '<', cutoff, limit=100):
            self.store.delete('messages', doc_id)
            self.store.delete('drafts', doc_id)
            counters['purged'] += 1
        expire = now - timedelta(days=cfg['a_savoir_expire_days'])
        for doc_id, m in self.store.query('messages', 'view', '==', 'a_savoir', limit=300):
            if m.get('received_at') and m['received_at'] < expire:
                self.store.set('messages', doc_id, {'treatment': 'vu', 'view': 'archive'}, merge=True)
        audit_cutoff = now - timedelta(days=365)
        for audit_id, _ in self.store.query('audit', 'at', '<', audit_cutoff, limit=50):
            self.store.delete('audit', audit_id)


def status_summary(store, cfg, now=None):
    now = now or utcnow()
    state = store.get('sync', 'state') or {}
    lock = store.get(LOCK_COL, LOCK_ID) or {}
    usage = store.get('sync', 'ai_usage') or {}
    pending = store.count('messages', 'state', '==', 'en_attente')
    last_ok = state.get('last_success_at')
    window = settings_mod.in_window(now, cfg)
    stale = window and (not last_ok or now - last_ok > timedelta(minutes=35))
    cursors = store.query('sync_cursors')
    return {
        'last_success_at': last_ok, 'last_tick_at': state.get('last_tick_at'),
        'last_error': state.get('last_error'), 'last_error_at': state.get('last_error_at'),
        'backlog': state.get('backlog', False), 'pending_analysis': pending,
        'locked': bool(lock.get('owner') and lock.get('lease_until') and lock['lease_until'] > now),
        'in_work_window': window, 'stale': stale,
        'ai': {'configured': ai.ai_configured(), 'enabled': cfg['ai_enabled'],
               'models': ai.models_for(cfg),
               'used_today': usage.get('count', 0) if usage.get('day') == settings_mod.local_today(cfg, now).date().isoformat() else 0,
               'daily_limit': cfg['ai_daily_limit'], 'backoff_until': usage.get('backoff_until')},
        'folders_initialized': [c.get('ref') for _, c in cursors if c.get('initial_done')],
        'folder_errors': [c.get('ref') for _, c in cursors if c.get('error')],
        'last_counters': state.get('last_counters'),
    }
