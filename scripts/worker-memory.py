"""Kernel allocation limits for Python agent workers, separate from Node."""
import os
import resource
import sys
from pathlib import Path

_applied = False


def apply_worker_limit():
    global _applied
    if _applied:
        return
    data_mb = min(256, max(96, int(os.environ.get('HERMES_PYTHON_DATA_MB', '192'))))
    amount = data_mb * 1048576
    soft, hard = resource.getrlimit(resource.RLIMIT_DATA)
    if hard != resource.RLIM_INFINITY:
        amount = min(amount, hard)
    resource.setrlimit(resource.RLIMIT_DATA, (amount, amount))
    # Optional virtual address-space cap; disabled by default because thread
    # stacks and reserved mappings can be much larger than resident RAM.
    address_mb = int(os.environ.get('HERMES_PYTHON_AS_MB', '0'))
    if address_mb > 0:
        amount_as = address_mb * 1048576
        _, hard_as = resource.getrlimit(resource.RLIMIT_AS)
        if hard_as != resource.RLIM_INFINITY:
            amount_as = min(amount_as, hard_as)
        resource.setrlimit(resource.RLIMIT_AS, (amount_as, amount_as))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    _applied = True
    print(f'[worker-memory] Python allocation cap: RLIMIT_DATA={amount//1048576}MiB; RLIMIT_AS={address_mb if address_mb > 0 else "disabled"}', file=sys.stderr, flush=True)


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
