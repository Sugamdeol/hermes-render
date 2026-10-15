"""Serialize adapter mutations with durable gateway admission across processes."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading

_local = threading.local()
_threads = threading.RLock()


@contextmanager
def mutation_lock(home=None):
    home = Path(home or os.environ.get('HERMES_HOME', '/opt/data')).resolve()
    identity = hashlib.sha256(str(home).encode()).hexdigest()
    with _threads:
        depth = getattr(_local, identity, 0)
        setattr(_local, identity, depth + 1)
        try:
            if depth:
                yield
            else:
                fd = os.open('/tmp/hermes-chat-' + identity + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
                with os.fdopen(fd, 'a+') as handle:
                    fcntl.flock(handle, fcntl.LOCK_EX)
                    yield
        finally:
            setattr(_local, identity, depth)


def deleted(home, sid):
    path = Path(home) / 'dashboard_chat/deleted_sessions.json'
    if not path.exists():
        return False
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise RuntimeError('Invalid deleted-session registry')
    return sid in data


def record_deleted(home, sid):
    path = Path(home) / 'dashboard_chat/deleted_sessions.json'
    data = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(data, dict):
        raise RuntimeError('Invalid deleted-session registry')
    data[sid] = True
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.deleted-')
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(data, handle); handle.flush(); os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
