import argparse
import contextlib
import email.message
import hashlib
import json
from pathlib import Path
import plistlib
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from local_store import Store, MailError, parse_emlx
from body_index import BodyIndex
import mail_sender


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        root=self.base/'V10';(root/'MailData').mkdir(parents=True);(root/'GMAIL').mkdir()
        self.root=root
        accountdb=self.base/'Accounts.sqlite'
        with contextlib.closing(sqlite3.connect(accountdb)) as c, c:
            c.executescript('''create table ZACCOUNTTYPE(Z_PK integer,ZIDENTIFIER text);
            insert into ZACCOUNTTYPE values(1,'com.apple.account.Google'),(2,'com.apple.account.IMAP');
            create table ZACCOUNT(Z_PK integer,ZIDENTIFIER text,ZACCOUNTTYPE integer,ZPARENTACCOUNT integer,ZACCOUNTDESCRIPTION text,ZUSERNAME text);
            insert into ZACCOUNT values(1,'PARENT',1,null,'Fixture','owner@example.com'),(2,'GMAIL',2,1,null,null);
            create table ZACCOUNTPROPERTY(ZOWNER integer,ZKEY text,ZVALUE blob);''')
            c.execute('insert into ZACCOUNTPROPERTY values(?,?,?)',(2,'EmailAliases',plistlib.dumps([{'IsEnabled':True,'EmailAddresses':[{'IsEnabled':True,'EmailAddress':'alias@example.com'}]}])))
        self.db=root/'MailData/Envelope Index'
        with contextlib.closing(sqlite3.connect(self.db)) as c, c:
            c.executescript('''create table mailboxes(ROWID integer primary key,url text,total_count integer,unread_count integer);
            insert into mailboxes values(1,'imap://GMAIL/%5BGmail%5D/All%20Mail',1,1),(2,'imap://GMAIL/INBOX',1,1),(3,'imap://GMAIL/%5BGmail%5D/Sent%20Mail',0,0);
            create table subjects(ROWID integer primary key,subject text);
            insert into subjects values(1,'100% literal _ invoice');
            create table addresses(ROWID integer primary key,address text,comment text);
            insert into addresses values(1,'sender@external.example','Sender'),(2,'owner@example.com','Owner');
            create table messages(ROWID integer primary key,message_id integer,global_message_id integer,remote_id,mailbox integer,date_received integer,date_sent integer,read integer,flagged integer,subject_prefix text,subject integer,sender integer,deleted integer);
            insert into messages values(123,500,600,12,1,1700000000,1700000000,0,0,'',1,1,0);
            alter table messages add column conversation_id integer default 1;
            create table recipients(message integer,address integer,type integer);
            insert into recipients values(123,2,0);
            create table labels(message_id integer,mailbox_id integer,primary key(message_id,mailbox_id));
            insert into labels values(123,1),(123,2);
            create table server_messages(ROWID integer primary key,message integer,mailbox integer,deleted integer,remote_id integer);
            create table server_labels(server_message integer,label integer);''')
        self.store=Store(root,accountdb,enabled=['owner@example.com'])
        self.file=self.write_message(123,body='Needleword café Straße decoded text')

    def write_message(self,rid,body='Body',attachments=None,partial=False,missing_attachment=False):
        msg=email.message.EmailMessage()
        msg['From']='sender@external.example';msg['To']='owner@example.com';msg['Subject']='100% literal _ invoice';msg['Message-ID']=f'<fixture-{rid}@example.com>'
        msg.set_content(body,cte='base64')
        for name,payload in (attachments or []):
            msg.add_attachment(payload,maintype='application',subtype='octet-stream',filename=name)
        if missing_attachment:
            part=list(msg.iter_attachments())[0]
            part['X-Apple-Content-Length']='100'
            part.set_payload('')
        raw=msg.as_bytes()
        digits='/'.join(reversed(str(rid//1000))) if rid>=1000 else ''
        path=self.root/'GMAIL'/'[Gmail].mbox'/'All Mail.mbox'/'DATA'/'Data'/digits/'Messages'/f'{rid}{".partial" if partial else ""}.emlx'
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(str(len(raw)).encode()+b'\n'+raw+b'<?xml tail ignored?>')
        return path

    def row(self):
        return self.store.query(mailbox='inbox')[0]

    def args(self,**kw):
        fields=dict(sender='owner@example.com',to=['owner@example.com'],cc=[],bcc=[],subject='Test',body='Body',body_file=None,attach=[],name='Owner',timeout=.1,cap=100,dry_run=True,request_id='fixture')
        fields.update(kw)
        return argparse.Namespace(**fields)

    def test_labels_and_deduplication(self):
        self.assertEqual([r['id'] for r in self.store.query(mailbox='inbox')],[123])
        self.assertEqual([r['id'] for r in self.store.query(mailbox='*')],[123])

    def test_exact_filename_in_bracket_mailbox(self):
        self.assertEqual(self.store.path(self.row()),self.file)
        self.file.unlink();self.write_message(12345)
        self.assertIsNone(self.store.path(self.row()))

    def test_alias_and_reject_external_sender(self):
        self.assertEqual(self.store.resolve('alias@example.com')[0]['uuid'],'GMAIL')
        with self.assertRaises(MailError):self.store.resolve('sender@external.example')

    def test_ref_rejects_reused_id(self):
        ref=self.store.ref(self.row())
        with contextlib.closing(sqlite3.connect(self.db)) as c,c:c.execute('update messages set message_id=999')
        with self.assertRaises(MailError):self.store.locate(ref)

    def test_decoded_body_and_literal_search(self):
        item,_=self.store.read(self.store.ref(self.row()))
        self.assertIn('café Straße',item['body'])
        self.assertTrue(item['body_available'])
        self.assertEqual(len(self.store.query(query='100%')),1)
        self.assertEqual(len(self.store.query(query='100_')),0)

    def test_sqlite_readonly(self):
        with self.assertRaises(MailError):
            with self.store.connect() as c:c.execute('delete from messages')

    def test_missing_and_malformed_body(self):
        ref=self.store.ref(self.row());self.file.unlink()
        with self.assertRaises(MailError):self.store.read(ref)
        self.file.write_bytes(b'broken')
        with self.assertRaises(MailError):parse_emlx(self.file)
        self.file.write_bytes(b'100\nshort')
        with self.assertRaises(MailError):parse_emlx(self.file)

    def test_duplicate_attachment_names_never_overwrite(self):
        self.write_message(123,attachments=[('../same.bin',b'one'),('same.bin',b'two')])
        saved=self.store.extract(self.store.ref(self.row()),self.base/'out')
        self.assertEqual({Path(p['file']).read_bytes() for p in saved},{b'one',b'two'})
        self.assertEqual(len({p['file'] for p in saved}),2)
        self.assertTrue(all(Path(p['file']).parent==(self.base/'out').resolve() for p in saved))

    def test_missing_partial_attachment_is_not_saved_empty(self):
        self.file.unlink()
        self.write_message(123,attachments=[('file.bin',b'payload')],partial=True,missing_attachment=True)
        item,_=self.store.read(self.store.ref(self.row()))
        self.assertFalse(item['attachments_complete'])
        with self.assertRaises(MailError):self.store.extract(item['ref'],self.base/'out')
        self.assertFalse((self.base/'out').exists())

    def test_separate_attachment(self):
        self.file.unlink()
        p=self.write_message(123,attachments=[('file.bin',b'payload')],partial=True,missing_attachment=True)
        side=p.parent.parent/'Attachments'/'123'/'2'/'file.bin';side.parent.mkdir(parents=True);side.write_bytes(b'separate payload')
        item,_=self.store.read(self.store.ref(self.row()))
        self.assertTrue(item['attachments_complete'])
        saved=self.store.extract(item['ref'],self.base/'out')
        self.assertEqual(Path(saved[0]['file']).read_bytes(),b'separate payload')

    def test_attached_email_preserves_message_and_binary_payload(self):
        inner=email.message.EmailMessage()
        inner['Subject']='Attached café'
        inner['Message-ID']='<attached@example.com>'
        inner.set_content('Nested message body')
        inner.add_attachment(b'\x00\xffbinary',maintype='application',subtype='octet-stream',filename='data.bin')
        outer=email.message.EmailMessage();outer.set_content('Outer body')
        outer.add_attachment(inner,filename='original.eml')
        raw=outer.as_bytes();self.file.write_bytes(str(len(raw)).encode()+b'\n'+raw)
        item,_=self.store.read(self.store.ref(self.row()))
        self.assertTrue(item['attachments_complete'])
        self.assertTrue(item['attachments'][0]['reconstructed_message'])
        saved=self.store.extract(item['ref'],self.base/'out')
        parsed=email.message_from_bytes(Path(saved[0]['file']).read_bytes(),policy=email.policy.default)
        self.assertEqual(parsed['Message-ID'],'<attached@example.com>')
        self.assertEqual(str(parsed['Subject']),'Attached café')
        self.assertEqual(list(parsed.iter_attachments())[0].get_payload(decode=True),b'\x00\xffbinary')

    def test_index_decoding_unicode_and_incremental_update(self):
        idx=BodyIndex(self.base/'index.sqlite');first=idx.refresh(self.store)
        self.assertEqual(first['errors'],0)
        for term in ['needleword','café','STRASSE','decoded text']:
            self.assertEqual(idx.search(term)[0],[123])
        self.assertEqual(idx.refresh(self.store)['unchanged'],1)
        self.write_message(123,body='replacement')
        idx.refresh(self.store)
        self.assertEqual(idx.search('needleword')[0],[])
        self.assertEqual(idx.search('replacement')[0],[123])

    def test_dry_run_never_sends(self):
        with patch.object(mail_sender.subprocess,'run',side_effect=AssertionError('attempted send')):
            result=mail_sender.send(self.store,self.args(),self.base/'state',self.base)
        self.assertEqual(result['state'],'dry_run')
        self.assertFalse((self.base/'state').exists())

    def test_timeout_does_not_resend_same_request(self):
        self.store.accounts["GMAIL"]["kind"]="com.apple.account.Exchange"
        with patch.object(mail_sender,'process_start',return_value='fixture process'), patch.object(mail_sender.subprocess,'run',side_effect=subprocess.TimeoutExpired('osascript',.1)) as runner:
            result=mail_sender.send(self.store,self.args(dry_run=False),self.base/'state',self.base)
            again=mail_sender.send(self.store,self.args(dry_run=False),self.base/'state',self.base)
        self.assertEqual(result['state'],'outcome_unknown')
        self.assertEqual(again['state'],'outcome_unknown')
        self.assertEqual(runner.call_count,1)

    def test_idempotency_rejects_changed_content(self):
        self.store.accounts["GMAIL"]["kind"]="com.apple.account.Exchange"
        with patch.object(mail_sender.subprocess,'run',side_effect=subprocess.TimeoutExpired('osascript',.1)):
            mail_sender.send(self.store,self.args(dry_run=False),self.base/'state',self.base)
        with self.assertRaises(MailError):
            mail_sender.send(self.store,self.args(dry_run=False,body='different'),self.base/'state',self.base)

    def test_cap_counts_cc_and_bcc(self):
        with patch.object(mail_sender.subprocess,'run',side_effect=AssertionError('attempted send')):
            with self.assertRaises(MailError):
                mail_sender.send(self.store,self.args(dry_run=False,cc=['cc@example.com'],bcc=['bcc@example.com'],cap=2),self.base/'state',self.base)

    def test_mail_boolean_alone_is_not_acceptance(self):
        self.store.accounts['GMAIL']['kind']='com.apple.account.Exchange'
        reply=subprocess.CompletedProcess([],0,json.dumps({'mail_send_result':True}), '')
        with patch.object(mail_sender.subprocess,'run',return_value=reply):
            result=mail_sender.send(self.store,self.args(dry_run=False),self.base/'state',self.base)
        self.assertEqual(result['state'],'provider_acceptance_unverified')

    def test_content_verifier_with_no_cc(self):
        self.write_message(123,body='Body')
        _,_,verification,_=mail_sender.prepare(self.store,self.args())
        item,parts=self.store.read(self.store.ref(self.row()))
        self.assertTrue(mail_sender.matches_content(self.store,item,parts,verification))

    def test_server_sent_membership_is_required(self):
        self.write_message(123,body='Body')
        now=int(time.time())
        with contextlib.closing(sqlite3.connect(self.db)) as c,c:
            c.execute('update messages set sender=2,date_received=?,date_sent=?',(now,now))
            c.execute("update subjects set subject='Test'")
            c.execute('insert into labels values(123,3)')
        _,_,v,_=mail_sender.prepare(self.store,self.args())
        v['before_ids']=[]
        self.assertIsNone(mail_sender.observed_acceptance(self.store,v))
        with contextlib.closing(sqlite3.connect(self.db)) as c,c:
            c.execute('insert into server_messages values(1,123,1,0,12)')
            c.execute('insert into server_labels values(1,3)')
        self.assertEqual(mail_sender.observed_acceptance(self.store,v)['evidence']['type'],'synced_imap_sent_membership')
        v['before_ids']=[123]
        self.assertIsNone(mail_sender.observed_acceptance(self.store,v))

    def test_rejects_header_injection(self):
        for args in [self.args(subject='Hello\nBcc: attacker@example.com'),self.args(to=['a@example.com\n'])]:
            with self.assertRaises(MailError):mail_sender.prepare(self.store,args)


if __name__=='__main__':unittest.main()
