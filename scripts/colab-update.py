"""Explicit official-upstream updates for the Colab launcher."""
import fcntl
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request

UPSTREAM = 'https://github.com/NousResearch/hermes-agent.git'


def resolve(ns, ref):
    if not isinstance(ref, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,199}', ref) or '..' in ref:
        raise ValueError('Use main, a release tag, or a full commit SHA')
    if re.fullmatch(r'[0-9a-f]{40}', ref):
        return ref
    result = ns['run'](['git', 'ls-remote', UPSTREAM, 'refs/heads/' + ref,
                        'refs/tags/' + ref, 'refs/tags/' + ref + '^{}'],
                       capture_output=True, text=True)
    rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
    # Prefer the commit behind an annotated release tag.
    rows.sort(key=lambda row: not row[1].endswith('^{}'))
    if not rows or not re.fullmatch(r'[0-9a-f]{40}', rows[0][0]):
        raise RuntimeError('Official upstream ref was not found')
    return rows[0][0]


def check(ns, ref='main'):
    current = ns['run'](['git', '-c', 'safe.directory=' + str(ns['INSTALL']),
                         'rev-parse', 'HEAD'], cwd=ns['INSTALL'], capture_output=True,
                        text=True).stdout.strip()
    target = resolve(ns, ref)
    result = {'current': current, 'target': target, 'ref': ref, 'update_available': current != target}
    print(json.dumps(result, indent=2))
    return result


def idle(agent, home):
    # active_sessions counts recently used chats, including idle conversations.
    # Read the actual gateway work count instead (status() omits these fields).
    with urllib.request.urlopen('http://127.0.0.1:10000/healthz', timeout=5) as response:
        status = json.load(response)
    count = status.get('active_agents')
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise RuntimeError('Cannot verify running tasks; retry when gateway status is available')
    if count or status.get('gateway_busy'):
        raise RuntimeError('Wait for running tasks to finish before updating')
    journal = home / '.render-recovery.json'
    records = json.loads(journal.read_text()) if journal.exists() else {}
    if not isinstance(records, dict) or any(not isinstance(v, dict) or v.get('status', 'pending') == 'pending' for v in records.values()):
        raise RuntimeError('Finish or stop unfinished tasks before updating')


def update(ns, agent, ref='main'):
    install, tools, home = (ns[k] for k in ('INSTALL', 'TOOLS', 'DATA'))
    with open('/tmp/hermes-colab-update.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another notebook update is already running')
        info = check(ns, ref)
        if not info['update_available']:
            return agent
        idle(agent, home)
        # Reject incompatible patch sets while the current agent is still running.
        with tempfile.TemporaryDirectory(prefix='hermes-update-check-') as td:
            candidate = Path(td) / 'candidate'
            ns['run'](['git', 'init', candidate])
            ns['run'](['git', 'remote', 'add', 'origin', UPSTREAM], cwd=candidate)
            ns['run'](['git', 'fetch', '--depth=1', 'origin', info['target']], cwd=candidate)
            ns['run'](['git', 'checkout', '--detach', 'FETCH_HEAD'], cwd=candidate)
            ns['run']([str(install / '.venv/bin/python'), tools / 'patch-colab-full.py', candidate])
        idle(agent, home)
        agent.backup()  # Never stop on an unconfirmed backup.
        stamp = str(time.time_ns())
        previous = install.with_name(install.name + '-before-update-' + stamp)
        old_tools = tools.with_name(tools.name + '-before-update-' + stamp)
        revision = ns['run'](['git', 'rev-parse', 'HEAD'], cwd=ns['SOURCE'],
                             capture_output=True, text=True).stdout.strip()
        shutil.copytree(tools, old_tools, symlinks=True)
        agent.stop()  # A second checkpoint closes the preflight/stop gap.
        if agent.process.poll() is None:
            raise RuntimeError('Previous host did not stop; update cancelled')
        install.rename(previous)
        try:
            ns['install_runtime'](revision, target=info['target'])
            with ns['StartupProgress'](ns['cache_directory'](), warm=False) as progress:
                replacement = ns['launch_with_progress'](agent.env, progress)
        except BaseException as error:
            running = ns.get('HERMES_COLAB')
            if running and running.process.poll() is None:
                running.stop(force=True)
                if running.process.poll() is None:
                    raise RuntimeError('Update failed and candidate is still running; stop it before recovery') from error
            if install.exists():
                install.rename(install.with_name(install.name + '-failed-update-' + stamp))
            previous.rename(install)
            if tools.exists():
                tools.rename(tools.with_name(tools.name + '-failed-update-' + stamp))
            old_tools.rename(tools)
            with ns['StartupProgress'](ns['cache_directory'](), warm=True) as progress:
                ns['HERMES_COLAB'] = ns['launch_with_progress'](agent.env, progress)
            raise RuntimeError('Update failed; previous runtime restarted. User data kept; inspect startup logs.') from error
        selection = ns['cache_directory']() / 'selected-upstream.txt'
        temporary = selection.with_suffix('.tmp')
        temporary.write_text(info['target'] + '\n')
        temporary.replace(selection)
        print('Updated Hermes to', info['target'], '\nPrevious runtime retained at', previous)
        return replacement
