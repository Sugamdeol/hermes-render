"""Whole-storage analysis, filename search and directory zip for the dashboard Files tab.

Lives beside ``web_routers/files.py`` (that module is already at its size ratchet).
All routes re-use the managed-files policy: paths resolve through
``_resolve_managed_path`` so a locked deployment (HERMES_HOME == /opt/data) never
reads outside its root, and the credential denylist (``_is_sensitive_path``)
hides the same files the browser hides.
"""

import asyncio
import heapq
import os
import shutil
import stat
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from hermes_cli.web_server_files import _resolve_managed_path
from hermes_cli.web_routers.files import _is_sensitive_path

router = APIRouter()

# Budgets. A warm-cache walk of /opt/data (~190k entries) costs ~1s; the caps
# exist so a cold cache or pathological tree degrades to "truncated" instead of
# hanging the dashboard worker.
_ANALYSIS_MAX_NODES = 500_000
_ANALYSIS_BUDGET_S = 15.0
_ANALYSIS_CACHE_TTL_S = 60.0
_ANALYSIS_CACHE_MAX = 8
_TOPK_DEFAULT = 50

_SEARCH_MAX_NODES = 500_000
_SEARCH_BUDGET_S = 15.0
_SEARCH_MIN_QUERY = 2
_SEARCH_MAX_MATCHES = 300

_ZIP_MAX_ENTRIES = 50_000
_ZIP_MAX_BYTES = 2 * 1024 * 1024 * 1024
_ZIP_BUDGET_S = 60.0

# Mount points (sandbox overlays, external volumes) are skipped so their bytes
# are neither double-counted nor hung on; they are reported, not traversed.
_ANALYSIS_MAX_MOUNTS = 64

# (monotonic stamp, result dict) per analyzed directory; sliced per request.
_analysis_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_analysis_cache_lock = asyncio.Lock()


@dataclass
class _ScanTotals:
    files: int = 0
    dirs: int = 0
    symlinks: int = 0
    bytes_total: int = 0
    other_devices: int = 0
    nodes: int = 0
    truncated: bool = False
    elapsed_s: float = 0.0


@dataclass
class _ScanResult:
    totals: _ScanTotals = field(default_factory=_ScanTotals)
    # first-level child name -> {"bytes", "files", "dirs"} (dirs only tracked here)
    children: Dict[str, Dict[str, int]] = field(default_factory=dict)
    # extension ("" = none, lowercase) -> [count, bytes]
    types: Dict[str, List[int]] = field(default_factory=dict)
    largest: List[Tuple[int, str]] = field(default_factory=list)  # min-heaps, keep-max
    recent: List[Tuple[float, str]] = field(default_factory=list)
    mount_points: List[str] = field(default_factory=list)


