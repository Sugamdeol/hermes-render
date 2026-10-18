"""Remove host wiring from a notebook's restored copy, never from the source host."""
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import sys

import yaml

POOL_ROOTS = ('/storage', '/mnt/hermes-storage')
REMOTE_ROOT = '/mnt/hermes-shared'
HOST_ROOT = '/data/hermes'
HOST_ENV = {
    'TERMINAL_CWD', 'MESSAGING_CWD', 'HERMES_DASHBOARD_FILES_ROOT',
    'GIT_STATE_BASE_FILE', 'GIT_STATE_WORKDIR', 'HERMES_RUNTIME_CACHE_DIR',
    'UV_CACHE_DIR', 'npm_config_cache', 'PYTHONPATH', 'PYTHONHOME',
}


def beneath(value, root):
    return isinstance(value, str) and (value == root or value.startswith(root + '/'))


def local_path(value, home):
    """Map saved host paths to the actual restored files, not a network mount."""
    if not isinstance(value, str):
        return value
    home = Path(home)
    if os.environ.get('HERMES_PORTABLE_PATHS') == '1':
        if any(beneath(value, root) for root in (*POOL_ROOTS, REMOTE_ROOT, HOST_ROOT+'/state', '/data/home', '/data/workspace')):
            if '..' in PurePosixPath(value).parts: raise ValueError('Saved host path contains parent traversal')
            return value
    relative = None
    roots = (*POOL_ROOTS, REMOTE_ROOT, HOST_ROOT + '/state', HOST_ROOT)
    root = next((root for root in roots if beneath(value, root)), None)
    if root is None:
        return value
    relative = PurePosixPath(value[len(root):].lstrip('/'))
    if '..' in relative.parts:
        raise ValueError('Saved host path contains parent traversal')
    if root == REMOTE_ROOT:
        return str(home / 'shared-storage' / relative)
    if root in POOL_ROOTS:
        # Prefer existing restored project files over an empty new workspace.
        candidates = (home / relative, home / 'shared-storage' / relative)
        for candidate in candidates:
            if candidate.is_dir() and any(candidate.iterdir()):
                return str(candidate)
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        return str(home / relative) if relative.parts else str(home / 'workspace')
    if root == HOST_ROOT + '/state':
        return str(home / relative)
    return str(home / 'workspace')


def host_setting(key, value):
    if os.environ.get('HERMES_PORTABLE_PATHS') == '1' and key in ('TERMINAL_CWD', 'MESSAGING_CWD', 'HERMES_DASHBOARD_FILES_ROOT'):
        return False
    return key.startswith(('INSTABOX_', 'HERMES_INSTABOX_')) or (
        key in HOST_ENV and any(beneath(value, root) for root in
                               (*POOL_ROOTS, REMOTE_ROOT, HOST_ROOT)))


def atomic(path, text):
    temporary = path.with_name(path.name + '.notebook-tmp')
    temporary.write_text(text)
    temporary.chmod(0o600)
    temporary.replace(path)


def apply(home):
    if os.environ.get('HERMES_NOTEBOOK_HOST') not in ('colab', 'kaggle'):
        return
    home = Path(home)
    if os.environ.get('HERMES_PORTABLE_PATHS') == '1':
        import runpy
        runpy.run_path(str(Path(__file__).with_name('portable-paths.py')))['apply'](home)
    (home / 'workspace').mkdir(parents=True, exist_ok=True)
    configs = [home / 'config.yaml', *sorted((home / 'profiles').glob('*/config.yaml'))]
    for path in configs:
        if not path.is_file():
            continue
        config = yaml.safe_load(path.read_text())
        if not isinstance(config, dict):
            raise ValueError('Invalid saved notebook configuration')
        terminal = config.get('terminal')
        changed = False
        if isinstance(terminal, dict):
            old = terminal.get('cwd')
            new = local_path(old, home)
            if new != old:
                Path(new).mkdir(parents=True, exist_ok=True)
                terminal['cwd'] = new
                changed = True
        skills = config.get('skills')
        if isinstance(skills, dict) and isinstance(skills.get('external_dirs'), list):
            old = skills['external_dirs']
            new = list(dict.fromkeys(local_path(entry, home) for entry in old))
            if new != old:
                skills['external_dirs'] = new
                changed = True
        if changed:
            atomic(path, yaml.safe_dump(config, sort_keys=False))
    env_path = home / '.env'
    if env_path.exists():
        spec = importlib.util.spec_from_file_location('notebook_seed', Path(__file__).with_name('seed-env.py'))
        seed = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(seed)
        text = seed.read_text(env_path)
        removed = {key for key, value in seed.parse_dotenv(text) if host_setting(key, value)}
        if removed:
            kept = []
            for line in text.splitlines(keepends=True):
                pairs = seed.parse_dotenv(line)
                if not pairs or pairs[0][0] not in removed:
                    kept.append(line)
            atomic(env_path, ''.join(kept))
    runtime = home / '.render-runtime-env.json'
    if runtime.exists():
        data = json.loads(runtime.read_text())
        values = data.get('variables')
        if data.get('version') != 1 or not isinstance(values, dict):
            raise ValueError('Invalid saved runtime environment')
        removed = [key for key, value in values.items() if host_setting(key, value)]
        if removed:
            for key in removed:
                del values[key]
            atomic(runtime, json.dumps(data) + '\n')


if __name__ == '__main__':
    apply(sys.argv[1])
