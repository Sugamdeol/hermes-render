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
   previous_mtime=(root/'app').stat().st_mtime_ns
   helper.restore(root,home)
   self.assertEqual((root/'app').stat().st_mtime_ns,previous_mtime)
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

class WriterGuardTests(unittest.TestCase):
 def test_disabled_compaction_and_missing_mount(self):
  from unittest.mock import patch
  m=load('git-storage.py')
  with patch.dict(os.environ,{'GIT_STATE_REPO':'owner/repo','GIT_STATE_TOKEN':'test','GIT_STATE_MAX_COMMITS':'0','GIT_STATE_REQUIRED_MOUNT':'/missing'}):
   self.assertEqual(m.GitConfig.from_env().max_commits,0)
   with patch.object(m.os.path,'ismount',return_value=False),self.assertRaisesRegex(RuntimeError,'not mounted'):m.require_data_mount()
 def test_duplicate_local_writer_is_rejected(self):
  m=load('git-storage.py')
  with tempfile.TemporaryDirectory() as d:
   c=m.GitConfig(repo='owner/repo',token='test',workdir=Path(d))
   first=m._exclusive_storage_operation(c,'snapshot')
   self.assertIsNotNone(first)
   try:self.assertIsNone(m._exclusive_storage_operation(c,'snapshot'))
   finally:first.close()
   next_guard=m._exclusive_storage_operation(c,'snapshot')
   self.assertIsNotNone(next_guard);next_guard.close()

class NetworkMetadataRetryTests(unittest.TestCase):
 def test_transient_errors_retry_but_permission_errors_fail(self):
  from unittest.mock import Mock, patch
  import errno
  m=load('backup-coverage.py')
  operation=Mock(side_effect=[OSError(errno.EIO,'temporary network error'),'ok'])
  with patch.object(m.time,'sleep') as sleep:
   self.assertEqual(m.retry_io(operation),'ok');self.assertEqual(operation.call_count,2);sleep.assert_called_once()
  operation=Mock(side_effect=PermissionError(errno.EACCES,'unreadable'))
  with patch.object(m.time,'sleep') as sleep,self.assertRaises(PermissionError):m.retry_io(operation)
  sleep.assert_not_called()

 def test_backup_mirror_mount_drop_blocks_audit(self):
  from unittest.mock import patch
  m=load('backup-coverage.py')
  with tempfile.TemporaryDirectory() as d:
   home=Path(d)/'home';home.mkdir();marker=Path(d)/'required-mounts.json'
   marker.write_text(json.dumps({'version':1,'data_root':str(home.resolve()),'required':['/expected-storage']}))
   with patch.dict(os.environ,{'GIT_STATE_REQUIRED_MOUNTS_FILE':str(marker)}),patch.object(m.os.path,'ismount',return_value=False):
    with self.assertRaisesRegex(OSError,'not mounted'):m.capture(home)
   self.assertFalse((home/m.NAME).exists())

class CrossFilesystemMirrorTests(unittest.TestCase):
 def test_journal_preserves_and_restores_a_file_on_another_volume(self):
  from unittest.mock import patch
  import errno
  m=load('incremental-mirror.py')
  with tempfile.TemporaryDirectory() as d:
   work=Path(d);cache=work/'.git';cache.mkdir();target=work/'data';target.mkdir();file=target/'file';file.write_text('old')
   transaction=m.Transaction(work,cache,target,[])
   with patch.object(m.os,'link',side_effect=OSError(errno.EXDEV,'different volume')):
    transaction.write(file,lambda p:p.write_text('new'))
   self.assertEqual(file.read_text(),'new')
   replace=m.os.replace
   def across(source,dest):
    if Path(source).parent==transaction.folder:raise OSError(errno.EXDEV,'different volume')
    return replace(source,dest)
   with patch.object(m.os,'replace',side_effect=across):transaction.finish(False)
   self.assertEqual(file.read_text(),'old');self.assertFalse(transaction.folder.exists())

class HostOverlayOrderTests(unittest.TestCase):
 def test_instabox_hook_runs_after_source_restore_and_is_host_gated(self):
  source=(ROOT/'scripts/bootstrap.sh').read_text()
  restore=source.index("runtime-source-backup.py')['restore']")
  hook=source.index('"${DATA_DIR}/bin/ensure-instabox-start.py"')
  self.assertLess(restore,hook)
  self.assertIn('[ "${HERMES_NOTEBOOK_HOST:-}" = "instabox" ]',source[restore:hook])
