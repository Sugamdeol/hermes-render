"""Linux state-change notifications; no third-party watcher dependency."""
import ctypes
import os
from pathlib import Path
import select
import struct
import threading


class StateEvents:
    MODIFY = 0x2
    ATTRIB = 0x4
    CLOSE_WRITE = 0x8
    MOVED_FROM = 0x40
    MOVED_TO = 0x80
    CREATE = 0x100
    DELETE = 0x200
    DELETE_SELF = 0x400
    MOVE_SELF = 0x800
    OVERFLOW = 0x4000
    IGNORED = 0x8000
    ISDIR = 0x40000000
    MASK = MODIFY | ATTRIB | CLOSE_WRITE | MOVED_FROM | MOVED_TO | CREATE | DELETE | DELETE_SELF | MOVE_SELF

    def __init__(self, root, excluded):
        self.root = Path(root).resolve()
        self.excluded = excluded
        self.notify = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._revision = 0
        self._paths = {}
        self._overflow_revision = -1
        self._consume_lock = threading.RLock()
        self.failed = False
        self._dirs = {}
        self._libc = ctypes.CDLL(None, use_errno=True)
        self._libc.inotify_init1.argtypes = [ctypes.c_int]
        self._libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self._libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
        self.fd = self._libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), 'inotify initialization failed')
        try:
            self._add_tree(self.root)
        except BaseException:
            os.close(self.fd)
            raise
        self._thread = threading.Thread(target=self._read, name='hermes-state-events', daemon=True)
        self._thread.start()

    def sample(self):
        with self._lock:
            return str(self._revision)

    def changes_since(self, revision):
        with self._lock:
            if self._overflow_revision > int(revision):
                return None
            return [name for name, version in self._paths.items() if version > int(revision)]

    def acknowledge(self, revision):
        with self._lock:
            self._paths = {name: version for name, version in self._paths.items()
                           if version > int(revision)}

    def flush(self):
        with self._consume_lock:
            while True:
                try:
                    data = os.read(self.fd, 1024 * 1024)
                except BlockingIOError:
                    break
                if not data:
                    break
                self._consume(data)

    def _changed(self, path=None):
        with self._lock:
            self._revision += 1
            if path is None or path == self.root:
                self._overflow_revision = self._revision
            else:
                self._paths[path.relative_to(self.root).as_posix()] = self._revision
        self.notify.set()

    def _skip(self, path):
        return path != self.root and self.excluded(path.relative_to(self.root).as_posix())

    def _add_tree(self, path):
        if path.is_symlink() or self._skip(path):
            return
        for current, dirs, _ in os.walk(path, followlinks=False):
            current = Path(current)
            dirs[:] = [d for d in dirs if not (current / d).is_symlink() and not self._skip(current / d)]
            wd = self._libc.inotify_add_watch(self.fd, os.fsencode(current), self.MASK)
            if wd < 0:
                error = ctypes.get_errno()
                if error in (2, 20):  # A directory vanished during registration.
                    continue
                raise OSError(error, 'inotify directory registration failed')
            self._dirs[wd] = current

    def _forget_tree(self, path):
        for wd, current in list(self._dirs.items()):
            if current == path or path in current.parents:
                self._dirs.pop(wd, None)
                self._libc.inotify_rm_watch(self.fd, wd)

    def _consume(self, data):
        offset = 0
        while offset + 16 <= len(data):
            wd, mask, _, size = struct.unpack_from('iIII', data, offset)
            name = os.fsdecode(data[offset + 16:offset + 16 + size].split(b'\0', 1)[0])
            offset += 16 + size
            if mask & self.OVERFLOW:
                self._changed()
                self._forget_tree(self.root)
                self._add_tree(self.root)
                continue
            parent = self._dirs.get(wd)
            if parent is None:
                continue
            if mask & self.IGNORED:
                self._dirs.pop(wd, None)
                continue
            path = parent / name if name else parent
            if self._skip(path):
                continue
            self._changed(path)
            if mask & self.ISDIR and mask & (self.MOVED_FROM | self.DELETE):
                self._forget_tree(path)
            if mask & self.ISDIR and mask & (self.CREATE | self.MOVED_TO):
                self._add_tree(path)

    def _read(self):
        try:
            while not self._stop.is_set():
                ready, _, _ = select.select([self.fd], [], [], 0.25)
                if ready:
                    self.flush()
        except (OSError, ValueError):
            self.failed = True
            self.notify.set()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        os.close(self.fd)
