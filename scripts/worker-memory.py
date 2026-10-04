"""Kernel allocation limits for Python agent workers, separate from Node."""
import os
import resource
import sys

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
