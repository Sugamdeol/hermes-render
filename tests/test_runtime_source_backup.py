import importlib.util, json, os, subprocess, tempfile, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def load(name):
 s=importlib.util.spec_from_file_location('snapshot',ROOT/'scripts'/name);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
class RuntimeSourceTests(unittest.TestCase):
 def test_source_round_trip_and_corruption_refusal(self):
  helper=load('runtime-source-backup.py')
  with tempfile.TemporaryDirectory() as d:
   root=Path(d)/'src';home=Path(d)/'home';root.mkdir();home.mkdir()
   def git(*args):return subprocess.run(['git',*args],cwd=root,check=True,capture_output=True)
   git('init');git('config','user.email','t@t');git('config','user.name','T')
   (root/'app').write_text('base');(root/'deleted').write_text('old');git('add','.');git('commit','-m','base')
   (root/'app').write_text('custom');(root/'app').chmod(0o755);(root/'deleted').unlink()
   self.assertTrue(helper.capture(root,home));self.assertFalse(helper.capture(root,home))
   git('reset','--hard','HEAD');self.assertTrue(helper.restore(root,home))
   self.assertEqual((root/'app').read_text(),'custom');self.assertFalse((root/'deleted').exists())
   self.assertEqual((root/'app').stat().st_mode&0o777,0o755)
   snapshot=home/helper.NAME;data=json.loads(snapshot.read_text());data['files']['app']['sha256']='bad';snapshot.write_text(json.dumps(data))
   with self.assertRaisesRegex(RuntimeError,'checksum'):helper.restore(root,home)
 def test_full_file_mode_keeps_dependencies_and_logs(self):
  from unittest.mock import patch
  m=load('git-storage.py')
  with patch.dict(os.environ,{'GIT_STATE_INCLUDE_ALL_FILES':'1'}):
   for name in ['logs/a.log','home/.cache/pip/data','project/node_modules/package/index.js','project/.git/config']:
    self.assertFalse(m.is_excluded(name))
   self.assertTrue(m.is_excluded('.restore-quarantine/old'))
   self.assertIn('.hermes-runtime-source.json',m.SENSITIVE_FILES)
 def test_coverage_lists_external_links_and_special_files(self):
  helper=load('backup-coverage.py')
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'file').write_text('abc');(root/'outside').symlink_to('/etc/hosts');os.mkfifo(root/'pipe')
   helper.capture(root);report=json.loads((root/helper.NAME).read_text())
   self.assertEqual(report['regular_files'],1);self.assertEqual(report['regular_bytes'],3)
   self.assertEqual(report['external_links'],['outside']);self.assertEqual(report['special_files'],['pipe'])
 def test_portable_aliases_keep_secondary_workspace_visible(self):
  from unittest.mock import patch
  helper=load('portable-paths.py')
  with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{'HERMES_NOTEBOOK_HOST':'colab'}):
   home=Path(d)/'home';fs=Path(d)/'fs';home.mkdir();fs.mkdir()
   (home/'workspace').mkdir();(home/'shared-storage/workspace/project').mkdir(parents=True)
   (home/'shared-storage/workspace/project/file').write_text('saved')
   helper.apply(home,fs)
   self.assertEqual((fs/'storage/workspace/project/file').read_text(),'saved')
   self.assertEqual((fs/'mnt/hermes-shared/workspace/project/file').read_text(),'saved')
   self.assertTrue(helper.apply(home,fs))
