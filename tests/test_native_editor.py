"""WebKit can expose duplicate accessibility children for a single banner."""
import unittest
from unittest.mock import patch
from native_editor import Accessibility, ensure_subject_available
from local_store import MailError


class NativeEditorTextTests(unittest.TestCase):
    def fixture(self):
        ax = Accessibility.__new__(Accessibility)
        ax.walk = lambda body: ['banner-one', 'banner-two', 'message']
        values = {'banner-one': 'External sender warning.', 'banner-two': 'External sender warning.',
                  'message': 'The original message.'}
        ax.attr = lambda node, name: 'AXStaticText' if name == 'AXRole' else values[node]
        ax.text = lambda value: value
        actual = '\nExternal sender warning.\n\nThe original message.\n'
        ax.parameter = lambda body, name, value: 'document-range' if name == 'AXTextMarkerRangeForUIElement' else actual
        return ax, actual

    def test_full_editor_text_does_not_duplicate_banner_children(self):
        ax, actual = self.fixture()
        self.assertEqual(ax.body_text('editor'), actual)

    def test_missing_native_document_range_fails_closed(self):
        ax, _ = self.fixture()
        ax.parameter = lambda *args: None
        with self.assertRaises(MailError):
            ax.body_text('editor')

    def test_empty_document_is_valid(self):
        ax, _ = self.fixture()
        ax.parameter = lambda body, name, value: 'document-range' if name == 'AXTextMarkerRangeForUIElement' else ''
        self.assertEqual(ax.body_text('editor'), '')

    def test_existing_real_subject_is_rejected_before_editing(self):
        with patch('native_editor.Accessibility') as constructor:
            ax = constructor.return_value
            ax.mail.return_value = 'app'
            ax.attr.side_effect = lambda element, name: ['window'] if name == 'AXWindows' else 'Re: A discussion'
            ax.array.side_effect = lambda value: value
            ax.text.side_effect = lambda value: value
            with self.assertRaises(MailError):
                ensure_subject_available('Re: A discussion')
            ax.close.assert_called_once()

    def test_absent_mail_has_no_conflicting_compose_window(self):
        with patch('native_editor.Accessibility') as constructor:
            ax = constructor.return_value
            ax.mail.return_value = None
            ensure_subject_available('Re: A discussion')
            ax.attr.assert_not_called()
            ax.close.assert_called_once()
