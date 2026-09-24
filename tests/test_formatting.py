"""Regressions for quote wrappers and paragraph fidelity, with synthetic mail."""
import email.message
import email.policy
from pathlib import Path
import sys
import contextlib
import json
import sqlite3
import subprocess
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_mail as fixtures
from local_store import body_format, message_text, MailError
from mail_sender import matches_content, prepare, send


class FormattingTests(unittest.TestCase):
    setUp = fixtures.Fixture.setUp
    write_message = fixtures.Fixture.write_message
    row = fixtures.Fixture.row
    args = fixtures.Fixture.args

    def put(self, plain, html=None):
        message = email.message.EmailMessage(policy=email.policy.SMTP)
        message['From'] = 'owner@example.com'
        message['To'] = 'owner@example.com'
        message['Subject'] = 'Test'
        message['Message-ID'] = '<formatting@example.com>'
        message.set_content(plain)
        if html is not None:
            message.add_alternative(html, subtype='html')
        raw = message.as_bytes()
        self.file.write_bytes(str(len(raw)).encode() + b'\n' + raw)
        return self.store.read(self.store.ref(self.row()))

    def verifies(self, requested, plain, html=None):
        item, parts = self.put(plain, html)
        verification = prepare(self.store, self.args(body=requested))[2]
        return matches_content(self.store, item, parts, verification)

    def test_scripted_share_quote_is_visible_and_rejected(self):
        requested = 'Hi Alex,\n\nA new message.\n\nSam'
        quoted = '\n> Hi Alex,\n> \n> A new message.\n> \n> Sam\n'
        html = ('<html><body><div class="Apple-Mail-URLShareUserContentTopClass"><br></div>'
                '<div class="Apple-Mail-URLShareWrapperClass"><blockquote type="cite">'
                'Hi Alex,<br><br>A new message.<br><br>Sam</blockquote></div></body></html>')
        item, parts = self.put(quoted, html)
        self.assertIn('> Hi Alex,', item['body'])
        self.assertTrue(item['body_format']['apple_share_wrapper'])
        self.assertTrue(item['body_format']['html_entire_body_quoted'])
        self.assertEqual(item['body_format']['plain_leading_blank_lines'], 1)
        self.assertTrue(item['warnings'])
        verification = prepare(self.store, self.args(body=requested))[2]
        self.assertFalse(matches_content(self.store, item, parts, verification))
        # Even a caller with an old, cleaned display string must fail validation.
        item['body'] = requested
        self.assertFalse(matches_content(self.store, item, parts, verification))

    def test_native_reply_allows_share_wrapper_only_in_quoted_history(self):
        requested = 'A new reply.\n\nSam'
        html = ('<body>A new reply.<br><br>Sam<br><br><blockquote type="cite">'
                '<div class="Apple-Mail-URLShareWrapperClass">Old message</div>'
                '</blockquote></body>')
        item, parts = self.put('', html)
        verification = prepare(self.store, self.args(body=requested))[2]
        self.assertFalse(item['body_format']['apple_share_wrapper'])
        self.assertTrue(matches_content(self.store, item, parts, verification, draft=True))
        wrapped_new = ('<body><blockquote><div class="Apple-Mail-URLShareWrapperClass">'
                       'A new reply.<br><br>Sam</div></blockquote></body>')
        item, parts = self.put('', wrapped_new)
        self.assertFalse(matches_content(self.store, item, parts, verification, draft=True))

    def test_clean_plain_and_html_alternatives_pass(self):
        text = 'Hi Alex,\n\nCafé & <angle>.\n\nSam'
        self.assertTrue(self.verifies(text, text,
            '<html><body>Hi Alex,<br><br>Café &amp; &lt;angle&gt;.<br><br>Sam</body></html>'))

    def test_empty_plain_draft_requires_matching_html_and_bcc(self):
        item, parts = self.put('', '<body>Body</body>')
        self.assertEqual(item['body'], 'Body')
        verification = prepare(self.store, self.args(body='Body'))[2]
        self.assertTrue(matches_content(self.store, item, parts, verification, draft=True))
        self.assertFalse(matches_content(self.store, item, parts, prepare(self.store, self.args(body='Other'))[2], draft=True))
        item['bcc'] = 'unexpected@example.net'
        self.assertFalse(matches_content(self.store, item, parts, verification, draft=True))

    def test_native_reply_keeps_mail_quoted_history_below_the_text(self):
        # Mail's own reply window: the entered text, then its attribution line and quote.
        requested = 'Hi Alex,\n\nThanks, that works.\n\nSam'
        plain = requested + '\n\n> On 20 Sep 2026, at 12:02, Alex <alex@example.net> wrote:\n> \n> Original paragraph.\n> \n'
        html = ('<html><body><div>Hi Alex,</div><div><br></div><div>Thanks, that works.</div><div><br></div><div>Sam</div>'
                '<div><br></div><div>On 20 Sep 2026, at 12:02, Alex &lt;alex@example.net&gt; wrote:</div><blockquote type="cite">'
                '<div>Original paragraph.</div></blockquote></body></html>')
        self.assertTrue(self.verifies(requested, plain, html))
        # The same history above the text, or text that only appears inside the quote, is not a reply amail wrote.
        self.assertFalse(self.verifies(requested, '> Original paragraph.\n\n' + requested))
        self.assertFalse(self.verifies(requested, '\n' + plain))
        self.assertFalse(self.verifies(requested, plain.replace('Thanks, that works.', 'Thanks, that fails.')))
        self.assertFalse(self.verifies('Hi Alex,\n\nThanks', plain))  # a partial paragraph is not a prefix match

    def test_signature_below_the_text_is_allowed(self):
        self.assertTrue(self.verifies('Body', 'Body\n\n--\nSam Example\n'))
        self.assertFalse(self.verifies('Body', 'Body extra words'))

    def test_forward_may_carry_the_original_attachments(self):
        item, parts = self.put('For context.\n\nBegin forwarded message:\n\nFrom: alex@example.net\n\nOriginal.', None)
        verification = prepare(self.store, self.args(body='For context.'))[2]
        parts = [{'available': True, '_path': None, '_payload': b'original bytes', 'type': 'application/octet-stream', 'name': 'original.bin'}]
        self.assertFalse(matches_content(self.store, item, parts, verification))
        verification['conversation'] = 'forward'
        self.assertTrue(matches_content(self.store, item, parts, verification))

    def test_failed_preparation_discards_the_compose_window(self):
        from native_transport import prepare_native
        operations = []
        def fake_script(request, operation, *args):
            operations.append(operation)
            return {'outgoing_id': 7, 'stage': 'composed'} if operation == 'compose' else {'stage': operation + 'ed'}
        with patch('native_transport.script', side_effect=fake_script), patch('native_editor.ensure_subject_available'), \
             patch('native_editor.enter_body', side_effect=MailError('Mail did not place the text at the top of the body')), \
             patch('native_transport.sweep_synced_drafts', return_value=0) as sweep:
            with self.assertRaises(MailError):
                prepare_native(self.store, {'subject': 'Test', 'body': 'Body', 'attach': [], 'account_id': 'GMAIL', 'sender': 'owner@example.com'}, {}, self.base, self.base, self.base / 'request.json', time.monotonic() + 5)
        self.assertEqual(operations, ['compose', 'discard'])
        sweep.assert_called_once()

    def test_sweep_deletes_only_server_synced_autosaved_drafts(self):
        from native_transport import sweep_synced_drafts
        import contextlib, sqlite3
        self.write_message(123, body='Body')
        with contextlib.closing(sqlite3.connect(self.db)) as c, c:
            c.execute("insert into mailboxes values(7,'imap://GMAIL/Drafts',1,0)")
            c.execute("update subjects set subject='Test'"); c.execute("update messages set remote_id=null where ROWID=123")
            c.execute('insert into labels values(123,7)')
        from local_store import Store
        store = Store(self.root, self.base / 'Accounts.sqlite', enabled=['owner@example.com'])
        request = {'subject': 'Test', 'account_id': 'GMAIL', 'sender': 'owner@example.com'}
        calls = []
        with patch('native_transport.script', side_effect=lambda req, op, *a: calls.append((op, req.get('sweep_ids'))) or {'stage': 'swept'}):
            self.assertEqual(sweep_synced_drafts(store, request, set(), self.base, self.base / 'r.json', wait=1.5), 0)
        self.assertEqual(calls, [])  # unsynced: never deleted
        with contextlib.closing(sqlite3.connect(self.db)) as c, c:
            c.execute("update messages set remote_id=555 where ROWID=123")
        with patch('native_transport.script', side_effect=lambda req, op, *a: calls.append((op, req.get('sweep_ids'))) or {'stage': 'swept'}):
            self.assertEqual(sweep_synced_drafts(store, request, set(), self.base, self.base / 'r.json', wait=5), 1)
        self.assertEqual(calls, [('sweep', [123])])
        self.assertEqual(sweep_synced_drafts(store, request, {123}, self.base, self.base / 'r.json', wait=1.5), 0)  # pre-existing drafts are untouched

    def test_editor_change_while_drafts_settle_stops_the_send(self):
        from native_transport import prepare_native
        from unittest.mock import MagicMock
        store = MagicMock()
        store.query.side_effect = lambda **kw: [] if store.query.call_count == 1 else [
            {'id': 5, 'subject': 'Test', 'address': 'owner@example.com', 'remote_id': 1}]
        store.read.return_value = ({'message_id': '<draft@example.com>', 'ref': 'fixture:draft'}, [])
        request = {'subject': 'Test', 'body': 'Body', 'attach': [], 'account_id': 'GMAIL', 'sender': 'owner@example.com'}
        for texts, settled in [(['Body', 'Body'], True), (['Body', 'Body '], None)]:
            with self.subTest(texts=texts):
                store.query.reset_mock()
                operations = []
                def fake_script(req, operation, *args):
                    operations.append(operation)
                    return {'outgoing_id': 7, 'stage': 'composed'} if operation == 'compose' else {'stage': operation + 'ed'}
                with patch('native_transport.script', side_effect=fake_script), patch('native_editor.ensure_subject_available'), \
                     patch('native_editor.enter_body'), patch('native_editor.editor_text', side_effect=texts), \
                     patch('mail_sender.matches_content', return_value=True), patch('mail_sender.canonical_address', side_effect=lambda s, a: a), \
                     patch('native_transport.settle_draft', return_value=True), patch('native_transport.sweep_synced_drafts', return_value=0):
                    if settled:
                        self.assertTrue(prepare_native(store, dict(request), {}, self.base, self.base, self.base / 'r.json', time.monotonic() + 30)['drafts_settled'])
                        self.assertEqual(operations, ['compose', 'prepare'])
                    else:
                        with self.assertRaises(MailError):  # a keystroke after verification is not what was reviewed
                            prepare_native(store, dict(request), {}, self.base, self.base, self.base / 'r.json', time.monotonic() + 30)
                        self.assertEqual(operations, ['compose', 'prepare', 'discard'])

    def test_send_waits_until_the_verified_draft_is_the_only_synced_copy(self):
        from native_transport import settle_draft
        import contextlib, sqlite3
        from local_store import Store
        with contextlib.closing(sqlite3.connect(self.db)) as c, c:
            c.execute("insert into mailboxes values(7,'imap://GMAIL/Drafts',2,0)")
            c.execute("update subjects set subject='Test'")
            c.execute("insert into messages values(200,501,601,null,7,1700000100,1700000100,1,0,'',1,2,0,1)")
            c.execute("insert into messages values(201,502,602,556,7,1700000200,1700000200,1,0,'',1,2,0,1)")
        store = Store(self.root, self.base / 'Accounts.sqlite', enabled=['owner@example.com'])
        request = {'subject': 'Test', 'account_id': 'GMAIL', 'sender': 'owner@example.com'}
        settle = lambda: settle_draft(store, request, {123}, 201, time.monotonic() + 1.5, stable=.3)
        self.assertIsNone(store.pending_actions('GMAIL', 'drafts'))  # no queue table: nothing to wait for
        self.assertFalse(settle())  # a superseded save re-added by a Drafts sync is still present
        with contextlib.closing(sqlite3.connect(self.db)) as c, c:
            c.execute('update messages set deleted=1 where ROWID=200')
            c.execute('create table local_message_actions(ROWID integer primary key,mailbox integer,source_mailbox integer,destination_mailbox integer,action_type integer,user_initiated integer)')
            c.execute('insert into local_message_actions values(9,7,7,null,5,0)')
            c.execute("insert into local_message_actions values(10,2,null,null,3,null)")
        self.assertEqual(store.pending_actions('GMAIL', 'drafts'), 1)
        self.assertEqual(store.pending_actions('GMAIL'), 2)
        self.assertFalse(settle())  # Mail still has a Drafts delete to replay
        with contextlib.closing(sqlite3.connect(self.db)) as c, c:
            c.execute('delete from local_message_actions where ROWID=9')
            c.execute('update messages set remote_id=null where ROWID=201')
        self.assertFalse(settle())  # the server does not know the verified draft yet
        with contextlib.closing(sqlite3.connect(self.db)) as c, c:
            c.execute('update messages set remote_id=556 where ROWID=201')
        self.assertTrue(settle())

    def test_empty_plain_draft_fallback_keeps_quote_semantics(self):
        item, _ = self.put('', '<blockquote>Body</blockquote>')
        self.assertEqual(item['body'], '> Body')
        self.assertTrue(item['body_format']['html_entire_body_quoted'])

    def test_matching_plain_does_not_hide_quoted_html(self):
        self.assertFalse(self.verifies('Body', 'Body', '<blockquote>Body</blockquote>'))
        self.assertFalse(self.verifies('Body', 'Body',
            '<html><head><title>Not visible</title></head><body><blockquote>Body</blockquote></body></html>'))

    def test_extra_leading_blank_line_is_rejected(self):
        self.assertFalse(self.verifies('Body', '\nBody'))
        self.assertFalse(self.verifies('Body', 'Body', '<body><br>Body</body>'))

    def test_html_paragraph_loss_is_rejected(self):
        self.assertFalse(self.verifies('First\n\nSecond', 'First\n\nSecond', '<body>First<br>Second</body>'))

    def test_hidden_preheader_cannot_disguise_whole_body_quotation(self):
        self.assertFalse(self.verifies('Body', 'Body',
            '<span style="display: none !important">preview</span><blockquote>Body</blockquote>'))

    def test_valueless_attributes_are_readable(self):
        item, parts = self.put('Body', '<table><tr><td style class>Body</td></tr></table>')
        self.assertFalse(item['body_format']['html_entire_body_quoted'])

    def test_html_source_indentation_does_not_add_rendered_lines(self):
        self.assertTrue(self.verifies('First\n\nSecond', 'First\n\nSecond',
            '<html>\n<head><title>Subject</title></head>\n<body>\nFirst<br>\n<br>\nSecond\n</body></html>'))

    def test_missing_paragraph_and_indentation_are_rejected(self):
        for changed in ['First paragraph. Second paragraph.', 'First paragraph.\nSecond paragraph.',
                        'First paragraph.\n\n  Second paragraph.']:
            with self.subTest(changed=changed):
                self.assertFalse(self.verifies('First paragraph.\n\nSecond paragraph.', changed))

    def test_transport_newlines_and_intentional_quotes_pass(self):
        text = '> Intentionally quoted\n\nMy answer.'
        self.assertTrue(self.verifies(text, text.replace('\n', '\r\n')))

    def test_attached_html_does_not_contaminate_body_structure(self):
        message = email.message.EmailMessage()
        message.set_content('Body')
        message.add_attachment('<blockquote>Attached quotation</blockquote>', subtype='html', filename='quote.html')
        self.assertFalse(body_format(message)['html_entire_body_quoted'])
        self.assertEqual(message_text(message), 'Body')

    def test_attached_email_does_not_contaminate_body_structure(self):
        nested = email.message.EmailMessage()
        nested.set_content('<blockquote>Attached quotation</blockquote>', subtype='html')
        message = email.message.EmailMessage()
        message.set_content('Body')
        message.add_attachment(nested, filename='original.eml')
        self.assertFalse(body_format(message)['html_entire_body_quoted'])

    def test_batch_stops_on_malformed_sent_copy_and_resume_does_not_resend(self):
        from workflows import batch_plan, batch_run
        self.store.accounts['GMAIL']['kind'] = 'com.apple.account.Exchange'
        manifest = self.base / 'batch.jsonl'
        manifest.write_text('\n'.join(json.dumps({
            'id': str(i), 'from': 'owner@example.com', 'to': ['owner@example.com' if i == 0 else 'second@example.net'],
            'subject': 'Test' if i == 0 else 'Second', 'body': 'Body',
        }) for i in range(2)))
        state = self.base / 'state'
        plan = batch_plan(self.store, state, manifest)
        submissions = []

        def submit(*args, **kwargs):
            submissions.append(args)
            self.put('Body', '<div class="Apple-Mail-URLShareWrapperClass"><blockquote>Body</blockquote></div>')
            with contextlib.closing(sqlite3.connect(self.db)) as c, c:
                now = int(time.time())
                c.execute('update messages set sender=2,date_received=?,date_sent=?,remote_id=?',
                          (now, now, 'synced-server-item-id'))
                c.execute("update subjects set subject='Test'")
                c.execute('insert or ignore into labels values(123,3)')
            return {'mail_send_result': True}

        def short_send(store, args, state, here):
            args.timeout = .05
            return send(store, args, state, here)

        with patch('mail_sender.process_start', return_value='test'), patch('native_transport.submit', side_effect=submit):
            first = batch_run(self.store, state, self.base, plan['id'], sender=short_send)
            again = batch_run(self.store, state, self.base, plan['id'], resume=True, sender=short_send)
        self.assertEqual(first['state'], 'paused')
        self.assertEqual(again['state'], 'paused')
        self.assertEqual(first['counts'], {'provider_acceptance_unverified': 1, 'pending': 1})
        self.assertEqual(len(submissions), 1)


if __name__ == '__main__':
    unittest.main()
