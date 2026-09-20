"""Targeted macOS Accessibility access to amail's own Mail compose window."""
import ctypes
import subprocess
import time
from pathlib import Path
from local_store import MailError


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
            (self.cf, 'CFArrayGetCount', integer, [ptr]),
            (self.cf, 'CFArrayGetValueAtIndex', ptr, [ptr, integer]),
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
        if not value:
            return ''
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

    def walk(self, root, maximum=500):
        pending = [root]
        for _ in range(maximum):
            if not pending:
                return
            element = pending.pop()
            yield element
            pending.extend(reversed(self.children(element)))
        raise MailError('Mail compose accessibility tree exceeded its bound')

    def mail(self, *, allow_absent=False):
        if not self.ax.AXIsProcessTrusted():
            raise MailError('Allow Accessibility access for the invoking terminal or agent host in System Settings, then run amail doctor')
        result = subprocess.run(['pgrep', '-x', 'Mail'], capture_output=True, text=True, timeout=2)
        pids = result.stdout.split()
        if not pids and allow_absent:
            return None
        if len(pids) != 1:
            raise MailError('Cannot identify one running Apple Mail process')
        self.pid = int(pids[0])
        return self.own(self.ax.AXUIElementCreateApplication(self.pid))

    def window(self, app, title):
        matches = [window for window in self.array(self.attr(app, 'AXWindows'))
                   if self.text(self.attr(window, 'AXTitle')) == title]
        if len(matches) != 1:
            raise MailError('The amail compose window is missing or ambiguous')
        return matches[0]

    def body(self, window):
        matches = [element for element in self.walk(window)
                   if self.text(self.attr(element, 'AXRole')) == 'AXWebArea']
        if len(matches) != 1:
            raise MailError('Cannot identify one Mail body editor')
        return matches[0]

    def set_true(self, element, attribute):
        value = ctypes.c_void_p.in_dll(self.cf, 'kCFBooleanTrue').value
        if self.ax.AXUIElementSetAttributeValue(element, self.string(attribute), value):
            raise MailError('Cannot focus the amail compose editor')

    def press(self, element, action='AXPress'):
        if self.ax.AXUIElementPerformAction(element, self.string(action)):
            raise MailError('Mail refused an editor action')

    def assert_focus(self, app, window, body):
        focused_window = self.attr(app, 'AXFocusedWindow')
        if not focused_window or not self.cf.CFEqual(focused_window, window):
            raise MailError('Mail focus changed; no further editor action was attempted')
        focused = self.attr(app, 'AXFocusedUIElement')
        if not focused or not self.cf.CFEqual(focused, body):
            raise MailError('Mail body focus changed; no further editor action was attempted')

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

    def selected_text(self, body):
        selection = self.attr(body, 'AXSelectedTextMarkerRange')
        result = ctypes.c_void_p()
        if not selection or self.ax.AXUIElementCopyParameterizedAttributeValue(
                body, self.string('AXStringForTextMarkerRange'), selection, ctypes.byref(result)):
            return None
        return self.text(self.own(result.value))


def ensure_subject_available(subject):
    """Keep the real subject visible without risking another open draft."""
    ax = Accessibility()
    try:
        app = ax.mail(allow_absent=True)
        if app and any(ax.text(ax.attr(window, 'AXTitle')) == subject
                       for window in ax.array(ax.attr(app, 'AXWindows'))):
            raise MailError('A Mail compose window with this subject is already open; close it before retrying')
    finally:
        ax.close()


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
            elements = list(self.ax.walk(self.window))
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


def enter_body(title, text, attachments=()):
    """Replace only the body of a uniquely identified compose window."""
    ax = Accessibility()
    board = None
    try:
        app = ax.mail()
        window = ax.window(app, title)
        body = ax.body(window)
        board = Pasteboard(ax)
        original_count = board.count()
        snapshot = board.snapshot()
        ax.set_true(app, 'AXFrontmost')
        ax.press(window, 'AXRaise')
        ax.set_true(body, 'AXFocused')
        ax.click_body(body)
        time.sleep(.05)
        ax.assert_focus(app, window, body)
        if board.count() != original_count:
            raise MailError('Clipboard changed during preparation; no body edit was attempted')
        written = board.write([[('public.utf8-plain-text', text.encode())]])
        try:
            ax.assert_focus(app, window, body)
            previous = ''.join(ax.body_text(body).replace('\ufffc', '').split())
            ax.key(0, command=True)  # Command-A, restricted to the Mail process.
            deadline = time.monotonic() + 3
            while ''.join((ax.selected_text(body) or '').replace('\ufffc', '').split()) != previous:
                ax.assert_focus(app, window, body)
                if time.monotonic() >= deadline:
                    raise MailError('Mail did not select the complete body; no text was pasted')
                time.sleep(.05)
            ax.assert_focus(app, window, body)
            if board.count() != written:
                raise MailError('Clipboard changed before body entry; nothing was pasted')
            ax.key(9, command=True) if text else ax.key(51)
            deadline = time.monotonic() + 3
            expected = ''.join(text.split())
            while True:
                ax.assert_focus(app, window, body)
                if ''.join(ax.body_text(body).split()) == expected:
                    break
                if time.monotonic() >= deadline:
                    raise MailError('Mail did not confirm body entry; the message was not sent')
                time.sleep(.05)
            if attachments:
                ax.assert_focus(app, window, body)
                ax.key(125, command=True)  # End of the editable document.
                ax.key(36)  # Put attachments after the final body paragraph.
                time.sleep(.1)
                if board.count() != written:
                    raise MailError('Clipboard changed before attachment entry; nothing was sent')
                written = board.write([[('public.file-url', Path(path).as_uri().encode())] for path in attachments])
                ax.assert_focus(app, window, body)
                if board.count() != written:
                    raise MailError('Clipboard changed before attachment paste; nothing was sent')
                ax.key(9, command=True)
                deadline = time.monotonic() + 5
                while True:
                    ax.assert_focus(app, window, body)
                    descriptions = [ax.text(ax.attr(item, 'AXDescription')) for item in ax.walk(body)
                                    if ax.text(ax.attr(item, 'AXRole')) in {'AXAttachment', 'AXButton'}]
                    if len(descriptions) == len(attachments) and all(any(Path(path).name in desc for desc in descriptions) for path in attachments):
                        break
                    if time.monotonic() >= deadline:
                        raise MailError('Mail did not confirm every attachment; nothing was sent')
                    time.sleep(.1)
        finally:
            # Do not overwrite a clipboard change made by the user meanwhile.
            if board.count() == written:
                board.write(snapshot)
    finally:
        if board:
            board.close()
        ax.close()
