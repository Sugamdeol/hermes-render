"""Apply recovery fixes to a running Colab VM and restart only its gateway."""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import urllib.request

TOOLS = Path('/opt/render-tools')
INSTALL = Path('/opt/hermes')
FILES = ('task-recovery.py', 'native-task-recovery.py',
         'native-ui-task-recovery.py', 'patch-native-task-recovery.py')
REPO = 'Sugamdeol/hermes-render'
PROC = Path('/proc')


def process_stat(pid):
    fields = (PROC/str(pid)/'stat').read_text().rsplit(')', 1)[1].split()
    return int(fields[1]), fields[19]


def command_line(pid):
    return [part.decode(errors='replace') for part in (PROC/str(pid)/'cmdline').read_bytes().split(b'\0') if part]


class GatewayWorker:
    def __init__(self, pid, started):
        self.pid, self.started = pid, started

    def terminate(self):
        if process_stat(self.pid)[1] != self.started:
            raise RuntimeError('Gateway process changed; run this update cell again.')
        os.kill(self.pid, signal.SIGTERM)


def gateway_worker(agent):
    if agent is None or agent.process.poll() is not None:
        raise RuntimeError('Hermes is not running. Run the latest Start cell instead.')
    processes = {}
    for path in PROC.iterdir():
        if path.name.isdigit():
            try:
                processes[int(path.name)] = process_stat(int(path.name))
            except (OSError, ValueError, IndexError):
                pass
    descendants, pending = set(), [agent.process.pid]
    while pending:
        pid = pending.pop()
        if pid in descendants:
            continue
        descendants.add(pid)
        pending.extend(child for child, (parent, started) in processes.items() if parent == pid)
    matches = []
    for pid in descendants - {agent.process.pid}:
        try:
            args = command_line(pid)
            gateway = any(args[i:i+2] == ['gateway', 'run'] for i in range(len(args)-1))
            if gateway and any('/opt/hermes/' in arg for arg in args):
                parent, started = processes[pid]
                parent_args = command_line(parent)
                if any('gateway-supervisor.sh' in arg for arg in parent_args):
                    matches.append(GatewayWorker(pid, started))
        except (OSError, ValueError, IndexError, KeyError):
            continue
    if len(matches) != 1:
        raise RuntimeError('Could not identify exactly one supervised gateway; no process was stopped.')
    return matches[0]


def download(url):
    request = urllib.request.Request(url, headers={'User-Agent':'Hermes-Recovery-Update', 'Cache-Control':'no-cache'})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def main(agent):
    gateway_worker(agent)  # Validate the process layout before changing files.
    revision = json.loads(download(f'https://api.github.com/repos/{REPO}/commits/main'))['sha']
    if not re.fullmatch('[0-9a-f]{40}', revision):
        raise RuntimeError('Unexpected update revision; no files were changed.')
    payloads = {}
    for name in FILES:
        content = download(f'https://raw.githubusercontent.com/{REPO}/{revision}/scripts/{name}')
        compile(content, name, 'exec')
        payloads[name] = content
    for name, content in payloads.items():
        destination = TOOLS / name
        previous = destination.stat() if destination.exists() else TOOLS.stat()
        temporary = destination.with_name(destination.name + '.update.tmp')
        temporary.write_bytes(content)
        temporary.chmod(0o755)
        os.chown(temporary, previous.st_uid, previous.st_gid)
        temporary.replace(destination)
    subprocess.run([str(INSTALL/'.venv/bin/python'), str(TOOLS/'patch-native-task-recovery.py'), str(INSTALL)],
                   env=agent.env, check=True)
    worker = gateway_worker(agent)  # Recheck after downloads; never target a stale PID.
    worker.terminate()
    print('Recovery fix installed. Only the Telegram gateway is restarting.')
    print('Dashboard, tunnel, storage worker and local sessions stay running.')
    print('The supervisor will restart the gateway and resume interrupted chats.')


if __name__ == '__main__':
    main(globals().get('HERMES_COLAB'))
