import hashlib
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from test_git_storage import load_storage

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('repair_npm', ROOT / 'scripts/repair-node-npm.py')
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)


class NodeNpmTests(unittest.TestCase):
    def test_bundled_npm_survives_but_project_modules_stay_excluded(self):
        storage = load_storage()
        for path in ('tools/node-26.7.0-linux-x64/lib/node_modules',
                     'tools/node-26.7.0-linux-x64/lib/node_modules/npm/bin/npm-cli.js',
                     'tools/node-26.7.0-linux-x64/lib/node_modules/npm/node_modules/semver/index.js'):
            self.assertFalse(storage.is_excluded(path), path)
        for path in ('projects/node_modules/npm/bin/npm-cli.js',
                     'tools/node-26.7.0-linux-x64/lib/node_modules/other/index.js',
                     'tools/not-node/lib/node_modules/npm/bin/npm-cli.js'):
            self.assertTrue(storage.is_excluded(path), path)

    def test_verified_archive_restores_only_npm_and_creates_launcher_links(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w:xz') as tar:
            for name in ('npm-cli.js', 'npx-cli.js'):
                data = b'// npm fixture'
                info = tarfile.TarInfo('node-v26.7.0-linux-x64/lib/node_modules/npm/bin/' + name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        payload = archive.getvalue()
        checksum = hashlib.sha256(payload).hexdigest()
        def download(url, **kwargs):
            return io.BytesIO((checksum + '  node-v26.7.0-linux-x64.tar.xz\n').encode() if url.endswith('SHASUMS256.txt') else payload)
        with tempfile.TemporaryDirectory() as td:
            node = Path(td) / 'tools/node-26.7.0-linux-x64/bin/node'
            node.parent.mkdir(parents=True)
            node.write_bytes(b'preserve node binary')
            with patch.object(repair.urllib.request, 'urlopen', side_effect=download), patch.object(repair.subprocess, 'check_output', return_value='v26.7.0\n'), patch.object(repair.subprocess, 'run'):
                self.assertEqual(repair.repair(td), 1)
                self.assertEqual(repair.repair(td), 0)
            self.assertEqual(node.read_bytes(), b'preserve node binary')
            self.assertTrue((node.parent / 'npm').is_file())

    def test_checksum_failure_leaves_existing_files_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            node = Path(td) / 'tools/node-26.7.0-linux-x64/bin/node'
            node.parent.mkdir(parents=True)
            node.write_bytes(b'existing')
            def download(url, **kwargs):
                return io.BytesIO((('0' * 64) + '  node-v26.7.0-linux-x64.tar.xz\n').encode() if url.endswith('SHASUMS256.txt') else b'invalid')
            with patch.object(repair.urllib.request, 'urlopen', side_effect=download), patch.object(repair.subprocess, 'check_output', return_value='v26.7.0\n'):
                with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
                    repair.repair(td)
            self.assertEqual(node.read_bytes(), b'existing')
            self.assertFalse((node.parent.parent / 'lib/node_modules/npm').exists())
