import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from assistant import settings as settings_mod
from assistant import store as store_mod
from assistant.auth import AuthError
from assistant.engine import Engine
from assistant.store import MemoryStore
from assistant.web import create_app
from tests.fakes import FakeMailbox

ADMIN = 'a' * 40
CRON = 'c' * 40
FIREBASE_CFG = json.dumps({'apiKey': 'public-key', 'authDomain': 'x.firebaseapp.com', 'projectId': 'x', 'other': 'drop'})


def verifier(token):
    if token == 'good':
        return {'uid': 'owner-uid', 'email': 'moi@x.fr'}
    if token == 'stranger':
        return {'uid': 'someone-else'}
    raise AuthError(401, 'invalid_token')


class WebTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            'ADMIN_API_TOKEN': ADMIN, 'CRON_SECRET': CRON, 'ALLOWED_UIDS': 'owner-uid',
            'FIREBASE_WEB_CONFIG': FIREBASE_CFG, 'OVH_EMAIL': 'moi@x.fr',
        }, clear=True)
        self.env.start()
        store_mod.set_base_store(MemoryStore())
        self.box = FakeMailbox()
        self.now = datetime(2026, 10, 29, 9, 0, tzinfo=timezone.utc)
        factory = lambda s: Engine(s, client_factory=self.box.client, now=lambda: self.now, budget=20)
        self.app = create_app(engine_factory=factory, firebase_verifier=verifier)
        self.client = self.app.test_client()

    def tearDown(self):
        self.env.stop()
        store_mod.set_base_store(None)

    def get(self, path, token='good'):
        return self.client.get(path, headers={'Authorization': f'Bearer {token}'} if token else {})

    def send(self, method, path, body=None, token='good'):
        return self.client.open(path, method=method, json=body,
                                headers={'Authorization': f'Bearer {token}'} if token else {})

    # -- securite -------------------------------------------------------------
    def test_health_is_public_and_minimal(self):
        r = self.client.get('/healthz')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json['status'], 'up')
        self.assertIn("script-src 'self'", r.headers['Content-Security-Policy'])

    def test_api_requires_authorized_firebase_user(self):
        self.assertEqual(self.get('/api/status', token=None).status_code, 401)
        self.assertEqual(self.get('/api/status', token='forged').status_code, 401)
        r = self.get('/api/status', token='stranger')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json['error'], 'forbidden_uid:someone-else')
        self.assertEqual(self.get('/api/status').status_code, 200)

    def test_technical_tokens_are_not_interchangeable(self):
        self.assertEqual(self.send('POST', '/internal/tick', token=ADMIN).status_code, 401)
        self.assertEqual(self.send('POST', '/api/diagnostic', token=CRON).status_code, 401)
        self.assertEqual(self.get('/api/status', token=ADMIN).status_code, 401)  # pas de connexion UI par jeton
        self.assertEqual(self.client.get('/internal/tick').status_code, 405)

    def test_short_secret_fails_closed(self):
        with patch.dict(os.environ, {'CRON_SECRET': 'short'}):
            self.assertEqual(self.send('POST', '/internal/tick', token='short').status_code, 401)

    def test_token_login_only_when_enabled(self):
        with patch.dict(os.environ, {'UI_TOKEN_LOGIN': 'true'}):
            self.assertEqual(self.get('/api/me', token=ADMIN).status_code, 200)

    def test_public_config_exposes_only_web_keys(self):
        r = self.client.get('/api/public-config').json
        self.assertEqual(set(r['firebase']), {'apiKey', 'authDomain', 'projectId'})
        self.assertNotIn(ADMIN, json.dumps(r))

    def test_tick_response_contains_no_mail_content(self):
        self.box.add(subject='CONFIDENTIEL projet X', body='SECRET', received=self.now - timedelta(hours=1))
        r = self.send('POST', '/internal/tick', token=CRON)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json['status'], 'ok')
        self.assertNotIn(b'CONFIDENTIEL', r.data)
        self.assertNotIn(b'SECRET', r.data)
        self.assertEqual(r.json['counters']['created'], 1)

    def test_tick_outside_hours_skipped(self):
        self.now = datetime(2026, 10, 29, 17, 0, tzinfo=timezone.utc)  # 18:00 Paris : hors plage
        r = self.send('POST', '/internal/tick', token=CRON)
        self.assertEqual(r.json['status'], 'skipped')
        self.assertEqual(self.box.calls, [])

    def test_diagnostic_error_does_not_leak(self):
        with patch('assistant.web.probe_mailbox', side_effect=RuntimeError('PASSWORD-SECRET')):
            r = self.send('POST', '/api/diagnostic', token=ADMIN)
        self.assertEqual(r.status_code, 503)
        self.assertNotIn(b'PASSWORD-SECRET', r.data)
        self.assertIn('trace', r.json)

    def test_diagnostic_success_persisted(self):
        with patch('assistant.web.probe_mailbox', return_value={'items_checked': 5, 'bodies_available': 5}):
            r = self.send('POST', '/api/diagnostic', token=ADMIN)
        self.assertEqual(r.json['items_checked'], 5)
        self.assertEqual(r.json['store_selftest'], 'ok')
        self.assertEqual(store_mod.get_base_store().get('system', 'ews_probe')['bodies_available'], 5)

    def test_unhandled_error_is_generic(self):
        with patch('assistant.engine.status_summary', side_effect=ValueError('mail body here')):
            r = self.get('/api/status')
        self.assertEqual(r.status_code, 500)
        self.assertNotIn(b'mail body', r.data)

    # -- parcours fonctionnels -----------------------------------------------
    def test_messages_flow(self):
        self.box.add(subject='Demande', received=self.now - timedelta(hours=1))
        self.send('POST', '/api/sync')
        items = self.get('/api/messages?view=a_traiter').json['items']
        self.assertEqual(len(items), 1)
        mid = items[0]['id']
        self.assertEqual(self.get(f'/api/messages/{mid}/body').json['body'], 'Bonjour')
        self.assertEqual(self.get('/api/counts').json['a_traiter'], 1)
        r = self.send('PATCH', f'/api/messages/{mid}', {'category': 'a_savoir'})
        self.assertEqual(r.json['view'], 'a_savoir')
        r = self.send('PATCH', f'/api/messages/{mid}', {'treatment': 'vu'})
        self.assertEqual(r.json['view'], 'archive')
        self.assertEqual(self.send('PATCH', f'/api/messages/{mid}', {'treatment': 'x'}).status_code, 400)
        self.assertEqual(self.get('/api/messages?view=nope').status_code, 400)

    def test_manual_draft_and_move(self):
        self.box.add(subject='Demande', received=self.now - timedelta(hours=1))
        self.send('POST', '/api/sync')
        mid = self.get('/api/messages?view=a_traiter').json['items'][0]['id']
        r = self.send('POST', f'/api/messages/{mid}/draft', {'body': 'Bonjour, bien reçu.'})
        self.assertEqual(r.status_code, 200, r.json)
        self.assertEqual(r.json['status'], 'enregistre')
        self.assertEqual(r.json['to'], ['client@exemple.fr'])
        self.assertEqual(self.send('POST', f'/api/messages/{mid}/draft', {'body': ''}).status_code, 400)
        r = self.send('POST', f'/api/messages/{mid}/move', {'folder_id': 'DUPONT'})
        self.assertEqual(r.status_code, 200)
        audit = self.get('/api/audit').json['items']
        self.assertEqual({a['op'] for a in audit}, {'brouillon', 'deplacement'})
        undo = next(a for a in audit if a['op'] == 'deplacement')
        self.assertEqual(self.send('POST', f"/api/audit/{undo['id']}/undo").status_code, 200)
        self.assertEqual(self.send('POST', f"/api/audit/{undo['id']}/undo").status_code, 400)
        self.assertEqual(self.send('POST', f'/api/messages/{mid}/move', {'folder_id': 'TRASH'}).status_code, 400)

    def test_tasks_crud_and_validation(self):
        r = self.send('POST', '/api/tasks', {'title': 'Relancer le BET', 'priority': 'haute', 'due_at': '2026-11-03'})
        self.assertEqual(r.status_code, 201)
        tid = r.json['id']
        self.assertEqual(r.json['due_source'], 'explicite')
        self.assertEqual(r.json['status'], 'a_faire')
        self.assertEqual(self.send('POST', '/api/tasks', {'title': ''}).status_code, 400)
        self.assertEqual(self.send('POST', '/api/tasks', {'title': 'x', 'due_at': '31/12'}).status_code, 400)
        self.assertEqual(self.send('PATCH', f'/api/tasks/{tid}', {'status': 'nope'}).status_code, 400)
        self.assertEqual(self.send('PATCH', f'/api/tasks/{tid}', {'status': 'terminee'}).status_code, 200)
        self.assertEqual(self.get('/api/tasks?scope=open').json['items'], [])
        self.assertEqual(len(self.get('/api/tasks?scope=closed').json['items']), 1)
        self.assertEqual(self.send('DELETE', f'/api/tasks/{tid}').status_code, 200)

    def test_projects_and_settings(self):
        r = self.send('POST', '/api/projects', {'name': 'Dupont', 'keywords': ['dupont'], 'contacts': ['@plombier.fr']})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.send('POST', '/api/projects', {'name': 'X', 'folder_id': 'inconnu'}).status_code, 400)
        r = self.send('PUT', '/api/settings', {'work_start': '07:30', 'work_days': [0, 1, 2, 3, 4, 5]})
        self.assertEqual(r.json['work_start'], '07:30')
        self.assertEqual(self.send('PUT', '/api/settings', {'work_end': '07:00'}).status_code, 400)
        self.assertEqual(self.send('PUT', '/api/settings', {'unknown': 1}).status_code, 400)
        self.assertEqual(self.send('PUT', '/api/settings', {'ai_enabled': 'yes'}).status_code, 400)

    def test_static_files(self):
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.client.get('/static/app.js').status_code, 200)
        self.assertEqual(self.client.get('/static/../app.py').status_code, 404)


