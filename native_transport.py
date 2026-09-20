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
        raise ProviderError('Mail refused ' + reply.get('stage', operation) + ' (error ' + str(reply['error_number']) + ')',
                            uncertain=reply.get('stage') == 'send')
    valid = (operation == 'compose' and type(reply.get('outgoing_id')) is int
             or operation == 'prepare' and reply.get('stage') == 'prepared'
             or operation == 'submit' and type(reply.get('mail_send_result')) is bool)
    if not valid:
        raise ProviderError('Mail returned an incomplete operation result', uncertain=operation == 'submit')
    return reply


def prepare_native(store, request, verification, state, here, path, deadline):
    from native_editor import enter_body, ensure_subject_available
    from mail_sender import matches_content, canonical_address
    request['compose_title'] = request['subject']
    ensure_subject_available(request['subject'])
    before = {row['id'] for row in store.query(account=request['account_id'], mailbox='drafts',
                                              subject=request['subject'], limit=10000)}
    composed = script(request, 'compose', here, path, deadline)
    request['outgoing_id'] = composed['outgoing_id']
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
                matched[item['message_id']] = item
        if len(matched) == 1:
            return next(iter(matched.values()))
        time.sleep(.1)
    raise MailError('The saved Mail draft did not match the reviewed body, recipients, threading and attachments; nothing was sent')


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
                return result
    except ProviderError as exc:
        if not attempted:
            exc.uncertain = False
        raise
    except (MailError, OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        raise ProviderError(str(exc) if not attempted else 'Native submission interrupted; reconcile before any retry',
                            uncertain=attempted) from exc
