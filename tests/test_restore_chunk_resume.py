import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('resume_storage',ROOT/'scripts/git-storage.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class ChunkResume(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name)
        self.data=self.root/'chunks';self.data.mkdir();self.cache=self.root/'cache'
        names=[]
        for i,payload in enumerate((b'a'*16,b'b'*16,b'c'*16)):
            name=f'{m.CHUNK_DIRECTORY}/fixture/{i:06d}';p=self.data/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(payload);names.append(name)
        self.record={'parts':names,'size':48,'sha256':hashlib.sha256(b'a'*16+b'b'*16+b'c'*16).hexdigest(),'mode':0o640}

    def test_interrupted_assembly_resumes_and_never_skips_integrity(self):
        original=m._restore_cache_json
        def interrupted(path,value):
            original(path,value)
            if value.get('next_part')==1:raise RuntimeError('interrupted')
        with patch.object(m,'_restore_cache_json',side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError,'interrupted'):
                m.assemble_restore_blob(self.data,self.record,self.cache,'state.bin')
        checkpoint=next(self.cache.glob('*/checkpoint.json'))
        self.assertEqual(json.loads(checkpoint.read_text())['next_part'],1)
        # Completed chunks need not be read from their source again.
        original_open=Path.open
        first=self.data/self.record['parts'][0]
        def check_open(path,*args,**kwargs):
            if path==first and args and args[0]=='rb':raise AssertionError('re-read completed source chunk')
            return original_open(path,*args,**kwargs)
        with patch.object(Path,'open',check_open):
            result=m.assemble_restore_blob(self.data,self.record,self.cache,'state.bin')
        self.assertEqual(result.read_bytes(),b'a'*16+b'b'*16+b'c'*16)
        self.assertEqual(result.stat().st_mode&0o777,0o640)

    def test_verified_cache_is_reused_without_source_reassembly(self):
        first=m.assemble_restore_blob(self.data,self.record,self.cache,'state.bin')
        original_open=Path.open
        def check_open(path,*args,**kwargs):
            if path in [self.data/n for n in self.record['parts']] and args and args[0]=='rb':raise AssertionError('reassembled cache')
            return original_open(path,*args,**kwargs)
        with patch.object(Path,'open',check_open):
            self.assertEqual(m.assemble_restore_blob(self.data,self.record,self.cache,'state.bin'),first)

    def test_tampered_cache_is_not_trusted(self):
        first=m.assemble_restore_blob(self.data,self.record,self.cache,'state.bin')
        first.write_bytes(b'z'*48)
        result=m.assemble_restore_blob(self.data,self.record,self.cache,'state.bin')
        self.assertEqual(result.read_bytes(),b'a'*16+b'b'*16+b'c'*16)

    def test_corrupt_chunks_and_bad_declared_size_are_rejected(self):
        with self.assertRaises(m.GitStateError):
            m.assemble_restore_blob(self.data,{**self.record,'size':100},self.cache,'state.bin')
        (self.data/self.record['parts'][1]).write_bytes(b'z'*16)
        with self.assertRaises(m.GitStateError):
            m.assemble_restore_blob(self.data,self.record,self.cache,'state.bin')
        self.assertFalse(list(self.cache.glob('*/verified')))

    def test_symlink_chunk_parent_is_rejected(self):
        (self.data/'alias').symlink_to(self.data/m.CHUNK_DIRECTORY,target_is_directory=True)
        record={**self.record,'parts':['alias/fixture/000000']}
        with self.assertRaises(m.GitStateError):m.assemble_restore_blob(self.data,record,self.cache,'state.bin')

    def test_same_chunks_for_two_logical_files_keep_both_sources(self):
        first=m.assemble_restore_blob(self.data,self.record,self.cache,'first.bin')
        second=m.assemble_restore_blob(self.data,self.record,self.cache,'second.bin')
        self.assertNotEqual(first,second)
        self.assertEqual(first.read_bytes(),second.read_bytes())
