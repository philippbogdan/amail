"""WebKit can expose duplicate accessibility children for a single banner."""
import unittest
from unittest.mock import patch
import native_editor
from native_editor import Accessibility, OBJECT, ensure_subject_available, enter_body
from local_store import MailError


class Editor:
    """Stands in for Accessibility: one compose window whose editor holds a document."""
    def __init__(self, history='', operation='works'):
        self.entered, self.objects, self.history, self.operation, self.events = '', '', history, operation, []

    def mail(self): return 'app'
    def window(self, app, title): return 'window'
    def body(self, window): return 'editor'
    def body_text(self, body): return self.entered + self.objects + self.history
    def close(self): self.events.append('closed')

    def insert_at_start(self, body, text):
        self.events.append('operation')
        if self.operation == 'works':
            self.entered = text + '\n'
        if self.operation == 'misplaces':
            self.history += text
        return self.operation != 'refused'


class Front:
    """Stands in for Foreground: records that Mail was brought forward and what was pasted."""
    def __init__(self, ax, app, window, body):
        self.ax = ax
        ax.events.append('front')

    def check(self): pass
    def key(self, code, command=False): self.ax.events.append(('key', code))
    def close(self): self.ax.events.append('front released')

    def paste(self, items):
        kind = items[0][0][0]
        self.ax.events.append(('paste', kind, len(items)))
        if kind == 'public.utf8-plain-text':
            self.ax.entered = items[0][0][1].decode() + '\n'
        else:
            self.ax.objects = OBJECT * len(items) + '\n'


class BodyEntryTests(unittest.TestCase):
    HISTORY = '\n\nOn 5 Nov 2026, someone wrote:\n\nEarlier text.\n'

    def enter(self, editor, text='Reviewed text.\n\nSecond paragraph.', attachments=()):
        quick = lambda ax, body, expected, seconds, check=None: native_editor.squash(ax.body_text(body)) == expected
        with patch('native_editor.Accessibility', return_value=editor), patch('native_editor.Foreground', Front), \
                patch('native_editor.settled', quick), patch('native_editor.time.sleep'):
            return enter_body('Subject', text, attachments)

    def test_text_goes_in_behind_other_apps_without_the_clipboard(self):
        editor = Editor(self.HISTORY)
        self.assertEqual(self.enter(editor), 'text_operation')
        self.assertEqual(editor.events, ['operation', 'closed'])
        self.assertTrue(editor.body_text('editor').startswith('Reviewed text.\n\nSecond paragraph.\n\n\nOn 5 Nov'))

    def test_an_editor_that_refuses_the_operation_gets_a_paste(self):
        editor = Editor(self.HISTORY, operation='refused')
        self.assertEqual(self.enter(editor), 'clipboard')
        self.assertEqual(editor.events, ['operation', 'front', ('paste', 'public.utf8-plain-text', 1), 'front released', 'closed'])

    def test_text_in_the_wrong_place_stops_the_send_and_is_never_pasted_again(self):
        editor = Editor(self.HISTORY, operation='misplaces')
        with self.assertRaises(MailError):
            self.enter(editor)
        self.assertEqual(editor.events, ['operation', 'closed'])

    def test_attachments_follow_the_text_and_need_mail_in_front(self):
        editor = Editor(self.HISTORY)
        self.assertEqual(self.enter(editor, attachments=['/tmp/one.pdf', '/tmp/two.png']), 'text_operation')
        self.assertEqual(editor.events, ['front', 'operation', ('key', 36), ('paste', 'public.file-url', 2), 'front released', 'closed'])
        self.assertIn('Second paragraph.\n' + OBJECT * 2, editor.body_text('editor'))

    def test_a_reply_with_no_new_text_still_takes_attachments(self):
        editor = Editor(self.HISTORY)
        self.assertIsNone(self.enter(editor, text='', attachments=['/tmp/one.pdf']))
        self.assertEqual(editor.events, ['front', ('key', 36), ('paste', 'public.file-url', 1), 'front released', 'closed'])


class HeavyHistoryTests(unittest.TestCase):
    """A reply to an HTML newsletter puts tens of thousands of elements inside the editor."""
    def fixture(self, quoted=100000):
        tree = {'window': ['toolbar', 'scroll'], 'toolbar': ['send'], 'scroll': ['editor'],
                'editor': ['quoted-%d' % i for i in range(quoted)]}
        roles = {'window': 'AXWindow', 'toolbar': 'AXToolbar', 'send': 'AXButton', 'scroll': 'AXScrollArea', 'editor': 'AXWebArea'}
        ax = Accessibility.__new__(Accessibility)
        ax.visited = []
        def attr(element, name):
            ax.visited.append(element)
            return roles.get(element, 'AXStaticText') if name == 'AXRole' else None
        ax.attr = attr
        ax.children = lambda element: tree.get(element, [])
        ax.text = lambda value: value or ''
        return ax

    def test_the_editor_is_found_without_walking_the_quoted_history(self):
        ax = self.fixture()
        self.assertEqual(ax.body('window', wait=0), 'editor')
        self.assertFalse(any(element.startswith('quoted') for element in ax.visited))

    def test_an_unpruned_walk_still_stops_at_its_bound(self):
        ax = self.fixture()
        with self.assertRaises(MailError):
            list(ax.walk('window'))


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

