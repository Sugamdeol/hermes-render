"""Worker launcher and memory observations; no memory caps or cancellations."""
import json
import os
from pathlib import Path
import signal
import pwd
import subprocess
import sys
import time
import runpy

container_memory = runpy.run_path(Path(__file__).with_name("worker-memory.py"))["container_memory"]

REGISTRY = Path(os.environ.get('HERMES_WORKER_REGISTRY', '/tmp/hermes-agent-workers'))
MIB = 1048576
STATUS = {'heartbeat': 0, 'workers': 0, 'rss': 0, 'mode': 'starting'}


def processes():
    result, visible_ids = {}, {}
    # Some hosts mount /proc from an outer PID namespace. Translate IDs to
    # the monitor's namespace before signalling; never target an outer PID.
    own_status = Path('/proc/self/status').read_text()
    own_ids = next((line.split()[1:] for line in own_status.splitlines() if line.startswith('NSpid:')), [str(os.getpid())])
    level = len(own_ids) - 1
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields = (path / 'stat').read_text().split(') ', 1)[1].split()
            if fields[0] == 'Z':
                continue
            if level:
                ids = next((line.split()[1:] for line in (path / 'status').read_text().splitlines() if line.startswith('NSpid:')), [])
                if len(ids) <= level:
                    continue
                visible_ids[int(path.name)] = int(ids[level])
            rss = int(fields[21]) * os.sysconf('SC_PAGE_SIZE')
            result[int(path.name)] = (int(fields[1]), int(fields[2]), fields[19], rss)
        except (OSError, ValueError, IndexError):
            continue
    if level:
        result = {visible_ids[pid]: (visible_ids.get(parent, 0), visible_ids.get(group, 0), start, rss) for pid, (parent, group, start, rss) in result.items()}
    return result


def register(role):
    REGISTRY.mkdir(mode=0o700, parents=True, exist_ok=True)
    pid = os.getpid()
    start = processes()[pid][2]
    (REGISTRY / str(pid)).write_text(json.dumps({'role': role, 'start': start}))


def descendants(table, roots, tracked):
    selected = {pid: role for pid, role in roots.items() if pid in table}
    for pid, (start, role) in tracked.items():
        if pid in table and table[pid][2] == start:
            selected[pid] = role
    changed = True
    while changed:
        changed = False
        for pid, (parent, group, start, rss) in table.items():
            if pid not in selected and (parent in selected or group in selected):
                selected[pid] = selected.get(parent, selected.get(group))
                changed = True
    return selected


def monitor():
    REGISTRY.mkdir(mode=0o700, parents=True, exist_ok=True)
    account = None
    if os.geteuid() == 0:
        try:
            account = pwd.getpwnam('hermes')
        except KeyError:
            account = None
        if account is not None:
            os.chown(REGISTRY, account.pw_uid, account.pw_gid)
    (REGISTRY / 'kernel').unlink(missing_ok=True)
    print('[agent-budget] observe only; no memory caps or worker cancellation', flush=True)
    STATUS['mode'] = 'observe'
    if account is not None:
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
    tracked = {}
    while True:
        table, roots = processes(), {}
        for record in REGISTRY.iterdir():
            if not record.name.isdigit():
                continue
            try:
                pid, data = int(record.name), json.loads(record.read_text())
                if pid in table and table[pid][2] == data['start']:
                    roots[pid] = data['role']
                else:
                    record.unlink(missing_ok=True)
            except (OSError, ValueError, KeyError):
                continue
        selected = descendants(table, roots, tracked)
        tracked = {pid: (table[pid][2], role) for pid, role in selected.items()}
        usage = sum(table[pid][3] for pid in selected)
        STATUS.update(heartbeat=time.monotonic(), workers=len(selected), rss=usage)
        total, noncache = container_memory()
        time.sleep(2)


if __name__ == '__main__':
    mode = sys.argv[1]
    if mode == 'monitor':
        monitor()
    elif mode == 'run':
        register(sys.argv[2])
        # Set before exec so glibc starts with one allocation arena, including
        # on existing deployments whose old Render env still specifies two.
        os.environ['MALLOC_ARENA_MAX'] = '1'
        os.environ['HERMES_AGENT_CACHE_MAX_SIZE'] = '1'
        os.environ['HERMES_AGENT_CACHE_IDLE_TTL_SECONDS'] = '30'
        # Remove the adapter's previous 64 MiB Node cap on existing services.
        if os.environ.get('NODE_OPTIONS') == '--max-old-space-size=64':
            os.environ.pop('NODE_OPTIONS')
        os.execvpe(sys.argv[3], sys.argv[3:], os.environ)
    else:
        raise SystemExit('unknown agent budget mode')
