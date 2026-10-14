import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('parallel_launcher', ROOT / 'run-colab.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class ParallelStartTests(unittest.TestCase):
    def test_fetch_starts_during_installation_and_is_finished_before_restore(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            script = root / 'fetch.py'
            script.write_text('import pathlib,time\npathlib.Path(__file__).with_suffix(".started").touch()\ntime.sleep(0.1)\npathlib.Path(__file__).with_suffix(".done").touch()\n')
            env = dict(os.environ, GIT_STATE_TOKEN='test', GIT_STATE_REPO='owner/repo')
            with patch.object(launcher, 'SOURCE', root/'source'):
                with launcher.StatePrefetch(env, script) as fetch:
                    deadline = time.monotonic()+5
                    while not script.with_suffix('.started').exists() and time.monotonic()<deadline:
                        time.sleep(0.01)
                    self.assertTrue(script.with_suffix('.started').exists())
                    self.assertIsNone(fetch.process.poll())
                    self.assertNotIn('test', ' '.join(fetch.process.args))
                self.assertEqual(fetch.process.poll(), 0)
                self.assertTrue(script.with_suffix('.done').exists())
                self.assertEqual((root/'hermes-state-prefetch.log').stat().st_mode & 0o777, 0o600)

    def test_install_failure_terminates_fetch_before_leaving_context(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            script = root/'fetch.py'
            script.write_text('import time\ntime.sleep(60)\n')
            with patch.object(launcher, 'SOURCE', root/'source'):
                with self.assertRaisesRegex(RuntimeError, 'installation failed'):
                    with launcher.StatePrefetch(dict(os.environ, GIT_STATE_TOKEN='test', GIT_STATE_REPO='owner/repo'), script) as fetch:
                        raise RuntimeError('installation failed')
                self.assertIsNotNone(fetch.process.poll())
                self.assertTrue(fetch.log.closed)

    def test_missing_credentials_do_not_start_download(self):
        with patch.object(launcher.subprocess, 'Popen') as popen:
            with launcher.StatePrefetch({}, Path('unused')):
                pass
            popen.assert_not_called()

    def test_prefetch_failure_is_checked_again_by_normal_restore(self):
        process = Mock()
        process.wait.return_value = 1
        process.poll.return_value = 1
        with tempfile.TemporaryDirectory() as td, patch.object(launcher, 'SOURCE', Path(td)/'source'), patch.object(launcher.subprocess, 'Popen', return_value=process):
            with launcher.StatePrefetch({'GIT_STATE_TOKEN':'test', 'GIT_STATE_REPO':'owner/repo'}, Path('fetch')):
                pass
            self.assertEqual(process.wait.call_count, 1)


if __name__ == '__main__':
    unittest.main()
