"""Client EWS (SOAP) minimal pour Exchange OVH.

Principes de securite :
- destination fixe en HTTPS, aucune redirection suivie, verification TLS active ;
- liste blanche d'operations : AUCUNE operation d'envoi (SendItem) n'existe ici et
  toute creation se fait avec MessageDisposition="SaveOnly" ;
- les erreurs ne contiennent ni XML, ni contenu de mail, ni identifiants.
"""
import hashlib
import re
from datetime import datetime, timezone
from xml.sax.saxutils import escape, quoteattr

from . import config

M = 'http://schemas.microsoft.com/exchange/services/2006/messages'
T = 'http://schemas.microsoft.com/exchange/services/2006/types'
S = 'http://schemas.xmlsoap.org/soap/envelope/'
NS = {'m': M, 't': T, 's': S}

SERVER_VERSION = 'Exchange2013_SP1'
ALLOWED_OPERATIONS = frozenset({
    'GetFolder', 'FindFolder', 'FindItem', 'GetItem', 'SyncFolderItems',
    'MoveItem', 'CreateItem', 'UpdateItem', 'DeleteItem', 'CreateFolder',
})
# Le contenu utilisateur est toujours echappe : un attribut ne peut provenir que du code.
FORBIDDEN_DISPOSITION = re.compile(r'MessageDisposition="(?!SaveOnly")|<m:SendItem')
# Propriete MAPI nommee servant a marquer les brouillons crees par l'assistant
# (reconciliation apres un arret brutal entre Exchange et Firestore).
CORRELATION_SET = 'c2f6a0b4-6d1e-4f43-9a51-6b9d4b1f2e77'
CORRELATION_NAME = 'AssistantMailCorrelation'

MAIL_FIELDS = (
    'item:Subject', 'item:DateTimeReceived', 'item:DateTimeSent', 'item:ItemClass',
    'item:HasAttachments', 'item:Importance', 'item:ConversationId', 'item:ParentFolderId',
    'item:LastModifiedTime', 'message:From', 'message:ToRecipients', 'message:CcRecipients',
    'message:InternetMessageId', 'message:IsRead',
)


class EwsError(Exception):
    """Erreur EWS assainie : `code` est un code de reponse Exchange (jamais un contenu)."""

    def __init__(self, code, message='EWS failure'):
        super().__init__(message)
        self.code = code


class EwsBusy(EwsError):
    pass


def envelope(body):
    return (f'<s:Envelope xmlns:s="{S}" xmlns:m="{M}" xmlns:t="{T}"><s:Header>'
            f'<t:RequestServerVersion Version="{SERVER_VERSION}"/></s:Header>'
            f'<s:Body>{body}</s:Body></s:Envelope>').encode('utf-8')


def attr(value):
    return quoteattr(str(value))


def item_id_xml(item_id, change_key=None, tag='t:ItemId'):
    ck = f' ChangeKey={attr(change_key)}' if change_key else ''
    return f'<{tag} Id={attr(item_id)}{ck}/>'


def folder_ref_xml(ref):
    """`ref` est soit un nom distingue (inbox, sentitems...), soit un FolderId."""
    if ref in DISTINGUISHED:
        return f'<t:DistinguishedFolderId Id="{ref}"/>'
    return f'<t:FolderId Id={attr(ref)}/>'


DISTINGUISHED = ('msgfolderroot', 'inbox', 'sentitems', 'drafts', 'deleteditems', 'junkemail')


def fields_xml(fields):
    return ''.join(f'<t:FieldURI FieldURI="{f}"/>' for f in fields)


def correlation_uri():
    return (f'<t:ExtendedFieldURI PropertySetId="{CORRELATION_SET}" '
            f'PropertyName="{CORRELATION_NAME}" PropertyType="String"/>')


def text(node, path):
    found = node.find(path, NS)
    return found.text if found is not None and found.text is not None else None


def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)
    except ValueError:
        return None


