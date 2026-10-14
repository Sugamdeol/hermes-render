import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from test_git_storage import load_storage, make_config


class BusySQLiteBackupTests(unittest.TestCase):
    def setUp(self):
        self.storage = load_storage()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root/'chrome.db'
        self.destination = self.root/'snapshot.db'
        conn = sqlite3.connect(self.source)
        conn.execute('CREATE TABLE state (value TEXT)')
        conn.execute("INSERT INTO state VALUES ('committed')")
        conn.commit()
        conn.close()

    def assert_snapshot(self, value='committed'):
        with sqlite3.connect(self.destination) as conn:
            self.assertEqual(conn.execute('SELECT value FROM state').fetchone()[0], value)
            self.assertEqual(conn.execute('PRAGMA quick_check').fetchone()[0], 'ok')

    def test_exclusive_chrome_lock_is_captured_without_closing_browser(self):
        writer = sqlite3.connect(self.source)
        try:
            writer.execute('PRAGMA locking_mode=EXCLUSIVE')
            writer.execute('BEGIN EXCLUSIVE')
            writer.execute("UPDATE state SET value='not committed'")
            before = self.storage._sqlite_image_identity(self.source)
            start = time.monotonic()
            with patch.dict(os.environ, {'GIT_STATE_SQLITE_TIMEOUT_SECONDS':'0.1'}):
                self.storage.snapshot_sqlite(self.source, self.destination)
            self.assertLess(time.monotonic()-start, 3)
            self.assert_snapshot()
            self.assertTrue(writer.in_transaction)
            self.assertEqual(before, self.storage._sqlite_image_identity(self.source))
            self.assertEqual(writer.execute('SELECT value FROM state').fetchone()[0], 'not committed')
        finally:
            writer.rollback()
            writer.close()

    def test_live_rollback_journal_is_folded_into_snapshot_and_not_replayed_on_restore(self):
        writer = sqlite3.connect(self.source)
        work = self.root/'work'; work.mkdir()
        data = self.root/'source'; data.mkdir()
        writer.close()
        self.source.rename(data/'chrome.db')
        writer = sqlite3.connect(data/'chrome.db')
        try:
            writer.execute('BEGIN EXCLUSIVE')
            writer.execute("UPDATE state SET value='not committed'")
            with patch.dict(os.environ, {'GIT_STATE_SQLITE_TIMEOUT_SECONDS':'0.1'}):
                self.storage.build_worktree(data, work, make_config(self.storage))
            self.assertFalse((work/'data/chrome.db-journal').exists())
            restored = self.root/'restored'
            self.storage.materialize(work, restored, make_config(self.storage))
            with sqlite3.connect(restored/'chrome.db') as conn:
                self.assertEqual(conn.execute('SELECT value FROM state').fetchone()[0], 'committed')
        finally:
            writer.rollback(); writer.close()

    def test_wal_commits_are_included_with_live_writer(self):
        writer = sqlite3.connect(self.source)
        try:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute("UPDATE state SET value='latest WAL'")
            writer.commit()
            self.storage.snapshot_sqlite(self.source, self.destination)
            self.assert_snapshot('latest WAL')
        finally:
            writer.close()

    def test_changing_offline_image_is_rejected_and_existing_snapshot_preserved(self):
        self.destination.write_bytes(b'previous snapshot')
        with patch.object(self.storage, '_snapshot_sqlite_connection', side_effect=self.storage.SQLiteSnapshotTimeout('busy')), patch.object(self.storage, '_sqlite_image_identity', side_effect=[{'':(1,)}, {'':(2,)}]*3):
            with self.assertRaisesRegex(self.storage.GitStateError, 'kept changing'):
                self.storage.snapshot_sqlite(self.source, self.destination)
        self.assertEqual(self.destination.read_bytes(), b'previous snapshot')

    def test_external_super_journal_is_not_followed_by_copy_recovery(self):
        journal = Path(str(self.source)+'-journal')
        journal.write_bytes(b'/live/external-journal' + (22).to_bytes(4, 'big') + b'\0'*4 + b'\xd9\xd5\x05\xf9\x20\xa1\x63\xd7')
        with self.assertRaisesRegex(self.storage.GitStateError, 'multi-database'):
            self.storage._validate_copied_sqlite_journal(self.source)

    def test_backup_progress_deadline_triggers_validated_copy_fallback(self):
        real_connect = sqlite3.connect
        class Busy:
            def execute(self, statement): return self
            def fetchone(self): return (1,)
            def backup(self, target, **kwargs):
                while True:
                    kwargs['progress'](sqlite3.SQLITE_BUSY, 1, 1)
                    time.sleep(0.01)
            def close(self): pass
        first = [True]
        def connect(*args, **kwargs):
            if first[0]:
                first[0] = False
                return Busy()
            return real_connect(*args, **kwargs)
        with patch.dict(os.environ, {'GIT_STATE_SQLITE_TIMEOUT_SECONDS':'0.05'}), patch.object(self.storage.sqlite3, 'connect', side_effect=connect):
            self.storage.snapshot_sqlite(self.source, self.destination)
        self.assert_snapshot()


