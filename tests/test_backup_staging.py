import subprocess
import tempfile
import unittest
from pathlib import Path
from test_git_storage import load_storage,make_config

class BackupStagingTests(unittest.TestCase):
    def test_batched_add_preserves_deletions_and_literal_filenames(self):
        m=load_storage();config=make_config(m)
        with tempfile.TemporaryDirectory() as td:
            work=Path(td);subprocess.run(['git','init','-q',str(work)],check=True)
            data=work/'data';data.mkdir();(data/'removed').write_text('old')
            m.run_git(['add','-A'],cwd=work,config=config)
            m.run_git(['commit','-q','-m','old'],cwd=work,config=config)
            (data/'removed').unlink()
            names=[':(glob)*','line\nbreak','Hindi-हिन्दी','-leading']
            for name in names:(data/name).write_text('keep')
            for n in range(2101):(data/f'{n:04d}').write_text(str(n))
            with self.assertLogs(m.LOG,level='INFO') as logs:m.stage_snapshot(work,config)
            self.assertGreaterEqual(len(logs.output),2)
            staged=m.run_git(['diff','--cached','--name-only','-z'],cwd=work,config=config).stdout.split('\0')
            for name in names+['removed']:self.assertIn('data/'+name,staged)
            m.run_git(['commit','-q','-m','new'],cwd=work,config=config)
            self.assertFalse(m.run_git(['status','--porcelain'],cwd=work,config=config).stdout)
