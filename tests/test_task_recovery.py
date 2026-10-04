import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).parents[1] / 'scripts/task-recovery.py'


class Store:
    def __init__(self, entry):
        self._lock = threading.Lock()
        self._entries = {entry.session_key: entry}
    def _ensure_loaded_locked(self):
        pass
    def _save(self):
        pass


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'HERMES_HOME': self.tmp.name})
        self.env.start()
        self.helper = runpy.run_path(str(SCRIPT))
        self.entry = SimpleNamespace(session_key='telegram:42', session_id='original-session', suspended=False)
        self.store = Store(self.entry)
        self.event = SimpleNamespace(text='Research chemistry and save notes', internal=False)
    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
    def test_sigkill_before_turn_keeps_original_task_and_session(self):
        code = "import runpy,sys,os,signal; from types import SimpleNamespace; h=runpy.run_path(sys.argv[1]); h['begin'](SimpleNamespace(session_key='telegram:42',session_id='original-session'), SimpleNamespace(text='Research chemistry and save notes',internal=False)); os.kill(os.getpid(),signal.SIGKILL)"
        result = subprocess.run([sys.executable, '-c', code, str(SCRIPT)], timeout=5)
        self.assertEqual(result.returncode, -9)
        self.helper['recover'](self.store)
        self.assertEqual(self.entry.session_id, 'original-session')
        self.assertTrue(self.entry.resume_pending)
        self.assertEqual(self.entry.resume_reason, 'restart_interrupted')
        self.assertIn(self.event.text, self.helper['resume_text'](self.entry.session_key))
    def test_retry_exhaustion_pauses_without_resetting_session(self):
        self.helper['begin'](self.entry, self.event)
        for _ in range(3):
            self.helper['recover'](self.store)
            self.assertEqual(self.entry.resume_reason, 'restart_interrupted')
        self.helper['recover'](self.store)
        self.assertEqual(self.entry.resume_reason, 'render_recovery_paused')
        self.assertFalse(self.entry.suspended)
        self.assertEqual(self.entry.session_id, 'original-session')
    def test_completed_task_is_not_replayed(self):
        self.helper['begin'](self.entry, self.event)
        self.helper['finish'](self.entry.session_key)
        self.helper['recover'](self.store)
        self.assertFalse(self.helper['tracked'](self.entry.session_key))
        self.assertFalse(hasattr(self.entry, 'resume_pending'))
    def test_explicit_stop_or_new_session_wins(self):
        for change in ('stop', 'new'):
            self.helper['begin'](self.entry, self.event)
            if change == 'stop':
                self.entry.suspended = True
            else:
                self.entry.suspended = False
                self.entry.session_id = 'user-selected-new-session'
            self.helper['recover'](self.store)
            self.assertFalse(self.helper['tracked'](self.entry.session_key))
    def test_internal_resume_does_not_reset_attempt_count_or_original_task(self):
        self.helper['begin'](self.entry, self.event)
        self.helper['recover'](self.store)
        self.helper['begin'](self.entry, SimpleNamespace(text='recovery prompt', internal=True))
        self.assertEqual(self.helper['_read']()[self.entry.session_key]['attempts'], 1)
        self.assertEqual(self.helper['_read']()[self.entry.session_key]['task'], self.event.text)