class _ZipLimitExceeded(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def _child_bucket(children: Dict[str, Dict[str, int]], root: str, path: str) -> Dict[str, int]:
    rel = path[len(root):].lstrip(os.sep)
    return children.setdefault(rel.split(os.sep, 1)[0], {"bytes": 0, "files": 0, "dirs": 0})


def _topk_push(heap: list, key: Any, item: str, limit: int) -> None:
    """Keep the ``limit`` largest entries by ``key`` in a min-heap."""
    if len(heap) < limit:
        heapq.heappush(heap, (key, item))
    elif key > heap[0][0]:
        heapq.heapreplace(heap, (key, item))


def _scan_tree(root: Path, budget_s: float, max_nodes: int, topk: int) -> _ScanResult:
    """Aggregate the subtree under ``root`` without following links or mounts."""
    result = _ScanResult()
    totals = result.totals
    root_str = str(root)
    try:
        root_dev = root.stat().st_dev
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid path: {exc}") from exc
    started = time.monotonic()
    stack: List[str] = [root_str]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as scan:
                entries = list(scan)
        except OSError:
            continue
        totals.nodes += len(entries)
        for entry in entries:
            if _is_sensitive_path(Path(entry.path)):
                continue
            try:
                if entry.is_symlink():
                    totals.symlinks += 1
                    continue
                if entry.is_dir(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    if st.st_dev != root_dev:
                        totals.other_devices += 1
                        if len(result.mount_points) < _ANALYSIS_MAX_MOUNTS:
                            result.mount_points.append(entry.path)
                        continue
                    totals.dirs += 1
                    _child_bucket(result.children, root_str, entry.path)["dirs"] += 1
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    size = st.st_size
                    totals.files += 1
                    totals.bytes_total += size
                    bucket = _child_bucket(result.children, root_str, entry.path)
                    bucket["bytes"] += size
                    bucket["files"] += 1
                    stem, ext = os.path.splitext(entry.name)
                    ext_stat = result.types.setdefault(ext.lower(), [0, 0])
                    ext_stat[0] += 1
                    ext_stat[1] += size
                    _topk_push(result.largest, size, entry.path, topk)
                    _topk_push(result.recent, st.st_mtime, entry.path, topk)
            except OSError:
                continue
        if totals.nodes > max_nodes or time.monotonic() - started > budget_s:
            totals.truncated = True
            break
    totals.elapsed_s = time.monotonic() - started
    return result


def _entry_json(path: str, st_size: Optional[int] = None, st_mtime: Optional[float] = None) -> Dict[str, Any]:
    p = Path(path)
    return {
        "name": p.name,
        "path": path,
        "is_directory": False,
        "size": st_size,
        "mtime": st_mtime,
    }


def _types_json(types: Dict[str, List[int]], limit: int) -> List[Dict[str, Any]]:
    ranked = sorted(types.items(), key=lambda kv: (-kv[1][1], -kv[1][0]))
    return [
        {"ext": ext or "(none)", "count": stat_pair[0], "bytes": stat_pair[1]}
        for ext, stat_pair in ranked[:limit]
    ]


def _children_json(root: Path, children: Dict[str, Dict[str, int]]) -> List[Dict[str, Any]]:
    ranked = sorted(children.items(), key=lambda kv: (-kv[1]["bytes"], kv[0]))
    out = []
    for name, bucket in ranked:
        child = root / name
        out.append({
            "name": name,
            "path": str(child),
            "is_directory": child.is_dir(),
            "bytes": bucket["bytes"],
            "files": bucket["files"],
            "dirs": bucket["dirs"],
        })
    return out


def _disk_json(root: Path) -> Dict[str, Any]:
    try:
        usage = shutil.disk_usage(root)
    except OSError:
        return None  # type: ignore[return-value]
    percent = round(usage.used / usage.total * 100, 1) if usage.total else 0.0
    return {
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
        "percent": percent,
        "device": str(Path(root).anchor) or str(root.drive),
    }


def _analysis_payload(root: Path, result: _ScanResult, display_path: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    totals = result.totals
    return {
        "path": display_path,
        "root": meta.get("locked_root"),
        "locked_root": meta.get("locked_root"),
        "can_change_path": meta.get("can_change_path", False),
        "generated_at": time.time(),
        "elapsed_ms": int(totals.elapsed_s * 1000),
        "truncated": totals.truncated,
        "disk": _disk_json(root),
        "totals": {
            "files": totals.files,
            "dirs": totals.dirs,
            "symlinks": totals.symlinks,
            "bytes": totals.bytes_total,
            "other_devices": totals.other_devices,
        },
        "children": _children_json(root, result.children),
        "types": _types_json(result.types, limit=15),
        "largest": [
            _entry_json(path, st_size=size)
            for size, path in sorted(result.largest, key=lambda item: (-item[0], item[1]))
        ],
        "recent": [
            _entry_json(path, st_size=None, st_mtime=mtime)
            for mtime, path in sorted(result.recent, key=lambda item: (-item[0], item[1]))
        ],
        "mount_points": result.mount_points,
    }


def _clamp(value: int, low: int, high: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


@router.get("/api/fs/analysis")
async def fs_analysis(
    request: Request, path: Optional[str] = None, top_n: int = 12, fresh: bool = False,
):
    """Whole-storage analysis for the Files tab: disk usage, per-child sizes,
    file-type breakdown, largest and most recently modified files."""
    policy, target, display_path = _resolve_managed_path(path, request)
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory")
    top_n = _clamp(top_n, 1, 50, 12)
    cache_key = str(target)
    now = time.monotonic()
    async with _analysis_cache_lock:
        hit = _analysis_cache.get(cache_key)
        if hit and not fresh and now - hit[0] < _ANALYSIS_CACHE_TTL_S:
            payload = dict(hit[1])
            payload["cached"] = True
            return payload
    result = await asyncio.to_thread(_scan_tree, target, _ANALYSIS_BUDGET_S, _ANALYSIS_MAX_NODES, _TOPK_DEFAULT)
    meta = {
        "locked_root": str(policy.locked_root) if policy.locked_root is not None else None,
        "can_change_path": policy.can_change_path,
    }
    payload = _analysis_payload(target, result, display_path, meta)
    payload["cached"] = False
    async with _analysis_cache_lock:
        if len(_analysis_cache) >= _ANALYSIS_CACHE_MAX:
            oldest = min(_analysis_cache, key=lambda key: _analysis_cache[key][0])
            _analysis_cache.pop(oldest, None)
        _analysis_cache[cache_key] = (time.monotonic(), payload)
    return payload


def _name_matches(name: str, query: str) -> bool:
    lowered = name.lower()
    if any(ch in query for ch in "*?["):
        return fnmatch(lowered, query)
    return query in lowered


def _search_tree(root: Path, query: str, limit: int) -> Tuple[List[Dict[str, Any]], _ScanTotals]:
    totals = _ScanTotals()
    matches: List[Dict[str, Any]] = []
    started = time.monotonic()
    stack: List[str] = [str(root)]
    while stack and len(matches) < limit:
        directory = stack.pop()
        try:
            with os.scandir(directory) as scan:
                entries = list(scan)
        except OSError:
            continue
        totals.nodes += len(entries)
        for entry in entries:
            if _is_sensitive_path(Path(entry.path)):
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                if _name_matches(entry.name, query):
                    if entry.is_symlink():
                        matches.append({
                            "name": entry.name, "path": entry.path, "is_directory": False,
                            "size": None, "mtime": None, "broken_link": not os.path.exists(entry.path),
                        })
                    elif is_dir:
                        matches.append({
                            "name": entry.name, "path": entry.path, "is_directory": True,
                            "size": None, "mtime": entry.stat(follow_symlinks=False).st_mtime,
                        })
                    else:
                        st = entry.stat(follow_symlinks=False)
                        matches.append({
                            "name": entry.name, "path": entry.path, "is_directory": False,
                            "size": st.st_size, "mtime": st.st_mtime,
                        })
                    if len(matches) >= limit:
                        break
                if is_dir and not entry.is_symlink():
                    stack.append(entry.path)
            except OSError:
                continue
        if totals.nodes > _SEARCH_MAX_NODES or time.monotonic() - started > _SEARCH_BUDGET_S:
            totals.truncated = True
            break
    totals.elapsed_s = time.monotonic() - started
    matches.sort(key=lambda item: (not item["is_directory"], item["name"].lower()))
    return matches, totals


@router.get("/api/fs/search")
async def fs_search(request: Request, query: str, path: Optional[str] = None, limit: int = 60):
    """Recursive filename search under a Files-tab directory (substring or glob)."""
    policy, target, display_path = _resolve_managed_path(path, request)
    needle = (query or "").strip().lower()
    if len(needle) < _SEARCH_MIN_QUERY:
        raise HTTPException(status_code=400, detail="Search needs at least 2 characters")
    limit = _clamp(limit, 1, _SEARCH_MAX_MATCHES, 60)
    matches, totals = await asyncio.to_thread(_search_tree, target, needle, limit)
    return {
        "path": display_path,
        "root": str(policy.locked_root) if policy.locked_root is not None else None,
        "query": query.strip(),
        "matches": matches,
        "truncated": totals.truncated,
        "elapsed_ms": int(totals.elapsed_s * 1000),
    }


def _write_zip(root: Path, archive_path: str) -> Tuple[int, int]:
    """Zip the subtree (regular files only) into ``archive_path``; caps enforced."""
    count = 0
    bytes_total = 0
    started = time.monotonic()
    root_str = str(root)
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        stack: List[str] = [root_str]
        while stack:
            directory = stack.pop()
            try:
                with os.scandir(directory) as scan:
                    entries = list(scan)
            except OSError:
                continue
            for entry in entries:
                if _is_sensitive_path(Path(entry.path)):
                    continue
                try:
                    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                        continue
                    st = entry.stat(follow_symlinks=False)
                    count += 1
                    bytes_total += st.st_size
                    if count > _ZIP_MAX_ENTRIES:
                        raise _ZipLimitExceeded("Directory has too many files to zip")
                    if bytes_total > _ZIP_MAX_BYTES:
                        raise _ZipLimitExceeded("Directory is too large to zip")
                    archive.write(entry.path, os.path.relpath(entry.path, root_str))
                except _ZipLimitExceeded:
                    raise
                except OSError:
                    continue
            stack.extend(
                e.path for e in entries
                if not e.is_symlink() and e.is_dir(follow_symlinks=False)
            )
            if time.monotonic() - started > _ZIP_BUDGET_S:
                raise _ZipLimitExceeded("Zipping took too long; directory too large")
    return count, bytes_total


@router.get("/api/fs/zip")
async def fs_zip_download(request: Request, path: str):
    """Download a directory (its whole subtree) as a single zip archive."""
    policy, target, _display = _resolve_managed_path(path, request)
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory")
    fd, tmp_name = tempfile.mkstemp(prefix="hermes-files-", suffix=".zip")
    os.close(fd)
    try:
        await asyncio.to_thread(_write_zip, target, tmp_name)
    except _ZipLimitExceeded as exc:
        os.unlink(tmp_name)
        raise HTTPException(status_code=413, detail=exc.detail) from exc
    except Exception:
        os.unlink(tmp_name)
        raise
    archive_name = (target.name or "files") + ".zip"
    return FileResponse(
        path=tmp_name,
        media_type="application/zip",
        filename=archive_name,
        content_disposition_type="attachment",
        background=BackgroundTask(os.unlink, tmp_name),
    )
