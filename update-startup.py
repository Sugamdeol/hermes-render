"""Run once in an existing Colab: save installed extras, then upgrade startup."""
import hashlib
import os
from pathlib import Path
import runpy
import sys
import tempfile
import urllib.request

REF = 'c901faed35de0cd201f60120745d057aeea02728'
EXPECTED = {'run-colab.py': 'b36e67032ea9bc0757714ed0840b07bc6d3d8af9ee77a8386e65034542d8710c', 'scripts/runtime-dependencies.py': '7896282cb47f79cebf5d38a39db28f3de19e302e67994e73e6d47ac92c908a99'}
base = 'https://raw.githubusercontent.com/Sugamdeol/hermes-render/' + REF + '/'
payloads = {}
for name,digest in EXPECTED.items():
    with urllib.request.urlopen(base+name,timeout=60) as response:
        payload = response.read()
    if hashlib.sha256(payload).hexdigest()!=digest:
        raise RuntimeError('Startup update failed verification; no files changed.')
    compile(payload,name,'exec')
    payloads[name] = payload
if not payloads:
    raise RuntimeError('This updater has not been pinned to its release.')

previous = globals().get('HERMES_COLAB')
if previous is None:
    raise RuntimeError('Run this in the notebook where HERMES_COLAB is already available.')

def write(path,payload):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,temp = tempfile.mkstemp(dir=path.parent,prefix='.startup-update-')
    try:
        with os.fdopen(fd,'wb') as f:f.write(payload);f.flush();os.fsync(f.fileno())
        os.chmod(temp,0o600 if path.name=='hermes-colab-launcher.py' else 0o755)
        os.replace(temp,path)
    finally:Path(temp).unlink(missing_ok=True)

write(Path('/opt/render-tools/runtime-dependencies.py'),payloads['scripts/runtime-dependencies.py'])
launcher = Path('/content/hermes-colab-launcher.py')
write(launcher,payloads['run-colab.py'])
namespace = runpy.run_path(str(launcher),run_name='hermes_startup_upgrade')
previous.__class__ = namespace['ColabAgent']
previous.env['HERMES_COLAB_SYSTEM_PYTHON'] = sys.executable
if previous.process.poll() is None:
    print('Saving your current packages and chats before the startup upgrade…')
    previous.stop()
launch_globals = namespace['main'].__globals__
launch_globals['HERMES_COLAB'] = previous
# Drive consent happens in the new launcher. Set this to 0 for VM-only caches.
os.environ['HERMES_COLAB_USE_DRIVE_CACHE'] = '1'
old_secrets = {name:os.environ.get(name) for name in ('GIT_STATE_TOKEN','STORAGE_ENCRYPTION_KEY')}
try:
    for name in old_secrets:
        if previous.env.get(name):os.environ[name] = previous.env[name]
    HERMES_COLAB = namespace['main'](confirm_switch=True)
finally:
    for name,value in old_secrets.items():
        if value is None:os.environ.pop(name,None)
        else:os.environ[name] = value
    if launch_globals.get('HERMES_COLAB') is not None:
        HERMES_COLAB = launch_globals['HERMES_COLAB']