class WindowTests(unittest.TestCase):
    def test_paris_summer_winter_weekend_and_end_excluded(self):
        cfg = settings_mod.merged({})
        for moment, expected in [
            ('2026-10-29T07:00:00+00:00', True),    # 08:00 heure d'hiver
            ('2026-10-29T16:50:00+00:00', True),    # 17:50
            ('2026-10-29T17:00:00+00:00', False),   # 18:00 exclu
            ('2026-07-06T06:00:00+00:00', True),    # 08:00 heure d'ete
            ('2026-07-06T05:59:00+00:00', False),
            ('2026-07-06T16:00:00+00:00', False),
            ('2026-10-31T10:00:00+00:00', False),   # samedi
            ('2026-10-25T00:30:00+00:00', False),   # nuit du changement d'heure
        ]:
            self.assertEqual(settings_mod.in_window(datetime.fromisoformat(moment), cfg), expected, moment)

    def test_validation(self):
        clean, errors = settings_mod.validate({'priority_contacts': [' Chef@X.fr ', ''], 'watched_folders': []})
        self.assertEqual(errors, [])
        self.assertEqual(clean['priority_contacts'], ['chef@x.fr'])
        self.assertEqual(clean['watched_folders'], ['inbox'])
        _, errors = settings_mod.validate({'timezone': 'Mars/Olympus', 'work_start': '8h'})
        self.assertEqual(len(errors), 2)


if __name__ == '__main__':
    unittest.main()
