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
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        logging.getLogger(__name__).exception(
            'task recovery journal is unreadable; keeping the gateway available'
        )
        return {}
    if not isinstance(data, dict):
        logging.getLogger(__name__).error('task recovery journal is not an object')
        return {}
    return data


def _json_value(value, depth=0):
    """Keep only bounded JSON metadata from a platform message event."""
    if depth > 5:
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, str):
            return value[:4000]
        return value
    if isinstance(value, (list, tuple)):
        return [_json_value(item, depth + 1) for item in value[:32]]
    if isinstance(value, dict):
        return {
            str(key)[:100]: _json_value(item, depth + 1)
            for key, item in list(value.items())[:64]
            if isinstance(key, (str, int))
        }
    return None


def _event_metadata(event):
    raw = getattr(event, '__dict__', {})
    if not isinstance(raw, dict):
        return {}
    ignored = {'text', 'source', 'internal', 'message_type'}
    return {
        key: value for key, item in raw.items()
        if key not in ignored and (value := _json_value(item)) is not None
    }


def _source_metadata(source):
    if source is None:
        return None
    serializer = getattr(source, 'to_dict', None)
    if callable(serializer):
        try:
            value = serializer()
        except Exception:
            value = None
        if isinstance(value, dict):
            return _json_value(value)
    return _json_value(getattr(source, '__dict__', None))


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
        previous = data.get(entry.session_key)
        if previous and (event.text or '').strip().lower().rstrip('.!') in ('continue', 'resume', 'keep going', 'carry on'):
            event.text = resume_text(entry.session_key)
            previous['session_id'] = entry.session_id
            previous['next_retry_at'] = 0
            _write(data)
            return
        data[entry.session_key] = {'session_id': entry.session_id,
            'task': event.text or '', 'attempts': 0, 'started_at': time.time(),
            'source': _source_metadata(getattr(event, 'source', None)),
            'event': _event_metadata(event)}
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


def pending_match(entry):
    with _lock:
        record = _read().get(entry.session_key)
        return bool(record and record['session_id'] == entry.session_id)


def recover(store):
    """Only explicitly unfinished tasks, irrespective of their age."""
    with _lock, store._lock:
        store._ensure_loaded_locked()
        data = _read()
        for key, record in list(data.items()):
            entry = store._entries.get(key)
            if entry is None:
                # A delayed SessionStore load must not erase a durable task.
                # The periodic scanner will pick it up once its lane is ready.
                continue
            if entry.suspended:
                del data[key]  # Explicit stop/reset wins.
                continue
            if entry.session_id != record.get('session_id'):
                # Keep the intent dormant. A manual "continue" in this lane
                # can rebind it; an unrelated new task will replace it.
                continue
            reason = 'restart_interrupted'
            entry.resume_pending = True
            entry.resume_reason = reason
            entry.last_resume_marked_at = datetime.now()
        _write(data)
        store._save()


def rebind(store, session_key, session_id, topic_db=None):
    """Compression changes the native ID, not the identity of unfinished work."""
    with _lock, store._lock:
        store._ensure_loaded_locked()
        entry = store._entries.get(session_key)
        if entry is None:
            return
        previous_id = entry.session_id
        data = _read()
        record = data.get(session_key)
        if record and record['session_id'] == entry.session_id:
            record['session_id'] = session_id
            _write(data)
        entry.session_id = session_id
        store._save()
        source = getattr(entry, 'origin', None)
        if topic_db is not None and source and source.thread_id:
            binding = topic_db.get_telegram_topic_binding(chat_id=str(source.chat_id), thread_id=str(source.thread_id))
            if binding and binding.get('session_id') == previous_id:
                topic_db.bind_telegram_topic(chat_id=str(source.chat_id), thread_id=str(source.thread_id),
                    user_id=str(source.user_id or ''), session_key=session_key, session_id=session_id)


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
        restore_event_metadata(event, entry.session_key)
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


def restore_event_metadata(event, session_key):
    """Restore supported message fields onto a gateway recovery event."""
    with _lock:
        record = _read().get(session_key, {})
    metadata = record.get('event')
    if not isinstance(metadata, dict):
        return event
    for key, value in metadata.items():
        if key in ('text', 'source', 'internal', 'message_type'):
            continue
        if hasattr(event, key):
            try:
                setattr(event, key, value)
            except (AttributeError, TypeError):
                pass
    return event


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
