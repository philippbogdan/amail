"""Targeted macOS Accessibility access to amail's own Mail compose window."""
import ctypes
import re
import subprocess
import time
from pathlib import Path
from local_store import MailError


EDITOR = ('AXWebArea',)  # the compose editor: one web area holding the new text and any quoted history
OBJECT = '\ufffc'       # the editor's text holds this character for each attachment or inline image


class Point(ctypes.Structure):
    _fields_ = [('x', ctypes.c_double), ('y', ctypes.c_double)]


class Accessibility:
    def __init__(self):
        self.cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        self.ax = ctypes.CDLL('/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices')
        self.owned = []
        ptr, integer = ctypes.c_void_p, ctypes.c_long
        for library, name, result, arguments in [
            (self.cf, 'CFStringCreateWithCString', ptr, [ptr, ctypes.c_char_p, ctypes.c_uint32]),
            (self.cf, 'CFStringGetCString', ctypes.c_bool, [ptr, ptr, integer, ctypes.c_uint32]),
            (self.cf, 'CFStringGetLength', integer, [ptr]),
            (self.cf, 'CFGetTypeID', ctypes.c_ulong, [ptr]),
            (self.cf, 'CFStringGetTypeID', ctypes.c_ulong, []),
            (self.cf, 'CFArrayGetCount', integer, [ptr]),
            (self.cf, 'CFArrayGetValueAtIndex', ptr, [ptr, integer]),
            (self.cf, 'CFArrayCreate', ptr, [ptr, ctypes.POINTER(ptr), integer, ptr]),
            (self.cf, 'CFDictionaryCreate', ptr, [ptr, ctypes.POINTER(ptr), ctypes.POINTER(ptr), integer, ptr, ptr]),
            (self.cf, 'CFRelease', None, [ptr]),
            (self.cf, 'CFEqual', ctypes.c_bool, [ptr, ptr]),
            (self.ax, 'AXIsProcessTrusted', ctypes.c_bool, []),
            (self.ax, 'AXUIElementCreateApplication', ptr, [ctypes.c_int]),
            (self.ax, 'AXUIElementCopyAttributeValue', ctypes.c_int, [ptr, ptr, ctypes.POINTER(ptr)]),
            (self.ax, 'AXUIElementSetAttributeValue', ctypes.c_int, [ptr, ptr, ptr]),
            (self.ax, 'AXUIElementCopyAttributeNames', ctypes.c_int, [ptr, ctypes.POINTER(ptr)]),
            (self.ax, 'AXUIElementIsAttributeSettable', ctypes.c_int, [ptr, ptr, ctypes.POINTER(ctypes.c_bool)]),
            (self.ax, 'AXUIElementPerformAction', ctypes.c_int, [ptr, ptr]),
            (self.ax, 'AXUIElementCopyParameterizedAttributeValue', ctypes.c_int, [ptr, ptr, ptr, ctypes.POINTER(ptr)]),
            (self.ax, 'AXValueGetValue', ctypes.c_bool, [ptr, ctypes.c_int, ptr]),
            (self.ax, 'CGEventCreateMouseEvent', ptr, [ptr, ctypes.c_uint32, Point, ctypes.c_uint32]),
            (self.ax, 'CGEventCreateKeyboardEvent', ptr, [ptr, ctypes.c_uint16, ctypes.c_bool]),
            (self.ax, 'CGEventSetFlags', None, [ptr, ctypes.c_uint64]),
            (self.ax, 'CGEventPostToPid', None, [ctypes.c_int, ptr]),
        ]:
            function = getattr(library, name)
            function.restype, function.argtypes = result, arguments

    def close(self):
        for value in reversed(self.owned):
            self.cf.CFRelease(value)
        self.owned.clear()

    def own(self, value):
        if value:
            self.owned.append(value)
        return value

    def string(self, value):
        return self.own(self.cf.CFStringCreateWithCString(None, value.encode(), 0x08000100))

    def text(self, value):
        if not value or self.cf.CFGetTypeID(value) != self.cf.CFStringGetTypeID():
            return ''  # AXValue of an image or attachment is a number, not text.
        buffer = ctypes.create_string_buffer(self.cf.CFStringGetLength(value) * 4 + 1)
        if not self.cf.CFStringGetCString(value, buffer, len(buffer), 0x08000100):
            raise MailError('Cannot decode a Mail accessibility attribute')
        return buffer.value.decode()

    def attr(self, element, name):
        value = ctypes.c_void_p()
        error = self.ax.AXUIElementCopyAttributeValue(element, self.string(name), ctypes.byref(value))
        return self.own(value.value) if error == 0 else None

    def array(self, value):
        if not value:
            return []
        return [self.cf.CFArrayGetValueAtIndex(value, i) for i in range(self.cf.CFArrayGetCount(value))]

    def names(self, element):
        value = ctypes.c_void_p()
        if self.ax.AXUIElementCopyAttributeNames(element, ctypes.byref(value)):
            return []
        return [self.text(item) for item in self.array(self.own(value.value))]

    def children(self, element):
        return self.array(self.attr(element, 'AXChildren'))

    def constant(self, name):
        return ctypes.addressof(ctypes.c_void_p.in_dll(self.cf, name))

    def cf_array(self, items):
        values = (ctypes.c_void_p * len(items))(*items)
        return self.own(self.cf.CFArrayCreate(None, values, len(items), self.constant('kCFTypeArrayCallBacks')))

    def cf_dictionary(self, mapping):
        keys = (ctypes.c_void_p * len(mapping))(*[self.string(key) for key in mapping])
        values = (ctypes.c_void_p * len(mapping))(*mapping.values())
        return self.own(self.cf.CFDictionaryCreate(None, keys, values, len(mapping), self.constant('kCFTypeDictionaryKeyCallBacks'),
                                                   self.constant('kCFTypeDictionaryValueCallBacks')))

    def walk(self, root, maximum=500, prune=()):
        """Elements under root, depth first, without entering an element whose role is in prune.

        The editor's web area holds the whole quoted history of a reply: tens
        of thousands of elements for an HTML newsletter. Every search here is
        for the window's own controls, so each one prunes the web area.
        """
        pending = [root]
        for _ in range(maximum):
            if not pending:
                return
            element = pending.pop()
            yield element
            if prune and self.text(self.attr(element, 'AXRole')) in prune:
                continue
            pending.extend(reversed(self.children(element)))
        raise MailError('Mail compose accessibility tree exceeded its bound')

    def mail(self, *, allow_absent=False):
        if not self.ax.AXIsProcessTrusted():
            raise MailError('Allow Accessibility access for the invoking terminal or agent host in System Settings, then run amail doctor')
        try:
            pids = subprocess.run(['pgrep', '-x', 'Mail'], capture_output=True, text=True, timeout=2).stdout.split()
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MailError('Cannot identify one running Apple Mail process') from exc
        if not pids and allow_absent:
            return None
        if len(pids) != 1:
            raise MailError('Cannot identify one running Apple Mail process')
        self.pid = int(pids[0])
        return self.own(self.ax.AXUIElementCreateApplication(self.pid))

    def frontmost(self, app, bring=False):
        """Whether an application is in front. With bring, ask for it to be, best effort."""
        true = ctypes.c_void_p.in_dll(self.cf, 'kCFBooleanTrue').value
        if bring:
            self.ax.AXUIElementSetAttributeValue(app, self.string('AXFrontmost'), true)
        return self.attr(app, 'AXFrontmost') == true

    def window(self, app, title):
        matches = [window for window in self.array(self.attr(app, 'AXWindows'))
                   if self.text(self.attr(window, 'AXTitle')) == title]
        if len(matches) != 1:
            raise MailError('The amail compose window is missing or ambiguous')
        return matches[0]

    def body(self, window, wait=5):
        # A freshly launched Mail can show the window before its editor has loaded.
        deadline = time.monotonic() + wait
        while True:
            matches = [element for element in self.walk(window, prune=EDITOR)
                       if self.text(self.attr(element, 'AXRole')) in EDITOR]
            if len(matches) == 1:
                return matches[0]
            if matches or time.monotonic() >= deadline:
                raise MailError('Cannot identify one Mail body editor')
            time.sleep(.1)

    def set_true(self, element, attribute):
        value = ctypes.c_void_p.in_dll(self.cf, 'kCFBooleanTrue').value
        if self.ax.AXUIElementSetAttributeValue(element, self.string(attribute), value):
            raise MailError('Cannot focus the amail compose editor')

    def press(self, element, action='AXPress'):
        if self.ax.AXUIElementPerformAction(element, self.string(action)):
            raise MailError('Mail refused an editor action')

    def lost_focus(self, app, window, body):
        """Why the compose editor does not have the keyboard, or None when it does."""
        focused_window = self.attr(app, 'AXFocusedWindow')
        if not focused_window or not self.cf.CFEqual(focused_window, window):
            return 'Mail focus changed; no further editor action was attempted'
        focused = self.attr(app, 'AXFocusedUIElement')
        if not focused or not self.cf.CFEqual(focused, body):
            return 'Mail body focus changed; no further editor action was attempted'
        return None

    def assert_focus(self, app, window, body):
        reason = self.lost_focus(app, window, body)
        if reason:
            raise MailError(reason)

    def click_body(self, body):
        point = Point()
        if not self.ax.AXValueGetValue(self.attr(body, 'AXPosition'), 1, ctypes.byref(point)):
            raise MailError('Cannot locate the Mail body editor')
        point.x += 20
        point.y += 10
        for kind in (1, 2):
            event = self.own(self.ax.CGEventCreateMouseEvent(None, kind, point, 0))
            self.ax.CGEventPostToPid(self.pid, event)

    def key(self, code, command=False):
        for down in (True, False):
            event = self.own(self.ax.CGEventCreateKeyboardEvent(None, code, down))
            self.ax.CGEventSetFlags(event, (1 << 20) if command else 0)
            self.ax.CGEventPostToPid(self.pid, event)

    def body_text(self, body):
        # Accessibility children can repeat the same table/banner text. Read
        # one native document range, as we do for the actual selection.
        document = self.parameter(body, 'AXTextMarkerRangeForUIElement', body)
        text = self.parameter(body, 'AXStringForTextMarkerRange', document) if document else None
        if text is None:
            raise MailError('Mail did not expose the complete editor text; no text was pasted')
        return self.text(text)

    def parameter(self, element, name, value):
        result = ctypes.c_void_p()
        error = self.ax.AXUIElementCopyParameterizedAttributeValue(
            element, self.string(name), value, ctypes.byref(result))
        return self.own(result.value) if error == 0 else None

    def insert_at_start(self, body, text):
        """Ask the editor to place text at the very start of its document. False when it cannot.

        WebKit's accessibility text operation replaces a text range the way a
        plain-text paste does, in whichever window holds the editor: Mail need
        not be in front, nothing is typed and the clipboard is untouched.
        """
        start = self.attr(body, 'AXStartTextMarker')
        span = self.parameter(body, 'AXTextMarkerRangeForUnorderedTextMarkers', self.cf_array([start, start])) if start else None
        if not span:
            return False
        operation = self.cf_dictionary({'AXTextOperationType': self.string('TextOperationReplace'),
                                        'AXTextOperationMarkerRanges': self.cf_array([span]),
                                        'AXTextOperationReplacementString': self.string(text),
                                        'AXTextOperationSmartReplace': ctypes.c_void_p.in_dll(self.cf, 'kCFBooleanFalse').value})
        result = ctypes.c_void_p()
        error = self.ax.AXUIElementCopyParameterizedAttributeValue(body, self.string('AXTextOperation'), operation, ctypes.byref(result))
        self.own(result.value)
        return error == 0

    def selected_text(self, body):
        selection = self.attr(body, 'AXSelectedTextMarkerRange')
        result = ctypes.c_void_p()
        if not selection or self.ax.AXUIElementCopyParameterizedAttributeValue(
                body, self.string('AXStringForTextMarkerRange'), selection, ctypes.byref(result)):
            return None
        return self.text(self.own(result.value))


