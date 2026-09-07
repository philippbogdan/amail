"""Private per-user account configuration and explicit first-run setup."""
import ast
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from local_store import MailError, Store

SUPPORTED = {'com.apple.account.Google', 'com.apple.account.Exchange'}


def state_path():
    return Path(os.environ.get('AMAIL_STATE_DIR') or os.environ.get('MAIL_STATE_DIR') or
                Path.home() / 'Library/Application Support/amail').expanduser().resolve()


def config_path(state=None):
    return Path(os.environ.get('AMAIL_CONFIG') or (state or state_path()) / 'config.json').expanduser().resolve()


def credentials_path():
    directory = Path(os.environ.get('GOG_HOME') or Path.home() / 'Library/Application Support/gogcli')
    return directory.expanduser() / 'credentials-amail.json'


def validate(value):
    from policy import PROFILES
    if not isinstance(value, dict) or set(value) - {'schema_version', 'accounts', 'display_name', 'policies'}:
        raise MailError('Config must contain only schema_version, accounts, display_name and policies')
    if value.get('schema_version', 1) != 1:
        raise MailError('Unsupported config schema version')
    accounts = value.get('accounts')
    if not isinstance(accounts, list) or not accounts or any(not isinstance(a, str) or not a.strip() or a == '*' for a in accounts):
        raise MailError('Select at least one account explicitly with amail setup --accounts ADDRESS ...')
    name = value.get('display_name', '')
    if not isinstance(name, str) or any(ord(ch) < 32 for ch in name):
        raise MailError('display_name must be plain text without control characters')
    overrides = value.get('policies', {})
    if not isinstance(overrides, dict) or set(overrides) - set(PROFILES):
        raise MailError('Policy overrides must use gmail or exchange')
    policies = {}
    for provider, changes in overrides.items():
        if not isinstance(changes, dict) or set(changes) - set(PROFILES[provider]):
            raise MailError('Unknown outreach policy setting')
        rule = dict(PROFILES[provider], **changes)
        for key, number in rule.items():
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number <= 0:
                raise MailError('Outreach policy values must be finite positive numbers')
            if key not in {'gap_min_seconds', 'gap_max_seconds'} and not isinstance(number, int):
                raise MailError('Recipient and message caps and window_seconds must be integers')
        if rule['gap_max_seconds'] < rule['gap_min_seconds']:
            raise MailError('gap_max_seconds must be at least gap_min_seconds')
        policies[provider] = rule
    return {'schema_version': 1, 'accounts': list(dict.fromkeys(a.casefold() for a in accounts)),
            'display_name': name, 'policies': policies}


def load(state=None, *, required=True):
    path = config_path(state)
    if not path.exists():
        if not required: return None
        raise MailError('amail is not configured. Run amail setup to discover accounts, then amail setup --accounts ADDRESS ...')
    try: return validate(json.loads(path.read_text()))
    except json.JSONDecodeError as e: raise MailError('Invalid JSON in private amail config') from e


def save(value, state=None):
    value = validate(value)
    path = config_path(state)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.config-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, indent=2)
            f.write('\n'); f.flush(); os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)
    return path


def apply(store, value):
    from policy import PROFILES
    for selection in value['accounts']:
        account = store.resolve(selection)[0]
        if account['kind'] not in SUPPORTED:
            raise MailError('This release supports Google and Exchange accounts configured in Apple Mail')
    store.display_name = value['display_name']
    store.policy_profiles = {p: dict(rule, **value['policies'].get(p, {})) for p, rule in PROFILES.items()}
    for account in store.accounts.values():
        provider = 'gmail' if account['kind'] == 'com.apple.account.Google' else 'exchange'
        account['outreach_policy'] = store.policy_profiles[provider].copy()
    return store


def setup(args, state, *, store_factory=Store):
    available = store_factory()
    supported = [a for a in available.accounts.values() if a['kind'] in SUPPORTED]
    if not args.accounts:
        if args.gmail_client_json or args.connect or args.name is not None:
            raise MailError('Supply --accounts when changing setup')
        return {'available_accounts': supported, 'configured': load(state, required=False),
                'next_step': 'amail setup --accounts ADDRESS ... [--name NAME] [--gmail-client-json FILE] [--connect]'}
    selected = [available.resolve(a)[0] for a in args.accounts]
    if any(a['kind'] not in SUPPORTED for a in selected):
        raise MailError('Select Google or Exchange accounts already connected to Apple Mail')
    previous = load(state, required=False) or {}
    value = validate(dict(previous, accounts=[a['auth_address'] or a['uuid'] for a in selected],
                          display_name=args.name if args.name is not None else previous.get('display_name', '')))
    if args.gmail_client_json:
        path = Path(args.gmail_client_json).expanduser().resolve()
        try:
            document = json.loads(path.read_text())
            client = document.get('installed', document.get('web', document))
            if not client.get('client_id') or not client.get('client_secret'): raise ValueError()
        except (ValueError, AttributeError, OSError) as e:
            raise MailError('Expected a downloaded Google OAuth client JSON file; never paste its contents into chat') from e
        p = subprocess.run(['gog', 'auth', 'credentials', 'set', str(path), '--client', 'amail'],
                           capture_output=True, text=True, timeout=30)
        if p.returncode: raise MailError('gog could not register the amail OAuth client; check gog installation and file access')
        if not credentials_path().is_file():
            raise MailError('gog stored credentials outside the expected config directory; check GOG_HOME')
    path = save(value, state)
    return {'state': 'configured', 'config_file': str(path), 'accounts': value['accounts'],
            'next_step': 'amail doctor', 'connect_requested': args.connect}


def migrate_legacy(executable, state):
    """Read legacy settings as data. Never execute the old CLI or copy secrets."""
    if config_path(state).exists() or not executable.exists(): return False
    old = executable.resolve().parent
    accounts = old / 'accounts.json'
    if not accounts.is_file(): return False
    value = json.loads(accounts.read_text())
    if not value.get('accounts'): return False
    value = {'accounts': value['accounts'], 'display_name': '', 'policies': {}}
    old_cli = old / 'cli.py'
    if old_cli.exists():
        for node in ast.walk(ast.parse(old_cli.read_text())):
            if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == '--name':
                for keyword in node.keywords:
                    if keyword.arg == 'default' and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                        value['display_name'] = keyword.value.value
    old_policy = old / 'policy.py'
    if old_policy.exists():
        for node in ast.parse(old_policy.read_text()).body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'PROFILES' for t in node.targets):
                rules = ast.literal_eval(node.value)
                for key, rule in rules.items():
                    provider = 'gmail' if key == 'gmail' else 'exchange'
                    value['policies'][provider] = rule
    save(value, state)
    return True
