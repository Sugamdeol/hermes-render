"""Bounded, read-only Files search and storage analysis using native dashboard policy."""
import fnmatch
import heapq
import os
from pathlib import Path
import stat
import time
from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from hermes_cli.web_routers.files import _is_sensitive_path, _require_token
from hermes_cli.web_server_files import _resolve_managed_path, _hosted_fs_read_guard

router = APIRouter()
MAX_NODES = 500_000
MAX_SECONDS = 15


def scan(root, query=None, max_nodes=MAX_NODES, max_seconds=MAX_SECONDS):
    root = Path(root)
    started = time.monotonic()
    device = root.stat().st_dev
    stack = [(root, None)]
    total = count = directories = visited = skipped = 0
    children, types, largest, matches = {}, {}, [], []
    partial = False
    while stack:
        directory, child = stack.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                visited += 1
                if visited > max_nodes or time.monotonic() - started > max_seconds:
                    partial = True
                    break
                path = Path(entry.path)
                if entry.is_symlink() or _is_sensitive_path(path):
                    skipped += 1
                    continue
                info = entry.stat(follow_symlinks=False)
                if info.st_dev != device:
                    skipped += 1
                    continue
                is_dir = stat.S_ISDIR(info.st_mode)
                if not is_dir and not stat.S_ISREG(info.st_mode):
                    skipped += 1
                    continue
                relative = path.relative_to(root).as_posix()
                group = child or entry.name
                if child is None:
                    children[group] = {'name': entry.name, 'path': str(path), 'is_directory': is_dir, 'size': 0}
                if query and (query.casefold() in entry.name.casefold() or fnmatch.fnmatch(entry.name.casefold(), query.casefold())):
                    if len(matches) < 200:
                        matches.append({'path': str(path), 'name': relative, 'is_directory': is_dir, 'size': None if is_dir else info.st_size})
                if is_dir:
                    directories += 1
                    stack.append((path, group))
                else:
                    count += 1
                    total += info.st_size
                    children[group]['size'] += info.st_size
                    extension = path.suffix.lower() or '(no extension)'
                    types[extension] = types.get(extension, 0) + info.st_size
                    item = (info.st_size, relative)
                    if len(largest) < 20:
                        heapq.heappush(largest, item)
                    elif item > largest[0]:
                        heapq.heapreplace(largest, item)
        if partial:
            break
    return {'path': str(root), 'bytes': total, 'files': count, 'directories': directories,
            'partial': partial, 'skipped': skipped, 'visited': visited,
            'seconds': round(time.monotonic() - started, 2),
            'children': sorted(children.values(), key=lambda x: x['size'], reverse=True),
            'types': sorted(types.items(), key=lambda x: x[1], reverse=True)[:20],
            'largest': [{'name': name, 'size': size} for size, name in sorted(largest, reverse=True)],
            'matches': matches, 'result_limit': 200}


async def execute(request, path, query=None):
    _require_token(request)
    _, root, _ = _resolve_managed_path(path, request)
    _hosted_fs_read_guard(root, request)
    if _is_sensitive_path(root):
        raise HTTPException(403, 'Sensitive path')
    if not root.is_dir():
        raise HTTPException(400, 'Choose a directory')
    try:
        return await run_in_threadpool(scan, root, query)
    except OSError:
        raise HTTPException(500, 'Directory changed or could not be read; refresh and retry')


@router.get('/api/fs/analysis')
async def analysis(request: Request, path: str | None = None):
    return await execute(request, path)


@router.get('/api/fs/search')
async def search(request: Request, q: str, path: str | None = None):
    if not q.strip() or len(q) > 200:
        raise HTTPException(400, 'Enter a search query between 1 and 200 characters')
    return await execute(request, path, q.strip())
