"""Connect durable task intent to native gateway startup and turn ownership."""
import asyncio
from datetime import datetime
from functools import wraps
import logging
import os

from hermes_cli import render_recovery as journal

LOG = logging.getLogger(__name__)


def _recover(store):
    with journal._lock, store._lock:
        store._ensure_loaded_locked()
        ended = getattr(store, '_is_session_ended_in_db', None)
        if callable(ended):
            data = journal._read()
            changed = False
            for key, record in data.items():
                if record.get('status', 'pending') == 'pending' and ended(record.get('session_id'), key):
                    record['status'] = 'deleted'
                    changed = True
            if changed:
                journal._write(data)
    journal.recover(store)


def install(runner_class):
    if os.environ.get('HERMES_RUNTIME_PROFILE') != 'colab-full':
        return False
    if os.environ.get('HERMES_RESUME_ALL_INTERRUPTED', '1') != '1':
        return False
    if getattr(runner_class, '_colab_task_recovery_installed', False):
        return False
    names = ('_mark_durable_active_turn', '_hmwa_persist_turn_transcript',
             '_resume_pending_candidates', '_run_startup_resume_event',
             '_start_finish_wiring', '_interrupt_and_clear_session')
    missing = [name for name in names if not callable(getattr(runner_class, name, None))]
    if missing:
        raise RuntimeError('Native recovery hooks changed: ' + ', '.join(missing))
    original = {name:getattr(runner_class, name) for name in names}

    @wraps(original['_mark_durable_active_turn'])
    async def mark(self, event, session_key):
        marked = await original['_mark_durable_active_turn'](self, event, session_key)
        if marked and not getattr(event, 'internal', False):
            entry = await self.async_session_store.lookup_by_session_key(session_key)
            if entry is not None:
                await asyncio.to_thread(journal.begin, entry, event)
        return marked

    @wraps(original['_hmwa_persist_turn_transcript'])
    async def persist(self, **kwargs):
        result = await original['_hmwa_persist_turn_transcript'](self, **kwargs)
        key, entry = kwargs.get('session_key'), kwargs.get('session_entry')
        if key and journal.turn_finished(kwargs.get('agent_result')):
            # Native compression has already persisted the new session identity.
            with journal._lock:
                data = journal._read()
                record = data.get(key)
                if record and entry is not None and record.get('status', 'pending') == 'pending':
                    record['session_id'] = entry.session_id
                    record['entry'] = entry.to_dict()
                    journal._write(data)
            await asyncio.to_thread(journal.finish, key)
            await self.async_session_store.clear_resume_pending(key)
        return result

    @wraps(original['_resume_pending_candidates'])
    def candidates(self, platform=None):
        _recover(self.session_store)
        rows = original['_resume_pending_candidates'](self, platform)
        if rows is None:
            return None
        rows = [entry for entry in rows if not journal.is_terminal(entry.session_key)]
        # Only explicit native interruption markers qualify. Refresh their
        # scheduler timestamp without disabling freshness checks for old tool tails.
        for entry in rows:
            entry.last_resume_marked_at = datetime.now()
        rows.sort(key=lambda entry:journal.priority(entry.session_key))
        return rows

    @wraps(original['_run_startup_resume_event'])
    async def resume(self, adapter, event, session_key):
        with self.session_store._lock:
            self.session_store._ensure_loaded_locked()
            entry = self.session_store._entries.get(session_key)
            expected_id = getattr(entry, 'session_id', None)
        if not hasattr(self, '_colab_resume_slots'):
            self._colab_resume_slots = asyncio.Semaphore(journal.session_concurrency())
        lease = None
        try:
            async with self._colab_resume_slots:
                from hermes_cli.active_sessions import try_acquire_active_session
                while True:
                    with self.session_store._lock:
                        entry = self.session_store._entries.get(session_key)
                        valid = (entry is not None and entry.session_id == expected_id
                                 and not entry.suspended and entry.resume_pending
                                 and not journal.is_terminal(session_key))
                    if not valid:
                        return
                    lease, refusal = await asyncio.to_thread(try_acquire_active_session,
                        session_id=session_key, surface='gateway:recovery', config=self.config,
                        metadata={'live_session_id':session_key})
                    if not refusal:
                        with self.session_store._lock:
                            entry = self.session_store._entries.get(session_key)
                            if (entry is None or entry.session_id != expected_id or entry.suspended
                                    or not entry.resume_pending or journal.is_terminal(session_key)):
                                return
                        break
                    await asyncio.sleep(2)
                if journal.tracked(session_key):
                    journal.restore_event_metadata(event, session_key)
                    event.text = journal.resume_text(session_key)
                metadata = dict(getattr(event, 'metadata', None) or {})
                metadata.update(gateway_session_strict=True, gateway_session_id=expected_id,
                                gateway_session_key=session_key)
                event.metadata = metadata
                LOG.info('Auto-resuming interrupted chat %s', session_key)
                # Native dispatch waits for the actual agent task, not just enqueue.
                await original['_run_startup_resume_event'](self, adapter, event, session_key)
        finally:
            if lease is not None:
                lease.release()
            from gateway.run import _AGENT_PENDING_SENTINEL
            state = self._peek_session_state(session_key)
            if state is not None and state.turn.agent is _AGENT_PENDING_SENTINEL:
                self._release_running_agent_state(session_key)

    @wraps(original['_interrupt_and_clear_session'])
    async def interrupt(self, session_key, *args, **kwargs):
        if kwargs.get('invalidation_reason') in ('stop_command', 'new_command'):
            await asyncio.to_thread(journal.finish, session_key, None, 'stopped')
        return await original['_interrupt_and_clear_session'](self, session_key, *args, **kwargs)

    async def watch(self):
        while True:
            await asyncio.sleep(10)
            try:
                self._schedule_resume_pending_sessions()
            except Exception:
                LOG.exception('Interrupted chat recovery scan failed; retrying')

    @wraps(original['_start_finish_wiring'])
    async def wiring(self, *args, **kwargs):
        result = await original['_start_finish_wiring'](self, *args, **kwargs)
        if not getattr(self, '_colab_resume_watch_started', False):
            self._colab_resume_watch_started = True
            self._retain_background_task(asyncio.create_task(watch(self)))
        return result

    for name, function in zip(names, (mark, persist, candidates, resume, wiring, interrupt)):
        setattr(runner_class, name, function)
    runner_class._colab_task_recovery_installed = True
    return True
