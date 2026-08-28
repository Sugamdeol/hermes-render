#!/opt/hermes/.venv/bin/python
"""Optional GoFile state sync for Render's Free filesystem.

Render Free services cannot attach persistent disks. When configured with a
GoFile account token, this module resolves a state folder and restores the
newest Hermes state archive before Hermes starts and periodically uploads a
fresh archive while Hermes is running. It keeps one current archive by deleting
older matching backups after a successful upload. Syncs are metadata-aware and
run at low priority so idle Free instances do not repeatedly recompress state.

Uploads are deliberately frugal, because a full-tree upload every interval is
what turns this feature into a bandwidth problem:

  * Volatile paths (logs, caches, __pycache__, tmp) are excluded from the
    archive AND from the change fingerprint, so ordinary log churn no longer
    triggers a re-upload of everything.
  * The fingerprint is two-tier: a cheap metadata scan, then a content hash of
    the archived files. Files that were rewritten with identical contents do
    not cost an upload.
  * A minimum interval between uploads rate-limits a busy agent, and a monthly
    byte budget stops the sync entirely rather than eating a bandwidth
    allowance. Shutdown always forces a final upload so nothing is lost.
  * A standby instance never uploads. See the failover section below.

The archive contains the Hermes data directory including ``.env``, so
dashboard settings, credentials, sessions, and user-created files survive a
replacement. Treat the configured GoFile folder as sensitive because the
archive contains secrets. No third-party Python package is required.

Failover
--------
When several instances share a GoFile folder (typically a Render service and a
laptop), they coordinate through tiny lease files so only one of them talks to
chat platforms:

  * Each instance publishes a lease named ``<prefix><priority>-<id>.json``.
    Priority and identity live in the *filename* and freshness is the file's
    createTime, so checking the cluster costs one folder listing and zero
    downloads.
  * The highest-priority instance with a fresh lease is ACTIVE. Everyone else
    is STANDBY: bootstrap.sh strips their chat-platform tokens, and their sync
    daemon uploads nothing, so a standby can never clobber the active state.
  * On shutdown the active instance uploads a final archive and only then
    releases its lease, so the next instance restores complete state rather
    than a stale archive.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import http.client
import json
import logging
import os
from pathlib import Path
import signal
import stat
import tarfile
import tempfile
import threading
import time
from typing import BinaryIO
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen
import uuid

LOG = logging.getLogger("hermes-gofile-storage")
GOFILE_API_URL = "https://api.gofile.io"
GOFILE_UPLOAD_URL = "https://upload.gofile.io/uploadfile"
# GoFile's current web client uses this rotating four-hour token. Keep it
# configurable because GoFile may rotate the salt without notice.
GOFILE_WT_SALT = "12af056dacea0b"
WT_WINDOW_SECONDS = 14400
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


# Paths that change constantly and are worthless in a restored instance.
# Excluding them from both the archive and the fingerprint is the single
# biggest bandwidth win: without it, one log line forces a full re-upload.
DEFAULT_EXCLUDES = (
    "logs",
    "*.log",
    "*.log.*",
    "__pycache__",
    "*.pyc",
    ".cache",
    "node_modules",
    "tmp",
    "*.tmp",
    "*.sock",
    ".git",
)
LEASE_PREFIX = "hermes-lease-"
ROLE_ACTIVE = "active"
ROLE_STANDBY = "standby"


class GofileError(RuntimeError):
    """A GoFile API response was unsuccessful."""


def sanitize_instance_id(raw: str) -> str:
    """Reduce a hostname to something safe to embed in a GoFile filename."""
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in raw.strip())
    cleaned = cleaned.strip("-") or "instance"
    return cleaned[:40]


def _excludes_from_env() -> "tuple[str, ...]":
    raw = os.environ.get("GOFILE_EXCLUDE", "").strip()
    if not raw:
        return DEFAULT_EXCLUDES
    extra = tuple(part.strip() for part in raw.split(",") if part.strip())
    if os.environ.get("GOFILE_EXCLUDE_REPLACE", "").strip().lower() in ("1", "true", "yes"):
        return extra
    return DEFAULT_EXCLUDES + extra


class StorageConfig:
    def __init__(
        self,
        token: str,
        folder_id: str,
        folder_name: str,
        prefix: str,
        interval: int,
        max_archive_bytes: int,
        user_agent: str,
        language: str,
        wt_salt: str,
        excludes: "tuple[str, ...]" = DEFAULT_EXCLUDES,
        min_upload_interval: int = 1800,
        content_check_interval: int = 3600,
        monthly_budget_bytes: int = 2048 * 1024 * 1024,
        instance_id: str = "instance",
        priority: int = 50,
        failover: bool = False,
        lease_ttl: int = 600,
        heartbeat_interval: int = 120,
        poll_interval: int = 30,
        role_switch_min: int = 300,
    ) -> None:
        self.token = token
        self.folder_id = folder_id
        self.folder_name = folder_name or "hermes-render-state"
        self.prefix = prefix or "hermes-state-"
        self.interval = interval
        self.max_archive_bytes = max_archive_bytes
        self.user_agent = user_agent
        self.language = language
        self.wt_salt = wt_salt
        self.excludes = excludes
        self.min_upload_interval = min_upload_interval
        self.content_check_interval = content_check_interval
        self.monthly_budget_bytes = monthly_budget_bytes
        self.instance_id = instance_id
        self.priority = priority
        self.failover = failover
        self.lease_ttl = lease_ttl
        self.heartbeat_interval = heartbeat_interval
        self.poll_interval = poll_interval
        self.role_switch_min = role_switch_min
        self.last_fingerprint: str | None = None
        self.last_content_hash: str | None = None
        self.last_upload_at: float = 0.0
        self.last_content_check: float = 0.0
        self.role: str = ROLE_ACTIVE

    @classmethod
    def from_env(cls) -> "StorageConfig | None":
        token = (os.environ.get("GOFILE_API_TOKEN") or os.environ.get("GOFILE_TOKEN", "")).strip()
        folder_id = os.environ.get("GOFILE_FOLDER_ID", "").strip()
        if not token:
            return None

        def positive_int(name: str, default: int) -> int:
            try:
                return max(1, int(os.environ.get(name, str(default))))
            except ValueError:
                LOG.warning("invalid %s; using %s", name, default)
                return default

        return cls(
            token=token,
            folder_id=folder_id,
            folder_name=os.environ.get("GOFILE_FOLDER_NAME", "hermes-render-state"),
            prefix=os.environ.get("GOFILE_STATE_PREFIX", "hermes-state-"),
            interval=positive_int("GOFILE_SYNC_INTERVAL_SECONDS", 300),
            max_archive_bytes=positive_int("GOFILE_MAX_ARCHIVE_MB", 100) * 1024 * 1024,
            user_agent=os.environ.get("GOFILE_USER_AGENT", DEFAULT_USER_AGENT),
            language=os.environ.get("GOFILE_LANGUAGE", "en-US"),
            wt_salt=os.environ.get("GOFILE_WT_SALT", GOFILE_WT_SALT),
            excludes=_excludes_from_env(),
            # Default 30 minutes: a chatty agent rewrites its session DB
            # constantly, and uploading the whole tree every 5 minutes is what
            # made this feature expensive.
            min_upload_interval=positive_int("GOFILE_MIN_UPLOAD_INTERVAL_SECONDS", 1800),
            content_check_interval=positive_int("GOFILE_CONTENT_CHECK_SECONDS", 3600),
            monthly_budget_bytes=positive_int("GOFILE_MONTHLY_BUDGET_MB", 2048) * 1024 * 1024,
            instance_id=sanitize_instance_id(
                os.environ.get("HERMES_INSTANCE_ID", "") or os.uname().nodename
            ),
            priority=max(0, min(999, positive_int("HERMES_INSTANCE_PRIORITY", 50))),
            failover=os.environ.get("HERMES_FAILOVER", "").strip().lower()
            in ("1", "true", "yes", "on"),
            lease_ttl=positive_int("HERMES_LEASE_TTL_SECONDS", 600),
            heartbeat_interval=positive_int("HERMES_LEASE_HEARTBEAT_SECONDS", 120),
            poll_interval=positive_int("HERMES_LEASE_POLL_SECONDS", 30),
            role_switch_min=positive_int("HERMES_ROLE_SWITCH_MIN_SECONDS", 300),
        )


def _website_token(config: StorageConfig, window_offset: int = 0) -> str:
    window = int(time.time() // WT_WINDOW_SECONDS) + window_offset
    raw = (
        f"{config.user_agent}::{config.language}::{config.token}::"
        f"{window}::{config.wt_salt}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _content_headers(config: StorageConfig, window_offset: int = 0) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.token}",
        "X-Website-Token": _website_token(config, window_offset),
        "X-BL": config.language,
        "User-Agent": config.user_agent,
        "Accept": "*/*",
        "Origin": "https://gofile.io",
        "Referer": "https://gofile.io/",
    }


def _api_url(path: str, query: dict[str, object] | None = None) -> str:
    url = f"{GOFILE_API_URL}{path}"
    if query:
        url += "?" + urlencode(query)
    return url


def _json_request(
    config: StorageConfig,
    method: str,
    path: str,
    *,
    query: dict[str, object] | None = None,
    body: bytes | None = None,
) -> dict:
    headers = _content_headers(config)
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(_api_url(path, query), data=body, headers=headers, method=method)
    with urlopen(request, timeout=45) as response:
        payload = json.loads(response.read())
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise GofileError(f"GoFile {method} {path} failed: {payload!r}")
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def _download(url: str, config: StorageConfig, destination: BinaryIO) -> int:
    """Stream a remote archive to disk, returning its byte count."""
    last_error: Exception | None = None
    # The website token rotates every four hours. Retry the previous window
    # around a boundary, matching the behavior of GoFile's web clients.
    for offset in (0, -1):
        destination.seek(0)
        destination.truncate(0)
        headers = _content_headers(config, offset)
        request = Request(url, headers=headers, method="GET")
        try:
            total = 0
            with urlopen(request, timeout=60) as response:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > config.max_archive_bytes:
                        raise GofileError(
                            "remote archive is larger than the "
                            f"{config.max_archive_bytes // (1024 * 1024)} MB limit"
                        )
                    destination.write(chunk)
            return total
        except (HTTPError, URLError, OSError, GofileError) as exc:
            last_error = exc
            if isinstance(exc, HTTPError) and exc.code == 404:
                break
    raise GofileError(f"GoFile download failed: {last_error}")


def _multipart_upload(
    config: StorageConfig, filename: str, archive: BinaryIO, archive_size: int
) -> dict:
    """Stream the multipart body instead of duplicating a whole archive in RAM."""
    boundary = f"----hermes-gofile-{uuid.uuid4().hex}".encode("ascii")
    folder_part = (
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="folderId"\r\n\r\n'
        + config.folder_id.encode("utf-8")
        + b"\r\n"
    )
    file_part = (
        b"--" + boundary + b"\r\n"
        + f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(
            "utf-8"
        )
        + b"Content-Type: application/gzip\r\n\r\n"
    )
    closing = b"\r\n--" + boundary + b"--\r\n"
    content_length = len(folder_part) + len(file_part) + archive_size + len(closing)

    parsed = urlsplit(GOFILE_UPLOAD_URL)
    connection = http.client.HTTPSConnection(parsed.netloc, timeout=120)
    headers = _content_headers(config)
    headers["Content-Type"] = f"multipart/form-data; boundary={boundary.decode('ascii')}"
    headers["Content-Length"] = str(content_length)
    try:
        connection.putrequest("POST", parsed.path or "/")
        for key, value in headers.items():
            connection.putheader(key, value)
        connection.endheaders()
        connection.send(folder_part)
        connection.send(file_part)
        while True:
            chunk = archive.read(1024 * 1024)
            if not chunk:
                break
            connection.send(chunk)
        connection.send(closing)
        response = connection.getresponse()
        result = json.loads(response.read())
    finally:
        connection.close()
    if not isinstance(result, dict) or result.get("status") != "ok":
        raise GofileError(f"GoFile upload failed: {result!r}")
    data = result.get("data")
    if not isinstance(data, dict):
        raise GofileError(f"GoFile upload returned no file metadata: {result!r}")
    return data


def is_excluded(relative: str, excludes: "tuple[str, ...]") -> bool:
    """True when a data-dir-relative path matches an exclude pattern.

    A pattern matches either a whole path component (so "logs" excludes
    "logs/gateway.log" at any depth) or a component glob (so "*.log" excludes
    "sessions/old.log"). Keeping this on components rather than full paths
    makes the patterns behave the way people expect from .gitignore-ish rules
    without pulling in a dependency.
    """
    parts = [part for part in relative.replace("\\", "/").split("/") if part not in ("", ".")]
    for part in parts:
        for pattern in excludes:
            if fnmatch.fnmatch(part, pattern):
                return True
    return False


def _archive_filter_for(excludes: "tuple[str, ...]"):
    def _filter(member: tarfile.TarInfo) -> "tarfile.TarInfo | None":
        # Sockets and device files cannot be meaningfully restored into a new
        # container and must not be copied from a remote archive.
        if not (member.isdir() or member.isfile()):
            return None
        name = member.name
        if name.startswith("./"):
            name = name[2:]
        if name in ("", "."):
            return member
        if is_excluded(name, excludes):
            return None
        return member

    return _filter


def _create_archive(
    data_dir: Path, destination: BinaryIO, excludes: "tuple[str, ...]" = DEFAULT_EXCLUDES
) -> None:
    # Level 3 materially reduces CPU spikes on the single Free CPU while
    # keeping the archive substantially smaller than an uncompressed tar.
    with tarfile.open(fileobj=destination, mode="w:gz", compresslevel=3) as archive:
        archive.add(
            data_dir, arcname=".", recursive=True, filter=_archive_filter_for(excludes)
        )


def _safe_extract(archive: tarfile.TarFile, data_dir: Path) -> None:
    root = data_dir.resolve()
    for member in archive.getmembers():
        target = (data_dir / member.name).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"refusing unsafe archive path: {member.name}") from exc
        if member.issym() or member.islnk():
            raise ValueError(f"refusing link in archive: {member.name}")
        # Paths and link types were validated above. Avoid TarFile's newer
        # `filter` argument so this also runs on Python 3.11 images.
        archive.extract(member, path=data_dir)


def _folder_children(config: StorageConfig, folder_id: str) -> dict:
    # GoFile's current web API accepts the website token for free accounts.
    data = _json_request(
        config,
        "GET",
        f"/contents/{quote(folder_id, safe='-_.~')}",
        query={
            "wt": _website_token(config),
            "page": 1,
            "pageSize": 1000,
            "contentFilter": "",
            "sortField": "createTime",
            "sortDirection": -1,
        },
    )
    if data.get("type") != "folder":
        raise GofileError(f"GoFile content {folder_id} is not a folder")
    children = data.get("children", {})
    return children if isinstance(children, dict) else {}


def _ensure_folder(config: StorageConfig) -> str:
    """Resolve or create the state folder so only the token is required."""
    if config.folder_id:
        return config.folder_id

    account = _json_request(config, "GET", "/accounts/getid")
    account_id = account.get("id")
    if not isinstance(account_id, str) or not account_id:
        raise GofileError("GoFile did not return an account id for this token")
    details = _json_request(
        config, "GET", f"/accounts/{quote(account_id, safe='-_.~')}"
    )
    root_id = details.get("rootFolder")
    if not isinstance(root_id, str) or not root_id:
        raise GofileError("GoFile did not return the account root folder")

    for child in _folder_children(config, root_id).values():
        if (
            isinstance(child, dict)
            and child.get("type") == "folder"
            and child.get("name") == config.folder_name
        ):
            folder_id = child.get("id")
            if isinstance(folder_id, str) and folder_id:
                config.folder_id = folder_id
                return folder_id

    body = json.dumps(
        {
            "parentFolderId": root_id,
            "folderName": config.folder_name,
            "public": False,
        }
    ).encode("utf-8")
    created = _json_request(config, "POST", "/contents/createFolder", body=body)
    folder_id = created.get("id")
    if not isinstance(folder_id, str) or not folder_id:
        raise GofileError(f"GoFile did not return the new folder id: {created!r}")
    config.folder_id = folder_id
    LOG.info("created GoFile state folder %s", folder_id)
    return folder_id


def _file_entries(config: StorageConfig) -> list[dict]:
    folder_id = _ensure_folder(config)
    entries = [
        child
        for child in _folder_children(config, folder_id).values()
        if isinstance(child, dict)
        and child.get("type") == "file"
        and str(child.get("name", "")).startswith(config.prefix)
    ]
    entries.sort(
        key=lambda item: (item.get("createTime", 0), item.get("modTime", 0)),
        reverse=True,
    )
    return entries


def _state_fingerprint(data_dir: Path, excludes: "tuple[str, ...]" = DEFAULT_EXCLUDES) -> str:
    """Return a cheap metadata fingerprint for change-aware syncs.

    Reading metadata is much cheaper than recompressing /opt/data every
    minute. Excluded paths are skipped so that log churn -- which is never
    archived -- cannot trigger an upload of everything else.
    """
    digest = hashlib.blake2b(digest_size=16)
    for root, dirs, files in os.walk(data_dir, topdown=True, followlinks=False):
        dirs[:] = sorted(
            name
            for name in dirs
            if not (Path(root) / name).is_symlink()
            and not is_excluded(
                (Path(root) / name).relative_to(data_dir).as_posix(), excludes
            )
        )
        for name in dirs:
            path = Path(root) / name
            try:
                info = path.stat()
            except OSError:
                continue
            if not stat.S_ISDIR(info.st_mode):
                continue
            relative = path.relative_to(data_dir).as_posix()
            digest.update(
                f"D\\0{relative}\\0{info.st_mtime_ns}\\0{info.st_mode & 0o7777}\\n".encode()
            )
        for name in sorted(files):
            path = Path(root) / name
            try:
                info = path.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            relative = path.relative_to(data_dir).as_posix()
            if is_excluded(relative, excludes):
                continue
            digest.update(
                f"F\\0{relative}\\0{info.st_size}\\0{info.st_mtime_ns}\\0{info.st_mode & 0o7777}\\n".encode()
            )
    return digest.hexdigest()


def _content_hash(data_dir: Path, excludes: "tuple[str, ...]" = DEFAULT_EXCLUDES) -> str:
    """Hash the *contents* of everything that would be archived.

    The metadata fingerprint is cheap but noisy: a rewritten-but-identical file
    or a touched mtime changes it. Confirming with contents before spending an
    upload is what makes a rate-limited sync safe to run often.
    """
    digest = hashlib.blake2b(digest_size=16)
    for root, dirs, files in os.walk(data_dir, topdown=True, followlinks=False):
        dirs[:] = sorted(
            name
            for name in dirs
            if not (Path(root) / name).is_symlink()
            and not is_excluded(
                (Path(root) / name).relative_to(data_dir).as_posix(), excludes
            )
        )
        for name in sorted(files):
            path = Path(root) / name
            relative = path.relative_to(data_dir).as_posix()
            if is_excluded(relative, excludes):
                continue
            try:
                if not stat.S_ISREG(path.lstat().st_mode):
                    continue
                digest.update(f"F\0{relative}\0".encode())
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(262144), b""):
                        digest.update(chunk)
            except OSError:
                continue
    return digest.hexdigest()


def _usage_path(data_dir: Path) -> Path:
    return data_dir / ".render-tools-sync-usage.json"


def read_usage(data_dir: Path, now: "dt.datetime | None" = None) -> "tuple[str, int]":
    """Return (month, bytes_uploaded_this_month), resetting on a new month."""
    month = (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m")
    try:
        raw = json.loads(_usage_path(data_dir).read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw.get("month") == month:
            return month, max(0, int(raw.get("bytes", 0)))
    except (OSError, ValueError, TypeError):
        pass
    return month, 0


def record_usage(data_dir: Path, size: int, now: "dt.datetime | None" = None) -> int:
    month, used = read_usage(data_dir, now)
    total = used + max(0, size)
    try:
        _usage_path(data_dir).write_text(
            json.dumps({"month": month, "bytes": total}), encoding="utf-8"
        )
    except OSError as exc:
        LOG.debug("could not record sync usage: %s", exc)
    return total


def restore(data_dir: Path, config: StorageConfig) -> bool:
    entries = _file_entries(config)
    if not entries:
        LOG.info("no GoFile Hermes state found; starting with a clean instance")
        return False
    link = entries[0].get("link")
    if not isinstance(link, str) or not link:
        raise GofileError("newest GoFile state file has no download link")
    with tempfile.TemporaryFile(mode="w+b") as payload:
        size = _download(link, config, payload)
        if not size:
            raise GofileError("newest GoFile state file was empty")
        payload.seek(0)
        with tarfile.open(fileobj=payload, mode="r:gz") as archive:
            _safe_extract(archive, data_dir)
    LOG.info("restored Hermes state from GoFile folder %s", config.folder_id)
    return True


def _delete_old_entries(config: StorageConfig, entries: list[dict], keep_id: str) -> None:
    for entry in entries:
        content_id = entry.get("id")
        if not isinstance(content_id, str) or content_id == keep_id:
            continue
        try:
            body = json.dumps({"contentsId": content_id}).encode("utf-8")
            _json_request(config, "DELETE", "/contents", body=body)
        except (GofileError, HTTPError, URLError, OSError) as exc:
            # Keeping an older backup is safer than failing a successful upload.
            LOG.warning("could not delete old GoFile backup %s: %s", content_id, exc)


def sync_once(
    data_dir: Path,
    config: StorageConfig,
    *,
    force: bool = False,
    now: "float | None" = None,
) -> bool:
    clock = time.time() if now is None else now

    # A standby must never upload: its /opt/data is a stale copy of the
    # active instance's, and uploading it would clobber the real state.
    if config.failover and config.role == ROLE_STANDBY:
        LOG.debug("standby instance; skipping upload")
        return True

    fingerprint = _state_fingerprint(data_dir, config.excludes)
    # The metadata fingerprint is a fast pre-filter, not the truth: a file
    # rewritten to the same size within one mtime tick looks unchanged. Fall
    # through to a full content hash periodically so such an edit -- a rotated
    # API key in .env is the realistic case -- cannot be missed forever.
    metadata_unchanged = config.last_fingerprint == fingerprint
    # Only meaningful once we have a hash to compare against; before the
    # first upload the metadata fingerprint is the only signal we have.
    content_check_due = (
        config.last_content_hash is not None
        and clock - config.last_content_check >= config.content_check_interval
    )
    if not force and metadata_unchanged and not content_check_due:
        LOG.debug("state unchanged; skipping GoFile archive")
        return True

    # Rate limit. Checked before the expensive content hash and archive, but
    # after the cheap metadata scan, so a quiet instance stays quiet.
    if not force and config.last_upload_at:
        waited = clock - config.last_upload_at
        if waited < config.min_upload_interval:
            LOG.debug(
                "last upload was %ds ago; below the %ds minimum",
                int(waited),
                config.min_upload_interval,
            )
            return True

    # Contents decide, not mtimes: an identical rewrite costs nothing.
    content_hash = _content_hash(data_dir, config.excludes)
    config.last_content_check = clock
    if not force and config.last_content_hash == content_hash:
        LOG.debug("file contents unchanged; skipping GoFile archive")
        config.last_fingerprint = fingerprint
        return True

    month, used = read_usage(data_dir, dt.datetime.fromtimestamp(clock, dt.timezone.utc))
    if config.monthly_budget_bytes and used >= config.monthly_budget_bytes:
        LOG.warning(
            "GoFile sync paused: %d MB uploaded in %s reaches the %d MB monthly budget "
            "(raise GOFILE_MONTHLY_BUDGET_MB to continue)",
            used // (1024 * 1024),
            month,
            config.monthly_budget_bytes // (1024 * 1024),
        )
        return False

    filename = f"{config.prefix}{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}.tar.gz"
    with tempfile.TemporaryFile(mode="w+b") as archive_file:
        _create_archive(data_dir, archive_file, config.excludes)
        size = archive_file.tell()
        if size > config.max_archive_bytes:
            LOG.error(
                "state archive is %d MB, above the %d MB safety limit; not uploading",
                size // (1024 * 1024),
                config.max_archive_bytes // (1024 * 1024),
            )
            return False
        archive_file.seek(0)
        old_entries = _file_entries(config)
        metadata = _multipart_upload(config, filename, archive_file, size)

    new_id = metadata.get("id") or metadata.get("fileId")
    if not isinstance(new_id, str) or not new_id:
        raise GofileError(f"GoFile upload did not return a file id: {metadata!r}")
    _delete_old_entries(config, old_entries, new_id)
    config.last_fingerprint = fingerprint
    config.last_content_hash = content_hash
    config.last_upload_at = clock
    total = record_usage(data_dir, size, dt.datetime.fromtimestamp(clock, dt.timezone.utc))
    LOG.info(
        "uploaded Hermes state to GoFile (%d KB; %d MB used in %s)",
        size // 1024,
        total // (1024 * 1024),
        month,
    )
    return True


# ---------------------------------------------------------------------------
# Failover leases
# ---------------------------------------------------------------------------
#
# A lease is an (almost) empty file whose NAME carries the instance identity
# and priority, and whose GoFile createTime carries its freshness:
#
#     hermes-lease-100-my-laptop.json
#
# Checking the cluster is therefore a single folder listing with no downloads,
# which keeps the coordination channel effectively free in bandwidth terms.


def lease_filename(priority: int, instance_id: str) -> str:
    return f"{LEASE_PREFIX}{max(0, min(999, priority)):03d}-{sanitize_instance_id(instance_id)}.json"


def parse_lease_name(name: str) -> "tuple[int, str] | None":
    """Return (priority, instance_id) for a lease filename, else None."""
    if not name.startswith(LEASE_PREFIX) or not name.endswith(".json"):
        return None
    body = name[len(LEASE_PREFIX):-len(".json")]
    priority_text, _, instance_id = body.partition("-")
    if not priority_text.isdigit() or not instance_id:
        return None
    return int(priority_text), instance_id


def decide_role(
    leases: "list[tuple[int, str, float]]",
    *,
    instance_id: str,
    priority: int,
    now: float,
    ttl: int,
) -> str:
    """Pure role decision: ACTIVE unless a fresher, higher-priority peer exists.

    `leases` is a list of (priority, instance_id, updated_epoch). Ties break on
    instance_id so two equal-priority instances still agree on which is active
    rather than both going quiet or both staying live.
    """
    mine = (priority, instance_id)
    for peer_priority, peer_id, updated in leases:
        if peer_id == instance_id:
            continue
        if now - updated > ttl:
            continue  # stale: that instance is presumed gone
        if (peer_priority, peer_id) > mine:
            return ROLE_STANDBY
    return ROLE_ACTIVE


def _lease_entries(config: StorageConfig) -> "list[dict]":
    folder_id = _ensure_folder(config)
    entries = []
    for child in _folder_children(config, folder_id).values():
        if not isinstance(child, dict) or child.get("type") != "file":
            continue
        name = str(child.get("name", ""))
        if name.startswith(LEASE_PREFIX):
            entries.append(child)
    return entries


def read_leases(config: StorageConfig) -> "list[tuple[int, str, float]]":
    parsed = []
    for entry in _lease_entries(config):
        info = parse_lease_name(str(entry.get("name", "")))
        if info is None:
            continue
        created = entry.get("createTime") or entry.get("modTime") or 0
        try:
            created = float(created)
        except (TypeError, ValueError):
            created = 0.0
        parsed.append((info[0], info[1], created))
    return parsed


def claim_lease(config: StorageConfig) -> str:
    """Publish/refresh this instance's lease and return the resulting role."""
    entries = _lease_entries(config)
    payload = json.dumps(
        {"id": config.instance_id, "priority": config.priority,
         "updated": int(time.time())}
    ).encode("utf-8")
    filename = lease_filename(config.priority, config.instance_id)
    with tempfile.TemporaryFile(mode="w+b") as handle:
        handle.write(payload)
        size = handle.tell()
        handle.seek(0)
        metadata = _multipart_upload(config, filename, handle, size)
    new_id = metadata.get("id") or metadata.get("fileId")

    # Drop our own previous lease files; leave other instances' alone.
    stale = [
        entry for entry in entries
        if str(entry.get("name", "")) == filename and entry.get("id") != new_id
    ]
    if stale:
        _delete_old_entries(config, stale, new_id if isinstance(new_id, str) else "")

    leases = read_leases(config)
    role = decide_role(
        leases,
        instance_id=config.instance_id,
        priority=config.priority,
        now=time.time(),
        ttl=config.lease_ttl,
    )
    config.role = role
    return role


