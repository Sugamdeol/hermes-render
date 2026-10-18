"""Update the running Colab backup worker without restarting chats or restoring data."""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
import urllib.request

REPO = 'Sugamdeol/hermes-render'
TOOLS = Path('/opt/render-tools')
PROC = Path('/proc')
LOG = Path('/content/hermes-colab.log')
FILES = ('git-storage.py', 'state-events.py', 'incremental-mirror.py', 'runtime-source-backup.py', 'backup-coverage.py')


def process_info(pid):
    root = PROC/str(pid)
    fields = (root/'stat').read_text().rsplit(')', 1)[1].split()
    if fields[0] == 'Z':
        raise ProcessLookupError('Process has exited')
    args = [item.decode(errors='replace') for item in (root/'cmdline').read_bytes().split(b'\0') if item]
    return int(fields[1]), fields[19], args


def processes():
    result = {}
    for path in PROC.iterdir():
        if path.name.isdigit():
            try:
                result[int(path.name)] = process_info(int(path.name))
            except (OSError, ValueError, IndexError):
                pass
    return result


def storage_worker(agent):
    if agent is None or agent.process.poll() is not None:
        raise RuntimeError('Hermes is not running. Use the latest Start cell instead.')
    records = processes()
    descendants, pending = set(), [agent.process.pid]
    managed = getattr(agent, 'storage_process', None)
    if managed is not None and managed.poll() is None:
        pending.append(managed.pid)
    while pending:
        pid = pending.pop()
        if pid in descendants:
            continue
        descendants.add(pid)
        pending.extend(child for child, info in records.items() if info[0] == pid)
    workers = [(pid, records[pid][1]) for pid in descendants if pid in records
               and str(TOOLS/'memory-log.py') in records[pid][2]]
    if len(workers) != 1:
        raise RuntimeError('Could not identify exactly one backup worker; no process was stopped.')
    return workers[0]


def wait_for_old_worker(pid, started, workdir, timeout=1800):
    if process_info(pid)[1] != started:
        raise RuntimeError('Backup process changed; run the cell again.')
    tracked = {pid:started}
    os.kill(pid, signal.SIGTERM)
    deadline, notice = time.monotonic()+timeout, 0
    while True:
        records = processes()
        # Track children created by the final save, including reparented ones.
        for child, (parent, birth, args) in records.items():
            if parent in tracked and records.get(parent, (None, None))[1] == tracked[parent]:
                tracked[child] = birth
            try:
                cwd = (PROC/str(child)/'cwd').resolve()
                if args and Path(args[0]).name.startswith('git') and (cwd == workdir or workdir in cwd.parents):
                    tracked[child] = birth
            except OSError:
                pass
        alive = [child for child, birth in tracked.items()
                 if child in records and records[child][1] == birth]
        if not alive:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError('Previous backup is still finishing; no second uploader was started. Keep Colab running.')
        if time.monotonic() >= notice:
            print('Waiting for the previous backup worker to finish safely…', flush=True)
            notice = time.monotonic()+30
        time.sleep(0.2)


def download(url):
    request = urllib.request.Request(url, headers={'User-Agent':'Hermes-Storage-Update', 'Cache-Control':'no-cache'})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def attach_stop(agent):
    if getattr(agent, '_storage_stop_attached', False):
        return
    original = agent.stop
    namespace = original.__func__.__globals__
    def stop(force=False):
        descendants = namespace['descendants']
        def include_worker(parent):
            result = descendants(parent)
            worker = getattr(agent, 'storage_process', None)
            if parent == agent.process.pid and worker is not None and worker.poll() is None:
                result.update(descendants(worker.pid))
            return result
        namespace['descendants'] = include_worker
        try:
            return original(force=force)
        finally:
            namespace['descendants'] = descendants
    agent.stop = stop
    agent._storage_stop_attached = True


def main(agent):
    pid, started = storage_worker(agent)
    revision = json.loads(download(f'https://api.github.com/repos/{REPO}/commits/main'))['sha']
    if not re.fullmatch('[0-9a-f]{40}', revision):
        raise RuntimeError('Unexpected update revision; no files were changed.')
    payloads = {}
    for name in FILES:
        content = download(f'https://raw.githubusercontent.com/{REPO}/{revision}/scripts/{name}')
        compile(content, name, 'exec')
        payloads[name] = content
    # Recheck the process identity before reading its environment or signalling it.
    if storage_worker(agent) != (pid, started):
        raise RuntimeError('Backup worker changed; run the cell again.')
    raw_env = (PROC/str(pid)/'environ').read_bytes()
    env = dict(item.decode().split('=', 1) for item in raw_env.split(b'\0') if b'=' in item)
    if env.get('HERMES_RUNTIME_PROFILE') != 'colab-full':
        raise RuntimeError('This update is only for the Colab runtime; no process was stopped.')
    for name, content in payloads.items():
        destination = TOOLS/name
        previous = destination.stat() if destination.exists() else TOOLS.stat()
        temporary = destination.with_name(destination.name+'.update.tmp')
        temporary.write_bytes(content)
        temporary.chmod(0o755)
        os.chown(temporary, previous.st_uid, previous.st_gid)
        temporary.replace(destination)
    workdir = Path(env.get('GIT_STATE_WORKDIR', '/content/hermes-git-cache/state')).resolve()
    attach_stop(agent)
    wait_for_old_worker(pid, started, workdir)
    repair = subprocess.run(
        ['/opt/hermes/.venv/bin/python', str(TOOLS/'git-storage.py'), 'repair-enc', env.get('HERMES_HOME', '/opt/data')],
        env=env, capture_output=True, text=True)
    if repair.stdout or repair.stderr:
        print((repair.stdout + repair.stderr).strip())
    if repair.returncode:
        raise RuntimeError('Missing .enc file recovery failed; local data was retained. Inspect the message above.')
    # Existing apps were already restored; keep recipe capture without reinstalling.
    env['HERMES_RUNTIME_DEPS_RESTORED'] = '1'
    with LOG.open('a') as log:
        agent.storage_process = subprocess.Popen(
            ['/opt/hermes/.venv/bin/python', str(TOOLS/'memory-log.py')],
            env=env, cwd='/opt/hermes', stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True)
    time.sleep(1)
    if agent.storage_process.poll() is not None:
        raise RuntimeError('Updated backup worker exited. Chats are still running; inspect /content/hermes-colab.log.')
    print('Changed-path backup worker installed. Dashboard and Telegram stayed running.')
    print('Confirming the first backup with the updated rules…')
    agent.backup()


if __name__ == '__main__':
    main(globals().get('HERMES_COLAB'))
