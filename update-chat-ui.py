"""Run in the existing Colab notebook to install and back up the chat UI."""
from pathlib import Path
import hashlib
import os
import tempfile
import urllib.request

EXPECTED = {'bundle/index.js': '9a27a4bbf1db59847284ec6efd7482bc641167aab9918c9bff19d3afa73695f8', 'bundle/style.css': 'b9e2be173119885c46577bfd57902f07eec52541a389de196600807ffcd005aa', 'manifest.json': 'ad69bcf5f5c1c746a5b9dd35a46082f506d44f9bfbc12b307f51a94d712b79ff', 'plugin_api.py': 'c2b9283e6e96b1692a7e510e48c37356726df941b9b71f77896a6d86be8cd90d'}
LAUNCHER_SHA = 'b383691b085f6ec2c51a55e9479f5f69a51b69711aea195fcec7302383f733f8'
BASE = 'https://raw.githubusercontent.com/Sugamdeol/hermes-render/f6014949d4623c6cf62fc104d922b31f7c64162b/dashboard-plugins/hermes-chat-dashboard/dashboard/'
TOOLS_EXPECTED = {'chat-bridge.py': 'b7f7301f5c6d50d96a00d06c75cc3d6ecad4e44b1de2a9ccbea9f8cf9896d8eb', 'patch-chat-bridge.py': 'e45faa08e831fc19c17a3673c1a5321364b91c67c4bd944ac7effaa776853a26'}
ROOT_URL = BASE.split('/dashboard-plugins/')[0]
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
# Download and verify both bridge modules before changing any files.
tool_payloads = {}
for name, digest in TOOLS_EXPECTED.items():
    with urllib.request.urlopen(ROOT_URL + '/scripts/' + name, timeout=60) as response:
        payload = response.read()
    if hashlib.sha256(payload).hexdigest() != digest:
        raise RuntimeError('Chat bridge download failed verification; no files changed.')
    compile(payload, name, 'exec')
    tool_payloads[name] = payload
web = Path('/opt/hermes/hermes_cli/web_server.py')
namespace = {'__name__': 'verified_chat_bridge_patch'}
exec(compile(tool_payloads['patch-chat-bridge.py'], 'patch-chat-bridge.py', 'exec'), namespace)
code = namespace['patch'](web.read_text())
if '_lite_pty_lock = asyncio.Lock()' not in code:
    raise RuntimeError('Unknown dashboard version; update the full launcher first.')
with urllib.request.urlopen('https://raw.githubusercontent.com/Sugamdeol/hermes-render/f6014949d4623c6cf62fc104d922b31f7c64162b/run-colab.py', timeout=60) as response:
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
for name, payload in tool_payloads.items():
    atomic_write(Path("/opt/render-tools") / name, payload)
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