def release_lease(config: StorageConfig) -> bool:
    """Remove this instance's lease so a standby can take over promptly."""
    filename = lease_filename(config.priority, config.instance_id)
    mine = [e for e in _lease_entries(config) if str(e.get("name", "")) == filename]
    if not mine:
        return True
    _delete_old_entries(config, mine, "")
    LOG.info("released failover lease %s", filename)
    return True


def current_role(config: StorageConfig) -> str:
    """Role this instance should take right now, without publishing a lease."""
    if not config.failover:
        return ROLE_ACTIVE
    return decide_role(
        read_leases(config),
        instance_id=config.instance_id,
        priority=config.priority,
        now=time.time(),
        ttl=config.lease_ttl,
    )


def _configured_or_log() -> StorageConfig | None:
    config = StorageConfig.from_env()
    if config is None:
        LOG.info("GoFile state sync disabled; set GOFILE_API_TOKEN to enable it")
    return config


def _find_gateway_pid() -> "int | None":
    """PID of the foreground `hermes gateway run` process, if we can see it.

    A role change has to be applied by restarting the gateway with a different
    environment (chat tokens present or stripped). The gateway is the
    container's foreground process, so terminating it exits the container and
    the platform restarts it -- at which point bootstrap.sh re-evaluates the
    role. We can only signal it because the sync daemon and the gateway run as
    the same unprivileged user.
    """
    me = os.getpid()
    my_uid = os.getuid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in (me, 1):
            continue
        try:
            if os.stat(f"/proc/{pid}").st_uid != my_uid:
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                argv = handle.read().split(b"\0")
        except OSError:
            continue
        parts = [a.decode("utf-8", "replace") for a in argv if a]
        if not parts:
            continue
        if any(part.endswith("hermes") for part in parts[:2]) and "gateway" in parts:
            return pid
    return None


