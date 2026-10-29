"""Couche de persistance : Firestore (production) ou memoire (tests, essais locaux).

Toutes les donnees metier sont rangees sous `users/{uid}/...`. Les requetes se
limitent a des filtres simples sur UN champ pour ne necessiter aucun index
composite Firestore (tri effectue cote serveur Python).
"""
import copy
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone

from . import config


def utcnow():
    return datetime.now(timezone.utc)


class MemoryStore:
    """Implementation en memoire, thread-safe, utilisee par les tests."""

    def __init__(self):
        self._data = {}
        self._lock = threading.RLock()

    def _col(self, path):
        return self._data.setdefault(path, {})

    def get(self, col, doc_id):
        with self._lock:
            doc = self._col(col).get(doc_id)
            return copy.deepcopy(doc) if doc is not None else None

    def set(self, col, doc_id, data, merge=False):
        with self._lock:
            target = self._col(col)
            if merge and doc_id in target:
                target[doc_id].update(copy.deepcopy(data))
            else:
                target[doc_id] = copy.deepcopy(data)

    def add(self, col, data):
        doc_id = uuid.uuid4().hex
        self.set(col, doc_id, data)
        return doc_id

    def delete(self, col, doc_id):
        with self._lock:
            self._col(col).pop(doc_id, None)

    def query(self, col, field=None, op='==', value=None, limit=None, order_by=None, descending=False):
        with self._lock:
            rows = [(k, copy.deepcopy(v)) for k, v in self._col(col).items()]
        if field is not None:
            rows = [r for r in rows if _match(r[1].get(field), op, value)]
        if order_by:
            rows.sort(key=lambda r: _sort_key(r[1].get(order_by)), reverse=descending)
        return rows[:limit] if limit else rows

    def count(self, col, field, op, value):
        return len(self.query(col, field, op, value))

    def acquire_lease(self, col, doc_id, owner, seconds, now=None):
        now = now or utcnow()
        with self._lock:
            doc = self._col(col).get(doc_id) or {}
            until = doc.get('lease_until')
            if doc.get('owner') and until and until > now and doc.get('owner') != owner:
                return False
            doc.update({'owner': owner, 'lease_until': now + timedelta(seconds=seconds),
                        'acquired_at': now, 'attempt': int(doc.get('attempt') or 0) + 1})
            self._col(col)[doc_id] = doc
            return True

    def release_lease(self, col, doc_id, owner, extra=None):
        with self._lock:
            doc = self._col(col).get(doc_id)
            if doc and doc.get('owner') == owner:
                doc.update({'owner': None, 'lease_until': None, **(extra or {})})


def _match(actual, op, value):
    if op == '==':
        return actual == value
    if op == 'in':
        return actual in value
    if op == 'array_contains':
        return isinstance(actual, list) and value in actual
    if actual is None:
        return False
    if op == '<':
        return actual < value
    if op == '>=':
        return actual >= value
    raise ValueError(op)


def _sort_key(value):
    if value is None:
        return (0, '')
    if isinstance(value, datetime):
        return (1, value.isoformat())
    return (1, str(value))


