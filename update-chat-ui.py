"""Run in the existing Colab notebook to install dashboard fixes and back up the chat UI."""
from pathlib import Path
import hashlib
import os
import tempfile
import urllib.request

EXPECTED = {'bundle/index.js': '9a27a4bbf1db59847284ec6efd7482bc641167aab9918c9bff19d3afa73695f8', 'bundle/style.css': 'b9e2be173119885c46577bfd57902f07eec52541a389de196600807ffcd005aa', 'manifest.json': 'ad69bcf5f5c1c746a5b9dd35a46082f506d44f9bfbc12b307f51a94d712b79ff', 'plugin_api.py': 'c2b9283e6e96b1692a7e510e48c37356726df941b9b71f77896a6d86be8cd90d'}
LAUNCHER_SHA = '335007717ec5b7af62496562e75da5ed6a4181e6e387fe98bf37916f03a80ff2'
BASE = 'https://raw.githubusercontent.com/Sugamdeol/hermes-render/5ab0f2b33fec11b82938abf248998278c66d81f6/dashboard-plugins/hermes-chat-dashboard/dashboard/'
TOOLS_EXPECTED = {'chat-bridge.py': 'b7f7301f5c6d50d96a00d06c75cc3d6ecad4e44b1de2a9ccbea9f8cf9896d8eb', 'patch-chat-bridge.py': '3d760cfb88dbdb4433a285c7695e05da751b8875f227f41204993e54411020b5', 'dashboard-runtime.py': 'f2e1f692a1ee3be6458e8159d99e198d8fd47bbd0092ad9aa7c71a6c58f697f8', 'patch-dashboard.py': '6ad3b603b64d11efcf93998918abb8d55751e16ded05535069a1739c51efaa67', 'debug-master.js': '967ef89275e56bbb7641f957cecb50af3131415375c8769592b6c6ddb54d886c', 'debug-master-api.py': '815ac5c3926e9f43762e1add7016c41ff2bb33ec66845995749650183a9d0da1', 'repair-dashboard-plugins.py': 'f79cf0feea1fb6a5e3f5e0cfe389f3a98ed4648f3ca8f45d399e5dc9d3fb56f9', 'dashboard-supervisor.sh': '9a59c9563963d8f8ef50dd25557505b2862c4f1bd48973aa1fc75f5ab9e32726', 'check-dashboard.py': '8b8f3f2309ac4d06e9ec9777c7130e816299581199e7d1a45f906ee4947286ff', 'bootstrap.sh': '663f53ad762aff5663c186b086e45ed4fff1e38fe34eb84ec14f762e97776021'}
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
    if name.endswith('.py'):
        compile(payload, name, 'exec')
    tool_payloads[name] = payload
web = Path('/opt/hermes/hermes_cli/web_server.py')
namespace = {'__name__': 'verified_chat_bridge_patch'}
exec(compile(tool_payloads['patch-chat-bridge.py'], 'patch-chat-bridge.py', 'exec'), namespace)
try:
    code = namespace['patch'](web.read_text())
except namespace['BridgeCompatibilityError'] as error:
    # Dashboard repairs must still work on older/different chat backends.
    # Keep the installed handler and all its auth checks untouched.
    code = web.read_text()
    print('Chat compatibility:', str(error))
namespace = {'__name__': 'verified_dashboard_patch'}
exec(compile(tool_payloads['patch-dashboard.py'], 'patch-dashboard.py', 'exec'), namespace)
code = namespace['patch'](code)
with urllib.request.urlopen('https://raw.githubusercontent.com/Sugamdeol/hermes-render/373bef1ae09f6650acb07e2cb97c8ef6356e892d/run-colab.py', timeout=60) as response:
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
    atomic_write(Path("/opt/render-tools") / name, payload, 0o755 if name.endswith('.sh') else 0o644)
atomic_write(web, code.encode())
run_repair = __import__("runpy").run_path("/opt/render-tools/repair-dashboard-plugins.py")
run_repair["repair"](target.parent.parent)
launcher_path = Path('/content/hermes-colab-launcher.py')
atomic_write(launcher_path, launcher, 0o600)
import runpy
namespace = runpy.run_path(str(launcher_path), run_name='hermes_launcher_update')
HERMES_COLAB.__class__ = namespace['ColabAgent']
print('Saving the updated UI and your data before restarting…')
HERMES_COLAB.stop()  # Includes a confirmed backup; failure leaves it running.
# The restart reuses the env captured at launch, which may predate the Colab-only
# root-gateway opt-in (run-colab.py runtime_env). Set it before the Popen below and
# list it as launcher-owned, so a saved .env value cannot replace it at boot.
HERMES_COLAB.env['HERMES_ALLOW_ROOT_GATEWAY'] = '1'
overrides = [name for name in HERMES_COLAB.env.get('HERMES_ENV_OVERRIDE_KEYS', '').split(',')
             if name and name != 'HERMES_ALLOW_ROOT_GATEWAY']
HERMES_COLAB.env['HERMES_ENV_OVERRIDE_KEYS'] = ','.join(overrides + ['HERMES_ALLOW_ROOT_GATEWAY'])
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
        if isinstance(status, dict):
            HERMES_COLAB.dashboard()
            checks = __import__('runpy').run_path('/opt/render-tools/check-dashboard.py')['check']()
            for result in checks:
                print('Dashboard check:', result['path'], result['status'])
            if not all(result['ok'] for result in checks):
                raise RuntimeError('Dashboard restarted, but an API check failed. Keep this runtime and inspect its dashboard logs.')
            if not status.get('gateway_running'):
                print('Dashboard is ready; the messaging gateway is still starting.')
            print('Updated, backed up and restarted. Refresh your dashboard.')
            break
    except (OSError, ValueError):
        pass
    time.sleep(2)
else:
    raise RuntimeError('Startup is still waiting. Inspect /content/hermes-colab.log; your data has been kept.')