def compose_window_open(subject):
    """Whether Mail shows a window titled with exactly this subject."""
    ax = Accessibility()
    try:
        app = ax.mail(allow_absent=True)
        return bool(app) and any(ax.text(ax.attr(window, 'AXTitle')) == subject
                                 for window in ax.array(ax.attr(app, 'AXWindows')))
    finally:
        ax.close()


def ensure_subject_available(subject):
    """Keep the real subject visible without risking another open draft."""
    if compose_window_open(subject):
        raise MailError('A Mail compose window with this subject is already open; close it before retrying')


class Pasteboard:
    """Preserve every available clipboard representation, without temp files."""
    def __init__(self, ax):
        self.ax = ax
        ctypes.CDLL('/System/Library/Frameworks/AppKit.framework/AppKit')
        self.objc = ctypes.CDLL('/usr/lib/libobjc.A.dylib')
        self.objc.objc_getClass.argtypes = [ctypes.c_char_p]
        self.objc.objc_getClass.restype = ctypes.c_void_p
        self.objc.sel_registerName.argtypes = [ctypes.c_char_p]
        self.objc.sel_registerName.restype = ctypes.c_void_p
        self.pool = self.call(self.call(self.cls('NSAutoreleasePool'), 'alloc'), 'init')
        self.board = self.call(self.cls('NSPasteboard'), 'generalPasteboard')

    def cls(self, name):
        return self.objc.objc_getClass(name.encode())

    def call(self, obj, selector, *args, result=ctypes.c_void_p):
        function = ctypes.CFUNCTYPE(result, ctypes.c_void_p, ctypes.c_void_p, *(kind for kind, _ in args))(
            ('objc_msgSend', self.objc))
        return function(obj, self.objc.sel_registerName(selector.encode()), *(value for _, value in args))

    def array(self, items):
        return [self.call(items, 'objectAtIndex:', (ctypes.c_ulong, i))
                for i in range(self.call(items, 'count', result=ctypes.c_ulong))]

    def count(self):
        return self.call(self.board, 'changeCount', result=ctypes.c_long)

    def snapshot(self):
        snapshot, total = [], 0
        for item in self.array(self.call(self.board, 'pasteboardItems')):
            values = []
            for kind in self.array(self.call(item, 'types')):
                data = self.call(item, 'dataForType:', (ctypes.c_void_p, kind))
                if not data:
                    raise MailError('Clipboard has an unavailable representation; leave it unchanged')
                size = self.call(data, 'length', result=ctypes.c_ulong)
                total += size
                if total > 64 * 1024 * 1024:
                    raise MailError('Clipboard is too large to preserve for editor entry')
                values.append((self.ax.text(kind), ctypes.string_at(self.call(data, 'bytes'), size)))
            snapshot.append(values)
        return snapshot

    def write(self, snapshot):
        objects = []
        for values in snapshot:
            item = self.call(self.call(self.cls('NSPasteboardItem'), 'alloc'), 'init')
            self.call(item, 'autorelease')
            for kind, payload in values:
                data = self.call(self.cls('NSData'), 'dataWithBytes:length:',
                                 (ctypes.c_char_p, payload), (ctypes.c_ulong, len(payload)))
                if not self.call(item, 'setData:forType:', (ctypes.c_void_p, data),
                                 (ctypes.c_void_p, self.ax.string(kind)), result=ctypes.c_bool):
                    raise MailError('Cannot prepare clipboard data for the Mail editor')
            objects.append(item)
        pointers = (ctypes.c_void_p * len(objects))(*objects)
        array = self.call(self.cls('NSArray'), 'arrayWithObjects:count:',
                          (ctypes.c_void_p, ctypes.cast(pointers, ctypes.c_void_p)), (ctypes.c_ulong, len(objects)))
        self.call(self.board, 'clearContents', result=ctypes.c_long)
        if objects and not self.call(self.board, 'writeObjects:', (ctypes.c_void_p, array), result=ctypes.c_bool):
            raise MailError('Cannot write the Mail editor clipboard')
        return self.count()

    def close(self):
        self.call(self.pool, 'drain', result=None)


