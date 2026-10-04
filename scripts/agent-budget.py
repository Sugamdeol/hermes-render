"""Aggregate agent budget. Kernel cgroup cap when delegated, watchdog otherwise.

Dashboard, proxy and storage run outside the worker group. The fallback is
sampling-based and deliberately never advertised as a strict kernel limit.
"""
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
CGROUP = Path(os.environ.get('HERMES_AGENT_CGROUP', '/sys/fs/cgroup/hermes-workers'))
MIB = 1048576
BUDGET = min(350, max(128, int(os.environ.get('HERMES_AGENT_RAM_MB', '300')))) * MIB
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


def enter_kernel_group():
    if (REGISTRY / 'kernel').exists():
        # Fail closed if a configured hard cap cannot be entered.
        (CGROUP / 'cgroup.procs').write_text(str(os.getpid()))


def setup_kernel_group():
    try:
        CGROUP.mkdir(exist_ok=True)
        (CGROUP / 'memory.max').write_text(str(BUDGET))
        (CGROUP / 'memory.swap.max').write_text('0')
        (CGROUP / 'memory.oom.group').write_text('1')
        os.chown(CGROUP / 'cgroup.procs', 10000, 10000)
        (REGISTRY / 'kernel').touch()
        return True
    except OSError:
        return False


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


def stop_workers(table, selected, role):
    victims = [pid for pid, worker_role in selected.items() if worker_role == role]
    for pid in reversed(victims):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            print(f'[agent-budget] cannot signal worker {pid}; permission denied', flush=True)
    print(f'[agent-budget] cancelled {role} worker tree ({len(victims)} processes); retry the task after restart', flush=True)


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
    try:
        hard = (REGISTRY / 'kernel').exists() and int((CGROUP / 'memory.max').read_text()) == BUDGET and os.access(CGROUP / 'cgroup.procs', os.W_OK)
    except (OSError, ValueError):
        hard = False
    if not hard:
        (REGISTRY / 'kernel').unlink(missing_ok=True)
        hard = setup_kernel_group()
    print(f'[agent-budget] aggregate={BUDGET//MIB}MiB; mode={"kernel-cgroup" if hard else "watchdog (sampled, not a hard cap)"}; dashboard/storage excluded', flush=True)
    STATUS['mode'] = 'kernel-cgroup' if hard else 'watchdog'
    # Same UID as Hermes workers allows signalling without CAP_KILL.
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
        # RSS counts shared pages conservatively in watchdog mode. A hard
        # cgroup accounts the worker group accurately in the kernel instead.
        if (not hard and usage > BUDGET) or noncache > 430 * MIB:
            role = 'chat' if 'chat' in selected.values() else 'gateway'
            if selected:
                print(f'[agent-budget] workersRSS={usage/MIB:.1f}MiB container={total/MIB:.1f}MiB noncache={noncache/MIB:.1f}MiB', flush=True)
                stop_workers(table, selected, role)
        time.sleep(0.2)


def gateway():
    while True:
        command = [sys.executable, __file__, 'run', 'gateway', '/opt/hermes/.venv/bin/hermes', 'gateway', 'run']
        proc = subprocess.Popen(command, start_new_session=True)
        status = proc.wait()
        # Also remove descendants that chose another process group and were
        # observed by the monitor. The monitor retains their PID/start pairs.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        print(f'[agent-budget] gateway exited ({status}); restarting in 20s', flush=True)
        time.sleep(20)


if __name__ == '__main__':
    mode = sys.argv[1]
    if mode == 'monitor':
        monitor()
    elif mode == 'gateway':
        gateway()
    elif mode == 'run':
        register(sys.argv[2])
        enter_kernel_group()
        os.execvpe(sys.argv[3], sys.argv[3:], os.environ)
    else:
        raise SystemExit('unknown agent budget mode')
