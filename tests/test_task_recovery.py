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
    def test_retries_back_off_without_permanently_pausing(self):
        self.helper['begin'](self.entry, self.event)
        with patch('time.time', return_value=1000):
            self.helper['recover'](self.store)
            self.assertTrue(self.helper['claim'](self.store, self.entry.session_key, self.entry.session_id))
            self.assertFalse(self.helper['claim'](self.store, self.entry.session_key, self.entry.session_id))
        for clock in (2000, 3000, 4000, 5000):
            with patch('time.time', return_value=clock):
                self.helper['recover'](self.store)
                self.assertTrue(self.helper['claim'](self.store, self.entry.session_key, self.entry.session_id))
        self.assertEqual(self.entry.resume_reason, 'restart_interrupted')
        self.assertFalse(self.entry.suspended)
        self.assertEqual(self.entry.session_id, 'original-session')

    def test_completed_task_is_not_replayed(self):
        self.helper['begin'](self.entry, self.event)
        self.helper['finish'](self.entry.session_key)
        self.helper['recover'](self.store)
        self.assertFalse(self.helper['tracked'](self.entry.session_key))
        self.assertFalse(hasattr(self.entry, 'resume_pending'))
    def test_explicit_stop_wins(self):
        self.helper['begin'](self.entry, self.event)
        self.entry.suspended = True
        self.helper['recover'](self.store)
        self.assertFalse(self.helper['tracked'](self.entry.session_key))

    def test_new_native_session_preserves_task_until_manual_continue(self):
        self.helper['begin'](self.entry, self.event)
        self.entry.session_id = 'fresh-native-session'
        self.helper['recover'](self.store)
        self.assertTrue(self.helper['tracked'](self.entry.session_key))
        self.assertFalse(getattr(self.entry, 'resume_pending', False))

        continuation = SimpleNamespace(text='continue', internal=False)
        self.helper['begin'](self.entry, continuation)
        self.assertIn(self.event.text, continuation.text)
        self.assertEqual(self.helper['_read']()[self.entry.session_key]['session_id'],
                         'fresh-native-session')

    def test_late_session_store_load_does_not_delete_orphaned_intent(self):
        self.helper['begin'](self.entry, self.event)
        self.store._entries.clear()
        self.helper['recover'](self.store)
        self.assertTrue(self.helper['tracked'](self.entry.session_key))
        self.store._entries[self.entry.session_key] = self.entry
        self.helper['recover'](self.store)
        self.assertTrue(self.entry.resume_pending)
    def test_internal_resume_does_not_reset_attempt_count_or_original_task(self):
        self.helper['begin'](self.entry, self.event)
        self.helper['recover'](self.store)
        self.helper['claim'](self.store, self.entry.session_key, self.entry.session_id)
        self.helper['begin'](self.entry, SimpleNamespace(text='recovery prompt', internal=True))
        self.assertEqual(self.helper['_read']()[self.entry.session_key]['attempts'], 1)
        self.assertEqual(self.helper['_read']()[self.entry.session_key]['task'], self.event.text)

    def test_waiting_sessions_do_not_exhaust_retries_or_lose_tasks(self):
        entries = [self.entry] + [SimpleNamespace(session_key=f'telegram:{i}',
            session_id=f'session-{i}', suspended=False) for i in range(3)]
        self.store._entries = {e.session_key: e for e in entries}
        for entry in entries:
            self.helper['begin'](entry, SimpleNamespace(text=entry.session_id, internal=False))
        for _ in range(5):
            self.helper['recover'](self.store)
        for entry in entries:
            self.assertEqual(self.helper['_read']()[entry.session_key]['attempts'], 0)
            self.assertEqual(entry.resume_reason, 'restart_interrupted')
            self.assertIn(entry.session_id, self.helper['resume_text'](entry.session_key))
        self.helper['claim'](self.store, entries[0].session_key, entries[0].session_id)
        self.assertGreater(self.helper['priority'](entries[0].session_key),
            self.helper['priority'](entries[1].session_key))

    def test_user_reset_while_queued_is_not_replayed(self):
        self.helper['begin'](self.entry, self.event)
        self.helper['recover'](self.store)
        original = self.entry.session_id
        self.entry.session_id = 'selected-new-session'
        self.assertFalse(self.helper['claim'](self.store, self.entry.session_key, original))
        self.assertEqual(self.helper['_read']()[self.entry.session_key]['attempts'], 0)

    def test_manual_continue_preserves_original_task(self):
        self.helper['begin'](self.entry, self.event)
        continuation = SimpleNamespace(text='continue', internal=False)
        self.helper['begin'](self.entry, continuation)
        record = self.helper['_read']()[self.entry.session_key]
        self.assertEqual(record['task'], self.event.text)
        self.assertIn(self.event.text, continuation.text)

    def test_original_event_metadata_can_be_restored(self):
        original = SimpleNamespace(text='analyze this', internal=False,
                                   media_urls=['https://files.example/image.png'],
                                   message_id=123)
        self.helper['begin'](self.entry, original)
        recovery_event = SimpleNamespace(text='', internal=True,
                                         media_urls=[], message_id=None)
        self.helper['restore_event_metadata'](recovery_event, self.entry.session_key)
        self.assertEqual(recovery_event.media_urls, ['https://files.example/image.png'])
        self.assertEqual(recovery_event.message_id, 123)

    def test_compression_rebind_keeps_recovery_record_and_new_session(self):
        self.helper['begin'](self.entry, self.event)
        self.helper['rebind'](self.store, self.entry.session_key, 'compressed-session')
        self.helper['recover'](self.store)
        self.assertEqual(self.entry.session_id, 'compressed-session')
        self.assertEqual(self.helper['_read']()[self.entry.session_key]['session_id'], 'compressed-session')
        self.assertTrue(self.entry.resume_pending)
