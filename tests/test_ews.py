import unittest
import xml.etree.ElementTree as ET

from assistant import ews
from assistant.ews import EwsClient, EwsError

M, T = ews.M, ews.T


def soap(body):
    return (f'<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="{ews.S}"><s:Body>{body}'
            '</s:Body></s:Envelope>').encode()


def response(op, inner, code='NoError'):
    return soap(f'<m:{op}Response xmlns:m="{M}" xmlns:t="{T}"><m:ResponseMessages>'
                f'<m:{op}ResponseMessage ResponseClass="Success"><m:ResponseCode>{code}</m:ResponseCode>'
                f'{inner}</m:{op}ResponseMessage></m:ResponseMessages></m:{op}Response>')


class FakeResponse:
    def __init__(self, status, content=b'', headers=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}


class FakeSession:
    def __init__(self, responses, challenge=None):
        self.responses = list(responses)
        self.sent = []
        self.challenge = challenge
        self.auth = None
        self.trust_env = True

    def get(self, url, **kw):
        self.get_kwargs = kw
        return self.challenge

    def post(self, url, data=None, headers=None, **kw):
        self.sent.append((headers['SOAPAction'], data.decode(), kw))
        return self.responses.pop(0)

    def close(self):
        pass


def client_with(*responses):
    session = FakeSession([FakeResponse(200, r) for r in responses])
    return EwsClient('u@x.fr', 'pw', session=session), session


class EnvelopeTests(unittest.TestCase):
    def test_envelope_is_valid_and_versioned(self):
        root = ET.fromstring(ews.envelope('<m:GetFolder/>'))
        version = root.find(f'.//{{{T}}}RequestServerVersion')
        self.assertEqual(version.get('Version'), 'Exchange2013_SP1')

    def test_operation_allowlist_has_no_send(self):
        self.assertNotIn('SendItem', ews.ALLOWED_OPERATIONS)
        client, _ = client_with()
        with self.assertRaises(EwsError):
            client.post('SendItem', '<m:SendItem/>')
        with self.assertRaises(EwsError):
            client.post('CreateItem', '<m:CreateItem MessageDisposition="SendAndSaveCopy"/>')
        with self.assertRaises(EwsError):
            client.post('CreateItem', '<m:CreateItem MessageDisposition="SendOnly"/>')

    def test_http_only_is_refused(self):
        with self.assertRaises(EwsError):
            EwsClient('u', 'p', endpoint='http://ex.mail.ovh.net/EWS/Exchange.asmx')


class AuthNegotiationTests(unittest.TestCase):
    def negotiate(self, challenge):
        import requests
        session = FakeSession([], challenge=challenge)
        original = requests.Session
        requests.Session = lambda: session
        try:
            client = EwsClient('u@x.fr', 'pw')
            client._ensure_session()
            return client, session
        finally:
            requests.Session = original

    def test_ntlm_preferred(self):
        client, session = self.negotiate(FakeResponse(401, headers={'WWW-Authenticate': 'Negotiate, NTLM, Basic realm="x"'}))
        self.assertEqual(client.auth_scheme, 'NTLM')
        self.assertFalse(session.trust_env)
        self.assertFalse(session.get_kwargs['allow_redirects'])

    def test_basic_fallback(self):
        client, _ = self.negotiate(FakeResponse(401, headers={'WWW-Authenticate': 'Basic realm="x"'}))
        self.assertEqual(client.auth_scheme, 'Basic')

    def test_redirect_or_unknown_refused(self):
        with self.assertRaises(EwsError):
            self.negotiate(FakeResponse(302, headers={'Location': 'https://evil'}))
        with self.assertRaises(EwsError):
            self.negotiate(FakeResponse(401, headers={'WWW-Authenticate': 'Bearer'}))

    def test_missing_credentials(self):
        client = EwsClient('', '')
        with self.assertRaises(EwsError):
            client._ensure_session()


