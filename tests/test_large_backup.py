import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_git_storage import load_storage, make_config

class LargeBackupTests(unittest.TestCase):
    def test_roundtrip_preserves_binary_and_executable_mode(self):
        m=load_storage()
        with tempfile.TemporaryDirectory() as td, patch.object(m,'GIT_BLOB_CHUNK_BYTES',16):
            root=Path(td);data=root/'source';data.mkdir();work=root/'repo';work.mkdir()
            binary=data/'tools/node';binary.parent.mkdir();payload=bytes(range(256))*4
            binary.write_bytes(payload);binary.chmod(0o755)
            (data/'small.txt').write_text('saved chat')
            config=make_config(m)
            manifest=m.build_worktree(data,work,config)
            self.assertIn('tools/node',manifest['large_files'])
            self.assertFalse((work/'data/tools/node').exists())
            self.assertTrue(all(p.stat().st_size<=16 for p in (work/'data').rglob('*') if p.is_file()))
            restored=root/'restored';m.materialize(work,restored,config)
            self.assertEqual((restored/'tools/node').read_bytes(),payload)
            self.assertEqual((restored/'tools/node').stat().st_mode&0o777,0o755)
            self.assertEqual((restored/'small.txt').read_text(),'saved chat')
            self.assertFalse((restored/m.CHUNK_DIRECTORY).exists())
    def test_chunked_encrypted_sqlite_roundtrip(self):
        m=load_storage()
        with tempfile.TemporaryDirectory() as td, patch.object(m,'GIT_BLOB_CHUNK_BYTES',64), patch.dict(os.environ,{'STORAGE_ENCRYPTION_KEY':'test-only-backup-key'}):
            root=Path(td);data=root/'source';data.mkdir();work=root/'repo';work.mkdir()
            db=sqlite3.connect(data/'state.db');db.execute('create table messages(body blob)')
            payload=os.urandom(2048);db.execute('insert into messages values (?)',(payload,));db.commit();db.close()
            config=make_config(m,env_mode='encrypt');manifest=m.build_worktree(data,work,config)
            self.assertIn('state.db.enc',manifest['large_files'])
            restored=root/'restored';m.materialize(work,restored,config)
            with sqlite3.connect(restored/'state.db') as connection:
                self.assertEqual(connection.execute('select body from messages').fetchone()[0],payload)
    def test_corrupt_chunk_does_not_overwrite_live_data(self):
        m=load_storage()
        with tempfile.TemporaryDirectory() as td, patch.object(m,'GIT_BLOB_CHUNK_BYTES',16):
            root=Path(td);data=root/'source';data.mkdir();work=root/'repo';work.mkdir()
            (data/'large').write_bytes(b'important'*12);(data/'small').write_text('new')
            config=make_config(m);manifest=m.build_worktree(data,work,config)
            part=work/'data'/manifest['large_files']['large']['parts'][0];part.write_bytes(b'corrupt')
            restored=root/'restored';restored.mkdir();(restored/'small').write_text('old')
            with self.assertRaises(m.GitStateError):m.materialize(work,restored,config)
            self.assertEqual((restored/'small').read_text(),'old')
    def test_traversal_and_reserved_directory_are_rejected(self):
        m=load_storage()
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'data').mkdir();(root/m.MANIFEST_NAME).write_text(json.dumps({'large_files':{'../escape':{'parts':['x']}}}))
            with self.assertRaises(m.GitStateError):m.materialize(root,root/'restore',make_config(m))
            (root/'data'/m.CHUNK_DIRECTORY).mkdir()
            with self.assertRaises(m.GitStateError):m.split_large_blobs(root/'data')
