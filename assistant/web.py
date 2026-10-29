"""Application web : API JSON privee + interface (fichiers statiques)."""
import os
import re
import threading
from datetime import date, datetime, timezone
from functools import wraps

from flask import Flask, g, jsonify, request, send_from_directory

from . import auth, config, engine as engine_mod
from . import settings as settings_mod
from .ews import EwsError, probe_mailbox, stable_hash
from . import store as store_mod
from .store import get_base_store, user_store, utcnow

STATIC = os.path.join(os.path.dirname(__file__), 'static')
DIAG_LOCK = threading.Lock()
WRITE_LOCK = threading.Lock()  # serialise les ecritures Exchange de l'interface
TASK_STATUSES = ('proposee', 'a_faire', 'en_cours', 'en_attente', 'terminee', 'rejetee')
PRIORITIES = ('haute', 'normale', 'basse')
DUE_SOURCES = ('explicite', 'suggeree', 'aucune')
VIEWS = ('a_traiter', 'a_savoir', 'ignore', 'traite', 'archive', 'envoye')
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self' https://identitytoolkit.googleapis.com https://securetoken.googleapis.com; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")


def serialize(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, dict):
        return {k: serialize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialize(v) for v in value]
    return value


def ok(payload=None, status=200):
    return jsonify(serialize(payload if payload is not None else {'ok': True})), status


def fail(status, code, message=None):
    body = {'error': code}
    if message:
        body['message'] = message
    return jsonify(body), status


