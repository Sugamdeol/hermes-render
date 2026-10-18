"""Save Hermes source edits in one encrypted backup entry, tied to its base commit."""
import base64
import hashlib
import json
from pathlib import Path
import subprocess

NAME = '.hermes-runtime-source.json'


def git(root, *args):
    return subprocess.check_output(['git', '-c', 'safe.directory=' + str(root), *args], cwd=root)


def capture(root, home):
    root, home = Path(root), Path(home)
    if not (root / '.git').is_dir():
        return False
    revision = git(root, 'rev-parse', 'HEAD').decode().strip()
    entries = {}
    for raw in git(root, 'ls-files', '-z', '--modified', '--others', '--exclude-standard').split(b'\0'):
        if not raw: continue
        name = raw.decode(errors='surrogateescape')
        path = root / name
        if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
            raise RuntimeError('Source path needs explicit mapping: ' + name)
        if not path.exists():
            entries[name] = {'deleted': True}
            continue
        content = path.read_bytes()
        entries[name] = {'content': base64.b64encode(content).decode(),
                         'sha256': hashlib.sha256(content).hexdigest(),
                         'mode': path.stat().st_mode & 0o777}
    payload = {'version': 1, 'base_revision': revision, 'files': entries}
    target = home / NAME
    value = json.dumps(payload, sort_keys=True) + '\n'
    if target.exists() and target.read_text() == value: return False
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix('.tmp'); temp.write_text(value); temp.chmod(0o600); temp.replace(target)
    return True


def restore(root, home):
    root, home = Path(root), Path(home)
    snapshot = home / NAME
    if not snapshot.exists(): return False
    payload = json.loads(snapshot.read_text())
    revision = git(root, 'rev-parse', 'HEAD').decode().strip()
    if payload.get('version') != 1 or payload.get('base_revision') != revision:
        raise RuntimeError('Saved Hermes edits target a different revision; migrate them before changing the runtime')
    verified = []
    for name, record in payload['files'].items():
        target = root / name
        if not target.resolve().is_relative_to(root.resolve()) or target.is_symlink():
            raise RuntimeError('Saved source path escapes its root')
        if record.get('deleted'):
            verified.append((target, None, None)); continue
        content = base64.b64decode(record['content'], validate=True)
        if hashlib.sha256(content).hexdigest() != record['sha256']:
            raise RuntimeError('Saved source checksum mismatch: ' + name)
        verified.append((target, content, record['mode'] & 0o777))
    for target, content, mode in verified:
        if content is None: target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content); target.chmod(mode)
    return True
