"""Native submission boundaries, without Mail, Accessibility or outbound calls."""
import concurrent.futures
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from gmail_backend import ProviderError
from native_transport import submit, script, discard


class NativeTransportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.request = {'sender': 'owner@example.com', 'formatted_sender': 'Owner <owner@example.com>',
                        'account_id': 'ACCOUNT', 'to': ['self@example.net'], 'cc': [], 'bcc': [],
                        'subject': 'Test', 'body': 'Body', 'attach': []}
        self.draft = {'message_id': '<checked@example.com>', 'ref': 'fixture:draft'}
        health = patch('native_transport.before_compose', return_value=None)
        self.before_compose = health.start()
        self.addCleanup(health.stop)

    def test_validation_and_identity_persistence_precede_submission(self):
        events = []
        def prepare(*args):
            events.append('prepare')
            return self.draft
        def persist(verification):
            self.assertEqual(verification['draft_message_id'], self.draft['message_id'])
            events.append('persist')
        def send(*args):
            self.assertEqual(args[1], 'submit')
            events.append('submit')
            return {'mail_send_result': True}
        with patch('native_transport.prepare_native', side_effect=prepare), patch('native_transport.script', side_effect=send):
            result = submit(None, self.request, {}, self.state, self.state, 2, prepared=persist)
        self.assertTrue(result['format_verified'])
        self.assertEqual(events, ['prepare', 'persist', 'submit'])

    def test_planned_mail_restart_does_not_spend_the_message_deadline(self):
        def restart(state):
            time.sleep(.3)
            return {'mail_restarted_after_composes': 40}
        self.before_compose.side_effect = restart
        def send(*args):
            self.assertGreater(args[4] - time.monotonic(), .2)
            return {'mail_send_result': True}
        with patch('native_transport.prepare_native', return_value=self.draft), patch('native_transport.script', side_effect=send):
            result = submit(None, self.request, {}, self.state, self.state, .4)
        self.assertEqual(result['mail_restarted_after_composes'], 40)

    def test_prepare_only_never_submits(self):
        self.request['outgoing_id'] = 17
        with patch('native_transport.prepare_native', return_value=self.draft), patch('native_transport.script') as sender:
            result = submit(None, self.request, {}, self.state, self.state, 2, prepare_only=True)
        self.assertEqual(result['state'], 'prepared')
        sender.assert_not_called()

    def test_all_preparation_failures_are_known_rejections(self):
        for failure in [ProviderError('uncertain preparation', uncertain=True), RuntimeError('failed'),
                        OSError('failed'), subprocess.TimeoutExpired('compose', 1)]:
            with self.subTest(failure=type(failure).__name__):
                with patch('native_transport.prepare_native', side_effect=failure), patch('native_transport.script') as sender:
                    with self.assertRaises(ProviderError) as raised:
                        submit(None, self.request, {}, self.state, self.state, 2)
                self.assertFalse(raised.exception.uncertain)
                sender.assert_not_called()

    def test_failed_identity_persistence_prevents_send(self):
        def fail(_):
            raise ProviderError('failed persistence', uncertain=True)
        with patch('native_transport.prepare_native', return_value=self.draft), patch('native_transport.script') as sender:
            with self.assertRaises(ProviderError) as raised:
                submit(None, self.request, {}, self.state, self.state, 2, prepared=fail)
        self.assertFalse(raised.exception.uncertain)
        sender.assert_not_called()

    def test_uncertain_submission_is_not_retried(self):
        with patch('native_transport.prepare_native', return_value=self.draft), patch('native_transport.script', side_effect=subprocess.TimeoutExpired('send', 1)) as sender:
            with self.assertRaises(ProviderError) as raised:
                submit(None, self.request, {}, self.state, self.state, 2)
        self.assertTrue(raised.exception.uncertain)
        self.assertEqual(sender.call_count, 1)

    def test_deadline_before_native_invocation_is_rejected(self):
        with patch('native_transport.subprocess.run') as process:
            with self.assertRaises(ProviderError) as raised:
                script({}, 'submit', self.state, self.state / 'request.json', time.monotonic() - 1)
        self.assertFalse(raised.exception.uncertain)
        process.assert_not_called()

    def test_accounts_share_one_editor_lock(self):
        count = 0
        maximum = 0
        mutex = threading.Lock()
        def prepare(*args):
            nonlocal count, maximum
            with mutex:
                count += 1
                maximum = max(maximum, count)
            time.sleep(.03)
            return self.draft
        def send(*args):
            nonlocal count
            with mutex:
                count -= 1
            return {'mail_send_result': True}
        with patch('native_transport.prepare_native', side_effect=prepare), patch('native_transport.script', side_effect=send):
            with concurrent.futures.ThreadPoolExecutor(4) as pool:
                results = list(pool.map(lambda i: submit(None, dict(self.request, account_id=str(i)), {}, self.state, self.state, 3), range(4)))
        self.assertTrue(all(result['mail_send_result'] for result in results))
        self.assertEqual(maximum, 1)

    def test_scratch_left_by_a_dead_process_is_removed_before_the_next_composition(self):
        stale = self.state / 'compose-dead'
        stale.mkdir()
        (stale / 'request.json').write_text('{"body": "an old message"}')
        with patch('native_transport.prepare_native', return_value=self.draft), patch('native_transport.script', return_value={'mail_send_result': True}):
            submit(None, self.request, {}, self.state, self.state, 2)
        self.assertEqual(list(self.state.glob('compose-*')), [])

    def test_a_window_mail_would_not_close_is_recorded_on_the_request(self):
        for still_open in (True, False):
            with self.subTest(still_open=still_open):
                request = dict(self.request, compose_title='Test')
                with patch('native_transport.script', side_effect=subprocess.TimeoutExpired('osascript', 15)), \
                        patch('native_editor.compose_window_open', return_value=still_open), \
                        patch('native_transport.sweep_synced_drafts') as sweep:
                    discard(None, request, set(), self.state, self.state / 'request.json')
                self.assertEqual(request.get('compose_window_left_open', False), still_open)
                sweep.assert_not_called()

    def test_result_reports_how_the_body_was_entered_and_where_the_time_went(self):
        def prepare(store, request, *rest):
            request.update(body_entry='text_operation', timings_ms={'compose': 600})
            return self.draft
        with patch('native_transport.prepare_native', side_effect=prepare), patch('native_transport.script', return_value={'mail_send_result': True}):
            result = submit(None, self.request, {}, self.state, self.state, 2)
        self.assertEqual(result['body_entry'], 'text_operation')
        self.assertIn('submit', self.request['timings_ms'])

    def test_a_window_prepared_but_never_submitted_is_closed(self):
        operations = []
        def run(request, operation, *rest):
            operations.append(operation)
            if operation == 'submit':
                raise ProviderError('Deadline elapsed before the next native operation; no submission was attempted')
            return {'stage': 'discarded'}
        with patch('native_transport.prepare_native', return_value=self.draft), patch('native_transport.script', side_effect=run), \
                patch('native_transport.sweep_synced_drafts') as sweep:
            with self.assertRaises(ProviderError) as raised:
                submit(None, self.request, {}, self.state, self.state, 2)
        self.assertFalse(raised.exception.uncertain)
        self.assertEqual(operations, ['submit', 'discard'])
        sweep.assert_called_once()

    def test_a_window_that_may_have_been_submitted_is_left_alone(self):
        operations = []
        def run(request, operation, *rest):
            operations.append(operation)
            raise ProviderError('Mail refused send (error -1712)', uncertain=True)
        with patch('native_transport.prepare_native', return_value=self.draft), patch('native_transport.script', side_effect=run):
            with self.assertRaises(ProviderError) as raised:
                submit(None, self.request, {}, self.state, self.state, 2)
        self.assertTrue(raised.exception.uncertain)
        self.assertEqual(operations, ['submit'])

    def test_a_mail_too_slow_to_show_its_window_was_never_asked_to_send(self):
        from local_store import MailError
        with patch('native_editor.SubmissionWatch', side_effect=MailError('The amail compose window is missing or ambiguous')), \
                patch('native_transport.subprocess.run') as process:
            with self.assertRaises(ProviderError) as raised:
                script(self.request, 'submit', self.state, self.state / 'request.json', time.monotonic() + 5)
        self.assertFalse(raised.exception.uncertain)
        process.assert_not_called()
