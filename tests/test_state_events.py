import importlib.util
import os
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import time
import threading
from unittest.mock import patch
import unittest

from test_git_storage import load_storage, make_config

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('state_events', ROOT / 'scripts/state-events.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux inotify')
class StateEventTests(unittest.TestCase):
    def wait_change(self, monitor, before):
        deadline = time.monotonic() + 3
        while monitor.sample() == before and time.monotonic() < deadline:
            monitor.notify.wait(0.05)
        self.assertNotEqual(monitor.sample(), before)

    def test_create_update_delete_and_new_directory(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            monitor = module.StateEvents(root, lambda p: p.startswith('logs'))
            try:
                before = monitor.sample()
                path = root / 'session.json'
                path.write_text('first')
                self.wait_change(monitor, before)
                before = monitor.sample()
                path.write_text('second')
                self.wait_change(monitor, before)
                before = monitor.sample()
                path.unlink()
                self.wait_change(monitor, before)
                sub = root / 'new-session'
                sub.mkdir()
                deadline = time.monotonic() + 3
                while sub not in list(monitor._dirs.values()) and time.monotonic() < deadline:
                    time.sleep(0.01)
                before = monitor.sample()
                (sub / 'chat.json').write_text('nested')
                self.wait_change(monitor, before)
            finally:
                monitor.close()

    def test_open_sqlite_wal_wakes_without_closing_connection(self):
        with tempfile.TemporaryDirectory() as td:
            db = sqlite3.connect(Path(td) / 'state.db')
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE sessions (message TEXT)')
            db.commit()
            monitor = module.StateEvents(td, lambda p: False)
            try:
                before = monitor.sample()
                db.execute("INSERT INTO sessions VALUES ('new message')")
                db.commit()
                self.wait_change(monitor, before)
            finally:
                monitor.close()
                db.close()

    def test_excluded_logs_do_not_trigger_backup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'logs').mkdir()
            monitor = module.StateEvents(root, lambda p: p.startswith('logs'))
            try:
                (root / 'logs' / 'agent.log').write_text('noise')
                self.assertFalse(monitor.notify.wait(0.3))
                self.assertEqual(monitor.sample(), '0')
            finally:
                monitor.close()

    def test_changes_during_push_remain_pending(self):
        storage = load_storage()
        with tempfile.TemporaryDirectory() as td:
            monitor = module.StateEvents(td, lambda p: False)
            try:
                watcher = storage.ChangeWatcher(Path(td), make_config(storage, debounce_seconds=0), sampler=monitor.sample)
                (Path(td) / 'chat').write_text('first')
                self.wait_change(monitor, '0')
                self.assertTrue(watcher.poll())
                snapshot = watcher.snapshot()
                before = monitor.sample()
                (Path(td) / 'chat').write_text('while uploading')
                self.wait_change(monitor, before)
                watcher.mark_pushed(snapshot)
                self.assertTrue(watcher.poll())
            finally:
                monitor.close()

    def test_queue_overflow_marks_dirty_and_rebuilds_watches(self):
        with tempfile.TemporaryDirectory() as td:
            monitor = module.StateEvents(td, lambda p: False)
            try:
                monitor._consume(struct.pack('iIII', -1, monitor.OVERFLOW, 0, 0))
                self.assertNotEqual(monitor.sample(), '0')
                self.assertIn(Path(td), monitor._dirs.values())
            finally:
                monitor.close()

    def test_exact_paths_flush_and_ack_preserve_writes_during_upload(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            monitor = module.StateEvents(root, lambda p: False)
            try:
                (root/'chat').write_text('first')
                monitor.flush()
                snapshot = monitor.sample()
                self.assertEqual(monitor.changes_since('0'), ['chat'])
                (root/'chat').write_text('during upload')
                (root/'new').write_text('new')
                monitor.flush()
                monitor.acknowledge(snapshot)
                self.assertEqual(set(monitor.changes_since(snapshot)), {'chat', 'new'})
                latest = monitor.sample()
                monitor.acknowledge(latest)
                self.assertEqual(monitor.changes_since(latest), [])
                monitor._consume(struct.pack('iIII', -1, monitor.OVERFLOW, 0, 0))
                self.assertIsNone(monitor.changes_since(latest))
            finally:
                monitor.close()

    def test_daemon_passes_only_dirty_paths_and_keeps_edits_during_upload(self):
        storage = load_storage()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); stop = threading.Event(); batches = []
            config = make_config(storage, interval=60, watch_seconds=30, debounce_seconds=0, min_push_interval=0)
            def sync(*args, **kwargs):
                batches.append(config._source_changes)
                if len(batches) == 1:
                    (root/'chat').write_text('first')
                elif len(batches) == 2:
                    (root/'chat').write_text('during upload')
                    (root/'new').write_text('new')
                elif len(batches) == 3:
                    stop.set()
            timer = threading.Timer(4, stop.set); timer.start()
            try:
                with patch.dict(os.environ, {'GIT_STATE_EVENT_WATCH':'1'}), patch.object(stop,'wait',return_value=False), patch.object(storage,'_write_sync_status'), patch.object(storage,'seed_on_startup'), patch.object(storage,'recovery_checkpoint',return_value=False), patch.object(storage,'sync_once',side_effect=sync):
                    storage.run_daemon(root,config,stop)
                self.assertIsNone(batches[0])
                self.assertEqual(set(batches[1]), {'chat'})
                self.assertEqual(set(batches[2]), {'chat','new'})
            finally:
                timer.cancel()

    def test_daemon_wakes_on_event_before_long_poll_interval(self):
        storage = load_storage()
        with tempfile.TemporaryDirectory() as td:
            stop = threading.Event()
            pushes = []
            config = make_config(storage, interval=60, watch_seconds=30, debounce_seconds=0, min_push_interval=0)
            def sync(*args, **kwargs):
                pushes.append(time.monotonic())
                if len(pushes) == 1:
                    (Path(td) / 'chat').write_text('new session')
                else:
                    stop.set()
            # Bound this test even if wake-up regresses.
            timer = threading.Timer(3, stop.set)
            timer.start()
            try:
                with patch.dict(os.environ, {'GIT_STATE_EVENT_WATCH': '1'}), patch.object(stop, 'wait', return_value=False), patch.object(storage.signal, 'signal'), patch.object(storage, '_write_sync_status'), patch.object(storage, 'seed_on_startup'), patch.object(storage, 'recovery_checkpoint'), patch.object(storage, 'sync_once', side_effect=sync), patch.object(storage, 'state_fingerprint', side_effect=AssertionError('idle events must not scan whole state')):
                    storage.run_daemon(Path(td), config, stop)
                self.assertGreaterEqual(len(pushes), 3)  # initial, event, final
                self.assertLess(pushes[1] - pushes[0], 2)
            finally:
                timer.cancel()
