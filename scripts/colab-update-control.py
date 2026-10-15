"""Notebook-owned update worker; HTTP handlers only queue authenticated requests."""
import fcntl
import json
import os
from pathlib import Path
import runpy
import threading
import time

BUSY = {'queued', 'running'}


def write(path, value):
    temporary = path.with_name(path.name + '.' + str(threading.get_ident()) + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(value, handle)
    temporary.replace(path)


def read(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def execute(ns, directory, request):
    status = {'id': request['id'], 'action': request['action'], 'phase': 'running',
              'message': 'Checking upstream…' if request['action'] == 'check' else 'Backing up and updating Hermes. The dashboard may disconnect.'}
    write(directory / 'status.json', status)
    try:
        agent = ns.get('HERMES_COLAB')
        if not agent or agent.process.poll() is not None:
            raise RuntimeError('Start Hermes in the notebook before updating')
        updater = runpy.run_path(str(ns['TOOLS'] / 'colab-update.py'))
        if request['action'] == 'check':
            status['result'] = updater['check'](ns, request['ref'])
            status['message'] = 'Update available.' if status['result']['update_available'] else 'Hermes is up to date.'
        elif request['action'] == 'update':
            ns['HERMES_COLAB'] = updater['update'](ns, agent, request['ref'])
            status['message'] = 'Hermes updated. Reopen the dashboard link from your notebook if the address changed.'
        else:
            raise ValueError('Unknown update action')
        status['phase'] = 'succeeded'
    except Exception as exc:
        status['phase'] = 'failed'
        status['message'] = ns['redact_secrets'](str(exc), getattr(ns.get('HERMES_COLAB'), 'env', {}))[:1500]
    status['finished_at'] = time.time()
    write(directory / 'status.json', status)


def serve(context, directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    # A terminated notebook must never replay an old update request.
    (directory / 'request.json').unlink(missing_ok=True)
    previous = read(directory / 'status.json')
    if previous.get('phase') in BUSY:
        write(directory / 'status.json', {'phase': 'failed', 'message': 'The notebook restarted during an update. Check its startup logs before retrying.'})
    worker = None
    while True:
        write(directory / 'heartbeat.json', {'time': time.time()})
        if worker is None or not worker.is_alive():
            with (directory / 'queue.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                request = read(directory / 'request.json')
                if request:
                    (directory / 'request.json').unlink(missing_ok=True)
                    worker = threading.Thread(target=execute, args=(context["ns"], directory, request), daemon=True)
                    worker.start()
        time.sleep(1)


def start(ns):
    # runpy creates new launcher globals on each notebook rerun. Keep one
    # process-wide worker and redirect it to the newest live launcher handle.
    existing = getattr(threading, '_hermes_colab_update_control', None)
    if existing and existing['thread'].is_alive():
        existing['ns'] = ns
        return
    directory = Path(ns['HERMES_COLAB'].env['HERMES_COLAB_UPDATE_DIR'])
    context = {'ns': ns}
    thread = threading.Thread(target=serve, args=(context, directory), daemon=True)
    context['thread'] = thread
    threading._hermes_colab_update_control = context
    thread.start()