class SubmissionWatch:
    """Keep Mail's validation prompts visible and scoped to the checked window."""
    def __init__(self, title, attachments):
        self.ax = Accessibility()
        try:
            self.app = self.ax.mail()
            self.window = self.ax.window(self.app, title)
            self.allow_without_attachment = not attachments
        except Exception:
            self.ax.close()
            raise

    def close(self):
        self.ax.close()

    def finish(self, timeout):
        deadline = time.monotonic() + max(0, min(timeout, 5))
        handled = False
        while True:
            windows = self.ax.array(self.ax.attr(self.app, 'AXWindows'))
            if not any(self.ax.cf.CFEqual(window, self.window) for window in windows):
                return handled
            elements = list(self.ax.walk(self.window, prune=EDITOR))
            alerts = [element for element in elements if self.ax.text(self.ax.attr(element, 'AXIdentifier')) == 'Mail.omittedAttachmentAlert']
            if alerts and self.allow_without_attachment and not handled:
                buttons = [element for element in self.ax.walk(alerts[0])
                           if self.ax.text(self.ax.attr(element, 'AXIdentifier')) == 'Mail.omittedAttachmentRecipientAlert.sendAnywayButton']
                if len(alerts) == 1 and len(buttons) == 1:
                    self.ax.press(buttons[0])
                    handled = True
            if time.monotonic() >= deadline:
                raise MailError('Mail has not closed the submitted compose window; inspect its pending validation prompt and reconcile before retrying')
            time.sleep(.05)


