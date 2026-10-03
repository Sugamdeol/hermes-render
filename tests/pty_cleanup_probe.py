"""A disconnected PTY must reap its launcher and kill a resistant backend."""
import os
from pathlib import Path
import sys
import time
from hermes_cli.pty_bridge import PtyBridge

child = 'import signal,time; signal.signal(signal.SIGHUP,signal.SIG_IGN); signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(120)'
parent = 'import subprocess,sys,time; p=subprocess.Popen([sys.executable,"-c",sys.argv[1]]); print(p.pid,flush=True); time.sleep(120)'
for attempt in range(5):
    bridge = PtyBridge.spawn([sys.executable, '-c', parent, child])
    output = b''
    deadline = time.monotonic() + 5
    while b'\n' not in output and time.monotonic() < deadline:
        output += bridge.read() or b''
    backend = int(output.strip())
    time.sleep(0.1)
    bridge.close()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            state = Path(f'/proc/{backend}/stat').read_text().split(') ', 1)[1].split()[0]
        except FileNotFoundError:
            break
        if state == 'Z':
            break
        time.sleep(0.05)
    else:
        os.kill(backend, 9)
        raise AssertionError('PTY backend survived disconnect')
    assert not bridge.is_alive()
print('Five disconnects terminated both launcher and resistant backend')
