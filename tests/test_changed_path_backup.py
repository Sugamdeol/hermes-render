import json
import os
from pathlib import Path
import runpy
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from test_git_storage import load_storage, make_config

HELPER = Path(__file__).resolve().parents[1]/'scripts/incremental-mirror.py'

class ChangedPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root/'home'; self.data.mkdir()
        self.work = self.root/'repo'; self.work.mkdir()
        self.m = load_storage(); self.config = make_config(self.m, workdir=self.work)
        self.env = patch.dict(os.environ, {'HERMES_RUNTIME_PROFILE':'colab-full', 'GIT_STATE_STATUS_FILE':str(self.root/'status.json')})
        self.env.start(); self.addCleanup(self.env.stop)
        self.h = runpy.run_path(str(HELPER))
    def put(self, name, content='saved'):
        path = self.data/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_text(content); return path
    def save(self, changes=None):
        self.config._source_changes=changes
        return self.m.build_worktree(self.data,self.work,self.config)
    def test_delta_does_not_walk_hash_copy_or_link_unchanged_files(self):
        for i in range(2000): self.put(f'tools/{i}')
        self.put('chat'); self.save(); inode=(self.work/'data/tools/0').stat().st_ino
        self.put('chat','updated'); self.put('new-chat'); (self.data/'tools/1999').unlink()
        with patch.object(self.m,'plan_tree',side_effect=AssertionError('full tree scan')), patch.object(self.m,'_hash_file',side_effect=AssertionError('unchanged hash')), patch.object(self.m.os,'link',side_effect=AssertionError('unchanged hardlink')), patch.object(self.m.shutil,'copy2',wraps=self.m.shutil.copy2) as copy:
            # Journal links are allowed for touched files, never unchanged files.
            self.m.os.link.side_effect = lambda a,b: os_link(a,b)
            self.save(['chat','new-chat','tools/1999'])
            self.assertEqual(copy.call_count,2)
        self.assertEqual((self.work/'data/tools/0').stat().st_ino,inode)
        self.assertFalse((self.work/'data/tools/1999').exists())
        self.assertEqual((self.work/'data/chat').read_text(),'updated')
    def test_directory_rename_delete_new_subtree_and_empty_directory(self):
        self.put('old/nested/chat'); self.save()
        (self.data/'old').rename(self.data/'renamed')
        self.save(['old','renamed'])
        self.assertFalse((self.work/'data/old').exists())
        self.assertEqual((self.work/'data/renamed/nested/chat').read_text(),'saved')
        shutil.rmtree(self.data/'renamed'); (self.data/'empty').mkdir(); result=self.save(['renamed','empty'])
        self.assertEqual(result['files'],0); self.assertIn('empty',result['empty_dirs'])
    def test_wal_write_snapshots_database_without_waiting_for_connection_close(self):
        db=sqlite3.connect(self.data/'state.db'); self.addCleanup(db.close)
        db.execute('PRAGMA journal_mode=WAL'); db.execute('CREATE TABLE chat (text)'); db.commit(); self.save()
        db.execute("INSERT INTO chat VALUES ('new')"); db.commit()
        self.save(['state.db-wal','state.db-shm'])
        self.assertFalse((self.work/'data/state.db-wal').exists())
        restored=self.root/'restore'; self.m.materialize(self.work,restored,self.config)
        with sqlite3.connect(restored/'state.db') as copy:
            self.assertEqual(copy.execute('SELECT text FROM chat').fetchall(),[('new',)])
    def test_failure_rolls_back_prior_files_and_inventory(self):
        self.put('a','old'); self.put('b','old'); self.save()
        manifest=(self.work/'MANIFEST.json').read_bytes(); index=(self.work/'hermes-source-inventory.json').read_bytes()
        self.put('a','new'); self.put('b','new')
        original=self.m.shutil.copy2
        def copy(source,target,*a,**kw):
            if source==self.data/'b': raise PermissionError('blocked')
            return original(source,target,*a,**kw)
        with patch.object(self.m.shutil,'copy2',side_effect=copy):
            with self.assertRaises(self.m.GitStateError): self.save(['a','b'])
        self.assertEqual((self.work/'data/a').read_text(),'old')
        self.assertEqual((self.work/'MANIFEST.json').read_bytes(),manifest)
        self.assertEqual((self.work/'hermes-source-inventory.json').read_bytes(),index)
    def test_crash_journal_recovers_before_next_snapshot(self):
        self.put('chat','old'); self.save()
        tx=self.h['Transaction'](self.work,self.work,self.work/'data',[self.work/'MANIFEST.json'])
        tx.write(self.work/'data/chat',lambda p:p.write_text('incomplete')); tx.index.close()
        self.save([])
        self.assertEqual((self.work/'data/chat').read_text(),'old')
    def test_github_roundtrip_includes_nested_git_and_ignored_files(self):
        self.m.run_git(['init','-q'],cwd=self.work,config=self.config)
        project=self.data/'project'; project.mkdir(); self.m.run_git(['init','-q'],cwd=project,config=self.config)
        self.put('project/.gitignore','ignored.txt\n'); self.put('project/ignored.txt','important')
        self.put('project/.hermes-dotgit','literal'); self.put('project/.hermes-dotgit.escape-61','literal2')
        self.save(); self.m.stage_snapshot(self.work,self.config)
        self.m.run_git(['commit','-q','-m','backup'],cwd=self.work,config=self.config)
        clone=self.root/'clone'; self.m.run_git(['clone','-q',str(self.work),str(clone)],config=self.config)
        restored=self.root/'restored'; self.m.materialize(clone,restored,self.config)
        self.assertEqual((restored/'project/ignored.txt').read_text(),'important')
        self.assertEqual((restored/'project/.git/config').read_text(),(project/'.git/config').read_text())
        self.assertEqual((restored/'project/.hermes-dotgit').read_text(),'literal')
        self.assertEqual((restored/'project/.hermes-dotgit.escape-61').read_text(),'literal2')
    def test_chunks_reused_then_deleted_on_delta(self):
        with patch.object(self.m,'GIT_BLOB_CHUNK_BYTES',16):
            self.put('large','X'*100); first=self.save(); part=first['large_files']['large']['parts'][0]
            inode=(self.work/'data'/part).stat().st_ino
            self.save(['large']); self.assertEqual((self.work/'data'/part).stat().st_ino,inode)
            self.put('large','small'); result=self.save(['large'])
            self.assertFalse(result['large_files']); self.assertFalse((self.work/'data'/part).exists())
    def test_deleted_rotating_file_during_copy_does_not_block_sessions(self):
        self.put('chat'); rotating=self.put('rotating'); self.save(); rotating.write_text('new')
        original=self.m.shutil.copy2
        def copy(source,target,*a,**kw):
            if source==rotating: source.unlink(); raise FileNotFoundError('rotation')
            return original(source,target,*a,**kw)
        with patch.object(self.m.shutil,'copy2',side_effect=copy): self.save(['rotating'])
        self.assertFalse((self.work/'data/rotating').exists()); self.assertTrue((self.work/'data/chat').exists())
    def test_encrypted_database_and_ordinary_enc_file_never_collide(self):
        self.config.env_mode = self.m.ENV_MODE_ENCRYPT
        with patch.dict(os.environ, {'STORAGE_ENCRYPTION_KEY':'test-secret'}):
            with sqlite3.connect(self.data/'chat.db') as db:
                db.execute('CREATE TABLE chat (text)'); db.execute("INSERT INTO chat VALUES ('saved')")
            self.put('chat.db.enc','ordinary encrypted artifact')
            self.put('.env','KEY=saved'); self.put('.env.enc','ordinary env artifact')
            self.save(); self.save(['chat.db'])
            # Exercise migration from a physical tree when the inventory is lost.
            (self.work/'hermes-source-inventory.json').unlink()
            self.save()
            out = self.root/'out'; self.m.materialize(self.work,out,self.config)
            self.assertEqual((out/'chat.db.enc').read_text(),'ordinary encrypted artifact')
            self.assertEqual((out/'.env.enc').read_text(),'ordinary env artifact')
            self.assertEqual((out/'.env').read_text(),'KEY=saved')
            with sqlite3.connect(out/'chat.db') as db:
                self.assertEqual(db.execute('SELECT text FROM chat').fetchall(),[('saved',)])

    def test_sensitive_modes_and_symlink_permissions_changes(self):
        self.config.env_mode=self.m.ENV_MODE_PLAINTEXT
        self.put('.env','TOKEN=saved'); path=self.put('run'); self.save()
        os.chmod(path,0o700); (self.data/'link').symlink_to(path)
        result=self.save(['run','link'])
        self.assertEqual(result['modes']['run'],'700'); self.assertEqual((self.work/'data/.env').stat().st_mode&0o777,0o600)
        out=self.root/'out';self.m.materialize(self.work,out,self.config)
        self.assertTrue((out/'link').is_symlink()); self.assertEqual((out/'run').stat().st_mode&0o777,0o700)

os_link=os.link
