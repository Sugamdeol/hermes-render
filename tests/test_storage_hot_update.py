import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT/file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StorageHotUpdateTests(unittest.TestCase):
    def setUp(self):
        self.update = load('hot_storage', 'update-storage.py')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.update.PROC = Path(self.tmp.name)
        self.agent = SimpleNamespace(process=SimpleNamespace(pid=10, poll=lambda:None))

    def process(self, pid, parent, args, birth='1234', state='S'):
        path = self.update.PROC/str(pid)
        path.mkdir(exist_ok=True)
        (path/'stat').write_text(f'{pid} (worker) ' + ' '.join([state, str(parent)]+['0']*17+[birth]))
        (path/'cmdline').write_bytes(b'\0'.join(arg.encode() for arg in args))

    def test_only_backup_worker_is_selected_and_managed_replacement_is_supported(self):
        self.process(10, 1, ['gateway-supervisor.sh'])
        self.process(20, 10, ['/opt/hermes/.venv/bin/python', '/opt/render-tools/memory-log.py'])
        self.process(30, 10, ['hermes', 'gateway', 'run'])
        self.process(40, 10, ['hermes', 'dashboard'])
        self.assertEqual(self.update.storage_worker(self.agent), (20, '1234'))
        self.process(20, 10, ['exited'], state='Z')
        self.process(50, 1, ['/opt/hermes/.venv/bin/python', '/opt/render-tools/memory-log.py'])
        self.agent.storage_process = SimpleNamespace(pid=50, poll=lambda:None)
        self.assertEqual(self.update.storage_worker(self.agent), (50, '1234'))

    def test_ambiguous_workers_are_not_stopped(self):
        for pid in (20, 21):
            self.process(pid, 10, ['python', '/opt/render-tools/memory-log.py'])
        with self.assertRaisesRegex(RuntimeError, 'exactly one'):
            self.update.storage_worker(self.agent)

    def test_pid_reuse_is_not_signalled(self):
        self.process(20, 10, ['worker'], birth='9999')
        with patch.object(self.update.os, 'kill') as kill:
            with self.assertRaisesRegex(RuntimeError, 'process changed'):
                self.update.wait_for_old_worker(20, '1234', Path('/state'))
            kill.assert_not_called()

    def test_waits_for_reparented_git_child_after_old_worker_exits(self):
        self.process(20, 10, ['worker'])
        first = {20:(10, '1234', ['worker']), 30:(20, '2222', ['git', 'push'])}
        second = {30:(1, '2222', ['git', 'push'])}
        with patch.object(self.update, 'processes', side_effect=[first, second, {}]), patch.object(self.update.os, 'kill') as kill, patch.object(self.update.time, 'sleep') as sleep:
            self.update.wait_for_old_worker(20, '1234', Path('/state'))
            self.assertEqual(sleep.call_count, 2)
            kill.assert_called_once_with(20, self.update.signal.SIGTERM)

    def test_existing_stop_control_includes_replacement_worker_and_restores_global(self):
        namespace = {'descendants':lambda pid:{pid:str(pid)}}
        exec('class Agent:\n def stop(self, force=False):\n  return descendants(self.process.pid)\n', namespace)
        agent = namespace['Agent']()
        agent.process = SimpleNamespace(pid=10)
        agent.storage_process = SimpleNamespace(pid=50, poll=lambda:None)
        original = namespace['descendants']
        self.update.attach_stop(agent)
        self.update.attach_stop(agent)
        self.assertEqual(agent.stop(), {10:'10', 50:'50'})
        self.assertIs(namespace['descendants'], original)

    def test_stopped_app_does_not_download_or_modify_files(self):
        self.agent.process.poll = lambda:0
        with patch.object(self.update, 'download') as download:
            with self.assertRaisesRegex(RuntimeError, 'not running'):
                self.update.main(self.agent)
            download.assert_not_called()


if __name__ == '__main__':
    unittest.main()
