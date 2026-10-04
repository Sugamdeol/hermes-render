"""Durable gateway task intent; transcript/tool progress stays in native Hermes."""
import json
import asyncio
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
from datetime import datetime

_lock = threading.RLock()
RETRY_BASE_SECONDS = 20
RETRY_MAX_SECONDS = 300


def _path():
    return Path(os.environ.get('HERMES_HOME', '/opt/data')) / '.render-recovery.json'


def _read():
    path = _path()
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError('invalid task recovery journal')
    return data


def _write(data):
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.recovery-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def begin(entry, event):
    if getattr(event, 'internal', False):
        return
    with _lock:
        data = _read()
        data[entry.session_key] = {'session_id': entry.session_id,
            'task': event.text or '', 'attempts': 0, 'started_at': time.time()}
        _write(data)


def finish(session_key):
    with _lock:
        data = _read()
        if session_key in data:
            del data[session_key]
            _write(data)


def tracked(session_key):
    with _lock:
        return session_key in _read()


def recover(store):
    """Only explicitly unfinished tasks, irrespective of their age."""
    with _lock, store._lock:
        store._ensure_loaded_locked()
        data = _read()
        for key, record in list(data.items()):
            entry = store._entries.get(key)
            if entry is None or entry.suspended or entry.session_id != record['session_id']:
                del data[key]  # Explicit stop/reset/resume selection wins.
                continue
            reason = 'restart_interrupted'
            entry.resume_pending = True
            entry.resume_reason = reason
            entry.last_resume_marked_at = datetime.now()
        _write(data)
        store._save()


def priority(session_key):
    """Untried lanes go first after a crash, preventing one lane starving others."""
    with _lock:
        record = _read().get(session_key, {})
        return (int(record.get('attempts', 0)), record.get('started_at', 0), session_key)


def claim(store, session_key, session_id):
    """Charge only a dispatched attempt; recheck stop/reset while it was queued."""
    with _lock, store._lock:
        store._ensure_loaded_locked()
        entry = store._entries.get(session_key)
        data = _read()
        record = data.get(session_key)
        if entry is None or entry.suspended or entry.session_id != session_id or not entry.resume_pending:
            return False
        if record is None:
            return False
        if time.time() < float(record.get('next_retry_at', 0)):
            return False
        record['attempts'] = int(record.get('attempts', 0)) + 1
        delay = min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * 2 ** min(record['attempts'] - 1, 4))
        record['next_retry_at'] = time.time() + delay
        _write(data)
        return True


async def dispatch(runner, adapter, entry, event, session_id):
    """Drain all interrupted lanes without allocating all their agents at once."""
    if not hasattr(runner, '_render_recovery_lock'):
        runner._render_recovery_lock = asyncio.Lock()
    async with runner._render_recovery_lock:
        from gateway.session import build_session_key
        extra = getattr(getattr(adapter, 'config', None), 'extra', {})
        key = build_session_key(event.source,
            group_sessions_per_user=extra.get('group_sessions_per_user', True),
            thread_sessions_per_user=extra.get('thread_sessions_per_user', False))
        if key in getattr(adapter, '_active_sessions', {}):
            return  # A real incoming message already took ownership of this lane.
        if entry.session_id != session_id:
            return
        if tracked(entry.session_key):
            if not claim(runner.session_store, entry.session_key, session_id):
                return
        else:
            # A user completed/stopped this task while it waited in the queue.
            if not entry.resume_pending or entry.suspended:
                return
        logging.getLogger(__name__).info('Continuing interrupted session %s', entry.session_key)
        event.text = resume_text(entry.session_key) or event.text
        await adapter.handle_message(event)
        # Native handle_message only enqueues the turn. Hold our recovery slot
        # until its real background owner (including pending-message drains) ends.
        while True:
            task = getattr(adapter, '_session_tasks', {}).get(key)
            if task is None or task is asyncio.current_task():
                break
            await asyncio.shield(task)
            if getattr(adapter, '_session_tasks', {}).get(key) is task:
                break


def resume_text(session_key):
    with _lock:
        record = _read().get(session_key)
    if not record:
        return ''
    return ('[Recovery after an unexpected process exit. Continue the unfinished task '
        'in this same session using the saved transcript and tool results. Check actual '
        'files and external state before retrying an action whose outcome is uncertain. '
        'Do not repeat completed actions or invent a new task. If already complete, '
        'report its saved outcome.]\nOriginal task: ' + record['task'])


async def watch(runner, interval=10):
    """Retry failed continuations and late adapters without requiring another restart."""
    while True:
        await asyncio.sleep(interval)
        try:
            recover(runner.session_store)
            runner._schedule_resume_pending_sessions()
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).exception('Recovery scan failed; will retry')
