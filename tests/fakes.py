"""Boite Exchange simulee pour tester le moteur sans reseau."""
import itertools
from datetime import datetime, timedelta, timezone

from assistant.ews import EwsError

T0 = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)


class FakeMailbox:
    def __init__(self):
        self.ids = itertools.count(1)
        self.folders = {
            'ROOT': {'name': 'Racine', 'parent_id': None},
            'INBOX': {'name': 'Inbox', 'parent_id': 'ROOT'},
            'SENT': {'name': 'Éléments envoyés', 'parent_id': 'ROOT'},
            'DRAFTS': {'name': 'Brouillons', 'parent_id': 'ROOT'},
            'TRASH': {'name': 'Éléments supprimés', 'parent_id': 'ROOT'},
            'JUNK': {'name': 'Courrier indésirable', 'parent_id': 'ROOT'},
            'CHANTIERS': {'name': 'Chantiers', 'parent_id': 'INBOX'},
            'DUPONT': {'name': 'Dupont', 'parent_id': 'CHANTIERS'},
        }
        self.dist = {'msgfolderroot': 'ROOT', 'inbox': 'INBOX', 'sentitems': 'SENT', 'drafts': 'DRAFTS',
                     'deleteditems': 'TRASH', 'junkemail': 'JUNK'}
        self.items = {}      # id -> item dict (avec 'folder_id')
        self.log = {}        # folder -> liste d'evenements (type, id, received_at)
        self.calls = []
        self.fail_on = {}    # nom de methode -> exception a lever (une fois)
        self.page_size_override = None

    def resolve(self, ref):
        return self.dist.get(ref, ref)

    def add(self, folder='inbox', subject='Sujet', sender='client@exemple.fr', body='Bonjour',
            received=None, imid=None, conversation=None, headers=None, to=None):
        fid = self.resolve(folder)
        item_id = f'ID{next(self.ids)}'
        n = item_id[2:]
        item = {
            'id': item_id, 'change_key': 'CK1', 'kind': 'Message', 'item_class': 'IPM.Note',
            'subject': subject, 'received_at': received or T0, 'sent_at': received or T0,
            'modified_at': received or T0, 'created_at': received or T0,
            'has_attachments': False, 'importance': 'Normal',
            'conversation_id': conversation or f'CONV{n}', 'folder_id': fid,
            'from': {'name': sender.split('@')[0], 'email': sender},
            'to': [{'name': 'Moi', 'email': to or 'moi@entreprise.fr'}], 'cc': [],
            'internet_message_id': imid if imid is not None else f'<msg{n}@exemple.fr>',
            'is_read': False, 'body': body, 'unique_body': body, 'attachments': [],
            'headers': headers or {}, 'correlation': None,
        }
        self.items[item_id] = item
        self.log.setdefault(fid, []).append(('create', item_id, item['received_at']))
        return item_id

    def client(self):
        return FakeClient(self)


class FakeClient:
    def __init__(self, box):
        self.box = box
        self.auth_scheme = 'NTLM'

    def _hit(self, name):
        self.box.calls.append(name)
        exc = self.box.fail_on.pop(name, None)
        if exc:
            raise exc

    def close(self):
        pass

    def distinguished_ids(self):
        self._hit('distinguished_ids')
        return dict(self.box.dist)

    def list_folders(self):
        self._hit('list_folders')
        return [{'id': k, 'name': v['name'], 'parent_id': v['parent_id'], 'total': 0}
                for k, v in self.box.folders.items() if k != 'ROOT']

    def create_folder(self, parent_ref, name):
        self._hit('create_folder')
        fid = f'F{next(self.box.ids)}'
        self.box.folders[fid] = {'name': name, 'parent_id': self.box.resolve(parent_ref)}
        return fid

    def sync_items(self, ref, state, max_changes=100):
        self._hit('sync_items')
        fid = self.box.resolve(ref)
        if fid not in self.box.folders:
            raise EwsError('ErrorFolderNotFound')
        events = self.box.log.get(fid, [])
        start = int(state or 0)
        size = self.box.page_size_override or max_changes
        page = events[start:start + size]
        changes = []
        for kind, item_id, received in page:
            changes.append({'type': kind, 'id': item_id, 'received_at': received, 'kind': 'Message'})
        end = start + len(page)
        return changes, str(end), end >= len(events)

    def get_items(self, ids, with_body=False, extra_fields=()):
        self._hit('get_items')
        out = {}
        for i in ids:
            item = self.box.items.get(i)
            if item is None:
                out[i] = None
                continue
            copy = dict(item)
            if not with_body:
                copy['body'] = copy['unique_body'] = None
            out[i] = copy
        return out

    def find_by_correlation(self, correlation, folder_ref='drafts'):
        self._hit('find_by_correlation')
        fid = self.box.resolve(folder_ref)
        return [dict(i) for i in self.box.items.values() if i['folder_id'] == fid and i.get('correlation') == correlation]

    def find_recent(self, folder_ref, limit=20):
        self._hit('find_recent')
        fid = self.box.resolve(folder_ref)
        return [dict(i) for i in self.box.items.values() if i['folder_id'] == fid][:limit]

    def move_item(self, item_id, to_ref):
        self._hit('move_item')
        item = self.box.items.pop(item_id, None)
        if item is None:
            raise EwsError('ErrorItemNotFound')
        old_folder = item['folder_id']
        new_id = f'ID{next(self.box.ids)}'
        item.update({'id': new_id, 'folder_id': self.box.resolve(to_ref)})
        self.box.items[new_id] = item
        self.box.log.setdefault(old_folder, []).append(('delete', item_id, None))
        self.box.log.setdefault(item['folder_id'], []).append(('create', new_id, item['received_at']))
        return new_id

    def create_reply_draft(self, item_id, change_key, body_text, reply_all=False):
        self._hit('create_reply_draft')
        src = self.box.items[item_id]
        draft_id = self.box.add('drafts', subject='RE: ' + src['subject'], sender='moi@entreprise.fr',
                                body=body_text, conversation=src['conversation_id'], imid='')
        d = self.box.items[draft_id]
        d['internet_message_id'] = None
        d['to'] = [src['from']]
        d['created_at'] = d['modified_at'] = datetime.now(timezone.utc)
        return draft_id, 'CK1'

    def set_correlation(self, item_id, change_key, correlation):
        self._hit('set_correlation')
        self.box.items[item_id]['correlation'] = correlation
        self.box.items[item_id]['modified_at'] = self.box.items[item_id]['modified_at'] + timedelta(seconds=1)
        return 'CK2'

    def trash_draft(self, item_id):
        self._hit('trash_draft')
        item = self.box.items.get(item_id)
        if item:
            item['folder_id'] = 'TRASH'
