"""Run in the existing Colab notebook to install and back up the chat UI."""
from pathlib import Path
import hashlib
import os
import tempfile
import urllib.request

EXPECTED = {'bundle/index.js': 'ae278176b301104233391e04f4f09544c8f11771985aa52254c1abeccd7f079d', 'bundle/style.css': 'b9e2be173119885c46577bfd57902f07eec52541a389de196600807ffcd005aa', 'manifest.json': '65edab15d6ecbc083db1657a2ee5aee06f9f4a5f6b0a825d8d7fbabc801eaa83', 'plugin_api.py': '6cd9e0ee22a74300bd1d2c52c8f84c032c824de1208cd049191f06c7ae450d0f'}
LAUNCHER_SHA = '2598973971977a850e8bce7f5fe3e4c61a45ebf5e423a042283e37bf28c3ae63'
BASE = 'https://raw.githubusercontent.com/Sugamdeol/hermes-render/f1d945ce395886945269dbf220a2e230a7f44572/dashboard-plugins/hermes-chat-dashboard/dashboard/'
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
# Validate the installed bridge and launcher before changing any files.
web = Path('/opt/hermes/hermes_cli/web_server.py')
code = web.read_text()
old = "stdout=asyncio.subprocess.PIPE, start_new_session=True,"
new = "stdout=asyncio.subprocess.PIPE, limit=8 * 1024 * 1024, start_new_session=True,"
if old in code:
    code = code.replace(old, new, 1)
elif new not in code:
    raise RuntimeError('Unknown chat bridge version; update the launcher before retrying.')
code = code.replace("sys.executable, '-m', 'tui_gateway.entry',", "sys.executable, '-u', '-m', 'tui_gateway.entry',", 1)
compile(code, str(web), 'exec')
with urllib.request.urlopen('https://raw.githubusercontent.com/Sugamdeol/hermes-render/f1d945ce395886945269dbf220a2e230a7f44572/run-colab.py', timeout=60) as response:
    launcher = response.read()
if hashlib.sha256(launcher).hexdigest() != LAUNCHER_SHA:
    raise RuntimeError('Launcher version changed. Download the latest updater and retry.')
compile(launcher, 'run-colab.py', 'exec')

def atomic_write(path, payload, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    info = path.stat() if path.exists() else target.stat()
    fd, temporary = tempfile.mkstemp(prefix='.chat-ui-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        if os.geteuid() == 0:
            os.chown(temporary, info.st_uid, info.st_gid)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)

# Update bootstrap's bundled copy as well as the restored runtime plugin.
for destination in (target, Path('/opt/render-tools/dashboard-plugins/hermes-chat-dashboard/dashboard')):
    for name, payload in payloads.items():
        atomic_write(destination / name, payload)
atomic_write(web, code.encode())
launcher_path = Path('/content/hermes-colab-launcher.py')
atomic_write(launcher_path, launcher, 0o600)
import runpy
namespace = runpy.run_path(str(launcher_path), run_name='hermes_launcher_update')
HERMES_COLAB.__class__ = namespace['ColabAgent']
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
HERMES_COLAB.started_at = time.monotonic()
print('Restarting with the updated chat backend…')
deadline = time.monotonic() + 600
while time.monotonic() < deadline:
    if HERMES_COLAB.process.poll() is not None:
        raise RuntimeError('Startup exited. Keep the runtime and inspect /content/hermes-colab.log.')
    try:
        with urllib.request.urlopen('http://127.0.0.1:10000/healthz', timeout=5) as response:
            import json
            status = json.load(response)
        if status.get('gateway_running') and status.get('gateway_state') == 'running':
            HERMES_COLAB.dashboard()
            print('Updated, backed up and restarted. Refresh your dashboard.')
            break
    except (OSError, ValueError):
        pass
    time.sleep(2)
else:
    raise RuntimeError('Startup is still waiting. Inspect /content/hermes-colab.log; your data has been kept.')