def parse_mailbox(node):
    if node is None:
        return None
    mbox = node.find('t:Mailbox', NS) if node.tag != f'{{{T}}}Mailbox' else node
    if mbox is None:
        return None
    email = text(mbox, 't:EmailAddress') or ''
    routing = (text(mbox, 't:RoutingType') or 'SMTP').upper()
    if routing != 'SMTP' or '@' not in email:
        email = ''
    return {'name': (text(mbox, 't:Name') or '')[:200], 'email': email.lower()[:320]}


def parse_recipients(node, tag):
    box = node.find(tag, NS)
    if box is None:
        return []
    out = []
    for mbox in box.findall('t:Mailbox', NS)[:50]:
        parsed = parse_mailbox(mbox)
        if parsed:
            out.append(parsed)
    return out


ITEM_TAGS = ('Message', 'MeetingRequest', 'MeetingResponse', 'MeetingCancellation',
             'MeetingMessage', 'Item', 'PostItem')


def iter_items(container):
    if container is None:
        return []
    return [child for child in container if child.tag.split('}')[1] in ITEM_TAGS]


HEADER_KEYS = ('list-unsubscribe', 'list-id', 'precedence', 'auto-submitted',
               'x-auto-response-suppress', 'in-reply-to', 'references')


def parse_item(node):
    iid = node.find('t:ItemId', NS)
    attachments = []
    att_box = node.find('t:Attachments', NS)
    if att_box is not None:
        for att in list(att_box)[:30]:
            name = text(att, 't:Name')
            if name and not (text(att, 't:IsInline') == 'true'):
                attachments.append(name[:200])
    headers = {}
    hbox = node.find('t:InternetMessageHeaders', NS)
    if hbox is not None:
        for header in hbox.findall('t:InternetMessageHeader', NS):
            key = (header.get('HeaderName') or '').lower()
            if key in HEADER_KEYS and header.text:
                headers[key] = header.text[:500]
    conv = node.find('t:ConversationId', NS)
    parent = node.find('t:ParentFolderId', NS)
    return {
        'id': iid.get('Id') if iid is not None else None,
        'change_key': iid.get('ChangeKey') if iid is not None else None,
        'kind': node.tag.split('}')[1],
        'item_class': text(node, 't:ItemClass'),
        'subject': (text(node, 't:Subject') or '')[:500],
        'received_at': parse_dt(text(node, 't:DateTimeReceived')),
        'sent_at': parse_dt(text(node, 't:DateTimeSent')),
        'modified_at': parse_dt(text(node, 't:LastModifiedTime')),
        'created_at': parse_dt(text(node, 't:DateTimeCreated')),
        'has_attachments': text(node, 't:HasAttachments') == 'true',
        'importance': text(node, 't:Importance'),
        'conversation_id': conv.get('Id') if conv is not None else None,
        'folder_id': parent.get('Id') if parent is not None else None,
        'from': parse_mailbox(node.find('t:From', NS)),
        'to': parse_recipients(node, 't:ToRecipients'),
        'cc': parse_recipients(node, 't:CcRecipients'),
        'internet_message_id': (text(node, 't:InternetMessageId') or '')[:500] or None,
        'is_read': text(node, 't:IsRead') == 'true',
        'body': text(node, 't:Body'),
        'unique_body': text(node, 't:UniqueBody'),
        'attachments': attachments,
        'headers': headers,
        'correlation': next((text(p, 't:Value') for p in node.findall('t:ExtendedProperty', NS)), None),
    }


def html_from_text(value):
    """Corps HTML minimal a partir d'un texte (tout est echappe)."""
    lines = escape(value or '').split('\n')
    return '<div style="font-family:Calibri,Arial,sans-serif;font-size:11pt">' + \
        '<br>'.join(lines) + '</div>'


