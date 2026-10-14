"""Transactional updates to the Colab snapshot; only dirty paths are revisited."""
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import time

ENCODING = 'git-components-v1'
PREFIX = '.hermes-dotgit'


def encode(name):
    return '/'.join(PREFIX if p == '.git' else PREFIX+'.escape-'+p.encode().hex()
                    if p.startswith(PREFIX) or p.endswith('.enc') else p for p in name.split('/'))


def decode(name):
    result = []
    for component in name.split('/'):
        if component == PREFIX:
            component = '.git'
        elif component.startswith(PREFIX+'.escape-'):
            component = bytes.fromhex(component[len(PREFIX+'.escape-'):]).decode()
            if not (component.startswith(PREFIX) or component.endswith('.enc')):
                raise ValueError('Invalid escaped backup component')
        if not component or component in ('.', '..') or '/' in component or '\x00' in component:
            raise ValueError('Unsafe decoded backup component')
        result.append(component)
    return '/'.join(result)


def atomic(path, writer):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.mirror-write-')
    os.close(fd)
    temporary = Path(name)
    try:
        writer(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Transaction:
    def __init__(self, work, cache, target, metadata):
        self.work, self.target, self.metadata = work, target, set(metadata)
        self.folder = cache/'hermes-incremental-journal'
        self.records = {}
        self.recover()
        self.mode = 'patch' if target.exists() else 'new-tree'
        self.folder.mkdir(parents=True, mode=0o700)
        (self.folder/'mode').write_text(self.mode)
        self.index = (self.folder/'index.jsonl').open('a')

    def valid(self, name):
        path = self.work/name
        if '..' in Path(name).parts or Path(name).is_absolute():
            raise RuntimeError('Unsafe snapshot journal entry')
        if path not in self.metadata and not path.is_relative_to(self.target):
            raise RuntimeError('Snapshot journal entry is outside the snapshot')
        if not path.parent.resolve().is_relative_to(self.work.resolve()):
            raise RuntimeError('Snapshot journal traverses an external symlink')
        return path

    def recover(self):
        if not self.folder.exists():
            return
        if (self.folder/'committed').exists():
            shutil.rmtree(self.folder)
            return
        records = []
        index = self.folder/'index.jsonl'
        if index.exists():
            for line in index.read_text().splitlines():
                records.append(json.loads(line))
        for name, backup in reversed(records):
            path = self.valid(name)
            if backup is None:
                path.unlink(missing_ok=True)
            else:
                saved = self.folder/backup
                if not saved.is_file():
                    raise RuntimeError('Incomplete snapshot rollback journal; refusing to publish')
                path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(saved, path)
        if (self.folder/'mode').exists() and (self.folder/'mode').read_text() == 'new-tree' and self.target.exists():
            shutil.rmtree(self.target)
        shutil.rmtree(self.folder)

    def remember(self, path):
        if self.mode == 'new-tree' and path.is_relative_to(self.target):
            return
        name = path.relative_to(self.work).as_posix()
        self.valid(name)
        if name in self.records:
            return
        backup = None
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file():
                raise RuntimeError('Snapshot cache contains a non-regular file')
            backup = f'file-{len(self.records)}'
            os.link(path, self.folder/backup)
        self.index.write(json.dumps([name, backup])+'\n')
        self.index.flush()
        self.records[name] = backup

    def write(self, path, writer):
        self.remember(path)
        atomic(path, writer)

    def remove(self, path):
        self.remember(path)
        path.unlink(missing_ok=True)

    def finish(self, ok):
        self.index.close()
        if ok:
            (self.folder/'committed').touch()
            shutil.rmtree(self.folder)
        else:
            self.recover()


def source_signature(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise OSError('Source changed to a non-regular file; retrying safely')
    return [info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            info.st_mode & 0o777, info.st_dev, info.st_ino]


def inventory_from_tree(target, manifest, g):
    encoded = manifest.get('path_encoding') == ENCODING
    sealed = set(manifest.get('encrypted', []))
    representations = {}
    for root, dirs, files in os.walk(target):
        if Path(root) == target:
            dirs[:] = [d for d in dirs if d != g['CHUNK_DIRECTORY']]
        for name in files:
            path = Path(root)/name
            stored = path.relative_to(target).as_posix()
            logical = stored
            if encoded:
                base = decode(stored[:-4]) if stored.endswith('.enc') else None
                logical = base+'.enc' if base in sealed else decode(stored)
            if (not encoded or stored.endswith('.enc')) and logical.endswith('.enc') and logical[:-4] in sealed:
                logical = logical[:-4]
            representations[logical] = stored
    for stored in manifest.get('large_files', {}):
        logical = stored
        if encoded:
            base = decode(stored[:-4]) if stored.endswith('.enc') else None
            logical = base+'.enc' if base in sealed else decode(stored)
        if (not encoded or stored.endswith('.enc')) and logical.endswith('.enc') and logical[:-4] in sealed:
            logical = logical[:-4]
        representations[logical] = stored
    return {'representations':representations, 'files':list(representations),
            'sqlite_hashes':{}, 'signatures':{}}


def affected_plan(data, changes, inventory, manifest, g):
    old_files = set(inventory['files'])
    files, affected = set(old_files), set()
    links = dict(manifest.get('symlinks', {}))
    empty = set(manifest.get('empty_dirs', []))
    ancestors = set()
    old_directories = inventory.get('directories', [])
    old_directories = set(old_directories)
    def parent_dirs(name):
        for parent in Path(name).parents:
            if str(parent) != '.': ancestors.add(parent.as_posix())
    for name in sorted(set(changes) | set(g['SENSITIVE_FILES'])):
        if not name or name == '.':
            return None
        if '..' in Path(name).parts or Path(name).is_absolute():
            raise g['GitStateError']('Unsafe change notification')
        if g['is_excluded'](name):
            continue
        path = data/name
        try:
            info = path.lstat()
        except FileNotFoundError:
            info = None
        prefix = name+'/'
        # Existing regular files are dictionary lookups, not whole-tree scans.
        subtree = name in old_directories or (
            info is not None and stat.S_ISDIR(info.st_mode))
        touched = {x for x in old_files if x.startswith(prefix)} if subtree else set()
        touched.add(name)
        affected.update(touched)
        files.difference_update(touched)
        for old in list(links):
            if old == name or subtree and old.startswith(prefix): links.pop(old)
        for old in list(empty):
            if old == name or subtree and old.startswith(prefix): empty.discard(old)
        parent_dirs(name)
        if info is None:
            continue
        if stat.S_ISLNK(info.st_mode):
            value = g['safe_symlink_target'](path, data)
            if value is not None: links[name] = value
        elif stat.S_ISREG(info.st_mode):
            files.add(name)
        elif stat.S_ISDIR(info.st_mode):
            for root, dirs, names in os.walk(path, onerror=lambda e: (_ for _ in ()).throw(e), followlinks=False):
                root = Path(root)
                rel = root.relative_to(data).as_posix()
                ancestors.add(rel)
                kept = []
                for child in dirs + names:
                    child_path = root/child
                    child_rel = child_path.relative_to(data).as_posix()
                    if g['is_excluded'](child_rel): continue
                    affected.add(child_rel)
                    if child_path.is_symlink():
                        value = g['safe_symlink_target'](child_path, data)
                        if value is not None: links[child_rel] = value
                    elif child_path.is_dir(): kept.append(child)
                    elif child_path.is_file(): files.add(child_rel)
                dirs[:] = kept
    # Changes to a WAL/SHM/rollback journal require a fresh database snapshot.
    for name in list(affected):
        for suffix in ('-wal', '-shm', '-journal'):
            if name.endswith(suffix):
                db = name[:-len(suffix)]
                if db in files: affected.add(db)
    for name in ancestors:
        path = data/name
        try:
            with os.scandir(path) as entries:
                populated = any(not g['is_excluded'](entry.path[len(str(data))+1:])
                                for entry in entries)
        except FileNotFoundError:
            empty.discard(name); continue
        if populated: empty.discard(name)
        else: empty.add(name)
    return files, affected, empty, links


def physical_paths(representations, large):
    result = set(representations.values()) - set(large)
    for record in large.values(): result.update(record['parts'])
    return result


def build_snapshot(data, work, config, g, changes=None):
    cache = work/'.git' if (work/'.git').is_dir() else work
    target = work/g['DATA_SUBDIR']
    index = cache/'hermes-source-inventory.json'
    sigfile = cache/'hermes-source-signatures.json'
    metadata = (work/g['MANIFEST_NAME'], index, sigfile)
    transaction = Transaction(work, cache, target, metadata)
    try:
        manifest = g['read_manifest'](work)
        try:
            inventory = json.loads(index.read_text())
            if inventory['root'] != g['local_tree_identity'](data): raise ValueError('different home')
        except (OSError, ValueError, KeyError):
            inventory = inventory_from_tree(target, manifest, g)
            try: inventory['signatures'] = json.loads(sigfile.read_text())
            except (OSError, ValueError): pass
            inventory['sqlite_hashes'] = (g['_read_sync_base'](config) or {}).get('sqlite_hashes', {})
            changes = None
        identity = g['local_tree_identity'](data)
        g['_write_sync_progress'](config, 'planning')
        plan = None if changes is None else affected_plan(data, changes, inventory, manifest, g)
        if plan is None:
            planned, dirs, links = g['plan_tree'](data, g['DEFAULT_EXCLUDES'])
            all_files, affected, empty = set(planned), set(planned) | set(inventory['files']), set(dirs)
        else:
            all_files, affected, empty, links = plan
            planned = sorted(all_files & affected)
        representations = dict(inventory['representations'])
        old_physical = physical_paths(representations, manifest.get('large_files', {}))
        large = dict(manifest.get('large_files', {}))
        signatures = dict(inventory.get('signatures', {}))
        hashes = dict(inventory.get('sqlite_hashes', {}))
        encrypted, plain, omitted, dbs = (set(manifest.get(k, [])) for k in
                                        ('encrypted','plaintext_env','omitted','sqlite_snapshots'))
        modes = dict(manifest.get('modes', {}))
        for name in affected:
            old = representations.pop(name, None)
            if old: large.pop(old, None)
            signatures.pop(name, None)
            encrypted.discard(name); plain.discard(name); omitted.discard(name)
            dbs.discard(name); modes.pop(name, None); hashes.pop(name, None)
        sqlite_dbs, sidecars = g['sqlite_files'](data, planned)
        target.mkdir(parents=True, exist_ok=True)
        changed, reused, last_progress = 0, 0, 0
        for position, name in enumerate(planned):
            if time.monotonic()-last_progress >= 5:
                g['_write_sync_progress'](config, 'snapshot', position, len(planned), name)
                last_progress = time.monotonic()
            if name in sidecars: continue
            source = data/name
            try:
                if not source.resolve().is_relative_to(data.resolve()):
                    raise OSError('Source traverses an external symlink')
                signature = source_signature(source)
                mode = signature[3]
                stored = encode(name)
                destination = target/stored
                old_stored = inventory['representations'].get(name)
                prior = target/old_stored if old_stored else None
                record = manifest.get('large_files', {}).get(old_stored)
                if name in g['SENSITIVE_FILES']:
                    mode = 0o600
                    payload, disposition = g['_prepare_sensitive'](source.read_bytes(), config)
                    if disposition == g['ENV_MODE_OMIT']:
                        if config.env_mode == g['ENV_MODE_ENCRYPT']: raise g['GitStateError']('Cannot encrypt '+name)
                        omitted.add(name); continue
                    if disposition == g['ENV_MODE_ENCRYPT']:
                        stored += '.enc'; destination = target/stored; encrypted.add(name)
                    else: plain.add(name)
                    # Cipher caching avoids changing bytes when settings did not change.
                    if destination.is_file() and destination.read_bytes() == payload: reused += 1
                    else:
                        transaction.write(destination, lambda p:p.write_bytes(payload)); changed += 1
                elif name in sqlite_dbs:
                    if config.env_mode == g['ENV_MODE_OMIT']: omitted.add(name); continue
                    fd, temporary = tempfile.mkstemp(dir=cache, prefix='hermes-db-')
                    os.close(fd); snapshot = Path(temporary)
                    try:
                        g['snapshot_sqlite'](source, snapshot)
                        digest = g['_hash_file'](snapshot)
                        hashes[name] = digest; dbs.add(name)
                        sealed = config.env_mode == g['ENV_MODE_ENCRYPT']
                        if sealed: stored += '.enc'; destination = target/stored; encrypted.add(name)
                        same = inventory.get('sqlite_hashes', {}).get(name) == digest and (name in manifest.get('encrypted', [])) == sealed
                        if same and record and g['reuse_chunks'](target, target, record):
                            large[stored] = record; reused += 1
                        elif same and prior and prior.is_file():
                            if prior != destination:
                                transaction.remember(destination); destination.parent.mkdir(parents=True,exist_ok=True); os.link(prior,destination)
                            reused += 1
                        else:
                            if sealed: transaction.write(destination, lambda p:g['encrypt_stream_file'](snapshot,p,config))
                            else: transaction.write(destination, lambda p:shutil.copyfile(snapshot,p))
                            changed += 1
                    finally: snapshot.unlink(missing_ok=True)
                elif inventory.get('signatures', {}).get(name) == signature and record and g['reuse_chunks'](target,target,record):
                    large[stored] = record; signatures[name] = signature; reused += 1
                elif prior and prior.is_file() and (inventory.get('signatures', {}).get(name) == signature
                     or prior.stat().st_size == signature[0] and g['_hash_file'](source) == g['_hash_file'](prior)):
                    if prior != destination:
                        transaction.remember(destination); destination.parent.mkdir(parents=True,exist_ok=True); os.link(prior,destination)
                    signatures[name] = signature; reused += 1
                else:
                    transaction.write(destination, lambda p:shutil.copy2(source,p))
                    signatures[name] = signature; changed += 1
                representations[name] = stored
                if stored not in large and destination.exists():
                    if destination.stat().st_mode & 0o777 != mode:
                        transaction.write(destination, lambda p:shutil.copy2(destination,p))
                        os.chmod(destination, mode)
                    if destination.stat().st_size > g['GIT_BLOB_CHUNK_BYTES']:
                        digest, parts, total = g['hashlib'].sha256(), [], 0
                        folder = g['hashlib'].sha256(stored.encode()).hexdigest()
                        with destination.open('rb') as handle:
                            while payload := handle.read(g['GIT_BLOB_CHUNK_BYTES']):
                                part = f"{g['CHUNK_DIRECTORY']}/{folder}/{len(parts):06d}"
                                transaction.write(target/part, lambda p, payload=payload:p.write_bytes(payload))
                                digest.update(payload); parts.append(part); total += len(payload)
                        large[stored] = {'parts':parts,'size':total,'sha256':digest.hexdigest(),'mode':mode}
                        transaction.remove(destination)
                if mode not in (0o644,0o755): modes[name] = format(mode,'o')
            except Exception as error:
                cause = error.__cause__ if isinstance(error,g['GitStateError']) else error
                missing = isinstance(cause,FileNotFoundError) or isinstance(cause,g['sqlite3'].OperationalError) and getattr(cause,'sqlite_errorcode',0)&255 == g['sqlite3'].SQLITE_CANTOPEN
                if missing and g['confirmed_source_deletion'](source,data):
                    all_files.discard(name); representations.pop(name,None)
                    signatures.pop(name,None); hashes.pop(name,None); dbs.discard(name)
                    encrypted.discard(name); plain.discard(name); omitted.discard(name); modes.pop(name,None)
                    continue
                raise g['GitStateError']('Cannot snapshot '+name+'; previous backup retained: '+g['redact'](str(error))) from error
        new_physical = physical_paths(representations, large)
        for name in old_physical-new_physical:
            transaction.remove(target/name)
            parent = (target/name).parent
            while parent != target:
                try: parent.rmdir()
                except OSError: break
                parent = parent.parent
        if g['local_tree_identity'](data) != identity: raise g['GitStateError']('Home replaced during snapshot')
        result = {**manifest,'path_encoding':ENCODING,'large_files':large,'files':len(representations),
                  'generation':int(manifest.get('generation',0))+1,'instance':config.instance_id,
                  'updated':g['dt'].datetime.now(g['dt'].timezone.utc).isoformat(timespec='seconds'),
                  'encrypted':sorted(encrypted),'plaintext_env':sorted(plain),'omitted':sorted(omitted),
                  'sqlite_snapshots':sorted(dbs),'modes':modes,'empty_dirs':sorted(empty),'symlinks':links}
        directories = set(inventory.get('directories', [])) if plan is not None else set()
        directory_inputs = affected if plan is not None else all_files | set(links) | empty
        directories.update(parent.as_posix() for name in directory_inputs
                           for parent in Path(name).parents if str(parent) != '.')
        directories.update(empty)
        new_inventory = {'root':identity,'files':sorted(all_files),'representations':representations,
                         'signatures':signatures,'sqlite_hashes':hashes,
                         'directories':sorted(directories)}
        for path, content in ((work/g['MANIFEST_NAME'],result),(index,new_inventory),(sigfile,signatures)):
            transaction.write(path,lambda p,content=content:p.write_text(json.dumps(content,separators=(',',':'))))
        config._sqlite_hashes = hashes
        config._incremental_mode = True
        g['LOG'].info('Snapshot ready: %d files; %d paths checked, %d written, %d reused',len(representations),len(planned),changed,reused)
        transaction.finish(True)
        return result
    except BaseException as error:
        transaction.finish(False)
        if isinstance(error, Exception) and not isinstance(error, g['GitStateError']):
            raise g['GitStateError']('Cannot update snapshot; previous backup retained: '+g['redact'](str(error))) from error
        raise
