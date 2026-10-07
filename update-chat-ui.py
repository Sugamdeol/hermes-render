"""Run in the existing Colab notebook to install and back up the chat UI."""
from pathlib import Path
import hashlib
import os
import tempfile
import urllib.request

EXPECTED = {'bundle/index.js': '002ba6776ef65c5cc774850cb50f1649100b205b1b9dc26f0852b2e2462df5dc', 'bundle/style.css': 'b9e2be173119885c46577bfd57902f07eec52541a389de196600807ffcd005aa', 'manifest.json': '2cd46f0420d1bdbc8dc377b24f54c7ebfcfd70ddaf7a08e345277c58e3d3373d', 'plugin_api.py': 'd14d1d28f15d43c1c20100d7265257b1ca82d7d55455b1bb7c81306f4dfe23e7'}
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
# Update the already-installed isolated WS bridge without reapplying all patches.
web = Path('/opt/hermes/hermes_cli/web_server.py')
code = web.read_text()
old = "stdout=asyncio.subprocess.PIPE, start_new_session=True,"
new = "stdout=asyncio.subprocess.PIPE, limit=8 * 1024 * 1024, start_new_session=True,"
if old in code:
    code = code.replace(old, new, 1)
elif new not in code:
    raise RuntimeError('Unknown chat bridge version; update the launcher before retrying.')
code = code.replace("sys.executable, '-m', 'tui_gateway.entry',", "sys.executable, '-u', '-m', 'tui_gateway.entry',", 1)
web.write_text(code)
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