class FirestoreStore:
    """Implementation Firestore via le SDK Admin (identite serveur uniquement)."""

    def __init__(self, client=None):
        if client is None:
            client = firestore_client()
        self.db = client

    def _ref(self, col):
        return self.db.collection(col)

    def get(self, col, doc_id):
        snap = self._ref(col).document(doc_id).get()
        return snap.to_dict() if snap.exists else None

    def set(self, col, doc_id, data, merge=False):
        self._ref(col).document(doc_id).set(data, merge=merge)

    def add(self, col, data):
        ref = self._ref(col).document()
        ref.set(data)
        return ref.id

    def delete(self, col, doc_id):
        self._ref(col).document(doc_id).delete()

    def query(self, col, field=None, op='==', value=None, limit=None, order_by=None, descending=False):
        from google.cloud.firestore_v1.base_query import FieldFilter
        q = self._ref(col)
        if field is not None:
            q = q.where(filter=FieldFilter(field, op, value))
            # Tri Python : evite les index composites (champ filtre != champ trie).
            rows = [(s.id, s.to_dict()) for s in q.limit(limit or 1000).stream()]
            if order_by:
                rows.sort(key=lambda r: _sort_key(r[1].get(order_by)), reverse=descending)
            return rows
        if order_by:
            from google.cloud import firestore
            q = q.order_by(order_by, direction=firestore.Query.DESCENDING if descending
                           else firestore.Query.ASCENDING)
        if limit:
            q = q.limit(limit)
        return [(s.id, s.to_dict()) for s in q.stream()]

    def count(self, col, field, op, value):
        """Agregation serveur : ~1 lecture facturee par tranche de 1000 documents."""
        from google.cloud.firestore_v1.base_query import FieldFilter
        q = self._ref(col).where(filter=FieldFilter(field, op, value))
        return int(q.count(alias='n').get()[0][0].value)

    def acquire_lease(self, col, doc_id, owner, seconds, now=None):
        from google.cloud import firestore
        ref = self._ref(col).document(doc_id)
        transaction = self.db.transaction()

        @firestore.transactional
        def run(tx):
            snap = ref.get(transaction=tx)
            doc = snap.to_dict() if snap.exists else {}
            current = now or utcnow()
            until = doc.get('lease_until')
            if doc.get('owner') and until and until > current and doc.get('owner') != owner:
                return False
            tx.set(ref, {'owner': owner, 'lease_until': current + timedelta(seconds=seconds),
                         'acquired_at': current, 'attempt': int(doc.get('attempt') or 0) + 1},
                   merge=True)
            return True

        return run(transaction)

    def release_lease(self, col, doc_id, owner, extra=None):
        from google.cloud import firestore
        ref = self._ref(col).document(doc_id)
        transaction = self.db.transaction()

        @firestore.transactional
        def run(tx):
            snap = ref.get(transaction=tx)
            if snap.exists and (snap.to_dict() or {}).get('owner') == owner:
                tx.set(ref, {'owner': None, 'lease_until': None, **(extra or {})}, merge=True)

        run(transaction)


def selftest(store):
    """Verifie ecriture, lecture, requete et bail transactionnel (diagnostic de deploiement)."""
    owner = uuid.uuid4().hex
    now = utcnow()
    store.set('system', 'selftest', {'at': now, 'owner': None, 'kind': 'selftest'})
    if not store.get('system', 'selftest'):
        raise RuntimeError('read')
    if not any(doc_id == 'selftest' for doc_id, _ in store.query('system', 'kind', '==', 'selftest', limit=5)):
        raise RuntimeError('query')
    if not store.acquire_lease('system', 'selftest', owner, 30, now):
        raise RuntimeError('lease')
    if store.acquire_lease('system', 'selftest', 'other', 30, now):
        raise RuntimeError('lease_exclusive')
    store.release_lease('system', 'selftest', owner)
    store.delete('system', 'selftest')
    return 'ok'


_firebase_lock = threading.Lock()


def firebase_app():
    import firebase_admin
    from firebase_admin import credentials
    with _firebase_lock:
        try:
            return firebase_admin.get_app()
        except ValueError:
            raw = config.env('FIREBASE_SERVICE_ACCOUNT_JSON')
            if not raw:
                raise RuntimeError('FIREBASE_SERVICE_ACCOUNT_JSON missing')
            return firebase_admin.initialize_app(credentials.Certificate(json.loads(raw)))


def firestore_client():
    from firebase_admin import firestore
    return firestore.client(firebase_app())


class UserStore:
    """Vue d'un Store limitee a `users/{uid}`."""

    def __init__(self, base, uid):
        self.base = base
        self.uid = uid
        self.prefix = f'users/{uid}/'

    def __getattr__(self, name):
        method = getattr(self.base, name)

        def scoped(col, *args, **kwargs):
            return method(self.prefix + col, *args, **kwargs)
        return scoped


_store = None
_store_lock = threading.Lock()


def get_base_store():
    global _store
    with _store_lock:
        if _store is None:
            _store = FirestoreStore() if config.store_backend() == 'firestore' else MemoryStore()
        return _store


def set_base_store(store):
    global _store
    with _store_lock:
        _store = store


def user_store(uid=None):
    return UserStore(get_base_store(), uid or config.owner_uid())
