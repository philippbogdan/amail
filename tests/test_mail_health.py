"""Mail restart budget and pause detection, without Mail."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import mail_health
from local_store import MailError


class MailHealthTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        for name, value in [('mail_pid', 4242), ('process_start', 'Tue 29 Sep 08:50:00 2026'), ('suspended', False)]:
            patcher = patch('mail_health.' + name, return_value=value)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        restart = patch('mail_health.restart')
        self.restart = restart.start()
        self.addCleanup(restart.stop)

    def opened(self, count):
        for _ in range(count):
            mail_health.record_compose(self.state)

    def test_compose_count_belongs_to_one_mail_process(self):
        self.opened(3)
        self.assertEqual(mail_health.composes(self.state, 4242), 3)
        self.process_start.return_value = 'Tue 29 Sep 09:10:00 2026'  # Mail relaunched with the same pid
        self.assertEqual(mail_health.composes(self.state, 4242), 0)
        self.opened(1)
        self.assertEqual(mail_health.composes(self.state, 4242), 1)
        self.assertEqual(mail_health.composes(self.state, 5151), 0)

    def test_paused_mail_is_refused_before_any_window_opens(self):
        self.suspended.return_value = True
        with patch('mail_health.open_compose_windows') as windows:
            with self.assertRaisesRegex(MailError, 'paused Mail'):
                mail_health.before_compose(self.state)
        windows.assert_not_called()
        self.restart.assert_not_called()

    def test_no_restart_below_the_budget(self):
        self.opened(mail_health.RESTART_AT - 1)
        with patch('mail_health.open_compose_windows') as windows:
            self.assertIsNone(mail_health.before_compose(self.state))
        windows.assert_not_called()
        self.restart.assert_not_called()

    def test_idle_mail_restarts_at_the_budget(self):
        self.opened(mail_health.RESTART_AT)
        with patch('mail_health.open_compose_windows', return_value=0):
            result = mail_health.before_compose(self.state)
        self.restart.assert_called_once_with(4242)
        self.assertEqual(result, {'mail_restarted_after_composes': mail_health.RESTART_AT})

    def test_open_windows_postpone_the_restart_until_the_hard_limit(self):
        self.opened(mail_health.RESTART_AT)
        with patch('mail_health.open_compose_windows', return_value=1):
            self.assertIsNone(mail_health.before_compose(self.state))
            self.opened(mail_health.REFUSE_AT - mail_health.RESTART_AT)
            with self.assertRaisesRegex(MailError, 'close the 1 open compose window'):
                mail_health.before_compose(self.state)
        self.restart.assert_not_called()

    def test_no_mail_process_needs_no_guard(self):
        self.mail_pid.return_value = None
        self.assertIsNone(mail_health.before_compose(self.state))
        self.assertEqual(mail_health.report(self.state), {'mail_running': False})

    def test_report_names_a_paused_mail(self):
        self.suspended.return_value = True
        info = mail_health.report(self.state)
        self.assertTrue(info['mail_paused'])
        self.assertIn('Force Quit', info['mail_paused_note'])
        self.assertEqual(info['mail_restart_after_composes'], mail_health.RESTART_AT)


if __name__ == '__main__':
    unittest.main()
