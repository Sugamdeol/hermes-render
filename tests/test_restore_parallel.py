import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('parallel_restore',ROOT/'scripts/git-storage.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class ParallelRestore(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.work=self.root/'work';self.source=self.work/'data';self.source.mkdir(parents=True)
        self.home=self.root/'restored';self.config=m.GitConfig('owner/repo','fixture',workdir=self.work)
        for i in range(24):(self.source/f'file{i}').write_bytes(b'content')
        (self.work/m.MANIFEST_NAME).write_text(json.dumps({}))

    def test_copies_overlap_but_stay_bounded_and_complete_before_return(self):
        original=m.copy_restored_plain_file;lock=threading.Lock();active=peak=finished=0
        def slow(*args):
            nonlocal active,peak,finished
            with lock:active+=1;peak=max(peak,active)
            try:time.sleep(.015);original(*args)
            finally:
                with lock:active-=1;finished+=1
        with patch.dict(os.environ,{'HERMES_RESTORE_COPY_WORKERS':'4'}),patch.object(m,'copy_restored_plain_file',side_effect=slow):
            count=m._materialize_tree(self.work,self.home,self.config)
        self.assertEqual(count,24);self.assertEqual(finished,24);self.assertEqual(active,0)
        self.assertGreater(peak,1);self.assertLessEqual(peak,4)
        self.assertEqual(len(list(self.home.glob('file*'))),24)

    def test_worker_failure_propagates_before_reconcile(self):
        with patch.dict(os.environ,{'HERMES_RESTORE_COPY_WORKERS':'4'}),patch.object(m,'copy_restored_plain_file',side_effect=RuntimeError('copy failed')),patch.object(m,'reconcile_restored_paths') as reconcile:
            with self.assertRaisesRegex(RuntimeError,'copy failed'):m._materialize_tree(self.work,self.home,self.config)
            reconcile.assert_not_called()

    def test_workers_retain_symlink_escape_protection(self):
        outside=self.root/'outside';outside.mkdir();(outside/'file').write_bytes(b'keep')
        self.home.mkdir();(self.home/'escape').symlink_to(outside,target_is_directory=True)
        (self.source/'escape').mkdir();(self.source/'escape/file').write_bytes(b'overwrite')
        with patch.dict(os.environ,{'HERMES_RESTORE_COPY_WORKERS':'4'}):
            with self.assertRaises(m.GitStateError):m._materialize_tree(self.work,self.home,self.config)
        self.assertEqual((outside/'file').read_bytes(),b'keep')
