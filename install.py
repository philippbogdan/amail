#!/usr/bin/env python3
"""Install amail without sudo; preserve private accounts, credentials and history."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from configuration import state_path, migrate_legacy

FILES = ['amail', 'cli.py', 'configuration.py', 'local_store.py', 'body_index.py',
         'mail_sender.py', 'gmail_backend.py', 'mail_operations.py', 'mail_operations.jxa',
         'send.applescript', 'workflows.py', 'policy.py', 'feedback.py', 'usage.py',
         'README.md', 'LICENSE', 'CONTRIBUTING.md', 'SECURITY.md', 'CHANGELOG.md',
         'docs/SETUP.md', 'docs/COMMANDS.md', 'docs/VALIDATION.md', 'skills/amail/SKILL.md']


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install(prefix, source=None):
    source = Path(source or __file__).resolve()
    if source.is_file(): source = source.parent
    prefix = Path(prefix).expanduser().resolve()
    fingerprint = hashlib.sha256(''.join(name + digest(source / name) for name in FILES).encode()).hexdigest()[:16]
    base = prefix / 'lib/amail'; releases = base / 'releases'
    releases.mkdir(parents=True, exist_ok=True)
    release = releases / fingerprint
    if not release.exists():
        staging = Path(tempfile.mkdtemp(prefix='.release-', dir=releases))
        try:
            for name in FILES:
                target = staging / name; target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / name, target)
            (staging / 'amail').chmod(0o755)
            (staging / 'manifest.json').write_text(json.dumps({name:digest(staging/name) for name in FILES}, indent=2)+'\n')
            os.rename(staging, release)
        finally:
            if staging.exists(): shutil.rmtree(staging)
    if any(digest(release/name) != digest(source/name) for name in FILES):
        raise RuntimeError('Release integrity check failed')
    executable = prefix / 'bin/amail'; executable.parent.mkdir(parents=True, exist_ok=True)
    previous = digest(executable) if executable.exists() else None
    backup = None
    if previous:
        backups = base / 'backups'; backups.mkdir(parents=True, exist_ok=True)
        backup = backups / ('amail-' + previous[:16])
        if not backup.exists(): shutil.copy2(executable, backup)
    # No mailbox permission or sign-in is needed to install. Setup is explicit.
    subprocess.run([sys.executable, str(release/'amail'), '--help'], check=True, capture_output=True, timeout=10)
    migrated = migrate_legacy(executable, state_path())
    if previous and digest(executable) != previous:
        raise RuntimeError('amail changed during installation; original left in place')
    temporary = executable.with_name('.amail-install-' + str(os.getpid()))
    try:
        temporary.symlink_to(release / 'amail'); os.replace(temporary, executable)
    finally:
        if temporary.is_symlink(): temporary.unlink()
    return {'installed': str(executable), 'release': str(release), 'backup': str(backup) if backup else None,
            'legacy_settings_migrated': migrated, 'state_directory': str(state_path()),
            'next_step': 'amail doctor' if migrated else 'amail setup',
            'system_mail': '/usr/bin/mail untouched'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', type=Path, default=Path.home()/'.local')
    args = parser.parse_args()
    if sys.platform != 'darwin': parser.error('amail requires macOS and an Apple Mail account')
    if sys.version_info < (3,10): parser.error('Python 3.10 or newer is required')
    try: print(json.dumps(install(args.prefix), indent=2))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as e:
        print(json.dumps({'error':str(e)}), file=sys.stderr); return 1
    return 0


if __name__ == '__main__': raise SystemExit(main())
