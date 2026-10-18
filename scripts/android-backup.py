"""Encrypted Android snapshots in archives/android on the existing private state branch."""
import argparse
import base64
import getpass
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import urllib.request

SETTINGS = Path.home() / '.config/hermes-android-backup.json'
HOME = Path.home() / '.hermes'
PREFIX = 'archives/android'
CHUNK = 24 * 1024 * 1024


def configure():
    values = {'repo': input('Private GitHub repository [Sugamdeol/hermes-storage]: ').strip() or 'Sugamdeol/hermes-storage',
              'branch': input('Branch [state]: ').strip() or 'state',
              'token': getpass.getpass('GitHub storage token (hidden): '),
              'key': getpass.getpass('Existing backup encryption key (hidden): ')}
    if not all(values.values()):
        raise RuntimeError('Token and encryption key are required')
    if len(values['repo'].split('/')) != 2 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._/' for c in values['repo']):
        raise RuntimeError('Repository must be owner/name')
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(SETTINGS, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(values, handle)
    SETTINGS.chmod(0o600)
    print('Backup credentials saved outside the backed-up home, mode 600.')


def cipher(key):
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))


def pack_home(source, output):
    """Online SQLite snapshots avoid copying a database independently of its WAL."""
    with tarfile.open(output, 'w:gz') as archive:
        for path in sorted(source.rglob('*')):
            name = path.relative_to(source).as_posix()
            if name == 'hermes-agent' or name.startswith('hermes-agent/'):
                continue  # Installed runtime, not personal state.
            if path.is_symlink():
                raise RuntimeError(f'Symlink needs manual migration before backup: {name}')
            if path.is_file() and path.name.endswith(('-wal', '-shm')):
                continue
            sqlite = False
            if path.is_file():
                with path.open('rb') as handle:
                    sqlite = handle.read(16) == b'SQLite format 3\x00'
            if sqlite:
                with tempfile.TemporaryDirectory() as directory:
                    snapshot = Path(directory) / 'snapshot.db'
                    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=30) as original:
                        with sqlite3.connect(snapshot) as target:
                            original.backup(target)
                    info = archive.gettarinfo(str(path), arcname=name)
                    info.size = snapshot.stat().st_size
                    with snapshot.open('rb') as handle:
                        archive.addfile(info, handle)
            else:
                archive.add(path, arcname=name, recursive=False)


def write_chunks(source, destination, key):
    destination.mkdir(parents=True, exist_ok=True)
    parts = []
    encrypt = cipher(key)
    with source.open('rb') as handle:
        while chunk := handle.read(CHUNK):
            name = f'{len(parts):06d}.enc'
            data = encrypt.encrypt(chunk)
            (destination / name).write_bytes(data)
            parts.append({'name': name, 'sha256': hashlib.sha256(data).hexdigest()})
    (destination / 'manifest.json').write_text(json.dumps({'version': 1, 'parts': parts}))


def read_chunks(source, output, key):
    manifest = json.loads((source / 'manifest.json').read_text())
    if manifest.get('version') != 1 or not manifest.get('parts'):
        raise RuntimeError('Unsupported or empty Android backup')
    decrypt = cipher(key)
    with output.open('wb') as handle:
        for index, part in enumerate(manifest['parts']):
            if part['name'] != f'{index:06d}.enc':
                raise RuntimeError('Invalid chunk order or name')
            data = (source / part['name']).read_bytes()
            if hashlib.sha256(data).hexdigest() != part['sha256']:
                raise RuntimeError('Backup chunk verification failed')
            handle.write(decrypt.decrypt(data))


def git(args, directory, env):
    result = subprocess.run(['git', *args], cwd=directory, env=env, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('Git operation failed; check connection, token permissions and branch conflicts. No force push was used.')
    return result.stdout.strip()


def operate(action):
    if not SETTINGS.is_file():
        raise RuntimeError('Run backup-setup first')
    settings = json.loads(SETTINGS.read_text())
    request = urllib.request.Request('https://api.github.com/repos/' + settings['repo'],
                                    headers={'Authorization': 'Bearer ' + settings['token'], 'User-Agent': 'hermes-android-backup'})
    with urllib.request.urlopen(request, timeout=30) as response:
        if not json.load(response).get('private'):
            raise RuntimeError('Backup destination must be private')
    with tempfile.TemporaryDirectory(prefix='hermes-android-backup-') as directory:
        root = Path(directory)
        askpass = root / 'askpass.sh'
        askpass.write_text('#!/bin/sh\ncase "$1" in *sername*) echo x-access-token;; *) printf "%s\\n" "$HERMES_BACKUP_TOKEN";; esac\n')
        askpass.chmod(0o700)
        env = {**os.environ, 'GIT_ASKPASS': str(askpass), 'GIT_TERMINAL_PROMPT': '0', 'HERMES_BACKUP_TOKEN': settings['token']}
        checkout = root / 'repo'
        checkout.mkdir()
        git(['init'], checkout, env)
        git(['remote', 'add', 'origin', 'https://github.com/' + settings['repo'] + '.git'], checkout, env)
        git(['config', 'user.name', 'Hermes Android Backup'], checkout, env)
        git(['config', 'user.email', 'hermes-android@localhost'], checkout, env)
        git(['sparse-checkout', 'set', '--no-cone', '/' + PREFIX + '/'], checkout, env)
        git(['fetch', '--depth=1', '--filter=blob:none', 'origin', settings['branch']], checkout, env)
        git(['checkout', '-b', 'snapshot', 'FETCH_HEAD'], checkout, env)
        destination = checkout / PREFIX
        archive = root / 'state.tar.gz'
        if action == 'backup':
            if not HOME.is_dir() or not any(HOME.iterdir()):
                raise RuntimeError('Refusing to back up an empty Hermes home')
            pack_home(HOME, archive)
            shutil.rmtree(destination, ignore_errors=True)
            write_chunks(archive, destination, settings['key'])
            git(['add', '--', PREFIX], checkout, env)
            git(['commit', '-m', 'Save encrypted Android Hermes snapshot'], checkout, env)
            # A regular push rejects any competing branch update. Other folders stay intact.
            git(['push', 'origin', 'HEAD:refs/heads/' + settings['branch']], checkout, env)
            print('Encrypted Android backup saved to ' + settings['repo'] + '@' + settings['branch'] + '/' + PREFIX)
        else:
            read_chunks(destination, archive, settings['key'])
            staging = root / 'restored'
            staging.mkdir()
            with tarfile.open(archive) as handle:
                # Python 3.12+ data filter rejects traversal, links outside target and special files.
                handle.extractall(staging, filter='data')
            if not any(staging.iterdir()):
                raise RuntimeError('Refusing an empty restore')
            previous = HOME.with_name('.hermes-before-android-restore')
            if previous.exists():
                raise RuntimeError('Move the previous .hermes-before-android-restore copy aside first')
            if HOME.exists():
                HOME.rename(previous)
            try:
                shutil.copytree(staging, HOME)
            except BaseException:
                shutil.rmtree(HOME, ignore_errors=True)
                if previous.exists():
                    previous.rename(HOME)
                raise
            # The runtime is excluded from snapshots but may be installed inside ~/.hermes.
            runtime = previous / 'hermes-agent'
            if runtime.exists():
                runtime.rename(HOME / 'hermes-agent')
            print('Android state restored. Previous local files retained at ' + str(previous))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['configure', 'backup', 'restore'])
    action = parser.parse_args().action
    if action == 'configure':
        configure()
    else:
        operate(action)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Never print URLs/HTTP response bodies containing credentials.
        raise SystemExit('Android backup failed: ' + (str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__))
