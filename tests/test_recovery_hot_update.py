import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class HotRecoveryUpdateTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('hot_recovery', ROOT/'update-task-recovery.py')
        self.update = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.update)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.update.PROC = Path(self.tmp.name)
        self.agent = SimpleNamespace(process=SimpleNamespace(pid=10, poll=lambda:None))
        self.process(10, 1, ['sh', '/opt/render-tools/gateway-supervisor.sh'])

    def process(self, pid, parent, args, started='1234'):
        directory = self.update.PROC / str(pid)
        directory.mkdir(exist_ok=True)
        fields = ['S', str(parent)] + ['0']*17 + [started]
        (directory/'stat').write_text(f'{pid} (worker) ' + ' '.join(fields))
        (directory/'cmdline').write_bytes(b'\0'.join(arg.encode() for arg in args))

    def test_only_supervised_gateway_is_selected(self):
        self.process(20, 10, ['/opt/hermes/.venv/bin/hermes', 'gateway', 'run'])
        self.process(30, 10, ['/opt/hermes/.venv/bin/hermes', 'dashboard'])
        self.process(40, 10, ['/opt/hermes/.venv/bin/python', '/opt/render-tools/memory-log.py'])
        self.process(50, 1, ['/opt/hermes/.venv/bin/hermes', 'gateway', 'run'])
        self.assertEqual(self.update.gateway_worker(self.agent).pid, 20)

    def test_ambiguous_processes_are_not_stopped(self):
        for pid in (20, 21):
            self.process(pid, 10, ['/opt/hermes/.venv/bin/hermes', 'gateway', 'run'])
        with self.assertRaisesRegex(RuntimeError, 'exactly one'):
            self.update.gateway_worker(self.agent)

    def test_pid_reuse_does_not_kill_new_process(self):
        self.process(20, 10, ['/opt/hermes/.venv/bin/hermes', 'gateway', 'run'])
        worker = self.update.gateway_worker(self.agent)
        self.process(20, 10, ['unrelated'], started='9999')
        with patch.object(self.update.os, 'kill') as kill:
            with self.assertRaisesRegex(RuntimeError, 'process changed'):
                worker.terminate()
            kill.assert_not_called()

    def test_stopped_launcher_does_not_install_or_restart(self):
        self.agent.process.poll = lambda:0
        with patch.object(self.update, 'download') as download:
            with self.assertRaisesRegex(RuntimeError, 'not running'):
                self.update.main(self.agent)
            download.assert_not_called()
