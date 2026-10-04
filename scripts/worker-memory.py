"""Smaller thread reservations for Python agents; no allocation caps."""
import os
import sys
import threading
from pathlib import Path

_applied = False


def apply_worker_limit():
    global _applied
    if _applied:
        return
    threading.stack_size(2 * 1048576)
    os.environ['HERMES_CRON_MAX_PARALLEL'] = '1'
    os.environ.setdefault('HERMES_TUI_RPC_POOL_WORKERS', '2')
    _applied = True
    print('[worker-memory] lightweight profile: smaller thread stacks, serial cron; allocation limits disabled', file=sys.stderr, flush=True)


def container_memory():
    """Total and conservative non-cache estimate, not another kernel limit."""
    current = Path(os.environ.get('HERMES_TOTAL_MEMORY_FILE', '/sys/fs/cgroup/memory.current'))
    try:
        total = int(current.read_text())
    except (OSError, ValueError):
        return 0, 0
    try:
        stats = {key: int(value) for key, value in (line.split() for line in current.with_name('memory.stat').read_text().splitlines())}
    except (OSError, ValueError):
        return total, total
    # Both active and inactive clean disk cache can be reclaimed. Preserve
    # tmpfs/shared memory, dirty/writeback and locked pages in the estimate.
    cache = max(0, stats.get('file', 0) - stats.get('shmem', 0) - stats.get('file_dirty', 0) - stats.get('file_writeback', 0) - stats.get('unevictable', 0))
    return total, max(0, total - cache)
