"""Frozen, bounded mailbox marking. Never writes Apple's cache database."""
import concurrent.futures
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import time
import uuid
from gmail_backend import Gmail, secure_json
from local_store import MailError
from mail_operations import bridge, context, gmail_id
from workflows import fingerprint


def plan(store, state, account, mailbox, target, before=None):
    accounts = store.resolve(account)
    if len(accounts) != 1 or account == '*':
        raise MailError('Bulk marking requires one explicit account; create a plan per account')
    cutoff = before or dt.datetime.now(dt.timezone.utc).isoformat()
    rows = store.query(account=account, mailbox=mailbox, before=cutoff, unread=target == 'read', limit=100000)
    if len(rows) == 100000:
        raise MailError('Selection reaches the 100000-message ceiling; narrow the mailbox or cutoff')
    rows = [row for row in rows if bool(row['read']) != (target == 'read')]
    messages = []
    for row in rows:
        ref = store.ref(row)
        _, selected, identity = context(store, ref)
        messages.append({'ref': ref, 'identity': identity})
    snapshot = {'account': accounts[0]['uuid'], 'mailbox': mailbox, 'before': cutoff,
                'target': target, 'messages': messages, 'source': 'frozen local cache selection'}
    identifier = 'mark-' + uuid.uuid4().hex
    record = {'id': identifier, 'snapshot': snapshot, 'hash': fingerprint(snapshot), 'results': {}, 'created': time.time()}
    path = state / 'mark-plans' / (identifier + '.json')
    secure_json(path, record)
    return show(state, identifier)


def load(state, identifier):
    if not re.fullmatch(r'mark-[a-f0-9]{32}', identifier): raise MailError('Invalid mark plan ID')
    path = state / 'mark-plans' / (identifier + '.json')
    record = json.loads(path.read_text())
    if record['id'] != identifier or fingerprint(record['snapshot']) != record['hash']:
        raise MailError('Mark plan integrity check failed')
    return path, record


def summary(record, details=False):
    snapshot = record['snapshot']
    confirmed = sum(result.get('state') == 'confirmed' for result in record['results'].values())
    result = {'id': record['id'], 'account': snapshot['account'], 'mailbox': snapshot['mailbox'],
              'before': snapshot['before'], 'target': snapshot['target'], 'source': snapshot['source'],
              'selected': len(snapshot['messages']), 'confirmed': confirmed,
              'remaining': len(snapshot['messages']) - confirmed,
              'exceptions': [{'ref': ref, **value} for ref, value in record['results'].items() if value.get('state') != 'confirmed'],
              'apply_command': 'amail mark-bulk apply ' + record['id']}
    if details: result.update(messages=snapshot['messages'], results=record['results'])
    return result


def show(state, identifier, details=False):
    return summary(load(state, identifier)[1], details)


def apply(store, state, here, identifier):
    path, record = load(state, identifier)
    snapshot = record['snapshot']
    account = store.resolve(snapshot['account'])[0]
    fd = os.open(path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'w') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e: raise MailError('This mark plan already has a running worker') from e
        # Another invocation may have completed while this one was opening its lock.
        path, record = load(state, identifier)
        pending = [item for item in snapshot['messages'] if record['results'].get(item['ref'], {}).get('state') != 'confirmed']
        google = account['kind'] == 'com.apple.account.Google'
        client = Gmail(account.get('auth_address') or account['addresses'][0], state) if google else None
        for offset in range(0, len(pending), 20):
            chunk = pending[offset:offset + 20]
            valid = []
            for item in chunk:
                try:
                    _, current_account, identity = context(store, item['ref'])
                    if current_account['uuid'] != snapshot['account'] or identity != item['identity']:
                        raise MailError('Message identity changed since preview')
                    valid.append(item)
                except (MailError, OSError, ValueError) as e:
                    record['results'][item['ref']] = {'state': 'failed', 'error': str(e)}
            if google:
                def resolve(item):
                    try:
                        return item, gmail_id(client, item['identity']['message_id']), None
                    except Exception as e:
                        return item, None, str(e)
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                    resolved = list(pool.map(resolve, valid))
                ids = [provider_id for _, provider_id, error in resolved if not error]
                failure = None
                if ids:
                    try:
                        client.request('/messages/batchModify', payload={'ids': ids,
                            'addLabelIds': ['UNREAD'] if snapshot['target'] == 'unread' else [],
                            'removeLabelIds': ['UNREAD'] if snapshot['target'] == 'read' else []})
                    except Exception as e: failure = str(e)
                for item, provider_id, error in resolved:
                    record['results'][item['ref']] = ({'state': 'failed' if error else 'unverified', 'error': error or failure} if error or failure else
                        {'state': 'confirmed', 'provider': 'gmail', 'provider_id': provider_id, 'verification': 'Gmail batchModify accepted'})
            elif valid:
                try:
                    response = bridge(account['uuid'], 'mark_many', here, state,
                                      target=snapshot['target'], messages=[item['identity'] for item in valid])
                    results = response['results']
                    if len(results) != len(valid): raise MailError('Incomplete Mail result; inspect and reapply the frozen plan')
                    for item, result in zip(valid, results): record['results'][item['ref']] = result
                except Exception as e:
                    for item in valid: record['results'][item['ref']] = {'state': 'unverified', 'error': str(e)}
            secure_json(path, record)
    return summary(record)
