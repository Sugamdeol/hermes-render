"""Log cgroup usage and RSS by executable, without commands or secrets."""
from pathlib import Path
import importlib.util
import threading
import time

spec = importlib.util.spec_from_file_location('agent_budget', Path(__file__).with_name('agent-budget.py'))
budget = importlib.util.module_from_spec(spec)
spec.loader.exec_module(budget)

def supervise_budget():
    while True:
        try:
            budget.monitor()
        except Exception as error:
            budget.STATUS['heartbeat'] = 0
            print(f'[agent-budget] monitor failed ({type(error).__name__}: {error}); restarting in 1s', flush=True)
            time.sleep(1)

threading.Thread(target=supervise_budget, daemon=True).start()

while True:
    try:
        used = int(Path('/sys/fs/cgroup/memory.current').read_text())
        stats = dict(line.split() for line in Path('/sys/fs/cgroup/memory.stat').read_text().splitlines())
        anon = int(stats.get('anon', 0)) / 1048576
        file_cache = int(stats.get('file', 0)) / 1048576
        inactive_file = int(stats.get('inactive_file', 0)) / 1048576
        processes = []
        for proc in Path('/proc').iterdir():
            if not proc.name.isdigit():
                continue
            try:
                status = proc.joinpath('status').read_text().splitlines()
                fields = dict(line.split(':', 1) for line in status if ':' in line)
                rss = int(fields.get('VmRSS', '0 kB').split()[0])
                processes.append((rss, proc.name, fields.get('Name', '').strip()))
            except (OSError, ValueError):
                continue
        top = ', '.join(f'{name}[{pid}]={rss/1024:.1f}MiB' for rss, pid, name in sorted(processes, reverse=True)[:8])
        healthy = time.monotonic() - budget.STATUS['heartbeat'] < 5
        print(f'[memory] cgroup={used/1048576:.1f}MiB anon={anon:.1f}MiB file={file_cache:.1f}MiB inactive_file={inactive_file:.1f}MiB; budget_alive={healthy} mode={budget.STATUS["mode"]} workers={budget.STATUS["workers"]} workersRSS={budget.STATUS["rss"]/1048576:.1f}MiB; {top}', flush=True)
    except (OSError, ValueError):
        pass
    time.sleep(30)
