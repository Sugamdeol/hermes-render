import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('small_restore', ROOT/'scripts/git-storage.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)


class SmallFiles(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); self.source = self.root/'source'
        self.source.write_bytes(b'new contents'); self.home = self.root/'home'

    def test_cold_copy_holds_one_verified_parent_until_publication(self):
        with patch.object(m, 'restore_parent', wraps=m.restore_parent) as parents:
            m.copy_restored_plain_file(self.source, self.home, 'deep/modules/pkg/file', 0o640)
        self.assertEqual(parents.call_count, 1)
        target = self.home/'deep/modules/pkg/file'
        self.assertEqual(target.read_bytes(), b'new contents')
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)

    def test_failed_write_keeps_old_file_and_cleans_temporary(self):
        self.home.mkdir(); target = self.home/'file'; target.write_bytes(b'old contents')
        with patch.object(m.shutil, 'copyfile', side_effect=OSError('disk error')):
            with self.assertRaises(m.GitStateError):
                m.copy_restored_plain_file(self.source, self.home, 'file', 0o600)
        self.assertEqual(target.read_bytes(), b'old contents')
        self.assertFalse(list(self.home.glob('.restore-*')))

    def test_source_directory_symlink_cannot_be_traversed(self):
        work = self.root/'work'; data = work/'data'; data.mkdir(parents=True)
        outside = self.root/'outside'; outside.mkdir(); (outside/'private').write_bytes(b'keep')
        (data/'escape').symlink_to(outside, target_is_directory=True)
        (work/m.MANIFEST_NAME).write_text('{}')
        config = m.GitConfig('owner/repo', 'fixture', workdir=work)
        with self.assertRaises(m.GitStateError): m.materialize(work, self.home, config)
        self.assertFalse((self.home/'escape/private').exists())
