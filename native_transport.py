"""Prepare, inspect, then submit native Mail messages through its editor."""
import fcntl
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from gmail_backend import ProviderError
from local_store import MailError


def script(request, operation, here, path, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProviderError('Deadline elapsed before the next native operation; no submission was attempted')
    path.write_text(json.dumps(dict(request, operation=operation)))
    path.chmod(0o600)
    watch = None
    if operation == 'submit':
        from native_editor import SubmissionWatch
        watch = SubmissionWatch(request['subject'], request['attach'])
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


def prepare_native(store, request, verification, state, here, path, deadline):
    from native_editor import editor_text, enter_body, ensure_subject_available
    from mail_sender import matches_content, canonical_address
    request['compose_title'] = request['subject']
    ensure_subject_available(request['subject'])
    before = {row['id'] for row in store.query(account=request['account_id'], mailbox='drafts',
                                              subject=request['subject'], limit=10000)}
    request['known_drafts'] = sorted(before)  # discard may sweep Mail's autosaved copy of our window
    composed = script(request, 'compose', here, path, deadline)
    request['outgoing_id'] = composed['outgoing_id']
    try:
        enter_body(request['compose_title'], request['body'], request['attach'])
        script(request, 'prepare', here, path, deadline)
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
                verified = editor_text(request['compose_title'])
                # Keep time for submission and acceptance; an unsettled send still goes out.
                item['drafts_settled'] = settle_draft(store, request, before, draft_id,
                                                      min(deadline - 8, time.monotonic() + 20))
                if editor_text(request['compose_title']) != verified:
                    raise MailError('The compose window changed after its draft was verified; nothing was sent')
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
    """
    try:
        script(request, 'discard', here, path, time.monotonic() + 15)
    except Exception:
        return
    sweep_synced_drafts(store, request, before, here, path)


def sweep_synced_drafts(store, request, before, here, path, wait=20):
    deadline = time.monotonic() + wait
    swept = set()
    while time.monotonic() < deadline:
        rows = [row for row in store.query(account=request['account_id'], mailbox='drafts',
                                           subject=request['subject'], limit=200)
                if row['id'] not in before and row['id'] not in swept and row['subject'] == request['subject']]
        ready = [row['id'] for row in rows if row['remote_id']]
        if ready:
            try:
                script(dict(request, sweep_ids=ready), 'sweep', here, path, time.monotonic() + 15)
            except Exception:
                return len(swept)
            swept.update(ready)
        if not rows or all(row['id'] in swept for row in rows):
            if swept or time.monotonic() > deadline - wait + 5:
                return len(swept)
        time.sleep(1)
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
            with tempfile.TemporaryDirectory(prefix='compose-', dir=state) as directory:
                path = Path(directory) / 'request.json'
                draft = prepare_native(store, request, verification, state, here, path, deadline)
                verification['draft_message_id'] = draft['message_id']
                if prepared:
                    prepared(verification)
                if prepare_only:
                    return {'state': 'prepared', 'draft_ref': draft['ref'], 'outgoing_id': request['outgoing_id'],
                            'message_id': draft['message_id'], 'format_verified': True}
                attempted = True
                result = script(request, 'submit', here, path, deadline)
                result.update(draft_message_id=draft['message_id'], format_verified=True)
                if 'drafts_settled' in draft:
                    result['drafts_settled'] = draft['drafts_settled']
                return result
    except ProviderError as exc:
        if not attempted:
            exc.uncertain = False
        raise
    except (MailError, OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        raise ProviderError(str(exc) if not attempted else 'Native submission interrupted; reconcile before any retry',
                            uncertain=attempted) from exc


def close_stale_window(verification, state, here, store=None):
    """Best effort: close the compose window a dead process left for a request proven unsent."""
    request = {'sender': verification['sender'], 'formatted_sender': verification['sender'],
               'account_id': verification['account'], 'subject': verification['subject'],
               'compose_title': verification['subject'], 'to': [], 'cc': [], 'bcc': []}
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
