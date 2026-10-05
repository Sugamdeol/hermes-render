import importlib.util
import os
import subprocess
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('storage_dependencies', Path(__file__).parents[1] / 'scripts/git-storage.py')
storage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(storage)

class StorageDependenciesTests(unittest.TestCase):
    def test_installed_packages_are_excluded_but_projects_are_kept(self):
        for path in ('home/.local/lib/python3.12/site-packages/playwright/driver/node', 'home/project/.venv/bin/python'):
            self.assertTrue(storage.is_excluded(path))
        self.assertFalse(storage.is_excluded('home/project/main.py'))
        self.assertFalse(storage.is_excluded('sessions/chat.json'))

    def test_orphaned_index_lock_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / '.git/index.lock'
            lock.parent.mkdir()
            lock.write_text('')
            storage.clean_stale_index_lock(Path(directory))
            self.assertFalse(lock.exists())

    def test_live_git_process_keeps_its_index_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / '.git/index.lock'
            lock.parent.mkdir()
            lock.write_text('')
            process = subprocess.Popen(['git', '5'], executable='/bin/sleep', cwd=directory)
            try:
                storage.clean_stale_index_lock(Path(directory))
                self.assertTrue(lock.exists())
            finally:
                process.terminate()
                process.wait()
