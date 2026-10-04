"""The merged diagnostics process must preserve storage shutdown semantics."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


class StorageShutdownTests(unittest.TestCase):
    def test_signal_reaches_embedded_storage_finalizer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy(Path(__file__).parents[1] / 'scripts/memory-log.py', root)
            (root / 'agent-budget.py').write_text("""
import time
STATUS = {'heartbeat': 0, 'mode': 'observe', 'workers': 0, 'rss': 0}
def monitor():
    while True:
        STATUS['heartbeat'] = time.monotonic()
        time.sleep(0.1)
def container_memory():
    return 0, 0
""")
            (root / 'git-storage.py').write_text("""
from pathlib import Path
class GitConfig:
    @classmethod
    def from_env(cls):
        return cls()
def run_daemon(data_dir, config, stop=None):
    (data_dir / 'started').touch()
    stop.wait()
    (data_dir / 'final-push').touch()
""")
            proc = subprocess.Popen([sys.executable, str(root / 'memory-log.py')],
                env={**os.environ, 'HERMES_GIT_SYNC_IN_PROCESS': '1', 'HERMES_HOME': directory},
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not (root / 'started').exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue((root / 'started').exists())
                proc.terminate()
                _, errors = proc.communicate(timeout=5)
                self.assertEqual(proc.returncode, 0, errors.decode())
                self.assertTrue((root / 'final-push').exists())
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.communicate()