def editor_text(title):
    """The exact text of the uniquely titled compose window's editor, read without focusing it."""
    ax = Accessibility()
    try:
        return ax.body_text(ax.body(ax.window(ax.mail(), title)))
    finally:
        ax.close()


def squash(value):
    """Whitespace-insensitive comparison key for editor text."""
    return ''.join(value.replace('\ufffc', '').split())


def layout(value):
    """Editor text without whitespace, keeping the character that stands for each attachment."""
    return ''.join(value.split())


def settled(ax, body, expected, seconds, check=None):
    """Whether the editor's text reaches the expected text within the given time."""
    deadline = time.monotonic() + seconds
    while True:
        if check:
            check()
        if squash(ax.body_text(body)) == expected:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(.05)


def frontmost_pid():
    """Process ID of the application in front, asked of Launch Services each time, or None.

    A process without a run loop cannot ask AppKit: its answer never changes
    after the first, and a batch worker lives for hours.
    """
    try:
        front = subprocess.run(['lsappinfo', 'front'], capture_output=True, text=True, timeout=2).stdout.strip()
        info = subprocess.run(['lsappinfo', 'info', '-only', 'pid', front], capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    found = re.search(r'pid"?\s*=\s*(\d+)', info)
    return int(found.group(1)) if found else None


class Foreground:
    """Mail in front with the compose editor focused and the clipboard borrowed, for as long as a paste needs.

    Closing it gives the clipboard back, and the keyboard back to the
    application that had it, unless the user has moved on meanwhile.
    """
    def __init__(self, ax, app, window, body):
        self.ax, self.app, self.window, self.body = ax, app, window, body
        self.board = Pasteboard(ax)
        self.written = None
        self.displaced = frontmost_pid()
        try:
            original = self.board.count()
            self.snapshot = self.board.snapshot()
            ax.set_true(app, 'AXFrontmost')
            ax.press(window, 'AXRaise')
            # Mail coming forward from behind takes a moment to give its new
            # window the keyboard, and a new message first focuses the To field.
            deadline = time.monotonic() + 3
            while True:
                ax.set_true(body, 'AXFocused')
                ax.click_body(body)
                time.sleep(.05)
                if not ax.lost_focus(app, window, body) or time.monotonic() >= deadline:
                    break
                time.sleep(.2)
            self.check()
            if self.board.count() != original:
                raise MailError('Clipboard changed during preparation; no body edit was attempted')
            self.key(126, command=True)  # Command-Up: the start of the document, above any quoted history.
            time.sleep(.05)
        except BaseException:
            self.close()
            raise

    def check(self):
        self.ax.assert_focus(self.app, self.window, self.body)

    def key(self, code, command=False):
        self.check()
        self.ax.key(code, command)

    def paste(self, items):
        if self.written is not None and self.board.count() != self.written:
            raise MailError('Clipboard changed before a paste, so nothing was sent')
        self.written = self.board.write(items)
        self.check()
        if self.board.count() != self.written:
            raise MailError('Clipboard changed before a paste, so nothing was sent')
        self.ax.key(9, command=True)  # Command-V, restricted to the Mail process.

    def close(self):
        try:
            # Do not overwrite a clipboard change made by the user meanwhile.
            if self.written is not None and self.board.count() == self.written:
                self.board.write(self.snapshot)
            if self.displaced and self.displaced != self.ax.pid and self.ax.frontmost(self.app):
                self.ax.frontmost(self.ax.own(self.ax.ax.AXUIElementCreateApplication(self.displaced)), bring=True)
        finally:
            self.board.close()


def enter_body(title, text, attachments=()):
    """Insert the reviewed text at the top of a uniquely identified compose window and return how it was entered.

    Whatever Mail already placed in the editor stays below the new text: the
    quoted history and attribution line of a native reply or forward, and any
    configured signature. Attachments follow the new text.

    Text goes in through the editor's accessibility text operation, so Mail
    stays in the background and the clipboard is untouched. Attachments are
    pasted, which takes Mail to the front and borrows the clipboard for a
    moment, and so does the text when the editor refuses the operation.
    """
    text = text.rstrip('\n')
    ax = Accessibility()
    front = None
    try:
        app = ax.mail()
        window = ax.window(app, title)
        body = ax.body(window)
        previous = squash(ax.body_text(body))
        expected = squash(text) + previous
        failed = MailError('Mail did not place the text at the top of the body; the message was not sent')
        entry = None
        if attachments:
            front = Foreground(ax, app, window, body)
        if text:
            if ax.insert_at_start(body, text):
                if not settled(ax, body, expected, 3):
                    raise failed
                entry = 'text_operation'
            elif squash(ax.body_text(body)) != previous:
                raise failed
            else:
                front = front or Foreground(ax, app, window, body)
                front.paste([[('public.utf8-plain-text', text.encode())]])
                if not settled(ax, body, expected, 3, front.check):
                    raise failed
                entry = 'clipboard'
        if attachments:
            front.key(36)  # Return: a new paragraph after the entered text, before any history.
            time.sleep(.1)
            front.paste([[('public.file-url', Path(path).as_uri().encode())] for path in attachments])
            # Wait for Mail to show the files. The saved draft's MIME parts are
            # the actual gate, so a file Mail shows some other way is not a failure here.
            shown = layout(text) + OBJECT * len(attachments)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                front.check()
                if layout(ax.body_text(body)).startswith(shown):
                    break
                time.sleep(.1)
            front.check()
        return entry
    finally:
        if front:
            front.close()
        ax.close()