class ParsingTests(unittest.TestCase):
    def test_sync_items(self):
        client, session = client_with(response('SyncFolderItems', (
            '<m:SyncState>STATE2</m:SyncState><m:IncludesLastItemInRange>false</m:IncludesLastItemInRange>'
            '<m:Changes><t:Create><t:Message><t:ItemId Id="A1" ChangeKey="c"/>'
            '<t:DateTimeReceived>2026-10-29T08:00:00Z</t:DateTimeReceived></t:Message></t:Create>'
            '<t:Delete><t:ItemId Id="A0"/></t:Delete>'
            '<t:ReadFlagChange><t:ItemId Id="A2"/><t:IsRead>true</t:IsRead></t:ReadFlagChange>'
            '</m:Changes>')))
        changes, state, last = client.sync_items('inbox', 'STATE1')
        self.assertEqual(state, 'STATE2')
        self.assertFalse(last)
        self.assertEqual([c['type'] for c in changes], ['create', 'delete'])
        self.assertEqual(changes[0]['received_at'].isoformat(), '2026-10-29T08:00:00+00:00')
        sent = session.sent[0][1]
        self.assertIn('<m:SyncState>STATE1</m:SyncState>', sent)
        self.assertIn('<t:DistinguishedFolderId Id="inbox"/>', sent)
        ET.fromstring(sent)

    def test_get_items_fields_and_missing(self):
        item = (
            '<m:Items><t:Message><t:ItemId Id="A1" ChangeKey="CK"/><t:ParentFolderId Id="F1"/>'
            '<t:Subject>Devis &amp; planning</t:Subject><t:Body BodyType="Text">Bonjour</t:Body>'
            '<t:Attachments><t:FileAttachment><t:Name>devis.pdf</t:Name><t:IsInline>false</t:IsInline></t:FileAttachment>'
            '<t:FileAttachment><t:Name>logo.png</t:Name><t:IsInline>true</t:IsInline></t:FileAttachment></t:Attachments>'
            '<t:DateTimeReceived>2026-10-29T08:00:00Z</t:DateTimeReceived>'
            '<t:InternetMessageHeaders><t:InternetMessageHeader HeaderName="List-Unsubscribe">x</t:InternetMessageHeader>'
            '<t:InternetMessageHeader HeaderName="Received">ignore</t:InternetMessageHeader></t:InternetMessageHeaders>'
            '<t:ConversationId Id="CV"/>'
            '<t:From><t:Mailbox><t:Name>Client</t:Name><t:EmailAddress>Client@Exemple.fr</t:EmailAddress>'
            '<t:RoutingType>SMTP</t:RoutingType></t:Mailbox></t:From>'
            '<t:ToRecipients><t:Mailbox><t:Name>Interne</t:Name><t:EmailAddress>/O=EX/CN=X</t:EmailAddress>'
            '<t:RoutingType>EX</t:RoutingType></t:Mailbox></t:ToRecipients>'
            '<t:InternetMessageId>&lt;m1@x&gt;</t:InternetMessageId></t:Message></m:Items>')
        body = soap(f'<m:GetItemResponse xmlns:m="{M}" xmlns:t="{T}"><m:ResponseMessages>'
                    f'<m:GetItemResponseMessage ResponseClass="Success"><m:ResponseCode>NoError</m:ResponseCode>{item}'
                    '</m:GetItemResponseMessage><m:GetItemResponseMessage ResponseClass="Error">'
                    '<m:ResponseCode>ErrorItemNotFound</m:ResponseCode></m:GetItemResponseMessage>'
                    '</m:ResponseMessages></m:GetItemResponse>')
        client, session = client_with(body)
        out = client.get_items(['A1', 'A9'], with_body=True)
        a1 = out['A1']
        self.assertIsNone(out['A9'])
        self.assertEqual(a1['subject'], 'Devis & planning')
        self.assertEqual(a1['from'], {'name': 'Client', 'email': 'client@exemple.fr'})
        self.assertEqual(a1['to'], [{'name': 'Interne', 'email': ''}])  # adresse X500 non exploitee
        self.assertEqual(a1['attachments'], ['devis.pdf'])
        self.assertEqual(a1['headers'], {'list-unsubscribe': 'x'})
        self.assertEqual(a1['internet_message_id'], '<m1@x>')
        self.assertEqual((a1['conversation_id'], a1['folder_id'], a1['change_key']), ('CV', 'F1', 'CK'))
        self.assertIn('<t:BodyType>Text</t:BodyType>', session.sent[0][1])

    def test_ids_are_escaped(self):
        client, session = client_with(response('GetItem', '<m:Items/>'))
        client.get_items(['a"/><t:X Id="b'])
        root = ET.fromstring(session.sent[0][1])
        ids = root.findall(f'.//{{{M}}}ItemIds/{{{T}}}ItemId')
        self.assertEqual([i.get('Id') for i in ids], ['a"/><t:X Id="b'])

    def test_reply_draft_xml(self):
        client, session = client_with(response('CreateItem', '<m:Items><t:Message><t:ItemId Id="D1" ChangeKey="K"/></t:Message></m:Items>'))
        draft_id, ck = client.create_reply_draft('A1', 'CK', 'Bonjour <b>&\nMerci', reply_all=True)
        self.assertEqual((draft_id, ck), ('D1', 'K'))
        sent = session.sent[0][1]
        root = ET.fromstring(sent)
        create = root.find(f'.//{{{M}}}CreateItem')
        self.assertEqual(create.get('MessageDisposition'), 'SaveOnly')
        reply = root.find(f'.//{{{T}}}ReplyAllToItem')
        self.assertEqual(reply.find(f'{{{T}}}ReferenceItemId').get('ChangeKey'), 'CK')
        html = reply.find(f'{{{T}}}NewBodyContent').text
        self.assertIn('Bonjour &lt;b&gt;&amp;<br>Merci', html)  # le texte reste du texte

    def test_correlation_update_xml(self):
        client, session = client_with(response('UpdateItem', '<m:Items><t:Message><t:ItemId Id="D1" ChangeKey="K2"/></t:Message></m:Items>'))
        self.assertEqual(client.set_correlation('D1', 'K', 'abc:1'), 'K2')
        root = ET.fromstring(session.sent[0][1])
        self.assertEqual(root.find(f'.//{{{M}}}UpdateItem').get('MessageDisposition'), 'SaveOnly')
        self.assertEqual(root.find(f'.//{{{T}}}ExtendedProperty/{{{T}}}Value').text, 'abc:1')

    def test_move_returns_new_id(self):
        client, session = client_with(response('MoveItem', '<m:Items><t:Message><t:ItemId Id="NEW"/></t:Message></m:Items>'))
        self.assertEqual(client.move_item('OLD', 'F2'), 'NEW')
        self.assertIn('<m:ReturnNewItemIds>true</m:ReturnNewItemIds>', session.sent[0][1])

    def test_trash_draft_is_recoverable(self):
        client, session = client_with(response('DeleteItem', ''))
        client.trash_draft('D1')
        self.assertIn('DeleteType="MoveToDeletedItems"', session.sent[0][1])

    def test_list_folders_paths_and_paging(self):
        page1 = response('FindFolder', (
            '<m:RootFolder IndexedPagingOffset="2" TotalItemsInView="3" IncludesLastItemInRange="false"><t:Folders>'
            '<t:Folder><t:FolderId Id="F1"/><t:ParentFolderId Id="R"/><t:FolderClass>IPF.Note</t:FolderClass>'
            '<t:DisplayName>Chantiers</t:DisplayName><t:TotalCount>4</t:TotalCount></t:Folder>'
            '<t:CalendarFolder><t:FolderId Id="CAL"/><t:DisplayName>Calendrier</t:DisplayName></t:CalendarFolder>'
            '</t:Folders></m:RootFolder>'))
        page2 = response('FindFolder', (
            '<m:RootFolder IndexedPagingOffset="3" TotalItemsInView="3" IncludesLastItemInRange="true"><t:Folders>'
            '<t:Folder><t:FolderId Id="F2"/><t:ParentFolderId Id="F1"/><t:DisplayName>Dupont</t:DisplayName></t:Folder>'
            '</t:Folders></m:RootFolder>'))
        client, session = client_with(page1, page2)
        folders = client.list_folders()
        self.assertEqual([f['id'] for f in folders], ['F1', 'F2'])
        self.assertIn('Offset="2"', session.sent[1][1])

    def test_errors_are_sanitized(self):
        client, _ = client_with(response('GetFolder', '', code='ErrorAccessDenied'))
        with self.assertRaises(EwsError) as ctx:
            client.check(client.post('GetFolder', '<m:GetFolder/>')[0])
        self.assertEqual(ctx.exception.code, 'ErrorAccessDenied')
        fault = soap('<s:Fault><faultcode>a:ErrorSchemaValidation</faultcode><faultstring>secret body</faultstring>'
                     '<detail><e:ResponseCode xmlns:e="http://schemas.microsoft.com/exchange/services/2006/errors">'
                     'ErrorSchemaValidation</e:ResponseCode></detail></s:Fault>')
        session = FakeSession([FakeResponse(500, fault)])
        client = EwsClient('u', 'p', session=session)
        with self.assertRaises(EwsError) as ctx:
            client.post('GetFolder', '<m:GetFolder/>')
        self.assertEqual(ctx.exception.code, 'ErrorSchemaValidation')
        self.assertNotIn('secret', str(ctx.exception))
        busy = soap('<s:Fault><detail><e:ResponseCode xmlns:e="x">ErrorServerBusy</e:ResponseCode></detail></s:Fault>')
        client = EwsClient('u', 'p', session=FakeSession([FakeResponse(500, busy)]))
        with self.assertRaises(ews.EwsBusy):
            client.post('GetFolder', '<m:GetFolder/>')

    def test_xml_bombs_rejected(self):
        bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;">]><x>&b;</x>'
        client = EwsClient('u', 'p', session=FakeSession([FakeResponse(200, bomb)]))
        with self.assertRaises(EwsError):
            client.post('GetFolder', '<m:GetFolder/>')

    def test_redirect_response_refused(self):
        client = EwsClient('u', 'p', session=FakeSession([FakeResponse(302, b'')]))
        with self.assertRaises(EwsError):
            client.post('GetFolder', '<m:GetFolder/>')


if __name__ == '__main__':
    unittest.main()
