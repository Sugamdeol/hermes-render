import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('streaming_restore', ROOT/'scripts/git-storage.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class StreamingRestore(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); self.work = self.root/'work'
        self.source = self.work/'data'; self.source.mkdir(parents=True)
        self.home = self.root/'home'; self.records = {}
        self.manifest = {'large_files': self.records}
        self.config = m.GitConfig('owner/repo', 'fixture', workdir=self.work)

    def add(self, name, payload):
        chunk = f'{m.CHUNK_DIRECTORY}/{len(self.records)}/000000'
        path = self.source/chunk; path.parent.mkdir(parents=True); path.write_bytes(payload)
        self.records[name] = {'parts': [chunk], 'size': len(payload),
                              'sha256': hashlib.sha256(payload).hexdigest(), 'mode': 0o640}

    def restore(self, workers='4'):
        (self.work/m.MANIFEST_NAME).write_text(json.dumps(self.manifest))
        with patch.dict(os.environ, {'HERMES_RESTORE_COPY_WORKERS': workers}):
            return m.materialize(self.work, self.home, self.config)

    def test_all_data_modes_and_chunks_survive_without_second_copy(self):
        for name in ('a/x', 'a-file', 'b', 'c', 'd'):
            self.add(name, name.encode()*100)
        (self.source/'small').write_bytes(b'small')
        original = m.shutil.copyfile
        def copy(source, target, *args, **kwargs):
            self.assertNotEqual(Path(source).name, 'verified', 'large file copied twice')
            return original(source, target, *args, **kwargs)
        with patch.object(m.shutil, 'copyfile', side_effect=copy):
            self.assertEqual(self.restore(), 6)
        for name, record in self.records.items():
            self.assertEqual(hashlib.sha256((self.home/name).read_bytes()).hexdigest(), record['sha256'])
            self.assertEqual((self.home/name).stat().st_mode & 0o777, 0o640)
            self.assertTrue((self.source/record['parts'][0]).exists())
        self.assertFalse(list(self.root.glob('.*-restore-cache/*/verified')))
        self.assertEqual(self.restore('1'), 6)

    def test_retry_verifies_existing_bytes_and_repairs_changed_file(self):
        self.add('large', b'payload')
        self.restore()
        with patch.object(m, 'assemble_restore_blob', side_effect=AssertionError('completed file reassembled')):
            self.assertEqual(self.restore(), 1)
        (self.home/'large').write_bytes(b'changed')
        self.restore()
        self.assertEqual((self.home/'large').read_bytes(), b'payload')

    def test_failed_publication_can_retry_from_original_chunks(self):
        self.add('large', b'payload')
        with patch.object(m.os, 'fsync', side_effect=OSError('disk interrupted')):
            with self.assertRaises(OSError): self.restore()
        self.assertFalse((self.home/'large').exists())
        self.assertEqual(self.restore(), 1)
        self.assertEqual((self.home/'large').read_bytes(), b'payload')

    def test_corrupt_blob_never_publishes_or_marks_restore_complete(self):
        self.add('large', b'good'); (self.source/self.records['large']['parts'][0]).write_bytes(b'evil')
        with patch.object(m, 'reconcile_restored_paths') as reconcile:
            with self.assertRaises(m.GitStateError): self.restore()
            reconcile.assert_not_called()
        self.assertFalse((self.home/'large').exists())

    def test_encrypted_sqlite_is_decrypted_and_staging_released(self):
        db = self.root/'original.db'
        with sqlite3.connect(db) as con:
            con.execute('create table entries(value)'); con.execute('insert into entries values (42)')
        sealed = self.root/'sealed'
        with patch.dict(os.environ, {'STORAGE_ENCRYPTION_KEY': 'fixture-key'}):
            m.encrypt_stream_file(db, sealed, self.config)
            self.add('state.db.enc', sealed.read_bytes())
            self.manifest.update(encrypted=['state.db'], sqlite_snapshots=['state.db'])
            self.assertEqual(self.restore(), 1)
        with sqlite3.connect(self.home/'state.db') as con:
            self.assertEqual(con.execute('select value from entries').fetchone(), (42,))
        self.assertFalse(list(self.root.glob('.*-restore-cache/*/verified')))

    def test_plain_legacy_sqlite_still_uses_snapshot_publication(self):
        db = self.root/'original.db'
        with sqlite3.connect(db) as con: con.execute('create table entries(value)')
        self.add('state.db', db.read_bytes())
        with patch.object(m, 'snapshot_sqlite', wraps=m.snapshot_sqlite) as snapshot:
            self.restore(); self.assertTrue(snapshot.called)

    def test_parallel_assembly_is_bounded(self):
        for i in range(8): self.add(f'file{i}', b'payload')
        original = m.assemble_restore_blob
        lock = threading.Lock(); active = peak = 0
        def slow(*args):
            nonlocal active, peak
            with lock: active += 1; peak = max(peak, active)
            try: time.sleep(.01); return original(*args)
            finally:
                with lock: active -= 1
        with patch.object(m, 'assemble_restore_blob', side_effect=slow): self.restore()
        self.assertEqual(peak, 2); self.assertEqual(active, 0)

    def test_move_falls_back_only_across_filesystems(self):
        source = self.root/'source'; source.write_bytes(b'payload'); target = self.root/'target'
        with patch.object(m.os, 'replace', side_effect=OSError(errno.EXDEV, 'different disk')):
            m.move_verified_restore(source, target)
        self.assertEqual(target.read_bytes(), b'payload'); self.assertTrue(source.exists())
        with patch.object(m.os, 'replace', side_effect=PermissionError('blocked')):
            with self.assertRaises(PermissionError): m.move_verified_restore(source, target)

    def test_parent_symlink_cannot_escape_during_large_restore(self):
        outside = self.root/'outside'; outside.mkdir()
        self.home.mkdir(); (self.home/'escape').symlink_to(outside, target_is_directory=True)
        self.add('escape/file', b'payload')
        with self.assertRaises(m.GitStateError): self.restore()
        self.assertFalse((outside/'file').exists())
