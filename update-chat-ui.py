"""Run in the existing Colab notebook to install and back up the chat UI."""
from pathlib import Path
import hashlib
import os
import tempfile
import urllib.request

EXPECTED = {'bundle/index.js': '6cdce62aa44f00ad2b390f99c5b06099c2ea9e26c9c5c20785c1dded93fd5b16', 'bundle/style.css': '94d28ccd182612e9c22193dd0946958556abc096737ac9fa4458e8c0be19e927', 'manifest.json': 'f37b7c32609606caa684172f2b90dbed65da218752cdb8b57144c1e248e46bdf'}
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
for name, payload in payloads.items():
    path = target / name
    info = path.stat()
    fd, temporary = tempfile.mkstemp(prefix='.chat-ui-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, info.st_mode & 0o777)
        if os.geteuid() == 0:
            os.chown(temporary, info.st_uid, info.st_gid)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
HERMES_COLAB.backup()
print('Chat UI updated and backed up. Press Ctrl+Shift+R on your dashboard tab.')
