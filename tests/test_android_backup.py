import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tarfile
import subprocess
from unittest.mock import patch
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('android_backup', ROOT / 'scripts/android-backup.py')
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


class BackupTests(unittest.TestCase):
    def test_encrypted_roundtrip_and_corruption_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source'
            source.write_bytes(b'private state' * 100)
            backup.write_chunks(source, root / 'chunks', 'secret-key')
            backup.read_chunks(root / 'chunks', root / 'out', 'secret-key')
            self.assertEqual(source.read_bytes(), (root / 'out').read_bytes())
            chunk = root / 'chunks/000000.enc'
            self.assertNotIn(b'private state', chunk.read_bytes())
            chunk.write_bytes(b'corrupted')
            with self.assertRaisesRegex(RuntimeError, 'verification'):
                backup.read_chunks(root / 'chunks', root / 'bad', 'secret-key')

    def test_wrong_key_and_chunk_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'source').write_bytes(b'state')
            backup.write_chunks(root / 'source', root / 'chunks', 'correct')
            with self.assertRaises(Exception):
                backup.read_chunks(root / 'chunks', root / 'out', 'wrong')
            manifest = root / 'chunks/manifest.json'
            data = json.loads(manifest.read_text())
            data['parts'][0]['name'] = '../secret'
            manifest.write_text(json.dumps(data))
            with self.assertRaisesRegex(RuntimeError, 'chunk order'):
                backup.read_chunks(root / 'chunks', root / 'out', 'correct')

    def test_sqlite_wal_snapshot_keeps_committed_rows_and_excludes_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / 'home'
            home.mkdir()
            (home / 'hermes-agent').mkdir()
            (home / 'hermes-agent/runtime').write_text('excluded')
            with sqlite3.connect(home / 'state.db') as connection:
                connection.execute('pragma journal_mode=wal')
                connection.execute('create table saved(value text)')
                connection.execute("insert into saved values ('keep me')")
                connection.commit()
                backup.pack_home(home, root / 'state.tar.gz')
            with tarfile.open(root / 'state.tar.gz') as archive:
                self.assertEqual(archive.getnames(), ['state.db'])
                archive.extractall(root / 'restored', filter='data')
            with sqlite3.connect(root / 'restored/state.db') as connection:
                self.assertEqual(connection.execute('select value from saved').fetchone()[0], 'keep me')

    def test_real_git_roundtrip_preserves_main_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            remote, seed, home = root / 'remote.git', root / 'seed', root / 'home'
            def command(*args, cwd=None):
                return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()
            command('init', '--bare', str(remote))
            command('init', str(seed))
            command('config', 'user.name', 'Test', cwd=seed)
            command('config', 'user.email', 'test@localhost', cwd=seed)
            (seed / 'data').mkdir()
            (seed / 'data/main.txt').write_text('preserve me')
            command('add', '.', cwd=seed)
            command('commit', '-m', 'Main state', cwd=seed)
            command('push', str(remote), 'HEAD:refs/heads/state', cwd=seed)
            home.mkdir()
            (home / 'memory.txt').write_text('Android memory')
            settings = root / 'settings.json'
            settings.write_text(json.dumps({'repo': 'owner/private', 'branch': 'state', 'token': 'fake', 'key': 'key'}))
            real_git = backup.git
            def local_git(args, directory, env):
                if args[:2] == ['remote', 'add']:
                    args = [*args[:3], str(remote)]
                return real_git(args, directory, env)
            response = io.BytesIO(b'{"private":true}')
            with patch.object(backup, 'SETTINGS', settings), patch.object(backup, 'HOME', home), patch.object(backup, 'git', local_git), patch.object(backup.urllib.request, 'urlopen', return_value=response):
                backup.operate('backup')
            self.assertEqual(command('--git-dir=' + str(remote), 'show', 'state:data/main.txt'), 'preserve me')
            (home / 'memory.txt').write_text('local changes')
            response = io.BytesIO(b'{"private":true}')
            with patch.object(backup, 'SETTINGS', settings), patch.object(backup, 'HOME', home), patch.object(backup, 'git', local_git), patch.object(backup.urllib.request, 'urlopen', return_value=response):
                backup.operate('restore')
            self.assertEqual((home / 'memory.txt').read_text(), 'Android memory')
            self.assertEqual((home.with_name('.hermes-before-android-restore') / 'memory.txt').read_text(), 'local changes')

    def test_symlink_is_not_silently_omitted(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'home').mkdir()
            (root / 'home/link').symlink_to('/etc')
            with self.assertRaisesRegex(RuntimeError, 'Symlink'):
                backup.pack_home(root / 'home', root / 'state.tar.gz')


if __name__ == '__main__':
    unittest.main()
