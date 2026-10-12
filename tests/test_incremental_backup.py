import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_git_storage import load_storage,make_config

class IncrementalBackupTests(unittest.TestCase):
    def test_unchanged_files_are_reused_and_only_changes_staged(self):
        m=load_storage();config=make_config(m)
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);data=root/'source';data.mkdir();work=root/'repo';work.mkdir()
            m.run_git(['init','-q'],cwd=work,config=config)
            (data/'keep').write_text('same');(data/'remove').write_text('old')
            m.build_worktree(data,work,config);m.stage_snapshot(work,config)
            m.run_git(['commit','-q','-m','first'],cwd=work,config=config)
            inode=(work/'data/keep').stat().st_ino
            (data/'remove').unlink();(data/'new').write_text('new')
            with patch.object(m.shutil,'copy2',wraps=m.shutil.copy2) as copy:
                m.build_worktree(data,work,config)
                self.assertEqual(copy.call_count,1)
            self.assertEqual((work/'data/keep').stat().st_ino,inode)
            with self.assertLogs(m.LOG,level='INFO') as logs:m.stage_snapshot(work,config)
            self.assertIn('3/3 paths',logs.output[-1])
            names=m.run_git(['diff','--cached','--name-only','-z'],cwd=work,config=config).stdout.split('\0')
            self.assertIn('data/remove',names);self.assertIn('data/new',names);self.assertNotIn('data/keep',names)
    def test_large_tool_chunks_reused_then_replaced_on_same_size_edit(self):
        m=load_storage();config=make_config(m)
        with tempfile.TemporaryDirectory() as td,patch.object(m,'GIT_BLOB_CHUNK_BYTES',16):
            root=Path(td);data=root/'source';data.mkdir();work=root/'repo';work.mkdir()
            source=data/'tool';source.write_bytes(b'A'*80)
            first=m.build_worktree(data,work,config);part=first['large_files']['tool']['parts'][0]
            inode=(work/'data'/part).stat().st_ino
            with patch.object(m,'_hash_file',side_effect=AssertionError('unchanged tool should not be reread')):
                m.build_worktree(data,work,config)
            self.assertEqual((work/'data'/part).stat().st_ino,inode)
            before=source.stat().st_mtime_ns;source.write_bytes(b'B'*80);os.utime(source,ns=(before,before))
            m.build_worktree(data,work,config);out=root/'restored';m.materialize(work,out,config)
            self.assertEqual((out/'tool').read_bytes(),b'B'*80)
    def test_legacy_snapshot_is_reused_by_content_without_source_cache(self):
        m=load_storage();config=make_config(m)
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);data=root/'source';data.mkdir();work=root/'repo';work.mkdir()
            (data/'file').write_text('same');m.build_worktree(data,work,config)
            (work/'hermes-source-signatures.json').unlink()
            inode=(work/'data/file').stat().st_ino
            m.build_worktree(data,work,config)
            self.assertEqual((work/'data/file').stat().st_ino,inode)
