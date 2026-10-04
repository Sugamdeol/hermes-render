"""Durable gateway task intent; transcript/tool progress stays in native Hermes."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from datetime import datetime

_lock = threading.RLock()
MAX_RETRIES = 3


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
            record['attempts'] = int(record.get('attempts', 0)) + 1
            reason = 'restart_interrupted' if record['attempts'] <= MAX_RETRIES else 'render_recovery_paused'
            entry.resume_pending = True
            entry.resume_reason = reason
            entry.last_resume_marked_at = datetime.now()
        _write(data)
        store._save()


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
