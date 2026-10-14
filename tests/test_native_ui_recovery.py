import importlib.util
import os
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location('native_ui_recovery_test', ROOT/'scripts/native-ui-task-recovery.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NativeUIRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.helper = load()
        self.markers = SimpleNamespace(_marker_path=lambda home:Path(home)/'markers', _load=lambda path:{}, marker_writer_state=lambda row:row.get('writer_state', 'unknown'))
        package = ModuleType('hermes_cli')
        package.render_recovery = SimpleNamespace(session_concurrency=lambda:10)
        tui = ModuleType('tui_gateway')
        tui.turn_marker = self.markers
        transport = ModuleType('tui_gateway.transport')
        self.peer = SimpleNamespace(closed=False)
        transport.current_transport = lambda:self.peer
        modules = patch.dict(sys.modules, {'hermes_cli':package, 'tui_gateway':tui, 'tui_gateway.transport':transport})
        modules.start()
        self.addCleanup(modules.stop)
        env = patch.dict(os.environ, {'HERMES_RUNTIME_PROFILE':'colab-full', 'HERMES_RESUME_ALL_INTERRUPTED':'1'})
        env.start()
        self.addCleanup(env.stop)
        self.server = SimpleNamespace(_auto_continue_config=lambda:(True, 900, 2), _hermes_home='/test-home', _sessions={}, _get_db=lambda:SimpleNamespace(get_session=lambda key:{'id':key}), _start_session_work=Mock(return_value=object()), _methods={'client.capabilities':lambda rid,params:{'result':{}}, 'session.resume':Mock(return_value={'result':{'session_id':'live', 'auto_continue':{'attempt':1}}})})

    def test_only_dead_writer_markers_with_unfinished_prompts_are_candidates(self):
        rows = {'dead':{'writer_state':'dead', 'prompt':'unfinished'}, 'live':{'writer_state':'alive', 'prompt':'running'}, 'unknown':{'prompt':'uncertain'}, 'disabled':{'writer_state':'dead', 'prompt':'no', 'auto_continue':False}, 'empty':{'writer_state':'dead', 'prompt':''}}
        self.markers._load = lambda path:rows
        self.assertEqual([key for key,row in self.helper.marker_candidates('/home', self.markers)], ['dead'])

    def test_reconnect_starts_one_scan_only_after_server_request_capability(self):
        self.assertTrue(self.helper.install(self.server))
        self.assertFalse(self.helper.install(self.server))
        self.server._methods['client.capabilities'](1, {'server_requests':False})
        self.server._start_session_work.assert_not_called()
        self.server._methods['client.capabilities'](2, {'server_requests':True})
        self.server._methods['client.capabilities'](3, {'server_requests':True})
        self.server._start_session_work.assert_called_once()
        self.assertEqual(self.server._auto_continue_config(), (True, float('inf'), 10))

    def test_deleted_session_does_not_recreate_a_chat(self):
        self.server._get_db = lambda:SimpleNamespace(get_session=lambda key:None)
        self.assertFalse(self.helper.resume_one(self.server, 'deleted'))
        self.server._methods['session.resume'].assert_not_called()

    def test_native_resume_is_used_and_capacity_refusal_remains_retryable(self):
        self.assertTrue(self.helper.resume_one(self.server, 'saved-id'))
        self.server._methods['session.resume'].assert_called_with('__colab_recovery__', {'session_id':'saved-id', 'omit_messages':True})
        self.server._methods['session.resume'].return_value = {'result':{'session_id':'live'}}
        self.assertFalse(self.helper.resume_one(self.server, 'saved-id'))

    def test_disabled_auto_continue_setting_is_preserved(self):
        self.server._auto_continue_config = lambda:(False, 900, 2)
        self.helper.install(self.server)
        self.assertFalse(self.server._auto_continue_config()[0])
        self.server._methods['client.capabilities'](1, {'server_requests':True})
        self.server._start_session_work.assert_not_called()

    def test_bulk_ui_resume_queues_all_markers_with_ten_workers(self):
        gate, ten_started, all_started = threading.Event(), threading.Event(), threading.Event()
        lock = threading.Lock()
        counts = {'active':0, 'peak':0, 'started':0}
        threads = []
        def spawn(target, **kwargs):
            thread = threading.Thread(target=target, daemon=True)
            threads.append(thread)
            thread.start()
            return thread
        def resume(server, key):
            with lock:
                counts['active'] += 1
                counts['started'] += 1
                counts['peak'] = max(counts['peak'], counts['active'])
                if counts['started'] == 10: ten_started.set()
                if counts['started'] == 15: all_started.set()
            gate.wait(5)
            with lock: counts['active'] -= 1
        self.server._start_session_work = spawn
        self.helper.time = SimpleNamespace(sleep=lambda seconds:(all_started.wait(5), setattr(self.peer, 'closed', True)))
        with patch.object(self.helper, 'marker_candidates', return_value=[(str(i), {}) for i in range(15)]), patch.object(self.helper, 'resume_one', side_effect=resume):
            self.helper.install(self.server)
            self.server._methods['client.capabilities'](1, {'server_requests':True})
            try:
                self.assertTrue(ten_started.wait(5))
                self.assertEqual(counts['active'], 10)
            finally:
                gate.set()
            self.assertTrue(all_started.wait(5))
            for thread in threads: thread.join(2)
        self.assertEqual(counts['started'], 15)
        self.assertEqual(counts['peak'], 10)


if __name__ == '__main__':
    unittest.main()
