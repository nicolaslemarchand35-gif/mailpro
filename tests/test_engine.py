import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from assistant import ai
from assistant.engine import Engine, UserError, compute_view
from assistant.ews import EwsError
from assistant.store import MemoryStore, UserStore
from tests.fakes import FakeMailbox

NOW = datetime(2026, 10, 29, 9, 5, tzinfo=timezone.utc)  # jeudi 10:05 a Paris


def fake_analysis(category='a_traiter', **extra):
    base = {'source': 'ia', 'categorie': category, 'resume': 'Résumé', 'action_requise': 'Répondre',
            'priorite': 'haute', 'echeance': '2026-10-30', 'echeance_source': 'explicite',
            'echeance_citation': 'avant vendredi', 'projet_id': None, 'dossier_id': None, 'confiance': 0.9,
            'reponse_necessaire': True, 'repondre_a_tous': False, 'brouillon': 'Bonjour, voici...',
            'taches': [{'titre': 'Envoyer le devis', 'priorite': 'haute', 'echeance': '2026-10-30',
                        'echeance_source': 'explicite'}], 'points_a_verifier': [], 'modele': 'test'}
    base.update(extra)
    return base


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'OPENROUTER_API_KEY': 'k' * 40, 'OVH_EMAIL': 'moi@entreprise.fr'})
        self.env.start()
        self.box = FakeMailbox()
        self.store = UserStore(MemoryStore(), 'u1')
        self.now = NOW
        self.ai_calls = []
        self.ai_result = fake_analysis()

    def tearDown(self):
        self.env.stop()

    def analyzer(self, item, ctx, timeout=20):
        self.ai_calls.append((item, ctx))
        if isinstance(self.ai_result, Exception):
            raise self.ai_result
        result = dict(self.ai_result)
        if result.get('dossier_id') == 'DUPONT':
            result['dossier_id'] = ctx['folder_reverse']['DUPONT']
        return result

    def engine(self, budget=30):
        return Engine(self.store, client_factory=self.box.client, analyzer=self.analyzer,
                      now=lambda: self.now, budget=budget)

    def messages(self):
        return dict(self.store.query('messages'))

    # -- synchronisation ---------------------------------------------------
    def test_outside_hours_does_not_touch_exchange(self):
        self.now = datetime(2026, 10, 31, 10, 0, tzinfo=timezone.utc)  # samedi
        result = self.engine().tick()
        self.assertEqual(result['status'], 'skipped')
        self.assertEqual(self.box.calls, [])

    def test_manual_sync_runs_outside_hours(self):
        self.now = datetime(2026, 10, 31, 10, 0, tzinfo=timezone.utc)
        self.box.add(received=self.now - timedelta(hours=1))
        self.assertEqual(self.engine().tick(manual=True)['status'], 'ok')
        self.assertEqual(len(self.messages()), 1)

    def test_initial_history_is_bounded(self):
        self.box.add(subject='ancien', received=NOW - timedelta(days=30))
        self.box.add(subject='recent', received=NOW - timedelta(days=1))
        result = self.engine().tick()
        self.assertEqual(result['status'], 'ok')
        subjects = {m['subject'] for m in self.messages().values()}
        self.assertEqual(subjects, {'recent'})

    def test_analysis_creates_task_draft_and_view(self):
        self.box.add(subject='Devis Dupont', received=NOW - timedelta(hours=2))
        self.engine().tick()
        (doc_id, msg), = self.messages().items()
        self.assertEqual(msg['state'], 'analyse')
        self.assertEqual(msg['view'], 'a_traiter')
        tasks = self.store.query('tasks')
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0][1]['status'], 'proposee')
        self.assertEqual(tasks[0][1]['source_message_ids'], [doc_id])
        draft = self.store.get('drafts', doc_id)
        self.assertEqual(draft['status'], 'proposee')
        self.assertFalse(any(c == 'create_reply_draft' for c in self.box.calls))  # pas d'auto par defaut

    def test_resync_is_idempotent(self):
        self.box.add(received=NOW - timedelta(hours=2), imid='<same@x>')
        self.engine().tick()
        # Meme mail reapparait (ex. deplace puis remis) : aucun doublon, analyse conservee.
        self.box.add(received=NOW - timedelta(hours=2), imid='<same@x>')
        self.engine().tick()
        self.assertEqual(len(self.messages()), 1)
        self.assertEqual(len(self.ai_calls), 1)
        self.assertEqual(len(self.store.query('tasks')), 1)

    def test_interruption_mid_batch_loses_nothing(self):
        for i in range(5):
            self.box.add(subject=f'm{i}', received=NOW - timedelta(hours=1))
        self.box.page_size_override = 2
        self.box.fail_on['get_items'] = None
        eng = self.engine()
        calls = {'n': 0}
        original = self.box.client

        def flaky():
            client = original()
            real = client.get_items

            def get_items(ids, **kw):
                calls['n'] += 1
                if calls['n'] == 2:
                    raise EwsError('ErrorInternalServerError')
                return real(ids, **kw)
            client.get_items = get_items
            return client
        eng.client_factory = flaky
        self.assertEqual(eng.tick()['status'], 'error')
        self.assertEqual(len(self.messages()), 2)  # premiere page ecrite, curseur avance
        self.assertEqual(self.engine().tick()['status'], 'ok')
        self.assertEqual({m['subject'] for m in self.messages().values()}, {f'm{i}' for i in range(5)})

    def test_moved_out_keeps_treatment_state(self):
        item_id = self.box.add(received=NOW - timedelta(hours=1))
        self.engine().tick()
        self.box.client().move_item(item_id, 'DUPONT')  # rangement manuel dans Outlook
        self.engine().tick()
        (_, msg), = self.messages().items()
        self.assertFalse(msg['in_watched'])
        self.assertEqual(msg['view'], 'a_traiter')

    def test_sent_reply_is_detected(self):
        self.box.add(received=NOW - timedelta(hours=3), conversation='C1')
        self.engine().tick()
        self.box.add('sentitems', sender='moi@entreprise.fr', received=NOW - timedelta(hours=1), conversation='C1')
        self.engine().tick()
        received = [m for m in self.messages().values() if m['direction'] == 'received'][0]
        self.assertEqual(received['replied_at'], NOW - timedelta(hours=1))

    def test_reply_already_sent_is_given_to_ai(self):
        self.ai_result = ai.AiError('Timeout')
        self.box.add(received=NOW - timedelta(hours=3), conversation='C1')
        self.box.add('sentitems', sender='moi@entreprise.fr', received=NOW - timedelta(hours=1), conversation='C1')
        self.engine().tick()
        self.ai_result = fake_analysis()
        self.engine().tick()
        ctx = self.ai_calls[-1][1]
        self.assertIsNotNone(ctx['replied_at'])
        self.assertTrue(any(t['sens'] == 'envoye_par_utilisateur' for t in ctx['thread']))

    def test_catch_up_after_absence(self):
        self.engine().tick()  # dossier initialise, vide
        self.now = NOW + timedelta(days=12)
        self.box.add(subject='pendant les congés', received=NOW + timedelta(days=2))
        self.engine().tick(manual=True)
        self.assertEqual({m['subject'] for m in self.messages().values()}, {'pendant les congés'})

    def test_lock_prevents_concurrent_tick_and_expires(self):
        base = self.store
        self.assertTrue(base.acquire_lease('jobs', 'sync', 'other', 60, NOW))
        self.assertEqual(self.engine().tick()['status'], 'busy')
        self.now = NOW + timedelta(seconds=61)
        self.assertEqual(self.engine().tick()['status'], 'ok')

    def test_error_is_sanitized_and_recorded(self):
        self.box.fail_on['distinguished_ids'] = RuntimeError('secret password in message')
        result = self.engine().tick()
        self.assertEqual(result, {'status': 'error', 'error': 'RuntimeError', 'counters': result['counters']})
        state = self.store.get('sync', 'state')
        self.assertEqual(state['last_error'], 'RuntimeError')
        self.assertNotIn('last_success_at', state)

    def test_ai_quota_sets_backoff_and_keeps_pending(self):
        self.ai_result = ai.AiQuotaError('RateLimited')
        self.box.add(received=NOW - timedelta(hours=1))
        self.box.add(received=NOW - timedelta(hours=2))
        self.engine().tick()
        self.assertEqual(len(self.ai_calls), 1)  # arret immediat apres le 429
        states = {m['state'] for m in self.messages().values()}
        self.assertEqual(states, {'en_attente'})
        self.assertGreater(self.store.get('sync', 'ai_usage')['backoff_until'], NOW)

    def test_ai_failures_fall_back_to_rules_after_three_attempts(self):
        self.ai_result = ai.AiError('InvalidJson')
        self.box.add(received=NOW - timedelta(hours=1))
        for _ in range(3):
            self.engine().tick()
        (_, msg), = self.messages().items()
        self.assertEqual(msg['state'], 'analyse')
        self.assertEqual(msg['analysis']['source'], 'regles')
        self.assertEqual(msg['view'], 'a_traiter')  # jamais oublie silencieusement

    def test_user_action_during_ai_call_is_kept(self):
        self.box.add(received=NOW - timedelta(hours=1))
        original = self.analyzer

        def slow_analyzer(item, ctx, timeout=20):
            doc_id = next(iter(self.messages()))
            self.store.set('messages', doc_id, {'treatment': 'traite'}, merge=True)  # clic pendant l'appel
            return original(item, ctx, timeout)
        eng = self.engine()
        eng.analyzer = slow_analyzer
        eng.tick()
        (_, msg), = self.messages().items()
        self.assertEqual(msg['treatment'], 'traite')
        self.assertEqual(msg['view'], 'traite')
        self.assertEqual(msg['analysis']['source'], 'ia')

    def test_bulk_mail_skips_ai(self):
        self.box.add(received=NOW - timedelta(hours=1), headers={'list-unsubscribe': '<mailto:x>'})
        self.engine().tick()
        (_, msg), = self.messages().items()
        self.assertEqual(self.ai_calls, [])
        self.assertEqual(msg['view'], 'ignore')

    def test_daily_ai_limit(self):
        self.store.set('settings', 'main', {'ai_daily_limit': 1})
        self.box.add(received=NOW - timedelta(hours=1))
        self.box.add(received=NOW - timedelta(hours=2))
        self.engine().tick()
        self.assertEqual(len(self.ai_calls), 1)
        self.assertTrue(self.store.get('sync', 'state')['backlog'])

    # -- brouillons -----------------------------------------------------------
    def test_create_draft_is_reply_saved_only(self):
        self.store.set('settings', 'main', {'signature': 'Nicolas'})
        self.box.add(received=NOW - timedelta(hours=1), conversation='C9')
        self.engine().tick()
        doc_id = next(iter(self.messages()))
        draft = self.engine().create_draft(doc_id)
        self.assertEqual(draft['status'], 'enregistre')
        exchange = self.box.items[draft['ews_draft_id']]
        self.assertEqual(exchange['folder_id'], 'DRAFTS')
        self.assertEqual(exchange['conversation_id'], 'C9')
        self.assertTrue(exchange['body'].endswith('Nicolas'))
        self.assertEqual(exchange['correlation'], f'{doc_id}:1')
        with self.assertRaises(UserError):
            self.engine().create_draft(doc_id)  # pas de doublon

    def test_draft_crash_is_reconciled_without_duplicate(self):
        self.box.add(received=NOW - timedelta(hours=1))
        self.engine().tick()
        doc_id = next(iter(self.messages()))
        self.box.fail_on['get_items'] = None
        eng = self.engine()
        real_factory = self.box.client

        def crashing():
            client = real_factory()
            real_get = client.get_items
            state = {'n': 0}

            def get_items(ids, **kw):
                state['n'] += 1
                if state['n'] == 2:  # apres creation + correlation, avant ecriture Firestore
                    raise EwsError('ErrorTimeout')
                return real_get(ids, **kw)
            client.get_items = get_items
            return client
        eng.client_factory = crashing
        with self.assertRaises(EwsError):
            eng.create_draft(doc_id)
        self.assertEqual(self.store.get('drafts', doc_id)['status'], 'creation_en_cours')
        self.engine().tick()
        draft = self.store.get('drafts', doc_id)
        self.assertEqual(draft['status'], 'enregistre')
        drafts_in_exchange = [i for i in self.box.items.values() if i['folder_id'] == 'DRAFTS']
        self.assertEqual(len(drafts_in_exchange), 1)

    def test_draft_modified_in_outlook_is_protected(self):
        self.box.add(received=NOW - timedelta(hours=1))
        self.engine().tick()
        doc_id = next(iter(self.messages()))
        draft = self.engine().create_draft(doc_id)
        self.box.items[draft['ews_draft_id']]['modified_at'] += timedelta(minutes=5)
        with self.assertRaises(UserError):
            self.engine().replace_draft(doc_id, 'nouveau texte')
        self.assertEqual(self.store.get('drafts', doc_id)['status'], 'modifie_dans_outlook')
        self.assertNotEqual(self.box.items[draft['ews_draft_id']]['folder_id'], 'TRASH')

    def test_replace_unmodified_draft(self):
        self.box.add(received=NOW - timedelta(hours=1))
        self.engine().tick()
        doc_id = next(iter(self.messages()))
        first = self.engine().create_draft(doc_id)
        second = self.engine().replace_draft(doc_id, 'texte corrigé')
        self.assertEqual(self.box.items[first['ews_draft_id']]['folder_id'], 'TRASH')
        self.assertEqual(second['version'], 2)
        self.assertIn('texte corrigé', self.box.items[second['ews_draft_id']]['body'])

    def test_sent_draft_detected(self):
        self.box.add(received=NOW - timedelta(hours=1))
        self.engine().tick()
        doc_id = next(iter(self.messages()))
        draft = self.engine().create_draft(doc_id)
        del self.box.items[draft['ews_draft_id']]
        self.engine().tick()
        self.assertEqual(self.store.get('drafts', doc_id)['status'], 'envoye_ou_supprime')

    def test_auto_draft_when_enabled(self):
        self.store.set('settings', 'main', {'auto_draft': True})
        self.box.add(received=NOW - timedelta(hours=1))
        self.engine().tick()
        doc_id = next(iter(self.messages()))
        self.assertEqual(self.store.get('drafts', doc_id)['status'], 'enregistre')

    # -- classement -------------------------------------------------------------
    def test_classement_proposal_move_and_undo(self):
        self.ai_result = fake_analysis(dossier_id='DUPONT')
        self.box.add(received=NOW - timedelta(hours=1))
        self.engine().tick()
        doc_id, msg = next(iter(self.messages().items()))
        self.assertEqual(msg['classement'], 'propose')
        self.assertEqual(msg['classement_target'], 'DUPONT')
        audit_id = self.engine().move_message(doc_id, 'DUPONT')
        msg = self.store.get('messages', doc_id)
        self.assertEqual(msg['folder_id'], 'DUPONT')
        self.assertFalse(msg['in_watched'])
        self.engine().tick()  # l'evenement Delete de l'ancien id est ignore
        self.engine().undo_move(audit_id)
        msg = self.store.get('messages', doc_id)
        self.assertEqual(msg['folder_id'], 'INBOX')
        self.assertEqual(self.store.get('audit', audit_id)['result'], 'annule')
        self.assertEqual(len(self.messages()), 1)

    def test_move_to_system_folder_refused(self):
        self.box.add(received=NOW - timedelta(hours=1))
        self.engine().tick()
        doc_id = next(iter(self.messages()))
        with self.assertRaises(UserError):
            self.engine().move_message(doc_id, 'TRASH')

    def test_auto_move_requires_validated_project(self):
        self.store.set('settings', 'main', {'auto_move': True})
        self.store.set('projects', 'p1', {'name': 'Dupont', 'keywords': ['dupont'], 'contacts': [],
                                          'folder_id': 'DUPONT', 'auto_move': True})
        self.ai_result = fake_analysis(projet_id='p1', dossier_id='DUPONT', confiance=0.95)
        self.box.add(subject='Chantier Dupont', received=NOW - timedelta(hours=1))
        self.box.add(subject='Autre sujet', received=NOW - timedelta(hours=1))
        self.engine().tick()
        msgs = {m['subject']: m for m in self.messages().values()}
        self.assertEqual(msgs['Chantier Dupont']['folder_id'], 'DUPONT')
        self.assertEqual(msgs['Autre sujet']['folder_id'], 'INBOX')  # aucun mot-cle valide

    def test_create_project_folder_in_affaires(self):
        self.box.folders['AFF'] = {'name': 'Affaires', 'parent_id': 'INBOX'}
        self.store.set('projects', 'p1', {'name': 'Résidence Les Tilleuls', 'keywords': [], 'contacts': []})
        out = self.engine().create_project_folder('p1')
        self.assertTrue(out['created'])
        self.assertEqual(self.box.folders[out['folder_id']], {'name': 'Résidence Les Tilleuls', 'parent_id': 'AFF'})
        self.assertEqual(self.store.get('projects', 'p1')['folder_id'], out['folder_id'])
        again = self.engine().create_project_folder('p1')  # pas de doublon
        self.assertFalse(again['created'])
        self.assertEqual(again['folder_id'], out['folder_id'])

    def test_create_project_folder_needs_parent(self):
        self.store.set('projects', 'p1', {'name': 'X'})
        with self.assertRaises(UserError):
            self.engine().create_project_folder('p1')
        self.store.set('settings', 'main', {'project_parent_folder': 'CHANTIERS'})
        out = self.engine().create_project_folder('p1')
        self.assertEqual(self.box.folders[out['folder_id']]['parent_id'], 'CHANTIERS')

    # -- maintenance -------------------------------------------------------------
    def test_purge_old_messages(self):
        self.box.add(received=NOW - timedelta(hours=1))
        self.engine().tick()
        self.now = NOW + timedelta(days=100)
        self.engine().tick(manual=True)
        self.assertEqual(self.messages(), {})

    def test_watched_folder_removed_is_reported(self):
        self.store.set('settings', 'main', {'watched_folders': ['inbox', 'GONE']})
        self.assertEqual(self.engine().tick()['status'], 'ok')
        cursors = self.store.query('sync_cursors')
        self.assertTrue(any(c.get('error') == 'ErrorFolderNotFound' for _, c in cursors))

    def test_compute_view(self):
        self.assertEqual(compute_view({'direction': 'sent'}), 'envoye')
        self.assertEqual(compute_view({'analysis': {'categorie': 'a_savoir'}}), 'a_savoir')
        self.assertEqual(compute_view({'analysis': {'categorie': 'a_savoir'}, 'treatment': 'vu'}), 'archive')
        self.assertEqual(compute_view({'analysis': {'categorie': 'a_savoir'}, 'category_override': 'a_traiter'}), 'a_traiter')
        self.assertEqual(compute_view({'analysis': {'categorie': 'a_traiter'}, 'treatment': 'traite'}), 'traite')


if __name__ == '__main__':
    unittest.main()