class EwsClient:
    """Une instance = une session HTTP (NTLM est lie a la connexion)."""

    def __init__(self, username=None, password=None, endpoint=None, session=None):
        self.username = username if username is not None else config.env('OVH_EMAIL')
        self.password = password if password is not None else config.env('OVH_PASSWORD')
        self.endpoint = endpoint or config.ews_endpoint()
        if not self.endpoint.startswith('https://'):
            raise EwsError('ConfigError', 'HTTPS required')
        self._session = session
        self._negotiated = session is not None
        self.auth_scheme = None

    # -- transport -------------------------------------------------------
    def _ensure_session(self):
        if self._negotiated:
            return self._session
        if not self.username or not self.password:
            raise EwsError('ConfigError', 'Missing mailbox credentials')
        import requests
        from requests.auth import HTTPBasicAuth
        from requests_ntlm import HttpNtlmAuth
        session = requests.Session()
        session.trust_env = False
        challenge = session.get(self.endpoint, timeout=(5, 12), allow_redirects=False)
        offered = challenge.headers.get('WWW-Authenticate', '').upper()
        if challenge.status_code != 401:
            session.close()
            raise EwsError('UnexpectedChallenge', 'Unexpected authentication challenge')
        if 'NTLM' in offered:
            session.auth = HttpNtlmAuth(self.username, self.password)
            self.auth_scheme = 'NTLM'
        elif 'BASIC' in offered:
            session.auth = HTTPBasicAuth(self.username, self.password)
            self.auth_scheme = 'Basic'
        else:
            session.close()
            raise EwsError('UnsupportedAuth', 'Unsupported authentication')
        self._session = session
        self._negotiated = True
        return session

    def close(self):
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def post(self, operation, body):
        """Execute une operation et renvoie la liste des ResponseMessage."""
        if operation not in ALLOWED_OPERATIONS:
            raise EwsError('Forbidden', 'Operation not allowed')
        if FORBIDDEN_DISPOSITION.search(body):
            raise EwsError('Forbidden', 'Sending is never allowed')
        from defusedxml.ElementTree import fromstring
        session = self._ensure_session()
        response = session.post(self.endpoint, data=envelope(body), headers={
            'Content-Type': 'text/xml; charset=utf-8', 'SOAPAction': f'{M}/{operation}',
        }, timeout=(5, 20), allow_redirects=False)
        if response.status_code == 401:
            raise EwsError('Unauthorized', 'Authentication refused')
        if response.status_code not in (200, 500):
            raise EwsError(f'HTTP{response.status_code}', 'HTTP failure')
        try:
            root = fromstring(response.content)
        except Exception:
            raise EwsError('InvalidXml', 'Invalid response')
        fault = root.find('.//s:Fault', NS)
        if fault is not None:
            code = root.find('.//{*}ResponseCode')
            code_text = code.text if code is not None else 'SoapFault'
            if code_text in ('ErrorServerBusy', 'ErrorTooManyObjectsOpened'):
                raise EwsBusy(code_text, 'Server busy')
            raise EwsError(safe_code(code_text))
        messages = root.findall('.//m:ResponseMessages/*', NS)
        if not messages:
            raise EwsError('EmptyResponse')
        for msg in messages:
            code = text(msg, 'm:ResponseCode')
            if code == 'ErrorServerBusy':
                raise EwsBusy(code, 'Server busy')
        return messages

    @staticmethod
    def check(msg):
        code = text(msg, 'm:ResponseCode') or 'Unknown'
        if code != 'NoError':
            raise EwsError(safe_code(code))
        return msg

    # -- dossiers ----------------------------------------------------------
    def distinguished_ids(self):
        refs = ''.join(f'<t:DistinguishedFolderId Id="{d}"/>' for d in DISTINGUISHED)
        msgs = self.post('GetFolder', '<m:GetFolder><m:FolderShape><t:BaseShape>IdOnly</t:BaseShape>'
                         '</m:FolderShape><m:FolderIds>' + refs + '</m:FolderIds></m:GetFolder>')
        out = {}
        for name, msg in zip(DISTINGUISHED, msgs):
            if text(msg, 'm:ResponseCode') != 'NoError':
                continue
            fid = msg.find('.//t:FolderId', NS)
            if fid is not None:
                out[name] = fid.get('Id')
        return out

    def list_folders(self, page_size=200, max_folders=1000):
        props = fields_xml(('folder:DisplayName', 'folder:ParentFolderId', 'folder:FolderClass',
                            'folder:TotalCount', 'folder:ChildFolderCount'))
        offset, folders = 0, []
        while len(folders) < max_folders:
            msg = self.check(self.post('FindFolder', (
                '<m:FindFolder Traversal="Deep"><m:FolderShape><t:BaseShape>IdOnly</t:BaseShape>'
                f'<t:AdditionalProperties>{props}</t:AdditionalProperties></m:FolderShape>'
                f'<m:IndexedPageFolderView MaxEntriesReturned="{page_size}" Offset="{offset}" BasePoint="Beginning"/>'
                '<m:ParentFolderIds><t:DistinguishedFolderId Id="msgfolderroot"/></m:ParentFolderIds>'
                '</m:FindFolder>'))[0])
            box = msg.find('.//t:Folders', NS)
            page = list(box) if box is not None else []
            for node in page:
                if node.tag != f'{{{T}}}Folder':
                    continue  # calendriers, contacts, dossiers de recherche...
                fclass = text(node, 't:FolderClass')
                if fclass and not fclass.startswith('IPF.Note'):
                    continue
                fid = node.find('t:FolderId', NS)
                parent = node.find('t:ParentFolderId', NS)
                folders.append({
                    'id': fid.get('Id'),
                    'name': (text(node, 't:DisplayName') or '')[:200],
                    'parent_id': parent.get('Id') if parent is not None else None,
                    'total': int(text(node, 't:TotalCount') or 0),
                })
            root_folder = msg.find('m:RootFolder', NS)
            last = root_folder is None or root_folder.get('IncludesLastItemInRange') != 'false'
            if last or not page:
                break
            offset = int(root_folder.get('IndexedPagingOffset') or offset + len(page))
        return folders

    def create_folder(self, parent_ref, name):
        msg = self.check(self.post('CreateFolder', (
            '<m:CreateFolder><m:ParentFolderId>' + folder_ref_xml(parent_ref) + '</m:ParentFolderId>'
            '<m:Folders><t:Folder><t:FolderClass>IPF.Note</t:FolderClass>'
            f'<t:DisplayName>{escape(name)}</t:DisplayName></t:Folder></m:Folders></m:CreateFolder>'))[0])
        fid = msg.find('.//t:FolderId', NS)
        if fid is None:
            raise EwsError('FolderNotCreated')
        return fid.get('Id')

    # -- synchronisation -------------------------------------------------
    def sync_items(self, folder_ref, sync_state=None, max_changes=100):
        """Une page de SyncFolderItems. Renvoie (changes, new_state, includes_last)."""
        state_xml = f'<m:SyncState>{escape(sync_state)}</m:SyncState>' if sync_state else ''
        msg = self.check(self.post('SyncFolderItems', (
            '<m:SyncFolderItems><m:ItemShape><t:BaseShape>IdOnly</t:BaseShape>'
            '<t:AdditionalProperties><t:FieldURI FieldURI="item:DateTimeReceived"/></t:AdditionalProperties>'
            '</m:ItemShape><m:SyncFolderId>' + folder_ref_xml(folder_ref) + '</m:SyncFolderId>'
            f'{state_xml}<m:MaxChangesReturned>{int(max_changes)}</m:MaxChangesReturned>'
            '<m:SyncScope>NormalItems</m:SyncScope></m:SyncFolderItems>'))[0])
        changes = []
        box = msg.find('m:Changes', NS)
        for change in (list(box) if box is not None else []):
            kind = change.tag.split('}')[1]
            if kind in ('Create', 'Update'):
                items = iter_items(change)
                if items:
                    parsed = parse_item(items[0])
                    changes.append({'type': kind.lower(), 'id': parsed['id'],
                                    'received_at': parsed['received_at'],
                                    'kind': parsed['kind']})
            elif kind == 'Delete':
                iid = change.find('t:ItemId', NS)
                if iid is not None:
                    changes.append({'type': 'delete', 'id': iid.get('Id')})
            # ReadFlagChange : ignore (l'etat lu/non lu n'est pas un etat de traitement).
        new_state = text(msg, 'm:SyncState')
        includes_last = text(msg, 'm:IncludesLastItemInRange') != 'false'
        return changes, new_state, includes_last

    # -- lecture ------------------------------------------------------------
    def get_items(self, ids, with_body=False, extra_fields=()):
        """GetItem par lot. Renvoie {id: item|None} (None = introuvable)."""
        if not ids:
            return {}
        fields = list(MAIL_FIELDS) + list(extra_fields)
        if with_body:
            fields += ['item:Body', 'item:UniqueBody', 'item:Attachments', 'item:InternetMessageHeaders']
        body_type = '<t:BodyType>Text</t:BodyType>' if with_body else ''
        out = {}
        for start in range(0, len(ids), 25):
            chunk = ids[start:start + 25]
            msgs = self.post('GetItem', (
                '<m:GetItem><m:ItemShape><t:BaseShape>IdOnly</t:BaseShape>' + body_type +
                '<t:AdditionalProperties>' + fields_xml(fields) + correlation_uri() +
                '</t:AdditionalProperties></m:ItemShape><m:ItemIds>' +
                ''.join(item_id_xml(i) for i in chunk) + '</m:ItemIds></m:GetItem>'))
            for item_id, msg in zip(chunk, msgs):
                code = text(msg, 'm:ResponseCode')
                if code == 'NoError':
                    items = iter_items(msg.find('m:Items', NS))
                    out[item_id] = parse_item(items[0]) if items else None
                elif code in ('ErrorItemNotFound', 'ErrorInvalidIdMalformed', 'ErrorInvalidIdNotAnItemAttachmentId'):
                    out[item_id] = None
                else:
                    raise EwsError(safe_code(code))
        return out

    def find_recent(self, folder_ref, limit=20):
        msg = self.check(self.post('FindItem', (
            '<m:FindItem Traversal="Shallow"><m:ItemShape><t:BaseShape>IdOnly</t:BaseShape>'
            '<t:AdditionalProperties>' + fields_xml(('item:Subject', 'item:ConversationId',
                                                     'item:DateTimeCreated')) + correlation_uri() +
            f'</t:AdditionalProperties></m:ItemShape><m:IndexedPageItemView MaxEntriesReturned="{int(limit)}" '
            'Offset="0" BasePoint="Beginning"/><m:SortOrder><t:FieldOrder Order="Descending">'
            '<t:FieldURI FieldURI="item:DateTimeCreated"/></t:FieldOrder></m:SortOrder>'
            '<m:ParentFolderIds>' + folder_ref_xml(folder_ref) + '</m:ParentFolderIds></m:FindItem>'))[0])
        return [parse_item(n) for n in iter_items(msg.find('.//t:Items', NS))]

    def find_by_correlation(self, correlation, folder_ref='drafts'):
        msg = self.check(self.post('FindItem', (
            '<m:FindItem Traversal="Shallow"><m:ItemShape><t:BaseShape>IdOnly</t:BaseShape></m:ItemShape>'
            '<m:Restriction><t:IsEqualTo>' + correlation_uri() +
            f'<t:FieldURIOrConstant><t:Constant Value={attr(correlation)}/></t:FieldURIOrConstant>'
            '</t:IsEqualTo></m:Restriction><m:ParentFolderIds>' + folder_ref_xml(folder_ref) +
            '</m:ParentFolderIds></m:FindItem>'))[0])
        items = iter_items(msg.find('.//t:Items', NS))
        return [parse_item(n) for n in items]

    # -- ecritures controlees ---------------------------------------------
    def move_item(self, item_id, to_folder_ref):
        msg = self.check(self.post('MoveItem', (
            '<m:MoveItem><m:ToFolderId>' + folder_ref_xml(to_folder_ref) + '</m:ToFolderId>'
            '<m:ItemIds>' + item_id_xml(item_id) + '</m:ItemIds>'
            '<m:ReturnNewItemIds>true</m:ReturnNewItemIds></m:MoveItem>'))[0])
        items = iter_items(msg.find('m:Items', NS))
        new_id = items[0].find('t:ItemId', NS) if items else None
        return new_id.get('Id') if new_id is not None else None

    def create_reply_draft(self, item_id, change_key, body_text, reply_all=False):
        """Cree un brouillon de REPONSE (fil conserve) dans Brouillons, sans envoi."""
        tag = 't:ReplyAllToItem' if reply_all else 't:ReplyToItem'
        msg = self.check(self.post('CreateItem', (
            '<m:CreateItem MessageDisposition="SaveOnly"><m:SavedItemFolderId>'
            '<t:DistinguishedFolderId Id="drafts"/></m:SavedItemFolderId><m:Items>'
            f'<{tag}>' + item_id_xml(item_id, change_key, 't:ReferenceItemId') +
            '<t:NewBodyContent BodyType="HTML">' + escape(html_from_text(body_text)) +
            f'</t:NewBodyContent></{tag}></m:Items></m:CreateItem>'))[0])
        items = iter_items(msg.find('m:Items', NS))
        iid = items[0].find('t:ItemId', NS) if items else None
        if iid is None:
            raise EwsError('DraftNotConfirmed')
        return iid.get('Id'), iid.get('ChangeKey')

    def set_correlation(self, item_id, change_key, correlation):
        msg = self.check(self.post('UpdateItem', (
            '<m:UpdateItem MessageDisposition="SaveOnly" ConflictResolution="AutoResolve">'
            '<m:ItemChanges><t:ItemChange>' + item_id_xml(item_id, change_key) +
            '<t:Updates><t:SetItemField>' + correlation_uri() + '<t:Message><t:ExtendedProperty>' +
            correlation_uri() + f'<t:Value>{escape(correlation)}</t:Value></t:ExtendedProperty>'
            '</t:Message></t:SetItemField></t:Updates></t:ItemChange></m:ItemChanges></m:UpdateItem>'))[0])
        items = iter_items(msg.find('m:Items', NS))
        iid = items[0].find('t:ItemId', NS) if items else None
        return iid.get('ChangeKey') if iid is not None else change_key

    def trash_draft(self, item_id):
        """Deplace un brouillon de l'assistant dans Elements supprimes (recuperable)."""
        msgs = self.post('DeleteItem', (
            '<m:DeleteItem DeleteType="MoveToDeletedItems"><m:ItemIds>' + item_id_xml(item_id) +
            '</m:ItemIds></m:DeleteItem>'))
        code = text(msgs[0], 'm:ResponseCode')
        if code not in ('NoError', 'ErrorItemNotFound'):
            raise EwsError(safe_code(code))


