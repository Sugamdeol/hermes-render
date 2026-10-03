"""Log cgroup usage and RSS by executable, without commands or secrets."""
from pathlib import Path
import time

while True:
    try:
        used = int(Path('/sys/fs/cgroup/memory.current').read_text())
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
        print(f'[memory] cgroup={used/1048576:.1f}MiB; {top}', flush=True)
    except (OSError, ValueError):
        pass
    time.sleep(30)