def _request_restart(reason: str) -> bool:
    pid = _find_gateway_pid()
    if pid is None:
        LOG.warning(
            "role change (%s) needs a gateway restart, but the gateway process "
            "was not found; restart the service manually", reason
        )
        return False
    LOG.info("role change (%s): terminating gateway pid %d so it restarts", reason, pid)
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError as exc:
        LOG.warning("could not signal gateway pid %d: %s", pid, exc)
        return False


def run_daemon(data_dir: Path, config: StorageConfig) -> None:
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    LOG.info(
        "GoFile state sync enabled; interval=%ss min-upload-interval=%ss budget=%dMB",
        config.interval,
        config.min_upload_interval,
        config.monthly_budget_bytes // (1024 * 1024),
    )

    last_heartbeat = 0.0
    last_poll = 0.0
    last_switch = time.time()
    if config.failover:
        try:
            config.role = claim_lease(config)
            last_heartbeat = time.time()
            LOG.info(
                "failover enabled; instance=%s priority=%d role=%s",
                config.instance_id, config.priority, config.role,
            )
        except (GofileError, OSError, HTTPError, URLError, ValueError) as exc:
            LOG.warning("could not claim failover lease; assuming active: %s", exc)
            config.role = ROLE_ACTIVE

    # Let the upstream entrypoint finish its first-boot directory setup before
    # the initial upload. Restore happens before setup in bootstrap.sh.
    if stop.wait(min(15, config.interval)):
        return

    next_sync = 0.0
    while not stop.is_set():
        now = time.time()

        if config.failover:
            if now - last_heartbeat >= config.heartbeat_interval:
                try:
                    claim_lease(config)
                    last_heartbeat = now
                except (GofileError, OSError, HTTPError, URLError, ValueError) as exc:
                    LOG.warning("failover heartbeat failed: %s", exc)
            if now - last_poll >= config.poll_interval:
                last_poll = now
                try:
                    role = current_role(config)
                except (GofileError, OSError, HTTPError, URLError, ValueError) as exc:
                    LOG.debug("failover poll failed: %s", exc)
                    role = config.role
                if role != config.role:
                    if now - last_switch < config.role_switch_min:
                        LOG.info(
                            "role would change to %s but the last switch was %ds ago; waiting",
                            role, int(now - last_switch),
                        )
                    else:
                        previous, config.role = config.role, role
                        last_switch = now
                        # Going standby: hand off cleanly by flushing state
                        # first, so the instance taking over restores it.
                        if previous == ROLE_ACTIVE:
                            try:
                                sync_once(data_dir, config, force=True)
                            except (GofileError, OSError, HTTPError, URLError,
                                    tarfile.TarError, ValueError) as exc:
                                LOG.warning("handoff upload failed: %s", exc)
                        _request_restart(f"{previous} -> {role}")
                        stop.wait(5)
                        continue

        if now >= next_sync:
            next_sync = now + config.interval
            try:
                sync_once(data_dir, config)
            except (GofileError, OSError, HTTPError, URLError, tarfile.TarError, ValueError) as exc:
                LOG.warning("GoFile state upload failed; will retry: %s", exc)

        stop.wait(min(config.poll_interval if config.failover else config.interval,
                      config.interval))

    # Render normally sends SIGTERM to the process group. Upload a final
    # archive BEFORE releasing the lease, so whoever takes over restores the
    # complete state rather than the previous snapshot.
    try:
        sync_once(data_dir, config, force=True)
    except (GofileError, OSError, HTTPError, URLError, tarfile.TarError, ValueError) as exc:
        LOG.warning("final GoFile state upload failed: %s", exc)
    if config.failover:
        try:
            release_lease(config)
        except (GofileError, OSError, HTTPError, URLError, ValueError) as exc:
            LOG.warning("could not release failover lease: %s", exc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("restore", "sync", "daemon", "role", "release")
    )
    parser.add_argument("data_dir", type=Path)
    args = parser.parse_args()
    logging.basicConfig(
        level=os.environ.get("GOFILE_LOG_LEVEL", "INFO").upper(),
        format="[hermes-gofile] %(message)s",
    )
    config = _configured_or_log()
    if config is None:
        # Without remote storage there is no coordination channel, so a lone
        # instance is always the active one.
        if args.command == "role":
            print(ROLE_ACTIVE)
        return 0
    args.data_dir.mkdir(parents=True, exist_ok=True)

    if args.command == "role":
        # Printed on stdout for bootstrap.sh to read; failures default to
        # active so a coordination outage never silences a lone instance.
        try:
            print(current_role(config))
        except (GofileError, OSError, HTTPError, URLError, ValueError) as exc:
            LOG.warning("could not determine failover role; assuming active: %s", exc)
            print(ROLE_ACTIVE)
        return 0

    if args.command == "release":
        try:
            release_lease(config)
        except (GofileError, OSError, HTTPError, URLError, ValueError) as exc:
            LOG.warning("could not release failover lease: %s", exc)
            return 1
        return 0
    if args.command == "restore":
        try:
            restore(args.data_dir, config)
        except (GofileError, OSError, HTTPError, URLError, tarfile.TarError, ValueError) as exc:
            LOG.warning("GoFile state restore failed; continuing with local state: %s", exc)
            # Tell bootstrap not to upload a fresh instance over a remote
            # archive that could not be read.
            return 2
        return 0
    if args.command == "sync":
        try:
            return 0 if sync_once(args.data_dir, config) else 1
        except (GofileError, OSError, HTTPError, URLError, tarfile.TarError, ValueError) as exc:
            LOG.error("GoFile state upload failed: %s", exc)
            return 1
    run_daemon(args.data_dir, config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
