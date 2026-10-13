import hashlib
import importlib.util
import io
from pathlib import Path
import tarfile
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_git_storage import load_storage

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('repair_npm', ROOT / 'scripts/repair-node-npm.py')
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)


class NodeNpmTests(unittest.TestCase):
    def test_managed_missing_npm_uses_working_named_install_then_rechecks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'pm').mkdir()
            (root / 'pm/cli.py').touch()
            results = [subprocess.CompletedProcess([], 10), subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0)]
            with patch.object(repair.subprocess, 'run', side_effect=results) as run:
                self.assertEqual(repair.repair_managed(root / 'data', root), 1)
                self.assertEqual(run.call_args_list[1].args[0][1:], ['-m', 'pm.cli', 'install', 'node', 'npm'])
                self.assertEqual(run.call_args_list[1].kwargs['env']['HERMES_HOME'], str(root / 'data'))
                self.assertEqual(run.call_count, 3)

    def test_managed_healthy_skips_install(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'pm').mkdir()
            (root / 'pm/cli.py').touch()
            with patch.object(repair.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
                self.assertEqual(repair.repair_managed(root, root), 0)
                self.assertEqual(run.call_count, 1)

    def test_managed_install_failure_stops_startup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'pm').mkdir()
            (root / 'pm/cli.py').touch()
            with patch.object(repair.subprocess, 'run', side_effect=[subprocess.CompletedProcess([], 10), subprocess.CalledProcessError(1, ['pm'])]):
                with self.assertRaises(subprocess.CalledProcessError):
                    repair.repair_managed(root, root)

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
