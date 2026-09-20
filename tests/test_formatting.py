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
from local_store import body_format, message_text
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

    def test_clean_plain_and_html_alternatives_pass(self):
        text = 'Hi Alex,\n\nCafé & <angle>.\n\nSam'
        self.assertTrue(self.verifies(text, text,
            '<html><body>Hi Alex,<br><br>Café &amp; &lt;angle&gt;.<br><br>Sam</body></html>'))

    def test_empty_plain_draft_requires_matching_html_and_bcc(self):
        item, parts = self.put('', '<body>Body</body>')
        self.assertEqual(item['body'], 'Body')
        verification = prepare(self.store, self.args(body='Body'))[2]
        self.assertTrue(matches_content(self.store, item, parts, verification, draft=True))
        self.assertFalse(matches_content(self.store, item, parts, verification))
        item['bcc'] = 'unexpected@example.net'
        self.assertFalse(matches_content(self.store, item, parts, verification, draft=True))

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
