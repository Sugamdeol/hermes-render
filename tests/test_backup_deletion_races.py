import errno
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_git_storage import load_storage, make_config


class BackupDeletionRaceTests(unittest.TestCase):
    def setUp(self):
        self.storage = load_storage()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data, self.work = self.root/'home', self.root/'work'
        self.data.mkdir(); self.work.mkdir()
        self.config = make_config(self.storage)
        (self.data/'keep').write_text('saved session')
        self.rotating = self.data/'ChromeExtMalware.store.32'
        self.rotating.write_text('old data')
        self.storage.build_worktree(self.data, self.work, self.config)
        self.rotating.write_text('new data')

    def test_rotating_chrome_file_disappearing_during_copy_is_a_real_deletion(self):
        original = self.storage.shutil.copy2
        def copy(source, destination, *args, **kwargs):
            if source == self.rotating:
                destination.write_text('partial')
                source.unlink()
                raise FileNotFoundError(errno.ENOENT, 'deleted', str(source))
            return original(source, destination, *args, **kwargs)
        with patch.object(self.storage.shutil, 'copy2', side_effect=copy):
            result = self.storage.build_worktree(self.data, self.work, self.config)
        self.assertEqual(result['files'], 1)
        self.assertEqual((self.work/'data/keep').read_text(), 'saved session')
        self.assertFalse((self.work/'data'/self.rotating.name).exists())

    def test_destination_failure_does_not_discard_an_existing_source(self):
        with patch.object(self.storage.shutil, 'copy2', side_effect=FileNotFoundError('destination vanished')):
            with self.assertRaisesRegex(self.storage.GitStateError, 'source still exists'):
                self.storage.build_worktree(self.data, self.work, self.config)
        self.assertEqual((self.work/'data'/self.rotating.name).read_text(), 'old data')
        self.assertTrue(self.rotating.exists())

    def test_replacement_source_is_not_mistaken_for_a_deletion(self):
        def copy(source, destination, *args, **kwargs):
            source.unlink()
            source.write_text('replacement')
            raise FileNotFoundError('old inode vanished')
        with patch.object(self.storage.shutil, 'copy2', side_effect=copy):
            with self.assertRaisesRegex(self.storage.GitStateError, 'source still exists'):
                self.storage.build_worktree(self.data, self.work, self.config)
        self.assertEqual((self.work/'data'/self.rotating.name).read_text(), 'old data')

    def test_unreadable_source_remains_a_failure(self):
        with patch.object(self.storage.shutil, 'copy2', side_effect=PermissionError('blocked')):
            with self.assertRaisesRegex(self.storage.GitStateError, 'retaining the previous backup'):
                self.storage.build_worktree(self.data, self.work, self.config)
        self.assertEqual((self.work/'data'/self.rotating.name).read_text(), 'old data')

    def test_removed_directory_during_walk_is_allowed_but_missing_home_is_not(self):
        def walk(root, **kwargs):
            kwargs['onerror'](FileNotFoundError(errno.ENOENT, 'deleted', str(self.data/'removed')))
            yield str(root), [], ['keep']
        with patch.object(self.storage.os, 'walk', walk):
            files, _, _ = self.storage.plan_tree(self.data, self.storage.DEFAULT_EXCLUDES)
            self.assertEqual(files, ['keep'])
        absent = self.root/'absent'
        self.assertFalse(self.storage.confirmed_source_deletion(absent/'file', absent))
        with self.assertRaises(self.storage.GitStateError):
            self.storage.plan_tree(absent, self.storage.DEFAULT_EXCLUDES)

    def test_missing_home_before_capture_keeps_existing_snapshot_intact(self):
        self.data.rename(self.root/'moved-home')
        with self.assertRaises(FileNotFoundError):
            self.storage.build_worktree(self.data, self.work, self.config)
        self.assertEqual((self.work/'data/keep').read_text(), 'saved session')
        self.assertEqual((self.work/'data'/self.rotating.name).read_text(), 'old data')

    def test_sqlite_database_deleted_before_open_is_mirrored_as_deleted(self):
        import sqlite3
        source = self.data/'rotating.db'
        with sqlite3.connect(source) as conn:
            conn.execute('CREATE TABLE records (value TEXT)')
        self.storage.build_worktree(self.data, self.work, self.config)
        snapshot = self.storage.snapshot_sqlite
        def delete_before_open(path, destination):
            path.unlink()
            return snapshot(path, destination)
        with patch.object(self.storage, 'snapshot_sqlite', side_effect=delete_before_open):
            self.storage.build_worktree(self.data, self.work, self.config)
        self.assertFalse((self.work/'data/rotating.db').exists())
        self.assertEqual((self.work/'data/keep').read_text(), 'saved session')

    def test_removed_file_during_scan_is_allowed(self):
        original = Path.stat
        def stat(path, *args, **kwargs):
            if path == self.rotating and kwargs.get('follow_symlinks', True):
                path.unlink(missing_ok=True)
                raise FileNotFoundError(errno.ENOENT, 'deleted', str(path))
            return original(path, *args, **kwargs)
        with patch.object(Path, 'stat', stat):
            files, _, _ = self.storage.plan_tree(self.data, self.storage.DEFAULT_EXCLUDES)
        self.assertEqual(files, ['keep'])


if __name__ == '__main__':
    unittest.main()