class EncFilenameTests(unittest.TestCase):
    def test_plaintext_tcl_enc_and_user_encrypted_artifacts_roundtrip_unchanged(self):
        storage = load_storage()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data, work, restored = root/'source', root/'work', root/'restored'
            data.mkdir(); work.mkdir()
            names = {'tools/python/lib/tcl9.0/encoding/macGreek.enc':b'# Encoding file: macGreek, single-byte\n',
                     'workspace/archive.enc':b'age-encryption.org/v1\nuser-owned artifact'}
            for name, content in names.items():
                path = data/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(content)
            storage.build_worktree(data, work, make_config(storage))
            with patch.object(storage, 'age_decrypt', side_effect=AssertionError('Plain user files must not be decrypted')):
                storage.materialize(work, restored, make_config(storage))
            for name, content in names.items():
                self.assertEqual((restored/name).read_bytes(), content)

    def test_legacy_plaintext_enc_is_preserved_without_manifest(self):
        storage = load_storage()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); work = root/'work'; (work/'data').mkdir(parents=True)
            (work/'data/macGreek.enc').write_text('# Encoding file: macGreek')
            storage.materialize(work, root/'restored', make_config(storage))
            self.assertEqual((root/'restored/macGreek.enc').read_text(), '# Encoding file: macGreek')

    def test_missing_plain_enc_recovery_uses_previous_tree_without_overwriting_live_files(self):
        storage = load_storage()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); work = root/'work'; data = root/'home'; data.mkdir()
            previous = work/'.git/hermes-state-data-previous'; previous.mkdir(parents=True)
            current = work/'data'; current.mkdir()
            (work/'MANIFEST.json').write_text(json.dumps({'encrypted':['config.yaml']}))
            (previous/'macGreek.enc').write_bytes(b'# Encoding file: macGreek')
            (previous/'config.yaml.enc').write_bytes(b'protected secret')
            (previous/'user.enc').write_bytes(b'old user file')
            (data/'user.enc').write_bytes(b'new user file')
            self.assertEqual(storage.repair_plaintext_enc_files(work, data), 1)
            self.assertEqual((data/'macGreek.enc').read_bytes(), b'# Encoding file: macGreek')
            self.assertEqual((data/'user.enc').read_bytes(), b'new user file')
            self.assertFalse((data/'config.yaml.enc').exists())
            self.assertEqual(storage.repair_plaintext_enc_files(work, data), 0)

    def test_recorded_broken_ciphertext_still_fails_closed(self):
        storage = load_storage()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); work = root/'work'; (work/'data').mkdir(parents=True)
            (work/'data/private.enc').write_bytes(b'broken')
            (work/'MANIFEST.json').write_text(json.dumps({'encrypted':['private']}))
            with patch.object(storage, 'age_decrypt', return_value=None):
                with self.assertRaisesRegex(storage.GitStateError, 'Could not decrypt'):
                    storage.materialize(work, root/'restored', make_config(storage))


if __name__ == '__main__':
    unittest.main()
