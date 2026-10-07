"""Run in the existing Colab notebook to install and back up the chat UI."""
from pathlib import Path
import hashlib
import os
import tempfile
import urllib.request

EXPECTED = {'bundle/index.js': '88d02dda5c581e5e1ad4ef1973485fb709cb8e7465ee567563b54ebaec669100', 'bundle/style.css': 'a7b8e4ec38b04094885635cf624cb7607471d7ecf68123bf906993743d5069e3', 'manifest.json': '3ba78999bab286a3b1e9d1110f771ae9bd1d5e78465053583b7ff87e05d253dd', 'plugin_api.py': '41ba458ce95277bc46adf76ba91ad653213a1422630be71dc0c126582d5e68ff'}
BASE = 'https://raw.githubusercontent.com/Sugamdeol/hermes-render/main/dashboard-plugins/hermes-chat-dashboard/dashboard/'
target = Path('/opt/data/plugins/hermes-chat-dashboard/dashboard')
if 'HERMES_COLAB' not in globals() or not target.is_dir():
    raise RuntimeError('Run this in the notebook where HERMES_COLAB is already running.')
payloads = {}
for name, digest in EXPECTED.items():
    with urllib.request.urlopen(BASE + name, timeout=60) as response:
        payload = response.read()
    if hashlib.sha256(payload).hexdigest() != digest:
        raise RuntimeError('UI version changed. Download the latest update-chat-ui.py and retry.')
    payloads[name] = payload
# Update the bundled copy too, so bootstrap cannot reinstall an older UI.
for destination in (target, Path('/opt/render-tools/dashboard-plugins/hermes-chat-dashboard/dashboard')):
    for name, payload in payloads.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        info = path.stat() if path.exists() else target.stat()
        fd, temporary = tempfile.mkstemp(prefix='.chat-ui-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o644)
            if os.geteuid() == 0:
                os.chown(temporary, info.st_uid, info.st_gid)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)
print('Saving the updated UI and your data before restarting…')
HERMES_COLAB.stop()  # Includes a confirmed backup; failure leaves it running.
import subprocess
import time
log = Path('/content/hermes-colab.log')
fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
with os.fdopen(fd, 'a') as logfile:
    HERMES_COLAB.process = subprocess.Popen(
        ['bash', '/opt/render-tools/bootstrap.sh', 'sleep', 'infinity'],
        env=HERMES_COLAB.env, cwd='/opt/hermes', stdout=logfile,
        stderr=subprocess.STDOUT, start_new_session=True)
print('Restarting with the faster history backend…')
deadline = time.monotonic() + 600
while time.monotonic() < deadline:
    if HERMES_COLAB.process.poll() is not None:
        raise RuntimeError('Startup exited. Keep the runtime and inspect /content/hermes-colab.log.')
    try:
        with urllib.request.urlopen('http://127.0.0.1:10000/healthz', timeout=5) as response:
            import json
            status = json.load(response)
        if status.get('gateway_running') and status.get('gateway_state') == 'running':
            print('Updated, backed up and restarted. Press Ctrl+Shift+R on your dashboard.')
            break
    except (OSError, ValueError):
        pass
    time.sleep(2)
else:
    raise RuntimeError('Startup is still waiting. Inspect /content/hermes-colab.log; your data has been kept.')