def safe_code(code):
    return code if code and re.fullmatch(r'[A-Za-z0-9_]{1,80}', code) else 'EwsFailure'


def stable_hash(*parts):
    raw = '\x1f'.join(p or '' for p in parts)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]


def probe_mailbox():
    """Diagnostic historique du prototype : lecture seule, compteurs uniquement."""
    with EwsClient() as client:
        ids = client.distinguished_ids()
        if 'inbox' not in ids:
            raise EwsError('InboxNotConfirmed')
        msg = client.check(client.post('FindItem', (
            '<m:FindItem Traversal="Shallow"><m:ItemShape><t:BaseShape>IdOnly</t:BaseShape></m:ItemShape>'
            '<m:IndexedPageItemView MaxEntriesReturned="5" Offset="0" BasePoint="Beginning"/>'
            '<m:SortOrder><t:FieldOrder Order="Descending"><t:FieldURI FieldURI="item:DateTimeReceived"/>'
            '</t:FieldOrder></m:SortOrder><m:ParentFolderIds><t:DistinguishedFolderId Id="inbox"/>'
            '</m:ParentFolderIds></m:FindItem>'))[0])
        found = [n.find('t:ItemId', NS).get('Id') for n in iter_items(msg.find('.//t:Items', NS))]
        items = client.get_items(found, with_body=True)
        bodies = sum(1 for i in items.values() if i and i.get('body') is not None)
        return {'items_checked': len(found), 'bodies_available': bodies,
                'folders_visible': len(client.list_folders()), 'auth': client.auth_scheme}
