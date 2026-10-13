"""Restore Node's bundled npm after an older backup excluded node_modules."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request


def repair(home):
    home = Path(home).resolve()
    repaired = 0
    for node in sorted((home / 'tools').glob('node-*-linux-*/bin/node')):
        root = node.parent.parent
        cli = root / 'lib/node_modules/npm/bin/npm-cli.js'
        if cli.is_file():
            continue
        if not root.resolve().is_relative_to(home):
            raise RuntimeError('Node tools directory escapes Hermes state')
        version = subprocess.check_output([str(node), '--version'], text=True, timeout=15).strip()
        if not re.fullmatch(r'v\d+\.\d+\.\d+', version):
            raise RuntimeError('Unrecognized installed Node version')
        arch = root.name.rsplit('-', 1)[-1]
        if arch not in ('x64', 'arm64'):
            raise RuntimeError('Unsupported Node architecture')
        name = f'node-{version}-linux-{arch}'
        filename = name + '.tar.xz'
        base = f'https://nodejs.org/dist/{version}/'
        print(f'[node-repair] Restoring bundled npm for Node {version}', flush=True)
        with urllib.request.urlopen(base + 'SHASUMS256.txt', timeout=60) as response:
            checksums = dict((line.split()[1].lstrip('*'), line.split()[0])
                             for line in response.read().decode().splitlines() if len(line.split()) == 2)
        if filename not in checksums:
            raise RuntimeError('Official Node checksum is missing')
        with tempfile.TemporaryDirectory(prefix='hermes-node-repair-') as td:
            archive = Path(td) / filename
            with urllib.request.urlopen(base + filename, timeout=120) as response, archive.open('wb') as output:
                shutil.copyfileobj(response, output)
            with archive.open('rb') as handle:
                if hashlib.file_digest(handle, 'sha256').hexdigest() != checksums[filename]:
                    raise RuntimeError('Node archive checksum mismatch')
            with tarfile.open(archive) as bundle:
                bundle.extractall(td, filter='data')
            source = Path(td) / name / 'lib/node_modules/npm'
            if not (source / 'bin/npm-cli.js').is_file():
                raise RuntimeError('Official Node archive has no npm CLI')
            modules = root / 'lib/node_modules'
            if not modules.resolve().is_relative_to(home):
                raise RuntimeError('Node package directory escapes Hermes state')
            modules.mkdir(parents=True, exist_ok=True)
            destination = modules / 'npm'
            staged = modules / '.npm-repair.tmp'
            old = modules / '.npm-old.tmp'
            if staged.exists() or old.exists():
                raise RuntimeError('An earlier npm repair needs inspection')
            shutil.copytree(source, staged, symlinks=True)
            if destination.exists():
                destination.rename(old)
            try:
                staged.rename(destination)
                subprocess.run([str(node), str(cli), '--version'], check=True, timeout=30)
            except BaseException:
                if destination.exists():
                    shutil.rmtree(destination)
                if old.exists():
                    old.rename(destination)
                raise
            if old.exists():
                shutil.rmtree(old)
            for executable, script in (('npm', 'npm-cli.js'), ('npx', 'npx-cli.js')):
                link = root / 'bin' / executable
                if not link.exists() and not link.is_symlink():
                    link.symlink_to('../lib/node_modules/npm/bin/' + script)
        repaired += 1
    print(f'[node-repair] npm installations repaired: {repaired}', flush=True)
    return repaired


if __name__ == '__main__':
    repair(os.environ.get('HERMES_HOME', '/opt/data'))
