"""Install the OpenIntelligentUI chat adaptation in an existing Colab."""
import hashlib
import os
from pathlib import Path
import tempfile
import urllib.request

REF = '45903cd1ed80b39b9bcb20d7b74491fb8d58fad9'
EXPECTED = {'bundle/index.js': 'dd314736f579196d40ace90dd10480c92154842c3e14a2b1c3661569c9d8607c', 'bundle/style.css': '84334327473c74a0490e576da684d7bb22a6125b0307b4ba6f3ce947f7c33019'}
agent = globals().get('HERMES_COLAB')
target = Path('/opt/data/plugins/hermes-chat-dashboard/dashboard')
if agent is None or not target.is_dir():
    raise RuntimeError('Run this cell in your existing Hermes Colab notebook.')
base = 'https://raw.githubusercontent.com/Sugamdeol/hermes-render/' + REF + '/dashboard-plugins/hermes-chat-dashboard/dashboard/'
payloads = {}
for name,digest in EXPECTED.items():
    with urllib.request.urlopen(base+name,timeout=60) as response:
        payload = response.read()
    if hashlib.sha256(payload).hexdigest()!=digest:
        raise RuntimeError('UI verification failed; no files changed.')
    payloads[name] = payload

def atomic(path,payload):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp = tempfile.mkstemp(dir=path.parent,prefix='.ui-update-')
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(payload);f.flush();os.fsync(f.fileno())
        os.chmod(tmp,0o644)
        os.replace(tmp,path)
    finally:Path(tmp).unlink(missing_ok=True)

for name,payload in payloads.items():
    atomic(target/name,payload)
    bundled = Path('/opt/render-tools/dashboard-plugins/hermes-chat-dashboard/dashboard')
    if bundled.is_dir():atomic(bundled/name,payload)
# Provider settings, tokens, conversation data and the running gateway are retained.
try:
    saved = agent.backup()
except Exception as error:
    print('UI installed. Backup still needs attention:',type(error).__name__)
else:
    if saved is False:print('UI installed. Retry HERMES_COLAB.backup() to save it.')
print('OpenIntelligentUI chat installed. Hard-refresh your dashboard tab. No restart needed.')
