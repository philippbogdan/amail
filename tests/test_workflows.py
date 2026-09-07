import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch
import test_mail as fixtures
from local_store import MailError, message_text
from mail_sender import ledger
from policy import PROFILES, WaitRequired, eligibility, reserve
from workflows import batch_plan, batch_run, batch_show, batch_control, draft_create, draft_get, arguments
from gmail_backend import build_mime
import email
import email.policy
import hashlib


class WorkflowTests(unittest.TestCase):
    setUp = fixtures.Fixture.setUp
    write_message = fixtures.Fixture.write_message
    row = fixtures.Fixture.row
    args = fixtures.Fixture.args
    def manifest(self, count=2, **extra):
        path=self.base/'campaign.jsonl'
        items=[dict(id=f'item-{i}', **{'from':'owner@example.com'}, to=[f'person{i}@example.net'],
                    subject=f'Subject {i}',body=f'Body {i}',**extra) for i in range(count)]
        path.write_text('\n'.join(json.dumps(i) for i in items))
        return path

    def test_approved_exchange_rate_windows(self):
        rules=dict(PROFILES['exchange'],recipients_per_day=400,messages_per_window=50,window_seconds=600,gap_min_seconds=5,gap_max_seconds=10)
        self.assertEqual(rules['recipients_per_day'],400)
        self.assertEqual((rules['gap_min_seconds'],rules['gap_max_seconds']),(5,10))
        until,reason=eligibility([(i*5,1) for i in range(50)],now=250,recipients=1,rules=rules)
        self.assertEqual(until,600)
        self.assertEqual(reason,'rolling message-rate cap')
        until,_=eligibility([(0,400)],now=1000,recipients=1,rules=rules)
        self.assertEqual(until,86400)
        self.assertEqual(eligibility([(0,400)],now=86400,recipients=1,rules=rules)[0],86400)

    def test_recipient_count_requires_enough_budget_to_expire(self):
        rules=dict(PROFILES['exchange'],recipients_per_day=4)
        until,_=eligibility([(100,1),(200,3)],now=1000,recipients=2,rules=rules)
        self.assertEqual(until,86600)

    def test_pacing_is_shared_between_connections(self):
        state=self.base/'state';account=self.store.resolve('owner@example.com')[0]
        with ledger(state) as c,c:
            reserve(c,account,1,['person@example.net'],'outreach',now=1000)
        with self.assertRaises(WaitRequired):
            with ledger(state) as c,c:
                reserve(c,account,1,['different@example.net'],'outreach',now=1001)

    def test_personal_reply_is_not_held_by_outreach_gap(self):
        state=self.base/'state';account=self.store.resolve('owner@example.com')[0]
        with ledger(state) as c,c:
            reserve(c,account,1,['person@example.net'],'outreach',now=1000)
            reserve(c,account,1,['reply@example.net'],'personal',now=1001)

    def test_draft_snapshots_body_and_attachment(self):
        body=self.base/'body.txt';body.write_text('Original body')
        attachment=self.base/'file.bin';attachment.write_bytes(b'original')
        message={'from':'owner@example.com','to':['person@example.net'],'subject':'S','body_file':'body.txt','attachments':['file.bin']}
        draft=draft_create(self.store,self.base/'state',message,self.base)
        body.write_text('Changed');attachment.write_bytes(b'changed')
        saved=draft_get(self.base/'state',draft['id'])
        self.assertEqual(saved['message']['body'],'Original body')
        self.assertEqual(Path(saved['message']['attachments'][0]['path']).read_bytes(),b'original')

    def test_draft_detects_tampered_snapshot_attachment(self):
        attachment=self.base/'file.bin';attachment.write_bytes(b'original')
        message={'from':'owner@example.com','to':['person@example.net'],'subject':'S','body':'B','attachments':['file.bin']}
        draft=draft_create(self.store,self.base/'state',message,self.base)
        Path(draft['message']['attachments'][0]['path']).write_bytes(b'tampered')
        with self.assertRaises(MailError):arguments(draft['message'],request_id='test')

    def test_batch_plan_is_repeatable_and_reviewable(self):
        file=self.manifest();state=self.base/'state'
        first=batch_plan(self.store,state,file);second=batch_plan(self.store,state,file)
        self.assertEqual(first['id'],second['id'])
        self.assertEqual(first['messages'][0]['body'],'Body 0')
        self.assertEqual(first['counts'],{'pending':2})

    def test_batch_never_resends_accepted_items(self):
        state=self.base/'state';plan=batch_plan(self.store,state,self.manifest());calls=[]
        def sender(store,args,state,here):calls.append(args.request_id);return {'state':'accepted','request_id':args.request_id}
        result=batch_run(self.store,state,self.base,plan['id'],sender=sender)
        batch_run(self.store,state,self.base,plan['id'],resume=True,sender=sender)
        self.assertEqual(result['state'],'completed');self.assertEqual(len(calls),2)

    def test_ambiguous_result_pauses_before_next_recipient(self):
        state=self.base/'state';plan=batch_plan(self.store,state,self.manifest());calls=[]
        def sender(store,args,state,here):calls.append(args.request_id);return {'state':'outcome_unknown'}
        result=batch_run(self.store,state,self.base,plan['id'],sender=sender)
        self.assertEqual(result['state'],'paused');self.assertEqual(len(calls),1)

    def test_cancel_preserves_already_accepted_items(self):
        state=self.base/'state';plan=batch_plan(self.store,state,self.manifest());calls=[]
        def sender(store,args,state,here):
            calls.append(args.request_id);batch_control(state,plan['id'],'cancel');return {'state':'accepted'}
        result=batch_run(self.store,state,self.base,plan['id'],sender=sender)
        self.assertEqual(result['state'],'cancelled');self.assertEqual(result['counts'],{'accepted':1,'cancelled':1})

    def test_suppressed_recipient_is_not_submitted(self):
        state=self.base/'state'
        with ledger(state) as c,c:c.execute('insert into suppressions values(?,?,?)',('person0@example.net','opt-out',time.time()))
        plan=batch_plan(self.store,state,self.manifest());calls=[]
        def sender(store,args,state,here):calls.append(args.to);return {'state':'accepted'}
        result=batch_run(self.store,state,self.base,plan['id'],sender=sender)
        self.assertEqual(calls,[['person1@example.net']]);self.assertEqual(result['counts'],{'accepted':1,'suppressed':1})

    def test_mime_preserves_binary_and_unicode(self):
        attachment=self.base/'raw.bin';data=b'\x00\xff\r\n\x01';attachment.write_bytes(data)
        request={'formatted_sender':'Owner <owner@example.com>','sender':'owner@example.com','to':['person@example.net'],'cc':[],'bcc':[],
                 'subject':'café λ','body':'café λ\nLine two.','attach':[str(attachment)],'request_id':'exact-test'}
        message=email.message_from_bytes(build_mime(request),policy=email.policy.default)
        self.assertEqual(message_text(message),'café λ\nLine two.')
        self.assertEqual(list(message.iter_attachments())[0].get_payload(decode=True),data)
        self.assertEqual(str(message['X-Amail-Request-ID']),'exact-test')


if __name__=='__main__':unittest.main()
