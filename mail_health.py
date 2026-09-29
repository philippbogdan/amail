"""Keep Apple Mail responsive across many amail compositions.

Mail 16 leaves one worker thread blocked for good behind every compose window
it opens, whether the window is sent, discarded or closed; Mail's own
Command-N does the same. Around sixty of them exhaust Mail's dispatch thread
pool and Mail stops responding. A person rarely opens that many windows in
one Mail session; a batch does. amail counts the windows it opens per Mail
process and restarts Mail between messages well before that point.

macOS can also pause Mail outright when the system runs out of swap. A paused
Mail answers nothing, so amail reports it instead of waiting on it.
"""
import ctypes
import json
import os
import subprocess
import time
from local_store import MailError
from mail_sender import process_start

RESTART_AT = 40  # compose windows per Mail process before a planned restart
REFUSE_AT = 55   # beyond this, one more window risks the hang itself

PAUSED = ('macOS has paused Mail because the system ran out of swap (low memory). Quit memory-heavy jobs, then resume '
          'Mail from Force Quit Applications (Option-Command-Escape, select Mail, Resume) or quit and reopen it; '
          'nothing was sent')


def mail_pid():
    try:
        result = subprocess.run(['pgrep', '-x', 'Mail'], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return None
    pids = result.stdout.split()
    return int(pids[0]) if len(pids) == 1 else None


def suspended(pid):
    """Whether the kernel holds Mail's task suspended, as the low-swap handler does."""
    try:
        libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
        task_self = ctypes.c_uint.in_dll(libc, 'mach_task_self_').value
        name = ctypes.c_uint()
        if libc.task_name_for_pid(task_self, pid, ctypes.byref(name)):
            return False
        try:
            info = (ctypes.c_int * 12)()  # mach_task_basic_info; suspend_count is its last field
            count = ctypes.c_uint(12)
            if libc.task_info(name, 20, info, ctypes.byref(count)):  # MACH_TASK_BASIC_INFO
                return False
            return info[11] > 0
        finally:
            libc.mach_port_deallocate(task_self, name)
    except (OSError, AttributeError, ValueError):
        return False


def _ledger(state):
    return state / 'mail-composes.json'


def composes(state, pid):
    """Compose windows amail has opened in this Mail process."""
    try:
        seen = json.loads(_ledger(state).read_text())
    except (OSError, ValueError):
        return 0
    if seen.get('pid') != pid or seen.get('start') != process_start(pid):
        return 0
    return int(seen.get('composes', 0))


def record_compose(state):
    pid = mail_pid()
    if pid is None:
        return
    path = _ledger(state)
    count = composes(state, pid) + 1
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'pid': pid, 'start': process_start(pid), 'composes': count}))
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _ask(source, timeout):
    result = subprocess.run(['osascript', '-e', source], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise MailError('Mail did not answer: ' + (result.stderr.strip() or 'no response'))
    return result.stdout.strip()


def open_compose_windows():
    return int(_ask('tell application "Mail" to count outgoing messages', 15))


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def restart(pid, wait=60):
    """Quit Mail normally and reopen it; its queued actions and Outbox persist across the restart."""
    try:
        _ask('tell application "Mail" to quit', 30)
    except (MailError, subprocess.TimeoutExpired):
        pass
    deadline = time.monotonic() + 30
    while _alive(pid):
        if time.monotonic() >= deadline:
            raise MailError('Mail did not quit for its planned restart; quit and reopen Mail, then retry. Nothing was sent')
        time.sleep(.2)
    subprocess.run(['open', '-a', 'Mail'], capture_output=True, timeout=15)
    deadline = time.monotonic() + wait
    while True:
        try:
            if int(_ask('tell application "Mail" to count accounts', 15)) > 0:
                return
        except (MailError, ValueError, subprocess.TimeoutExpired):
            pass
        if time.monotonic() >= deadline:
            raise MailError('Mail did not come back after its planned restart; nothing was sent')
        time.sleep(1)


def before_compose(state):
    """Refuse a paused Mail, and restart Mail between messages before its compose threads run out."""
    pid = mail_pid()
    if pid is None:
        return None
    if suspended(pid):
        raise MailError(PAUSED)
    count = composes(state, pid)
    if count < RESTART_AT:
        return None
    windows = open_compose_windows()
    if windows == 0:
        restart(pid)
        return {'mail_restarted_after_composes': count}
    if count >= REFUSE_AT:
        raise MailError(f'Mail has opened {count} compose windows since it started and will soon stop responding (a Mail 16 '
                        f'fault); close the {windows} open compose window(s) so amail can restart Mail, then retry. '
                        'Nothing was sent')
    return None


def report(state):
    """Mail-wide health fields for amail doctor."""
    pid = mail_pid()
    if pid is None:
        return {'mail_running': False}
    info = {'mail_running': True, 'mail_paused': suspended(pid), 'mail_composes_since_launch': composes(state, pid),
            'mail_restart_after_composes': RESTART_AT}
    if info['mail_paused']:
        info['mail_paused_note'] = PAUSED.replace('; nothing was sent', '')
    return info
