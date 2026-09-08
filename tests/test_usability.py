"""Recorded agent mistakes exercised through the public parser and runtime."""
import json
import contextlib
import os
import subprocess
import sys
import unittest
from unittest.mock import patch
import test_mail as fixtures
from cli import parser, run, project
from workflows import batch_plan, batch_summary, input_schema
from mail_sender import ledger


class UsabilityTests(unittest.TestCase):
    setUp = fixtures.Fixture.setUp
    write_message = fixtures.Fixture.write_message
    row = fixtures.Fixture.row

    def execute(self, *argv):
        return run(parser().parse_args(argv), self.store, self.base / 'state', self.base)

    def test_empty_other_folder_check_exposes_inbox_scope(self):
        result = self.execute('list', '--after', '2025-01-01')
        self.assertEqual(result['messages'], [])
        self.assertEqual(result['scope']['mailbox'], 'inbox')
        self.assertFalse(result['has_more'])
        self.assertIsNone(result['next_cursor'])
        self.assertEqual(self.execute('list', '--all-mailboxes')['scope']['mailbox'], '*')
        self.assertIsInstance(self.execute('list', '--bare'), list)

    def test_field_only_search_has_no_dummy_positional_argument(self):
        result = self.execute('search', '--to', 'owner@example.com')
        self.assertEqual(len(result['messages']), 1)
        self.assertEqual(result['scope']['mailbox'], '*')

    def test_pagination_reveals_unseen_results(self):
        import sqlite3
        with contextlib.closing(sqlite3.connect(self.db)) as c, c:
            c.execute('insert into messages select 124,message_id,global_message_id,remote_id,mailbox,date_received+1,date_sent,read,flagged,subject_prefix,subject,sender,deleted,conversation_id from messages where ROWID=123')
        first = self.execute('list', '--all-mailboxes', '--limit', '1')
        self.assertTrue(first['has_more'])
        second = self.execute('list', '--all-mailboxes', '--limit', '1', '--cursor', first['next_cursor'])
        self.assertFalse(second['has_more'])
        self.assertNotEqual(first['messages'][0]['ref'], second['messages'][0]['ref'])

    def test_complete_thread_in_one_command_without_network(self):
        with patch('subprocess.run', side_effect=AssertionError('network or Mail call')):
            result = self.execute('thread', self.store.ref(self.row()), '--full')
        self.assertIn('Needleword', result[0]['body'])
        self.assertFalse(self.store.query()[0]['read'])

    def test_status_without_id_is_diagnostics(self):
        with patch('gmail_backend.Gmail.profile', return_value={'emailAddress': 'owner@example.com'}):
            result = self.execute('status')
        self.assertEqual(result[0]['send_route'], 'gmail_api')

    def test_schema_works_with_no_configuration_or_mail_access(self):
        from pathlib import Path
        repo = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, str(repo/'amail'), 'schema', 'batch'],
            env={**os.environ, 'AMAIL_CONFIG': str(self.base/'missing.json')}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        schema = json.loads(result.stdout)
        self.assertEqual(schema['example']['id'], 'colleague-001')
        self.assertIn('id', schema['schema']['required'])
        example = schema['example']; example['from'] = 'owner@example.com'
        manifest = self.base/'batch.jsonl'; manifest.write_text(json.dumps(example))
        batch_plan(self.store, self.base/'state', manifest)

    def test_compact_monitor_keeps_exceptions_and_reports_absent_worker(self):
        example = input_schema('batch')['example']; example['from'] = 'owner@example.com'
        manifest = self.base/'batch.jsonl'; manifest.write_text(json.dumps(example))
        state = self.base/'state'; plan = batch_plan(self.store, state, manifest)
        with ledger(state) as c, c:
            c.execute("update batches set state='running'")
            c.execute("update batch_items set state='outcome_unknown',result=?", (json.dumps({'error': 'connection lost', 'large_evidence': 'X'*100000}),))
        summary = self.execute('batch', 'status', plan['id'])
        self.assertFalse(summary['worker_running'])
        self.assertIn('absent', summary['next_step'])
        self.assertEqual(summary['exceptions'][0]['state'], 'outcome_unknown')
        self.assertLess(len(json.dumps(summary)), 1000)
        self.assertIn('items', self.execute('batch', 'status', plan['id'], '--details'))

    def test_fields_preserve_scope(self):
        result = project(self.execute('list'), 'ref,subject')
        self.assertEqual(set(result['messages'][0]), {'ref', 'subject'})
        self.assertEqual(result['scope']['mailbox'], 'inbox')

    def test_bulk_preview_freezes_exact_refs_and_apply_is_bounded(self):
        import sqlite3
        from bulk_mark import plan, apply
        state = self.base/'state'
        with patch('gmail_backend.Gmail.request', side_effect=AssertionError('preview must be inert')):
            preview = plan(self.store, state, 'owner@example.com', 'inbox', 'read')
        self.assertEqual(preview['selected'], 1)
        with contextlib.closing(sqlite3.connect(self.db)) as c, c:
            c.execute('insert into messages select 124,message_id,global_message_id,remote_id,mailbox,date_received+1,date_sent,read,flagged,subject_prefix,subject,sender,deleted,conversation_id from messages where ROWID=123')
            c.execute('insert into labels values(124,2)')
        with patch('bulk_mark.gmail_id', return_value='provider123') as lookup, patch('gmail_backend.Gmail.request', return_value={}) as request:
            result = apply(self.store, state, self.base, preview['id'])
            again = apply(self.store, state, self.base, preview['id'])
        self.assertEqual(result['confirmed'], 1)
        self.assertEqual(again['remaining'], 0)
        self.assertEqual(lookup.call_count, 1)
        self.assertEqual(request.call_args.kwargs['payload']['ids'], ['provider123'])
        self.assertEqual(request.call_count, 1)

    def test_bulk_ref_reuse_fails_closed(self):
        import sqlite3
        from bulk_mark import plan, apply
        state = self.base/'state'
        preview = plan(self.store, state, 'owner@example.com', 'inbox', 'read')
        with contextlib.closing(sqlite3.connect(self.db)) as c, c: c.execute('update messages set date_received=date_received+1')
        with patch('gmail_backend.Gmail.request', side_effect=AssertionError('changed identity cannot be marked')):
            result = apply(self.store, state, self.base, preview['id'])
        self.assertEqual(result['confirmed'], 0)
        self.assertEqual(len(result['exceptions']), 1)

    def test_bulk_requires_explicit_account_and_keeps_failed_results(self):
        from bulk_mark import plan, apply
        from local_store import MailError
        state = self.base/'state'
        with self.assertRaises(MailError): plan(self.store, state, '*', 'inbox', 'read')
        preview = plan(self.store, state, 'owner@example.com', 'inbox', 'read')
        with patch('bulk_mark.gmail_id', side_effect=MailError('not found')):
            result = apply(self.store, state, self.base, preview['id'])
        self.assertEqual(result['remaining'], 1)
        self.assertEqual(result['exceptions'][0]['error'], 'not found')

    def test_mail_bulk_bridge_checks_identity_and_only_marks_selected_messages(self):
        import shutil
        from pathlib import Path
        node = shutil.which('node')
        if not node: self.skipTest('Node is only needed for the inert JXA contract harness')
        source = Path(__file__).resolve().parents[1]/'mail_operations.jxa'
        harness = r'''
const fs=require('fs'), vm=require('vm'), assert=require('assert');
const requests=[{id:1,mailbox:'Inbox',subject:'One',message_id:'<one@example.org>'},
                {id:2,mailbox:'Inbox',subject:'Two',message_id:'<original@example.org>'}];
let marks=[];
function message(id,subject,mid) {
 let read=false;
 const obj={subject:()=>subject,messageId:()=>mid};
 Object.defineProperty(obj,'readStatus',{get:()=>()=>read,set:v=>{read=v;marks.push(id)}});
 return obj;
}
const messages={1:message(1,'One','one@example.org'),2:message(2,'Two','replaced@example.org'),3:message(3,'Later','later@example.org')};
const account={name:()=> 'Fixture',mailboxes:{whose:()=>()=>[box]}};
const box={name:()=> 'Inbox',container:()=>account,messages:{whose:({id})=>()=>messages[id]?[messages[id]]:[]}};
const q={account:'EXCHANGE',operation:'mark_many',target:'read',messages:requests};
const sandbox={ObjC:{import:()=>{},unwrap:v=>v},$:{NSString:{stringWithContentsOfFileEncodingError:()=>JSON.stringify(q)}},
 Application:()=>({accounts:{byId:()=>account}})};
vm.createContext(sandbox);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),sandbox);
const result=JSON.parse(sandbox.run(['fixture']));
assert.deepEqual(marks,[1]);
assert.equal(result.results[0].state,'confirmed');assert.equal(result.results[1].state,'failed');
q.messages=Array(21).fill(requests[0]);assert.throws(()=>sandbox.run(['fixture']),/bounded/);
assert.deepEqual(marks,[1]);
'''
        result = subprocess.run([node, '-e', harness, str(source)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_multiple_reads_keep_bodies_and_missing_ref_fails(self):
        from local_store import MailError
        ref = self.store.ref(self.row())
        results = self.execute('read', ref, ref)
        self.assertEqual(len(results), 2)
        self.assertIn('Needleword', results[1]['body'])
        with self.assertRaises(MailError): self.execute('read', ref, 'invalid')
