import argparse
import concurrent.futures
import email
import email.policy
import json
from pathlib import Path
import subprocess
import sys
import argparse
import unittest
from unittest.mock import patch
import test_mail as fixtures
from cli import parser, run
from feedback import classify
from gmail_backend import ProviderError
ACCEPTED={'sent_ref':'fixture:sent','message_id':'<sent@example.com>','effective_from':'Owner <owner@example.com>','evidence':{'type':'fixture'}}
SUBMITTED={'mail_send_result':True}
def after_prepare(failure):
    # Model native_transport: the verified draft is persisted before Mail is asked to send.
    def submit(store, request, verification, state, here, timeout, prepared=None, prepare_only=False):
        if prepared: prepared(dict(verification, draft_message_id='<prepared@example.com>'))
        raise failure
    return submit
from local_store import MailError
from mail_sender import ledger, send
from workflows import batch_plan, batch_run


class ContractTests(unittest.TestCase):
    setUp=fixtures.Fixture.setUp
    write_message=fixtures.Fixture.write_message
    row=fixtures.Fixture.row
    args=fixtures.Fixture.args

    def test_draft_retry_preserves_request_id_and_never_retries_unknown(self):
        from workflows import draft_create
        for uncertain in (False, True):
            with self.subTest(uncertain=uncertain):
                state = self.base / ('retry-' + str(uncertain))
                draft = draft_create(self.store, state, {'from': 'owner@example.com', 'to': ['person@example.net'],
                    'subject': 'Reply', 'body': 'Reviewed body'}, self.base)
                first_args = parser().parse_args(['draft', 'send', draft['id']])
                retry_args = parser().parse_args(['draft', 'send', draft['id'], '--retry-rejected'])
                with patch('native_transport.submit', side_effect=after_prepare(ProviderError('failed', uncertain=uncertain))):
                    first = run(first_args, self.store, state, self.base)
                with patch('native_transport.submit', return_value=SUBMITTED) as backend, patch('mail_sender.observed_acceptance', return_value=None if uncertain else ACCEPTED):
                    retry = run(retry_args, self.store, state, self.base)
                self.assertEqual(first['request_id'], retry['request_id'])
                self.assertEqual(backend.call_count, 0 if uncertain else 1)
                self.assertEqual(retry['state'], 'outcome_unknown' if uncertain else 'accepted')

    def test_a_compose_window_mail_would_not_close_is_reported_then_closed(self):
        from mail_sender import status
        state = self.base / 'left-open'
        window = {'outgoing_id': 7, 'mail_process': '10 fixture'}
        def refuse(store, request, verification, state, here, timeout, prepared=None, prepare_only=False):
            request.update(compose_window_left_open=True, compose_window=window)  # as native_transport records them
            raise ProviderError('The saved Mail draft did not match', uncertain=False)
        args = self.args(dry_run=False, request_id='left-open', timeout=5)  # the fixture's 0.1 s can run out before submission
        with patch('native_transport.submit', side_effect=refuse):
            first = send(self.store, args, state, self.base)
        self.assertEqual(first['state'], 'rejected')
        self.assertTrue(first['compose_window_left_open'])
        self.assertIn('amail status', first['compose_window_note'])
        with patch('native_transport.close_stale_window', return_value=None):  # Mail still does not answer
            self.assertTrue(status('left-open', state, self.store)['compose_window_left_open'])
        with patch('native_transport.close_stale_window', return_value=1) as close:
            closed = status('left-open', state, self.store)
        self.assertEqual(close.call_args[0][0]['compose_window'], window)  # the window it opened, not any with that subject
        self.assertNotIn('compose_window_left_open', closed)
        self.assertNotIn('compose_window', closed)
        self.assertEqual(closed['closed_stale_windows'], 1)
        with patch('native_transport.close_stale_window', side_effect=AssertionError('already closed')):
            self.assertEqual(status('left-open', state, self.store)['state'], 'rejected')

    def test_the_next_send_from_the_account_first_closes_a_window_an_earlier_attempt_left(self):
        for retry in (True, False):
            with self.subTest(retry_of_the_same_request=retry):
                state = self.base / ('left-open-' + str(retry))
                events = []
                def refuse(store, request, verification, state, here, timeout, prepared=None, prepare_only=False):
                    request.update(compose_window_left_open=True, compose_window={'outgoing_id': 7, 'mail_process': '10 fixture'})
                    raise ProviderError('The saved Mail draft did not match', uncertain=False)
                def accept(*args, **kwargs):
                    events.append('submit')
                    return SUBMITTED
                with patch('native_transport.submit', side_effect=refuse):
                    send(self.store, self.args(dry_run=False, request_id='left-open', timeout=5), state, self.base)
                following = self.args(dry_run=False, timeout=5, **({'request_id': 'left-open', 'retry_rejected': True} if retry else {'request_id': 'another'}))
                with patch('native_transport.close_stale_window', side_effect=lambda *a: events.append('close') or 1), \
                        patch('native_transport.submit', side_effect=accept), patch('mail_sender.observed_acceptance', return_value=ACCEPTED):
                    result = send(self.store, following, state, self.base)
                self.assertEqual(events, ['close', 'submit'])
                self.assertEqual(result['state'], 'accepted')
                self.assertNotIn('compose_window_left_open', result)

    def test_a_reconciliation_that_lost_a_race_leaves_the_newer_result_alone(self):
        from mail_sender import status
        state = self.base / 'race'
        with patch('native_transport.submit', return_value=SUBMITTED), patch('mail_sender.observed_acceptance', return_value=None):
            send(self.store, self.args(dry_run=False, request_id='slow', timeout=1), state, self.base)
        newer = {'request_id': 'slow', 'state': 'accepted', 'sent_ref': 'fixture:settled-elsewhere'}
        def meanwhile(store, verification):  # another process settles the request while this one is still asking Mail
            with ledger(state) as c, c:
                c.execute("update sends set state='accepted',result=? where request_id='slow'", (json.dumps(newer),))
            return None
        with patch('mail_sender.observed_acceptance', side_effect=meanwhile):
            seen = status('slow', state, self.store)
        self.assertEqual((seen['state'], seen['sent_ref']), ('accepted', 'fixture:settled-elsewhere'))
        with ledger(state) as c:
            self.assertEqual(json.loads(c.execute("select result from sends where request_id='slow'").fetchone()[0]), newer)

    def test_a_new_send_first_settles_what_earlier_sends_left_open(self):
        state = self.base / 'reconcile'
        with patch('native_transport.submit', return_value=SUBMITTED), patch('mail_sender.observed_acceptance', return_value=None):
            first = send(self.store, self.args(dry_run=False, request_id='slow-sync', timeout=1), state, self.base)
        self.assertEqual(first['state'], 'provider_acceptance_unverified')
        second = self.args(dry_run=False, request_id='next', subject='Next', timeout=5)
        with patch('native_transport.submit', side_effect=AssertionError('sent past an unknown outcome')), patch('mail_sender.observed_acceptance', return_value=None):
            with self.assertRaises(MailError):  # still unknown: the account stays blocked
                send(self.store, second, state, self.base)
        with patch('native_transport.submit', return_value=SUBMITTED) as backend, patch('mail_sender.observed_acceptance', return_value=ACCEPTED):
            result = send(self.store, second, state, self.base)  # the Sent copy has synced since
        self.assertEqual(result['state'], 'accepted')
        self.assertEqual(backend.call_count, 1)
        from mail_sender import status
        self.assertEqual(status('slow-sync', state, self.store)['state'], 'accepted')

    def test_timeout_is_bounded(self):
        for timeout in (0, 301):
            with self.assertRaises(MailError):
                send(self.store, self.args(timeout=timeout), self.base / 'state', self.base)
        self.assertEqual(send(self.store, self.args(timeout=300), self.base / 'state', self.base)['state'], 'dry_run')

    def test_recipient_filter_and_cursor(self):
        self.assertEqual(len(self.store.query(recipient='owner@example.com')),1)
        self.assertEqual(self.store.query(recipient='external.example'),[])
        row=self.row()
        self.assertEqual(self.store.query(cursor=self.store.cursor(row)),[])
        with self.assertRaises(MailError):self.store.query(cursor='not-json')

    def test_json_flags_at_each_level(self):
        for argv in [['--json','accounts'],['accounts','--json'],['batch','show','test','--json'],['draft','--json','list']]:
            self.assertTrue(parser().parse_args(argv).json)

    def test_draft_cli_does_not_send(self):
        file=self.base/'message.json';file.write_text(json.dumps({'from':'owner@example.com','to':['recipient@example.net'],'subject':'Hi','body':'Draft only'}))
        args=parser().parse_args(['draft','create',str(file)])
        with patch('subprocess.run',side_effect=AssertionError('outbound call')):
            result=run(args,self.store,self.base/'state',self.base)
        self.assertEqual(result['message']['body'],'Draft only')

    def test_concurrent_sends_share_pacing_and_atomic_ledger(self):
        state=self.base/'state'
        def one(i):
            try:
                result=send(self.store,self.args(dry_run=False,purpose='outreach',cap=None,request_id='parallel-'+str(i)),state,self.base)
                return result['state']
            except MailError:return 'held'
        with patch('native_transport.submit',return_value=SUBMITTED) as backend, patch('mail_sender.observed_acceptance',return_value=ACCEPTED):
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(one,range(16)))
        self.assertEqual(results.count('accepted'),1)
        self.assertEqual(backend.call_count,1)
        with ledger(state) as c:self.assertEqual(c.execute("select count(*) from sends where state='accepted'").fetchone()[0],1)

    def test_batch_resume_never_retries_an_ambiguous_provider_send(self):
        file=self.base/'batch.jsonl';file.write_text(json.dumps({'id':'one','from':'owner@example.com','to':['recipient@example.net'],'subject':'Hi','body':'Body'}))
        state=self.base/'state';plan=batch_plan(self.store,state,file)
        with patch('native_transport.submit',side_effect=ProviderError('connection lost',uncertain=True)) as backend:
            first=batch_run(self.store,state,self.base,plan['id'])
            second=batch_run(self.store,state,self.base,plan['id'],resume=True)
        self.assertEqual(first['state'],'paused');self.assertEqual(second['state'],'paused')
        self.assertEqual(backend.call_count,1)

    def test_explicit_optout_needs_a_known_outreach_recipient(self):
        message=email.message_from_string('From: recipient@example.net\nSubject: Unsubscribe\n\nPlease unsubscribe me.',policy=email.policy.default)
        requests=[('request','ACCOUNT',{'to':['recipient@example.net'],'cc':[]},{})]
        self.assertEqual(classify(message,requests)[0]['type'],'opt_out')
        self.assertEqual(classify(message,[]),[])
        normal=email.message_from_string('From: recipient@example.net\nSubject: A discussion\n\nHere is my answer.\nUnsubscribe',policy=email.policy.default)
        self.assertEqual(classify(normal,requests),[])

    def test_bounce_requires_original_message_correlation(self):
        raw='''From: mailer-daemon@example.net
Subject: Delivery failure
Content-Type: multipart/report; boundary="report"; report-type=delivery-status

--report
Content-Type: text/plain

Delivery failed.
--report
Content-Type: message/delivery-status

Reporting-MTA: dns; example.net

Final-Recipient: rfc822; recipient@example.net
Action: failed
Status: 5.1.1

--report
Content-Type: message/rfc822

From: owner@example.com
To: recipient@example.net
Message-ID: <original@example.com>
X-Amail-Request-ID: request
Subject: Original

Original body
--report--
'''
        message=email.message_from_string(raw,policy=email.policy.default)
        requests=[('request','ACCOUNT',{'to':['recipient@example.net'],'cc':[]},{'message_id':'<original@example.com>'})]
        result=classify(message,requests)
        self.assertEqual(result[0]['type'],'hard_bounce')
        unrelated=[('different','ACCOUNT',{'to':['recipient@example.net'],'cc':[]},{'message_id':'<unrelated@example.com>'})]
        self.assertEqual(classify(message,unrelated),[])

    def test_crashed_submitting_process_becomes_unknown_without_resending(self):
        args=self.args(dry_run=False,timeout=5,request_id='crash-test')
        args_file=self.base/'args.json';args_file.write_text(json.dumps(vars(args)))
        repo=Path(__file__).resolve().parents[1]
        script='''import sys,json,argparse,os
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from local_store import Store
from mail_sender import send
from unittest.mock import patch
s=Store(Path(sys.argv[2])/'V10',Path(sys.argv[2])/'Accounts.sqlite',enabled=['owner@example.com'])
a=argparse.Namespace(**json.loads(Path(sys.argv[3]).read_text()))
def die(store,request,verification,state,here,timeout,prepared=None,prepare_only=False):
 prepared(dict(verification,draft_message_id='<prepared@example.com>')); os._exit(73)
with patch('native_transport.submit',side_effect=die):
 send(s,a,Path(sys.argv[2])/'state',Path(sys.argv[1]))
'''
        from mail_sender import status
        # A process that died before any draft was prepared never asked Mail to send: safe to retry.
        early=self.args(dry_run=False,timeout=5,request_id='crash-early')
        early_file=self.base/'early.json';early_file.write_text(json.dumps(vars(early)))
        p=subprocess.run([sys.executable,'-c',script.replace("prepared(dict(verification,draft_message_id='<prepared@example.com>')); ",""),str(repo),str(self.base),str(early_file)],capture_output=True,timeout=10)
        self.assertEqual(p.returncode,73,p.stderr.decode())
        resolved=status('crash-early',self.base/'state',self.store)
        self.assertEqual(resolved['state'],'rejected');self.assertIn('never asked',resolved['error'])
        # A process that died after the draft was prepared may have sent: stays unknown and blocks resends.
        p=subprocess.run([sys.executable,'-c',script,str(repo),str(self.base),str(args_file)],capture_output=True,timeout=10)
        self.assertEqual(p.returncode,73,p.stderr.decode())
        self.assertEqual(status('crash-test',self.base/'state',self.store)['state'],'outcome_unknown')
        with patch('native_transport.submit',side_effect=AssertionError('duplicate send')):
            self.assertEqual(send(self.store,args,self.base/'state',repo)['state'],'outcome_unknown')

    def test_gmail_server_ref_is_account_scoped(self):
        from mail_operations import context
        row,account,args=context(self.store,'gmail:GMAIL:abc123')
        self.assertEqual(account['uuid'],'GMAIL');self.assertEqual(args['api_id'],'abc123')
        for value in ['gmail:GMAIL:../secrets','gmail:UNKNOWN:abc123']:
            with self.assertRaises(MailError):context(self.store,value)

    def test_native_source_keeps_full_mailbox_path(self):
        from mail_operations import context
        _,_,source=context(self.store,self.store.ref(self.row()))
        self.assertEqual(source['mailbox'],'[Gmail]/All Mail')

    def test_history_exposes_requested_sender_and_correlated_feedback(self):
        from mail_sender import status
        state=self.base/'state'
        with patch('native_transport.submit',return_value=SUBMITTED), patch('mail_sender.observed_acceptance',return_value=ACCEPTED):
            send(self.store,self.args(dry_run=False,request_id='history-check'),state,self.base)
        with ledger(state) as c,c:
            c.execute('create table feedback(request_id text,type text,recipient text,created real)')
            c.execute("insert into feedback values('history-check','hard_bounce','recipient@example.net',100)")
            c.execute("insert into feedback values('different','hard_bounce','other@example.net',100)")
        result=status('history-check',state,self.store)
        self.assertEqual(result['requested_sender'],'owner@example.com')
        self.assertEqual(result['effective_from'],'Owner <owner@example.com>')
        self.assertEqual([e['recipient'] for e in result['feedback']],['recipient@example.net'])

    def test_unchanged_logical_item_is_not_resent_in_a_new_plan(self):
        state=self.base/'state'
        with patch('native_transport.submit',return_value=SUBMITTED) as backend, patch('mail_sender.observed_acceptance',return_value=ACCEPTED):
            first=send(self.store,self.args(dry_run=False,request_id='plan-one',logical_item='recipient-1',purpose='outreach'),state,self.base)
            second=send(self.store,self.args(dry_run=False,request_id='plan-two',logical_item='recipient-1',purpose='outreach'),state,self.base)
        self.assertEqual(first['state'],'accepted');self.assertEqual(second['deduplicated_from'],'plan-one')
        self.assertEqual(backend.call_count,1)

    def test_waiting_gmail_does_not_block_ready_exchange_messages(self):
        self.store.accounts['EXCHANGE']={'uuid':'EXCHANGE','name':'University','addresses':['uni@example.edu'],'auth_address':'uni@example.edu','kind':'com.apple.account.Exchange'}
        items=[{'id':'google','from':'owner@example.com','to':['one@example.net'],'subject':'One','body':'One'},
               {'id':'uni','from':'uni@example.edu','to':['two@example.net'],'subject':'Two','body':'Two'}]
        file=self.base/'mixed.jsonl';file.write_text('\n'.join(json.dumps(x) for x in items))
        plan=batch_plan(self.store,self.base/'state',file);clock=[0];accepted=[]
        from policy import WaitRequired
        def sender(store,args,state,here):
            if args.sender=='owner@example.com' and clock[0]<3:raise WaitRequired(3,'test wait')
            accepted.append(args.sender);return {'state':'accepted'}
        result=batch_run(self.store,self.base/'state',self.base,plan['id'],sender=sender,clock=lambda:clock[0],sleeper=lambda duration:clock.__setitem__(0,clock[0]+duration))
        self.assertEqual(result['state'],'completed')
        self.assertEqual(accepted,['uni@example.edu','owner@example.com'])


if __name__=='__main__':unittest.main()
