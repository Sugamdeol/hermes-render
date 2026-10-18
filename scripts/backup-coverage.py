"""Record ordinary-file coverage and explicit exceptions without following external links."""
import json
import errno
import time
import os
from pathlib import Path
import stat

NAME='.backup-coverage.json'


def retry_io(operation):
    transient = {errno.EIO, errno.ESTALE, errno.ETIMEDOUT, errno.EAGAIN}
    for attempt in range(4):
        try: return operation()
        except OSError as exc:
            if exc.errno not in transient or attempt == 3: raise
            time.sleep(0.25 * (2 ** attempt))


def walk(home):
    # Retry the failed directory read, rather than restarting a large network
    # scan from the beginning. Never follow links outside the data home.
    pending = [Path(home)]
    while pending:
        root = pending.pop()
        entries = retry_io(lambda: list(os.scandir(root)))
        dirs, names = [], []
        for entry in entries:
            if retry_io(lambda: entry.is_dir(follow_symlinks=False)): dirs.append(entry.name)
            else: names.append(entry.name)
        yield root, dirs, names
        pending.extend(root/name for name in reversed(dirs))


def capture(home):
    home=Path(home).resolve()
    marker=Path(os.environ.get('GIT_STATE_REQUIRED_MOUNTS_FILE', '/tmp/hermes-required-backup-mounts.json'))
    if marker.exists():
        gate=json.loads(marker.read_text())
        if gate.get('version') != 1 or not isinstance(gate.get('required'), list):
            raise ValueError('Invalid host backup mount guard')
        if gate.get('data_root') == str(home):
            for mount in gate['required']:
                if not isinstance(mount, str) or not Path(mount).is_absolute():
                    raise ValueError('Invalid required backup mount path')
                if not os.path.ismount(mount):
                    raise OSError(errno.ENODEV, 'Required backup storage is not mounted', mount)
    report={'version':1,'regular_files':0,'regular_bytes':0,'external_links':[],
            'special_files':[],'excluded':['.restore-quarantine']}
    for directory, dirs, names in walk(home):
        root=Path(directory)
        if root==home: dirs[:]=[d for d in dirs if d!='.restore-quarantine']
        for name in dirs+names:
            path=root/name;relative=path.relative_to(home).as_posix()
            if relative==NAME:continue
            try:
                info=retry_io(path.lstat)
            except FileNotFoundError:
                continue  # A live tool removed this entry after the directory scan.
            if stat.S_ISLNK(info.st_mode):
                if not retry_io(path.resolve).is_relative_to(home):report['external_links'].append(relative)
            elif stat.S_ISREG(info.st_mode):
                report['regular_files']+=1;report['regular_bytes']+=info.st_size
            elif not stat.S_ISDIR(info.st_mode):report['special_files'].append(relative)
    for key in ['external_links','special_files']:report[key].sort()
    value=json.dumps(report,sort_keys=True,indent=2)+'\n';target=home/NAME
    if target.exists() and target.read_text()==value:return False
    temp=target.with_suffix('.tmp');temp.write_text(value);temp.chmod(0o600);temp.replace(target)
    return True
