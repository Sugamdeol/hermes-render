"""Run inside the existing Colab cell to restore Files Search/Analysis without restoring data."""
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import urllib.request

# Public source assets only. Hashes prevent partial or mismatched downloads.
ASSETS = {'patch-files-analysis.py': 'd483b20ec10fec0c7d397c8c4c13f963eb27f91d4807cebcec062e1b4383cf8c', 'files-analysis-router.py': '63b3c9d980880df2827c3e0008007bb38177553057a0bc41a8621a1fe3a4d00f', 'FilesStorageTools.tsx': 'c71ffe409ca1f2b425e11559d73867520daede1afb55d40c5cb72f7dfc4cef2a'}


def main(agent):
    root = Path('/opt/hermes')
    if not (root / '.venv/bin/python').is_file():
        raise RuntimeError('Start the Hermes Colab runtime first')
    env = {**os.environ, **agent.env}
    env['HERMES_HOME'] = '/opt/data'
    log = Path('/content/hermes-files-tab-repair.log')
    with tempfile.TemporaryDirectory(prefix='hermes-files-tab-') as directory:
        assets = Path(directory)
        for name, expected in ASSETS.items():
            url = 'https://raw.githubusercontent.com/Sugamdeol/hermes-render/main/scripts/' + name
            with urllib.request.urlopen(url, timeout=60) as response:
                content = response.read()
            if hashlib.sha256(content).hexdigest() != expected:
                raise RuntimeError('Files tab repair assets changed; download the latest updater and retry')
            (assets / name).write_bytes(content)
        print('Building restored Files Search/Analysis. Build output: ' + str(log), flush=True)
        with log.open('w') as output:
            result = subprocess.run([str(root / '.venv/bin/python'), str(assets / 'patch-files-analysis.py'), str(root), '--build'],
                                    cwd=root, env=env, stdout=output, stderr=subprocess.STDOUT)
    redact = agent.backup.__func__.__globals__['redact_secrets']
    print(redact('\n'.join(log.read_text(errors='replace').splitlines()[-60:]), agent.env))
    if result.returncode:
        raise RuntimeError('Files tab repair failed; send the redacted output above')
    # Restart only the native dashboard worker; its existing supervisor respawns it.
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            args = (proc / 'cmdline').read_bytes().decode(errors='replace').split('\0')
            if 'dashboard' in args and any(arg.startswith('/opt/hermes/') for arg in args):
                os.kill(int(proc.name), signal.SIGTERM)
        except (OSError, ValueError):
            pass
    print('Files tab assets installed. Dashboard restart requested; refresh after 15 seconds.')


if __name__ == '__main__':
    agent = globals().get('HERMES_COLAB') or globals().get('HERMES')
    if agent is None:
        raise RuntimeError('Run this updater in the same Colab notebook as HERMES_COLAB')
    main(agent)
