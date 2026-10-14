import asyncio
from datetime import datetime, timedelta
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Entry(SimpleNamespace):
    def to_dict(self):
        return {'session_key':self.session_key, 'session_id':self.session_id}


class NativeRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'HERMES_HOME':self.tmp.name, 'HERMES_RUNTIME_PROFILE':'colab-full', 'HERMES_RESUME_ALL_INTERRUPTED':'1', 'HERMES_MAX_CONCURRENT_SESSIONS':'10'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.journal = load('native_test_journal', 'scripts/task-recovery.py')
        package = ModuleType('hermes_cli')
        package.render_recovery = self.journal
        gateway = ModuleType('gateway')
        active = ModuleType('hermes_cli.active_sessions')
        active.try_acquire_active_session = lambda **kwargs:(SimpleNamespace(release=lambda:None), None)
        run = ModuleType('gateway.run')
        self.sentinel = run._AGENT_PENDING_SENTINEL = object()
        modules = patch.dict(sys.modules, {'hermes_cli':package, 'hermes_cli.render_recovery':self.journal, 'hermes_cli.active_sessions':active, 'gateway':gateway, 'gateway.run':run})
        modules.start()
        self.addCleanup(modules.stop)
        self.native = load('native_test_recovery', 'scripts/native-task-recovery.py')
        sentinel = self.sentinel

        class Runner:
            def __init__(self):
                self.config = {}
                store = self.session_store = SimpleNamespace(_lock=threading.RLock(), _entries={}, _ensure_loaded_locked=lambda:None, _save=lambda:None, _is_session_ended_in_db=lambda sid,key:False)
                async def lookup(key): return store._entries.get(key)
                async def clear(key): store._entries[key].resume_pending = False
                self.async_session_store = SimpleNamespace(lookup_by_session_key=lookup, clear_resume_pending=clear)
                self.active = self.peak = 0
                self.started = []
                self.gate = asyncio.Event()
                self.states = {}
            async def _mark_durable_active_turn(self, event, key): return True
            async def _hmwa_persist_turn_transcript(self, **kwargs): return 'persisted'
            def _resume_pending_candidates(self, platform=None): return [e for e in self.session_store._entries.values() if e.resume_pending and not e.suspended]
            async def _run_startup_resume_event(self, adapter, event, key):
                self.active += 1
                self.peak = max(self.peak, self.active)
                self.started.append((key, event))
                try: await self.gate.wait()
                finally: self.active -= 1
            async def _start_finish_wiring(self, *args): return True
            async def _interrupt_and_clear_session(self, key, *args, **kwargs):
                self.session_store._entries[key].resume_pending = False
            def _peek_session_state(self, key): return self.states.get(key)
            def _release_running_agent_state(self, key): self.states.pop(key, None)
            def _retain_background_task(self, task): return task

        self.Runner = Runner
        self.assertTrue(self.native.install(Runner))
        self.runner = Runner()

    def entry(self, index=0):
        key = f'telegram:chat:topic:{index}'
        entry = Entry(session_key=key, session_id=f'session-{index}', suspended=False,
                      resume_pending=True, resume_reason='restart_interrupted',
                      last_resume_marked_at=datetime.now()-timedelta(days=3), origin=None)
        self.runner.session_store._entries[key] = entry
        self.runner.states[key] = SimpleNamespace(turn=SimpleNamespace(agent=self.sentinel))
        return entry

    def event(self, text='unfinished task', internal=False):
        return SimpleNamespace(text=text, internal=internal, metadata={})

    async def test_tracks_owned_turn_and_completes_only_after_persistence(self):
        entry = self.entry()
        await self.runner._mark_durable_active_turn(self.event(), entry.session_key)
        self.assertTrue(self.journal.tracked(entry.session_key))
        result = await self.runner._hmwa_persist_turn_transcript(session_key=entry.session_key, session_entry=entry, agent_result={'completed':True})
        self.assertEqual(result, 'persisted')
        self.assertFalse(self.journal.tracked(entry.session_key))
        self.assertFalse(entry.resume_pending)
        self.assertEqual(self.runner._resume_pending_candidates(), [])

    async def test_internal_resume_preserves_original_intent_and_interruption(self):
        entry = self.entry()
        await self.runner._mark_durable_active_turn(self.event('original'), entry.session_key)
        await self.runner._mark_durable_active_turn(self.event('recovery note', True), entry.session_key)
        await self.runner._hmwa_persist_turn_transcript(session_key=entry.session_key, session_entry=entry, agent_result={'interrupted':True})
        self.assertIn('original', self.journal.resume_text(entry.session_key))
        self.assertNotIn('recovery note', self.journal.resume_text(entry.session_key))

    async def test_process_local_turn_token_is_not_saved_in_journal(self):
        entry = self.entry()
        event = self.event()
        event._gateway_active_turn_token = 'ephemeral-owner-token'
        event._gateway_active_turn_session_key = entry.session_key
        await self.runner._mark_durable_active_turn(event, entry.session_key)
        self.assertNotIn('ephemeral-owner-token', str(self.journal._read()))

    async def test_all_old_interrupted_topics_are_candidates_but_stopped_are_not(self):
        entries = [self.entry(i) for i in range(15)]
        entries[-1].suspended = True
        rows = self.runner._resume_pending_candidates()
        self.assertEqual(len(rows), 14)
        self.assertTrue(all((datetime.now()-e.last_resume_marked_at).total_seconds()<1 for e in rows))

    async def test_parallel_recovery_holds_ten_slots_until_real_turns_end(self):
        entries = [self.entry(i) for i in range(15)]
        tasks = [asyncio.create_task(self.runner._run_startup_resume_event(None, self.event(internal=True), e.session_key)) for e in entries]
        for _ in range(100):
            await asyncio.sleep(.001)
            if len(self.runner.started)==10: break
        self.assertEqual(self.runner.active, 10)
        self.runner.gate.set()
        await asyncio.gather(*tasks)
        self.assertEqual(len(self.runner.started), 15)
        self.assertEqual(self.runner.peak, 10)
        for key, event in self.runner.started:
            self.assertEqual(event.metadata['gateway_session_key'], key)
            self.assertTrue(event.metadata['gateway_session_strict'])

    async def test_stop_and_reset_while_queued_prevent_replay(self):
        entries = [self.entry(i) for i in range(12)]
        for entry in entries:
            self.journal.begin(entry, self.event())
        tasks = [asyncio.create_task(self.runner._run_startup_resume_event(None, self.event(internal=True), e.session_key)) for e in entries]
        for _ in range(100):
            await asyncio.sleep(.001)
            if len(self.runner.started)==10: break
        await self.runner._interrupt_and_clear_session(entries[-1].session_key, invalidation_reason='stop_command')
        entries[-2].session_id = 'new-session-after-reset'
        self.runner.gate.set()
        await asyncio.gather(*tasks)
        self.assertEqual(len(self.runner.started), 10)
        self.assertNotIn(entries[-1].session_key, self.runner.states)

    async def test_deleted_chat_is_not_rebuilt_from_journal(self):
        entry = self.entry()
        self.journal.begin(entry, self.event())
        self.runner.session_store._is_session_ended_in_db = lambda sid,key:True
        self.assertEqual(self.runner._resume_pending_candidates(), [])
        self.assertEqual(self.journal._read()[entry.session_key]['status'], 'deleted')

    async def test_install_is_idempotent_and_native_drift_fails_explicitly(self):
        self.assertFalse(self.native.install(self.Runner))
        with self.assertRaisesRegex(RuntimeError, 'hooks changed'):
            self.native.install(type('Changed', (), {}))

    async def test_render_is_unchanged(self):
        with patch.dict(os.environ, {'HERMES_RUNTIME_PROFILE':'render-lite'}):
            self.assertFalse(self.native.install(type('Render', (), {})))


class RecoveryPatchTests(unittest.TestCase):
    def test_patch_is_idempotent_and_keeps_native_main(self):
        helper = load('native_recovery_patch', 'scripts/patch-native-task-recovery.py')
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root/'gateway').mkdir()
            (root/'hermes_cli').mkdir()
            (root/'tui_gateway').mkdir()
            (root/'tui_gateway/server.py').write_text('import sys\n')
            path = root/'gateway/run.py'
            path.write_text('class GatewayRunner: pass\nif __name__ == "__main__":\n    pass\n')
            helper.patch(root)
            once = path.read_text()
            helper.patch(root)
            self.assertEqual(path.read_text(), once)
            self.assertEqual(once.count(helper.MARKER), 1)
            self.assertTrue((root/'hermes_cli/native_task_recovery.py').exists())


if __name__ == '__main__':
    unittest.main()
