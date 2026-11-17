"""Prepare, inspect, then submit native Mail messages through its editor."""
import fcntl
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from gmail_backend import ProviderError
from local_store import MailError
from mail_health import before_compose, mail_process, record_compose


def script(request, operation, here, path, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProviderError('Deadline elapsed before the next native operation; no submission was attempted')
    path.write_text(json.dumps(dict(request, operation=operation)))
    path.chmod(0o600)
    watch = None
    if operation == 'submit':
        from native_editor import SubmissionWatch
        try:
            watch = SubmissionWatch(request['subject'], request['attach'])
        except MailError as exc:  # a Mail too slow to show its window has not been asked to send
            raise ProviderError('Mail did not answer for its compose window before submission, so nothing was sent: ' + str(exc)) from exc
    try:
        result = subprocess.run(['osascript', str(here / 'send.applescript'), str(path)],
                                capture_output=True, text=True, timeout=remaining)
        if result.returncode:
            raise MailError('Mail compose script failed')
        reply = json.loads(result.stdout)
        if not isinstance(reply, dict):
            raise MailError('Mail returned an invalid operation result')
        if watch and reply.get('mail_send_result') is True:
            reply['handled_missing_attachment_warning'] = watch.finish(deadline - time.monotonic())
    finally:
        if watch:
            watch.close()
    if 'error_number' in reply:
        detail = reply.get('error_text')
        raise ProviderError('Mail refused ' + reply.get('stage', operation) + ' (error ' + str(reply['error_number']) + ')'
                            + (': ' + detail if detail else ''), uncertain=reply.get('stage') == 'send')
    valid = (operation == 'compose' and type(reply.get('outgoing_id')) is int
             or operation == 'prepare' and reply.get('stage') == 'prepared'
             or operation == 'discard' and reply.get('stage') == 'discarded'
             or operation == 'sweep' and reply.get('stage') == 'swept'
             or operation == 'close_stale' and reply.get('stage') == 'closed_stale'
             or operation == 'submit' and type(reply.get('mail_send_result')) is bool)
    if not valid:
        raise ProviderError('Mail returned an incomplete operation result', uncertain=operation == 'submit')
    return reply


def timed(request, stage, started):
    """Record how long one stage of a send took, for the result's timings_ms."""
    request.setdefault('timings_ms', {})[stage] = round((time.monotonic() - started) * 1000)


def prepare_native(store, request, verification, state, here, path, deadline):
    from native_editor import editor_text, enter_body, ensure_subject_available
    from mail_sender import matches_content, canonical_address
    request['compose_title'] = request['subject']
    ensure_subject_available(request['subject'])
    before = {row['id'] for row in store.query(account=request['account_id'], mailbox='drafts',
                                              subject=request['subject'], limit=10000)}
    request['known_drafts'] = sorted(before)  # discard may sweep Mail's autosaved copy of our window
    started = time.monotonic()
    try:
        composed = script(request, 'compose', here, path, deadline)
    finally:
        record_compose(state)  # Mail keeps a thread for every window it opens, even one that fails
    timed(request, 'compose', started)
    request['outgoing_id'] = composed['outgoing_id']
    request['compose_window'] = {'outgoing_id': composed['outgoing_id'], 'mail_process': mail_process()}
    try:
        started = time.monotonic()
        request['body_entry'] = enter_body(request['compose_title'], request['body'], request['attach'])
        timed(request, 'body', started)
        started = time.monotonic()
        script(request, 'prepare', here, path, deadline)
        timed(request, 'save', started)
        started = time.monotonic()
        while time.monotonic() < deadline:
            matched = {}
            for row in store.query(account=request['account_id'], mailbox='drafts', subject=request['subject'], limit=200):
                if row['id'] in before or row['subject'] != request['subject']:
                    continue
                try:
                    item, parts = store.read(store.ref(row))
                except (MailError, OSError):
                    continue
                if canonical_address(store, row['address'] or '') != canonical_address(store, request['sender']):
                    continue
                if matches_content(store, item, parts, verification, draft=True) and item.get('message_id'):
                    matched[item['message_id']] = item, row['id']
            if len(matched) == 1:
                item, draft_id = next(iter(matched.values()))
                timed(request, 'draft_verified', started)
                started = time.monotonic()
                verified = editor_text(request['compose_title'])
                # Keep time for submission and acceptance; an unsettled send still goes out.
                item['drafts_settled'] = settle_draft(store, request, before, draft_id,
                                                      min(deadline - 8, time.monotonic() + 20))
                if editor_text(request['compose_title']) != verified:
                    raise MailError('The compose window changed after its draft was verified; nothing was sent')
                timed(request, 'drafts_settled', started)
                return item
            time.sleep(.1)
        raise MailError('The saved Mail draft did not match the reviewed body, recipients, threading and attachments; nothing was sent')
    except BaseException:
        discard(store, request, before, here, path)
        raise


def settle_draft(store, request, before, draft_id, deadline, stable=1.0):
    """Wait until the verified draft is the window's only saved copy, known to the server, with no Drafts action queued.

    Mail saves the window more than once and deletes each superseded save. A
    Drafts sync that lands between that local delete and the server delete
    re-adds the old save locally. Sending then deletes every local copy, the
    re-added one included; the server refuses to delete an item already gone,
    and Mail retries that delete forever while every later copy, move and
    delete for the account waits behind it (a stuck "Copying Messages").
    """
    since = None
    while time.monotonic() < deadline:
        rows = [row for row in store.query(account=request['account_id'], mailbox='drafts',
                                           subject=request['subject'], limit=200)
                if row['id'] not in before and row['subject'] == request['subject']]
        settled = ([row['id'] for row in rows] == [draft_id] and rows[0]['remote_id']
                   and not store.pending_actions(request['account_id'], 'drafts'))
        now = time.monotonic()
        if not settled:
            since = None
        elif since is None:
            since = now
        elif now - since >= stable:  # a sync already in flight could still re-add a copy
            return True
        time.sleep(.2)
    return False


def discard(store, request, before, here, path):
    """Best effort: close amail's own unsent compose window, then remove Mail's autosaved copy.

    The copy is deleted only once the server knows it (the cache row has a
    remote id); deleting an unsynced draft leaves Mail a move it can never
    complete, which jams its whole action queue.

    A window Mail would not close is recorded on the request: the result says
    so, and a status check or an explicit retry of the request closes it.
    """
    try:
        script(request, 'discard', here, path, time.monotonic() + 15)
    except Exception:
        try:
            from native_editor import compose_window_open
            if compose_window_open(request['compose_title']):
                request['compose_window_left_open'] = True
        except Exception:
            pass
        return
    try:
        sweep_synced_drafts(store, request, before, here, path)
    except Exception:
        pass  # a cache read that failed must not replace the error that led here


def sweep_synced_drafts(store, request, before, here, path, wait=20, quiet=5.0, stable=1.0):
    """Remove the copies Mail saved of amail's own closed, unsent window. Return how many.

    A copy is deleted only once the server knows it, Mail has no Drafts action
    queued and nothing has changed for a second: deleting an unsynced draft, or
    one whose server delete is still on its way, leaves Mail an action it can
    never complete. A deleted copy that a sync shows again under the same
    server id is left for the next sync to remove. Mail can write one more
    save after the window closes, so the sweep goes on until no copy has
    shown for a few seconds.
    """
    deadline = time.monotonic() + wait
    swept, gone = set(), set()  # cache rows deleted, and the server ids they had
    seen = time.monotonic()
    steady = None
    while time.monotonic() < deadline:
        rows = [row for row in store.query(account=request['account_id'], mailbox='drafts',
                                           subject=request['subject'], limit=200)
                if row['id'] not in before and row['id'] not in swept and row['subject'] == request['subject']
                and row['remote_id'] not in gone]
        now = time.monotonic()
        if not rows:
            if now - seen >= quiet:
                break
        else:
            seen = now
            ready = sorted(row['id'] for row in rows if row['remote_id'])
            if not ready or store.pending_actions(request['account_id'], 'drafts'):
                steady = None
            elif steady is None or steady[0] != ready:
                steady = ready, now
            elif now - steady[1] >= stable:
                try:
                    reply = script(dict(request, sweep_ids=ready), 'sweep', here, path, time.monotonic() + 15)
                except Exception:
                    break
                done = set(reply.get('swept_ids', ready))
                gone.update(row['remote_id'] for row in rows if row['id'] in done)
                swept.update(done)
                steady = None
        time.sleep(.25)
    return len(swept)


def submit(store, request, verification, state, here, timeout, *, prepared=None, prepare_only=False):
    deadline = time.monotonic() + timeout
    attempted = False
    try:
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_fd = os.open(state / 'native-editor.lock', os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(lock_fd, 'w') as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise MailError('Another native Mail composition is still running; nothing was sent')
                    time.sleep(.05)
            for scratch in state.glob('compose-*'):
                shutil.rmtree(scratch, ignore_errors=True)  # left by a process that died, and only the lock holder composes
            with tempfile.TemporaryDirectory(prefix='compose-', dir=state) as directory:
                path = Path(directory) / 'request.json'
                started = time.monotonic()
                restarted = before_compose(state)
                if restarted:
                    deadline += time.monotonic() - started  # a planned Mail restart is not this message's time
                draft = prepare_native(store, request, verification, state, here, path, deadline)
                verification['draft_message_id'] = draft['message_id']
                verification['compose_window'] = request.get('compose_window')
                if prepared:
                    prepared(verification)
                if prepare_only:
                    return {'state': 'prepared', 'draft_ref': draft['ref'], 'outgoing_id': request['outgoing_id'],
                            'message_id': draft['message_id'], 'format_verified': True}
                attempted = True
                started = time.monotonic()
                try:
                    result = script(request, 'submit', here, path, deadline)
                except ProviderError as exc:
                    if not exc.uncertain:
                        # Mail was never asked to send, so the prepared window must not stay behind.
                        discard(store, request, set(request.get('known_drafts', ())), here, path)
                    raise
                timed(request, 'submit', started)
                result.update(draft_message_id=draft['message_id'], format_verified=True, body_entry=request.get('body_entry'))
                if 'drafts_settled' in draft:
                    result['drafts_settled'] = draft['drafts_settled']
                if restarted:
                    result.update(restarted)
                return result
    except ProviderError as exc:
        if not attempted:
            exc.uncertain = False
        raise
    except (MailError, OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        raise ProviderError(str(exc) if not attempted else 'Native submission interrupted; reconcile before any retry',
                            uncertain=attempted) from exc


def close_stale_window(verification, state, here, store=None):
    """Best effort: close the compose window an earlier attempt left for a request proven unsent. Return how many closed.

    Only amail's own window is closed: same subject and sender and, when the
    request recorded which window it opened, that very window in the Mail that
    opened it. After a Mail restart the window cannot be told from one of the
    user's, so nothing is closed.
    """
    request = {'sender': verification['sender'], 'formatted_sender': verification['sender'],
               'account_id': verification['account'], 'subject': verification['subject'],
               'compose_title': verification['subject'], 'to': [], 'cc': [], 'bcc': []}
    window = verification.get('compose_window')
    if window:
        if window.get('mail_process') != mail_process():
            return 0
        request['outgoing_id'] = window['outgoing_id']
    try:
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix='stale-', dir=state) as directory:
            path = Path(directory) / 'request.json'
            closed = script(request, 'close_stale', here, path, time.monotonic() + 15).get('closed', 0)
            if store is not None and verification.get('draft_message_id'):
                ready = [row['id'] for row in store.query(account=verification['account'], mailbox='drafts',
                                                          subject=verification['subject'], limit=200)
                         if row['remote_id'] and _draft_matches(store, row, verification['draft_message_id'])]
                if ready:
                    script(dict(request, sweep_ids=ready), 'sweep', here, path, time.monotonic() + 15)
            return closed
    except Exception:
        return None


def _draft_matches(store, row, message_id):
    try:
        return store.read(store.ref(row))[0].get('message_id') == message_id
    except (MailError, OSError):
        return False
