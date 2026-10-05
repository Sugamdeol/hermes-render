"""Durable gateway task intent; transcript/tool progress stays in native Hermes."""
import json
import asyncio
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
import secrets
from datetime import datetime

_lock = threading.RLock()
RETRY_BASE_SECONDS = 20
RETRY_MAX_SECONDS = 300


def session_concurrency():
    """Maximum independently running gateway chats, shared with recovery."""
    try:
        return max(1, int(os.environ.get('HERMES_MAX_CONCURRENT_SESSIONS', '2')))
    except ValueError:
        return 2


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
        if previous and previous.get('status', 'pending') == 'pending' and (event.text or '').strip().lower().rstrip('.!') in ('continue', 'resume', 'keep going', 'carry on'):
            event.text = resume_text(entry.session_key)
            previous['session_id'] = entry.session_id
            previous['next_retry_at'] = 0
            _write(data)
            return
        data[entry.session_key] = {'session_id': entry.session_id,
            'task': event.text or '', 'attempts': 0, 'started_at': time.time(),
            'source': _source_metadata(getattr(event, 'source', None) or getattr(entry, 'origin', None)),
            'entry': entry.to_dict() if callable(getattr(entry, 'to_dict', None)) else None,
            'event': _event_metadata(event)}
        _write(data)


def turn_finished(result):
    # A returned error/partial response is still a settled turn. Retry only
    # after an actual interruption, not forever after a visible API failure.
    return isinstance(result, dict) and not result.get('interrupted')


def finish(session_key, store=None, status="completed"):
    with _lock:
        data = _read()
        if session_key in data:
            data[session_key]['status'] = status
            data[session_key]['finished_at'] = time.time()
            _write(data)
    if store is not None:
        store.clear_resume_pending(session_key)
    request_checkpoint()


def request_checkpoint():
    if not os.environ.get('GIT_STATE_REPO') or not os.environ.get('GIT_STATE_TOKEN'):
        return
    request = Path(os.environ.get('HERMES_RECOVERY_CHECKPOINT_FILE', '/tmp/hermes-recovery-checkpoint.json'))
    request.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=request.parent, prefix='.checkpoint-')
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump({'nonce': secrets.token_hex(16)}, handle)
        os.replace(name, request)
    finally:
        Path(name).unlink(missing_ok=True)


def tracked(session_key):
    with _lock:
        return _read().get(session_key, {}).get('status', 'pending') == 'pending' and session_key in _read()


def is_terminal(session_key):
    with _lock:
        record = _read().get(session_key)
        return bool(record and record.get('status', 'pending') != 'pending')


def pending_match(entry):
    with _lock:
        record = _read().get(entry.session_key)
        return bool(record and record.get('status', 'pending') == 'pending' and record['session_id'] == entry.session_id)


def recover(store):
    """Only explicitly unfinished tasks, irrespective of their age."""
    with _lock, store._lock:
        store._ensure_loaded_locked()
        data = _read()
        journal_changed = False
        store_changed = False
        for key, record in list(data.items()):
            entry = store._entries.get(key)
            if record.get('status', 'pending') != 'pending':
                if entry is not None and entry.session_id == record.get('session_id') and getattr(entry, 'resume_pending', False):
                    entry.resume_pending = False
                    entry.resume_reason = None
                    store_changed = True
                continue
            if entry is None:
                snapshot = record.get('entry')
                if not isinstance(snapshot, dict) and isinstance(record.get('source'), dict):
                    # Migrate journals written before entry snapshots existed.
                    stamp = datetime.fromtimestamp(record.get('started_at', time.time())).isoformat()
                    snapshot = {'session_key': key, 'session_id': record['session_id'],
                        'created_at': stamp, 'updated_at': stamp,
                        'origin': record['source'], 'platform': record['source'].get('platform')}
                if not isinstance(snapshot, dict) or not snapshot.get('origin'):
                    continue
                try:
                    from gateway.session import SessionEntry
                    entry = SessionEntry.from_dict(snapshot)
                    entry.session_key = key
                    entry.session_id = record['session_id']
                    store._entries[key] = entry
                    store_changed = True
                except (KeyError, ValueError, TypeError):
                    logging.getLogger(__name__).exception('Cannot rebuild recovery lane %s', key)
                    continue
            if entry.suspended:
                del data[key]  # Explicit stop/reset wins.
                journal_changed = True
                continue
            if entry.session_id != record.get('session_id'):
                # Keep the intent dormant. A manual "continue" in this lane
                # can rebind it; an unrelated new task will replace it.
                continue
            reason = 'restart_interrupted'
            if (not getattr(entry, 'resume_pending', False)
                    or getattr(entry, 'resume_reason', None) != reason
                    or getattr(entry, 'last_resume_marked_at', None) is None):
                entry.resume_pending = True
                entry.resume_reason = reason
                entry.last_resume_marked_at = datetime.now()
                store_changed = True
        # This scan runs every few seconds. Rewriting unchanged files made the
        # Git backup daemon believe state was perpetually dirty, causing a full
        # snapshot/push at every minimum interval while chats were idle.
        if journal_changed:
            _write(data)
        if store_changed:
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
        if record and callable(getattr(entry, 'to_dict', None)):
            record['entry'] = entry.to_dict()
            _write(data)
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
        if record is None or record.get('status', 'pending') != 'pending':
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
        runner._render_recovery_lock = asyncio.Semaphore(session_concurrency())
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
    if not record or record.get('status', 'pending') != 'pending':
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


async def checkpoint():
    """Attempt remote durability without making a storage outage a chat outage."""
    strict = os.environ.get('HERMES_RECOVERY_REQUIRE_REMOTE', '0').lower() in ('1', 'true', 'yes')
    try:
        return await _checkpoint_upload(strict)
    except RuntimeError:
        if strict:
            raise
        logging.getLogger(__name__).warning(
            'Remote checkpoint unavailable; continuing with local recovery journal. '
            'Storage will retry; full container loss can lose unuploaded progress.', exc_info=True)
        return False


async def _checkpoint_upload(strict):
    """Do not allocate an agent until its recovery state is stored remotely.

    The existing storage thread performs the upload, avoiding a second Git
    writer or a second Python process. SIGKILL cannot run shutdown hooks.
    """
    if not os.environ.get('GIT_STATE_REPO') or not os.environ.get('GIT_STATE_TOKEN'):
        return
    request = Path(os.environ.get('HERMES_RECOVERY_CHECKPOINT_FILE', '/tmp/hermes-recovery-checkpoint.json'))
    ack = request.with_suffix('.ack')
    nonce = secrets.token_hex(16)
    request.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=request.parent, prefix='.checkpoint-')
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump({'nonce': nonce}, handle)
        os.replace(name, request)
    finally:
        Path(name).unlink(missing_ok=True)
    deadline = time.monotonic() + float(os.environ.get('HERMES_RECOVERY_CHECKPOINT_TIMEOUT_SECONDS', '180' if strict else '10'))
    while time.monotonic() < deadline:
        try:
            result = json.loads(ack.read_text())
            if isinstance(result, dict) and result.get('nonce') == nonce:
                if not result.get('ok'):
                    raise RuntimeError('Private backup could not save this task. It remains queued for recovery; check storage logs.')
                logging.getLogger(__name__).info('Recovery checkpoint saved to private storage')
                return
        except (OSError, ValueError):
            pass
        await asyncio.sleep(0.2)
    raise RuntimeError('Private backup checkpoint timed out. Task remains queued; recovery will retry automatically.')
