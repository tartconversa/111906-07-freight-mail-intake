"""真实IMAP客户端、MIME字节、世代变化和交接回执。"""
from support import *
import imaplib
from email.parser import BytesParser
from email.policy import default
class Protocol(SourceCase):
    def test_imap_uidvalidity_move_and_mime(self):
        mail=imaplib.IMAP4('127.0.0.1',self.app.mail_port);self.addCleanup(mail.logout);mail.login('collector','local-demo');self.assertEqual(mail.select('INBOX',readonly=True)[0],'OK');self.assertEqual(mail.response('UIDVALIDITY')[1],[b'100']);self.assertEqual(mail.uid('search',None,'ALL')[1],[b'101 102 103'])
        status,parts=mail.uid('fetch','101','(BODY.PEEK[])');raw=next(p[1] for p in parts if isinstance(p,tuple));msg=BytesParser(policy=default).parsebytes(raw);payload=json.loads(next(msg.iter_attachments()).get_payload(decode=True));self.assertEqual(payload['shipment'],'F-701')
        self.control(1);mail.select('INBOX',readonly=True);self.assertEqual(mail.response('UIDVALIDITY')[1],[b'200']);self.assertIn(b'502',mail.uid('search',None,'ALL')[1][0]);mail.select('Archive',readonly=True);status,parts=mail.uid('fetch','601','(BODY.PEEK[])');moved=next(p[1] for p in parts if isinstance(p,tuple));self.assertEqual(raw,moved)
        self.request('/handoff',{'reference':'source-check','carrier':'C1','shipment':'F-701','boxes':['BX1'],'evidence':[],'lose_response':True});self.assertEqual(self.value('/handoff')['source-check']['boxes'][0]['state'],'accepted')