def json_body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def create_app(engine_factory=None, firebase_verifier=None):
    app = Flask(__name__, static_folder=None)
    app.config['MAX_CONTENT_LENGTH'] = 256 * 1024

    def store():
        return user_store(config.owner_uid())

    def make_engine():
        if engine_factory:
            return engine_factory(store())
        return engine_mod.Engine(store())

    @app.after_request
    def headers(resp):
        resp.headers['X-Content-Type-Options'] = 'nosniff'
        resp.headers['Referrer-Policy'] = 'no-referrer'
        resp.headers['X-Frame-Options'] = 'DENY'
        resp.headers['Content-Security-Policy'] = CSP
        resp.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
        if request.path.startswith('/api') or request.path.startswith('/internal'):
            resp.headers['Cache-Control'] = 'no-store'
        if request.headers.get('X-Forwarded-Proto') == 'https':
            resp.headers['Strict-Transport-Security'] = 'max-age=31536000'
        return resp

    @app.errorhandler(404)
    def not_found(_):
        return fail(404, 'not_found')

    @app.errorhandler(405)
    def not_allowed(_):
        return fail(405, 'method_not_allowed')

    @app.errorhandler(413)
    def too_large(_):
        return fail(413, 'too_large')

    @app.errorhandler(Exception)
    def crash(exc):
        if isinstance(exc, engine_mod.UserError):
            return fail(400, 'user_error', str(exc))
        if isinstance(exc, EwsError):
            return fail(502, 'exchange_error', f'Exchange a refusé l’opération ({exc.code}).')
        if hasattr(exc, 'code') and isinstance(getattr(exc, 'code'), int):
            return fail(exc.code, 'http_error')
        # Aucun detail : l'exception pourrait contenir des donnees de mail.
        app.logger.error('Unhandled %s on %s', type(exc).__name__, request.path)
        return fail(500, 'internal_error')

    def require_user(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                g.user = auth.authenticate(request.headers.get('Authorization', ''), firebase_verifier)
            except auth.AuthError as exc:
                return fail(exc.status, exc.code)
            return fn(*args, **kwargs)
        return wrapper

    def require_secret(name):
        def deco(fn):
            @wraps(fn)
            def wrapper(*args, **kwargs):
                token = auth.bearer(request.headers.get('Authorization', ''))
                if not auth.token_matches(token, config.env(name)):
                    return fail(401, 'unauthorized')
                return fn(*args, **kwargs)
            return wrapper
        return deco

    # -- public --------------------------------------------------------------
    @app.get('/healthz')
    def healthz():
        # Disponibilite du processus uniquement : n'indique PAS qu'une synchro a reussi.
        return ok({'status': 'up', 'version': '1.0.0'})

    @app.get('/')
    def index():
        return send_from_directory(STATIC, 'index.html', max_age=0)

    @app.get('/static/<path:name>')
    def static_files(name):
        if not re.fullmatch(r'[a-z0-9_-]+\.(js|css|svg)', name):
            return fail(404, 'not_found')
        return send_from_directory(STATIC, name, max_age=300)

    @app.get('/api/public-config')
    def public_config():
        return ok({'firebase': config.firebase_web_config(),
                   'token_login': config.env_bool('UI_TOKEN_LOGIN')})

    # -- technique (jetons distincts) ----------------------------------------
    @app.post('/api/diagnostic')
    @require_secret('ADMIN_API_TOKEN')
    def diagnostic():
        if not DIAG_LOCK.acquire(blocking=False):
            return ok({'status': 'busy'}, 409)
        trace = {}
        try:
            counts = probe_mailbox(trace)
            result = {'status': 'ok', 'checked_at': utcnow(), **counts}
            try:
                base = get_base_store()
                result['store'] = config.store_backend()
                result['store_selftest'] = store_mod.selftest(base)
                base.set('system', 'ews_probe', result)
            except Exception as exc:
                result['store_selftest'] = 'error:' + type(exc).__name__
            return ok(result)
        except EwsError as exc:
            return ok({'status': 'error', 'type': exc.code, 'trace': trace,
                       'note': 'Diagnostic incomplet ; aucune écriture Exchange.'}, 503)
        except Exception as exc:
            # Ex. ConnectionError / SSLError / ConnectTimeout : OVH injoignable depuis Render.
            return ok({'status': 'error', 'type': type(exc).__name__, 'trace': trace,
                       'note': 'Diagnostic incomplet ; aucune écriture Exchange.'}, 503)
        finally:
            DIAG_LOCK.release()

    @app.post('/internal/tick')
    @require_secret('CRON_SECRET')
    def tick():
        # La reponse ne contient que des compteurs : cron-job.org peut l'enregistrer.
        result = make_engine().tick(manual=False)
        return ok(result)

    # -- interface : session --------------------------------------------------
    @app.get('/api/me')
    @require_user
    def me():
        return ok({'uid': g.user['uid'], 'email': g.user['email'], 'method': g.user['method']})

    @app.get('/api/status')
    @require_user
    def status():
        s = store()
        cfg = settings_mod.merged(s.get('settings', 'main'))
        return ok(engine_mod.status_summary(s, cfg))

    @app.post('/api/sync')
    @require_user
    def sync_now():
        return ok(make_engine().tick(manual=True))

    @app.get('/api/counts')
    @require_user
    def counts():
        s = store()
        return ok({
            'a_traiter': s.count('messages', 'view', '==', 'a_traiter'),
            'a_savoir': s.count('messages', 'view', '==', 'a_savoir'),
            'classement': s.count('messages', 'classement', '==', 'propose'),
            'brouillons': s.count('drafts', 'status', '==', 'proposee'),
            'taches': s.count('tasks', 'status', 'in', list(engine_mod.OPEN_TASK_STATUSES)),
        })

    # -- messages --------------------------------------------------------------
    def message_out(doc_id, m, folders=None):
        analysis = m.get('analysis') or {}
        return {
            'id': doc_id, 'subject': m.get('subject'), 'from_name': m.get('from_name'),
            'from_email': m.get('from_email'), 'to': m.get('to'), 'cc': m.get('cc'),
            'received_at': m.get('received_at'), 'has_attachments': m.get('has_attachments'),
            'state': m.get('state'), 'view': m.get('view'), 'treatment': m.get('treatment'),
            'category_override': m.get('category_override'), 'replied_at': m.get('replied_at'),
            'in_watched': m.get('in_watched'), 'folder_path': (folders or {}).get(m.get('folder_id')),
            'project_id': m.get('project_id') or analysis.get('projet_id'),
            'classement': m.get('classement'), 'classement_target': m.get('classement_target'),
            'classement_path': (folders or {}).get(m.get('classement_target')),
            'is_bulk': m.get('is_bulk'), 'ai_error': m.get('ai_error'),
            'analysis': {k: analysis.get(k) for k in (
                'source', 'categorie', 'resume', 'action_requise', 'priorite', 'echeance',
                'echeance_source', 'echeance_citation', 'confiance', 'reponse_necessaire',
                'points_a_verifier', 'modele')},
        }

    def folder_names():
        meta = store().get('meta', 'folders') or {}
        return {f['id']: f.get('path') for f in meta.get('folders') or []}

    @app.get('/api/messages')
    @require_user
    def list_messages():
        view = request.args.get('view', 'a_traiter')
        s = store()
        if view == 'classement':
            rows = s.query('messages', 'classement', '==', 'propose', limit=300)
        elif view in VIEWS:
            rows = s.query('messages', 'view', '==', view, limit=300)
        else:
            return fail(400, 'bad_view')
        rank = {'haute': 0, 'normale': 1, 'basse': 2}
        if view == 'a_traiter':
            rows.sort(key=lambda r: (rank.get((r[1].get('analysis') or {}).get('priorite'), 1),
                                     -(r[1].get('received_at') or datetime.min.replace(tzinfo=timezone.utc)).timestamp()))
        else:
            rows.sort(key=lambda r: r[1].get('received_at') or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        folders = folder_names()
        return ok({'items': [message_out(i, m, folders) for i, m in rows]})

    @app.get('/api/messages/<doc_id>')
    @require_user
    def get_message(doc_id):
        m = store().get('messages', doc_id)
        if not m:
            return fail(404, 'not_found')
        out = message_out(doc_id, m, folder_names())
        out['analysis'] = m.get('analysis') or {}
        out['draft'] = store().get('drafts', doc_id)
        out['tasks'] = [{'id': t, **d} for t, d in store().query('tasks', 'source_message_ids',
                                                                     'array_contains', doc_id, limit=20)]
        return ok(out)

    @app.get('/api/messages/<doc_id>/body')
    @require_user
    def message_body(doc_id):
        """Corps lu en direct dans Exchange (texte brut, jamais stocke)."""
        m = store().get('messages', doc_id)
        if not m:
            return fail(404, 'not_found')
        eng = make_engine()
        try:
            item = eng.client().get_items([m['ews_id']], with_body=True).get(m['ews_id'])
        finally:
            eng.close()
        if not item:
            return fail(404, 'not_in_exchange', 'Mail introuvable à son emplacement connu dans Exchange.')
        return ok({'body': (item.get('body') or '')[:50000], 'attachments': item.get('attachments')})

    @app.patch('/api/messages/<doc_id>')
    @require_user
    def patch_message(doc_id):
        s = store()
        m = s.get('messages', doc_id)
        if not m:
            return fail(404, 'not_found')
        data = json_body()
        update = {}
        if 'treatment' in data:
            if data['treatment'] not in ('ouvert', 'traite', 'vu', 'ignore'):
                return fail(400, 'bad_treatment')
            update['treatment'] = data['treatment']
        if 'category' in data:
            if data['category'] not in ('a_traiter', 'a_savoir', 'ignorer', None):
                return fail(400, 'bad_category')
            update['category_override'] = data['category']
            if update.get('treatment') is None and m.get('treatment') in ('vu', 'ignore'):
                update['treatment'] = 'ouvert'
        if 'project_id' in data:
            if data['project_id'] is not None and not s.get('projects', str(data['project_id'])):
                return fail(400, 'bad_project')
            update['project_id'] = data['project_id']
        if 'classement' in data:
            if data['classement'] not in ('refuse', 'propose'):
                return fail(400, 'bad_classement')
            update['classement'] = data['classement']
            if data['classement'] == 'propose':
                target = data.get('classement_target')
                if not target or target not in folder_names():
                    return fail(400, 'bad_folder')
                update['classement_target'] = target
        m.update(update)
        update['view'] = engine_mod.compute_view(m)
        update['updated_at'] = utcnow()
        s.set('messages', doc_id, update, merge=True)
        return ok(message_out(doc_id, {**m, **update}, folder_names()))

    @app.post('/api/messages/<doc_id>/reanalyze')
    @require_user
    def reanalyze(doc_id):
        with WRITE_LOCK:
            doc = make_engine().reanalyze(doc_id)
        return ok(message_out(doc_id, doc, folder_names()))

    @app.post('/api/messages/<doc_id>/move')
    @require_user
    def move(doc_id):
        folder_id = json_body().get('folder_id')
        if not isinstance(folder_id, str):
            return fail(400, 'bad_folder')
        eng = make_engine()
        with WRITE_LOCK:
            try:
                audit_id = eng.move_message(doc_id, folder_id)
            finally:
                eng.close()
        return ok({'audit_id': audit_id})

    @app.post('/api/messages/<doc_id>/draft')
    @require_user
    def create_draft(doc_id):
        data = json_body()
        body = data.get('body')
        if body is not None and (not isinstance(body, str) or len(body) > 20000):
            return fail(400, 'bad_body')
        eng = make_engine()
        with WRITE_LOCK:
            try:
                current = store().get('drafts', doc_id) or {}
                if current.get('status') == 'enregistre':
                    draft = eng.replace_draft(doc_id, body, data.get('reply_all'))
                else:
                    draft = eng.create_draft(doc_id, body=body, reply_all=data.get('reply_all'))
            finally:
                eng.close()
        return ok(draft)

    # -- brouillons ------------------------------------------------------------
    @app.get('/api/drafts')
    @require_user
    def list_drafts():
        rows = store().query('drafts', 'status', 'in',
                             ['proposee', 'creation_en_cours', 'enregistre', 'modifie_dans_outlook', 'erreur'],
                             limit=300)
        rows.sort(key=lambda r: r[1].get('updated_at') or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return ok({'items': [{'id': i, **d} for i, d in rows]})

    @app.patch('/api/drafts/<doc_id>')
    @require_user
    def patch_draft(doc_id):
        s = store()
        d = s.get('drafts', doc_id)
        if not d:
            m = s.get('messages', doc_id)
            if not m or m.get('direction') != 'received':
                return fail(404, 'not_found')
            # Brouillon redige a la main (sans proposition IA).
            d = {'message_id': doc_id, 'subject': m.get('subject'), 'to_hint': m.get('from_email'),
                 'body': '', 'reply_all': False, 'checks': [], 'status': 'proposee', 'version': 0,
                 'created_at': utcnow()}
            s.set('drafts', doc_id, d)
        data = json_body()
        update = {'updated_at': utcnow()}
        if 'body' in data:
            if not isinstance(data['body'], str) or len(data['body']) > 20000:
                return fail(400, 'bad_body')
            update['body'] = data['body']
        if 'reply_all' in data:
            update['reply_all'] = bool(data['reply_all'])
        if data.get('status') in ('rejete', 'archive'):
            update['status'] = data['status']
        elif data.get('status') is not None:
            return fail(400, 'bad_status')
        s.set('drafts', doc_id, update, merge=True)
        return ok({'id': doc_id, **d, **update})

    @app.post('/api/drafts/<doc_id>/check')
    @require_user
    def check_draft(doc_id):
        with WRITE_LOCK:
            return ok(make_engine().check_draft(doc_id))

    # -- dossiers / classement ---------------------------------------------------
    @app.get('/api/folders')
    @require_user
    def folders():
        meta = store().get('meta', 'folders') or {}
        return ok({'refreshed_at': meta.get('refreshed_at'),
                   'folders': [{k: f.get(k) for k in ('id', 'name', 'path', 'parent_id', 'system', 'is_inbox', 'total')}
                               for f in meta.get('folders') or []]})

    @app.post('/api/folders/refresh')
    @require_user
    def refresh_folders():
        eng = make_engine()
        try:
            eng.refresh_folders(force=True)
        finally:
            eng.close()
        return folders()

    @app.post('/api/folders')
    @require_user
    def create_folder():
        data = json_body()
        with WRITE_LOCK:
            folder_id = make_engine().create_folder(data.get('parent_id'), data.get('name'))
        return ok({'id': folder_id})

    @app.get('/api/audit')
    @require_user
    def audit():
        rows = store().query('audit', order_by='at', descending=True, limit=100)
        return ok({'items': [{'id': i, **a} for i, a in rows]})

    @app.post('/api/audit/<audit_id>/undo')
    @require_user
    def undo(audit_id):
        with WRITE_LOCK:
            make_engine().undo_move(audit_id)
        return ok()

    # -- taches ----------------------------------------------------------------
    def clean_task(data, partial):
        out, errors = {}, []
        if 'title' in data or not partial:
            title = data.get('title')
            if not isinstance(title, str) or not title.strip() or len(title) > 300:
                errors.append('Titre obligatoire (300 caractères max).')
            else:
                out['title'] = title.strip()
        for key, allowed, default in (('status', TASK_STATUSES, 'a_faire'),
                                      ('priority', PRIORITIES, 'normale'),
                                      ('due_source', DUE_SOURCES, None)):
            if key in data:
                if data[key] not in allowed:
                    errors.append(f'Valeur invalide : {key}')
                else:
                    out[key] = data[key]
            elif not partial and default:
                out[key] = default
        if 'due_at' in data:
            if data['due_at'] in (None, ''):
                out['due_at'] = None
                out['due_source'] = 'aucune'
            else:
                try:
                    out['due_at'] = date.fromisoformat(str(data['due_at'])).isoformat()
                except ValueError:
                    errors.append('Échéance invalide (AAAA-MM-JJ).')
                if 'due_source' not in data:
                    out['due_source'] = 'explicite'
        elif not partial:
            out['due_at'], out['due_source'] = None, 'aucune'
        if 'project_id' in data:
            pid = data['project_id']
            if pid is not None and not store().get('projects', str(pid)):
                errors.append('Projet inconnu.')
            else:
                out['project_id'] = pid
        if 'notes' in data:
            if not isinstance(data['notes'], str) or len(data['notes']) > 5000:
                errors.append('Notes trop longues.')
            else:
                out['notes'] = data['notes']
        if 'source_message_ids' in data:
            ids = data['source_message_ids']
            if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids) or len(ids) > 50:
                errors.append('Références de mails invalides.')
            else:
                out['source_message_ids'] = ids
        return out, errors

    @app.get('/api/tasks')
    @require_user
    def list_tasks():
        scope = request.args.get('scope', 'open')
        statuses = list(engine_mod.OPEN_TASK_STATUSES) if scope == 'open' else ['terminee', 'rejetee']
        rows = store().query('tasks', 'status', 'in', statuses, limit=500)
        rank = {'haute': 0, 'normale': 1, 'basse': 2}
        rows.sort(key=lambda r: (r[1].get('status') != 'proposee', r[1].get('due_at') or '9999',
                                 rank.get(r[1].get('priority'), 1)))
        return ok({'items': [{'id': i, **t} for i, t in rows]})

    @app.post('/api/tasks')
    @require_user
    def create_task():
        data, errors = clean_task(json_body(), partial=False)
        if errors:
            return fail(400, 'invalid', ' '.join(errors))
        now = utcnow()
        data.update({'created_by': 'manuel', 'created_at': now, 'updated_at': now,
                     'title_norm': stable_hash(data['title'].lower())})
        data.setdefault('source_message_ids', [])
        data.setdefault('project_id', None)
        data.setdefault('notes', '')
        task_id = store().add('tasks', data)
        return ok({'id': task_id, **data}, 201)

    @app.patch('/api/tasks/<task_id>')
    @require_user
    def patch_task(task_id):
        s = store()
        current = s.get('tasks', task_id)
        if not current:
            return fail(404, 'not_found')
        data, errors = clean_task(json_body(), partial=True)
        if errors:
            return fail(400, 'invalid', ' '.join(errors))
        data['updated_at'] = utcnow()
        if data.get('status') == 'terminee':
            data['done_at'] = utcnow()
        s.set('tasks', task_id, data, merge=True)
        return ok({'id': task_id, **current, **data})

    @app.delete('/api/tasks/<task_id>')
    @require_user
    def delete_task(task_id):
        store().delete('tasks', task_id)
        return ok()

    # -- projets & reglages --------------------------------------------------
    def clean_project(data):
        errors, out = [], {}
        name = data.get('name')
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            errors.append('Nom de projet obligatoire.')
        else:
            out['name'] = name.strip()
        for key in ('keywords', 'contacts'):
            value = data.get(key, [])
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value) or len(value) > 100:
                errors.append(f'{key} : liste attendue.')
            else:
                out[key] = [v.strip().lower() if key == 'contacts' else v.strip() for v in value if v.strip()]
        folder = data.get('folder_id')
        if folder is not None and folder not in folder_names():
            errors.append('Dossier cible inconnu.')
        out['folder_id'] = folder
        out['auto_move'] = data.get('auto_move') is True
        out['active'] = data.get('active', True) is not False
        return out, errors

    @app.get('/api/projects')
    @require_user
    def list_projects():
        rows = store().query('projects')
        rows.sort(key=lambda r: (r[1].get('name') or '').lower())
        return ok({'items': [{'id': i, **p} for i, p in rows]})

    @app.post('/api/projects')
    @require_user
    def create_project():
        data, errors = clean_project(json_body())
        if errors:
            return fail(400, 'invalid', ' '.join(errors))
        data['created_at'] = utcnow()
        pid = 'p' + stable_hash(data['name'].lower(), str(utcnow().timestamp()))[:10]
        store().set('projects', pid, data)
        return ok({'id': pid, **data}, 201)

    @app.put('/api/projects/<pid>')
    @require_user
    def update_project(pid):
        s = store()
        current = s.get('projects', pid)
        if not current:
            return fail(404, 'not_found')
        data, errors = clean_project(json_body())
        if errors:
            return fail(400, 'invalid', ' '.join(errors))
        data['updated_at'] = utcnow()
        s.set('projects', pid, data, merge=True)
        return ok({'id': pid, **current, **data})

    @app.delete('/api/projects/<pid>')
    @require_user
    def delete_project(pid):
        store().delete('projects', pid)
        return ok()

    @app.get('/api/settings')
    @require_user
    def get_settings():
        return ok(settings_mod.merged(store().get('settings', 'main')))

    @app.put('/api/settings')
    @require_user
    def put_settings():
        clean, errors = settings_mod.validate(json_body())
        if errors:
            return fail(400, 'invalid', ' '.join(errors))
        s = store()
        if 'work_start' in clean or 'work_end' in clean:
            merged = settings_mod.merged({**(s.get('settings', 'main') or {}), **clean})
            if merged['work_start'] >= merged['work_end']:
                return fail(400, 'invalid', "L'heure de fin doit suivre l'heure de début.")
        s.set('settings', 'main', clean, merge=True)
        return ok(settings_mod.merged(s.get('settings', 'main')))

    return app
