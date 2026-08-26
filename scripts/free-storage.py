#!/opt/hermes/.venv/bin/python
"""Optional GoFile state sync for Render's Free filesystem.

Render Free services cannot attach persistent disks. When configured with a
GoFile account token and folder, this module restores the newest Hermes state
archive before Hermes starts and periodically uploads a fresh archive while
Hermes is running. It keeps one current archive by deleting older matching
backups after a successful upload.

The archive excludes /opt/data/.env and runtime logs. Render environment
variables are the durable source for secrets, and Render already retains
service logs. No third-party Python package is required.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import signal
import tarfile
import tempfile
import threading
import time
from typing import BinaryIO
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
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


class GofileError(RuntimeError):
    """A GoFile API response was unsuccessful."""


class StorageConfig:
    def __init__(
        self,
        token: str,
        folder_id: str,
        prefix: str,
        interval: int,
        max_archive_bytes: int,
        user_agent: str,
        language: str,
        wt_salt: str,
    ) -> None:
        self.token = token
        self.folder_id = folder_id
        self.prefix = prefix or "hermes-state-"
        self.interval = interval
        self.max_archive_bytes = max_archive_bytes
        self.user_agent = user_agent
        self.language = language
        self.wt_salt = wt_salt

    @classmethod
    def from_env(cls) -> "StorageConfig | None":
        token = (os.environ.get("GOFILE_API_TOKEN") or os.environ.get("GOFILE_TOKEN", "")).strip()
        folder_id = os.environ.get("GOFILE_FOLDER_ID", "").strip()
        if not token or not folder_id:
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
            prefix=os.environ.get("GOFILE_STATE_PREFIX", "hermes-state-"),
            interval=positive_int("GOFILE_SYNC_INTERVAL_SECONDS", 60),
            max_archive_bytes=positive_int("GOFILE_MAX_ARCHIVE_MB", 25) * 1024 * 1024,
            user_agent=os.environ.get("GOFILE_USER_AGENT", DEFAULT_USER_AGENT),
            language=os.environ.get("GOFILE_LANGUAGE", "en-US"),
            wt_salt=os.environ.get("GOFILE_WT_SALT", GOFILE_WT_SALT),
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


def _download(url: str, config: StorageConfig) -> bytes:
    last_error: Exception | None = None
    # The website token rotates every four hours. Retry the previous window
    # around a boundary, matching the behavior of GoFile's web clients.
    for offset in (0, -1):
        headers = _content_headers(config, offset)
        request = Request(url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=60) as response:
                payload = response.read(config.max_archive_bytes + 1)
            if len(payload) > config.max_archive_bytes:
                raise GofileError(
                    f"remote archive is larger than the {config.max_archive_bytes // (1024 * 1024)} MB limit"
                )
            return payload
        except (HTTPError, URLError, OSError, GofileError) as exc:
            last_error = exc
            if isinstance(exc, HTTPError) and exc.code == 404:
                break
    raise GofileError(f"GoFile download failed: {last_error}")


def _multipart_upload(config: StorageConfig, filename: str, payload: bytes) -> dict:
    boundary = f"----hermes-gofile-{uuid.uuid4().hex}"
    boundary_bytes = boundary.encode("ascii")
    parts = [
        b"--" + boundary_bytes + b"\r\n",
        b'Content-Disposition: form-data; name="folderId"\r\n\r\n',
        config.folder_id.encode("utf-8"),
        b"\r\n--" + boundary_bytes + b"\r\n",
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(
            "utf-8"
        ),
        b"Content-Type: application/gzip\r\n\r\n",
        payload,
        b"\r\n--" + boundary_bytes + b"--\r\n",
    ]
    body = b"".join(parts)
    headers = _content_headers(config)
    headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    request = Request(GOFILE_UPLOAD_URL, data=body, headers=headers, method="POST")
    with urlopen(request, timeout=120) as response:
        result = json.loads(response.read())
    if not isinstance(result, dict) or result.get("status") != "ok":
        raise GofileError(f"GoFile upload failed: {result!r}")
    data = result.get("data")
    if not isinstance(data, dict):
        raise GofileError(f"GoFile upload returned no file metadata: {result!r}")
    return data


def _archive_filter(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
    name = member.name.removeprefix("./").rstrip("/")
    if name == ".env" or name.startswith(".env/"):
        return None
    if name == "logs" or name.startswith("logs/"):
        return None
    # Do not copy sockets, device files, or symlinks into a new instance.
    if not (member.isdir() or member.isfile()):
        return None
    return member


def _create_archive(data_dir: Path, destination: BinaryIO) -> None:
    with tarfile.open(fileobj=destination, mode="w:gz") as archive:
        archive.add(data_dir, arcname=".", recursive=True, filter=_archive_filter)


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


def _file_entries(config: StorageConfig) -> list[dict]:
    # GoFile's current web API accepts the website token for free accounts.
    # Ask for newest first and still sort locally for defensive compatibility.
    data = _json_request(
        config,
        "GET",
        f"/contents/{quote(config.folder_id, safe='-_.~')}",
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
        raise GofileError("GOFILE_FOLDER_ID is not a folder owned by this token")
    children = data.get("children", {})
    if not isinstance(children, dict):
        return []
    entries = [
        child
        for child in children.values()
        if isinstance(child, dict)
        and child.get("type") == "file"
        and str(child.get("name", "")).startswith(config.prefix)
    ]
    entries.sort(
        key=lambda item: (item.get("createTime", 0), item.get("modTime", 0)),
        reverse=True,
    )
    return entries


def restore(data_dir: Path, config: StorageConfig) -> bool:
    entries = _file_entries(config)
    if not entries:
        LOG.info("no GoFile Hermes state found; starting with a clean instance")
        return False
    link = entries[0].get("link")
    if not isinstance(link, str) or not link:
        raise GofileError("newest GoFile state file has no download link")
    payload = _download(link, config)
    if not payload:
        raise GofileError("newest GoFile state file was empty")
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
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


def sync_once(data_dir: Path, config: StorageConfig) -> bool:
    with tempfile.TemporaryFile(mode="w+b") as archive_file:
        _create_archive(data_dir, archive_file)
        size = archive_file.tell()
        if size > config.max_archive_bytes:
            LOG.error(
                "state archive is %d MB, above the %d MB safety limit; not uploading",
                size // (1024 * 1024),
                config.max_archive_bytes // (1024 * 1024),
            )
            return False
        archive_file.seek(0)
        payload = archive_file.read()

    filename = f"{config.prefix}{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}.tar.gz"
    old_entries = _file_entries(config)
    metadata = _multipart_upload(config, filename, payload)
    new_id = metadata.get("id") or metadata.get("fileId")
    if not isinstance(new_id, str) or not new_id:
        raise GofileError(f"GoFile upload did not return a file id: {metadata!r}")
    _delete_old_entries(config, old_entries, new_id)
    LOG.info("uploaded Hermes state to GoFile (%d bytes)", len(payload))
    return True


def _configured_or_log() -> StorageConfig | None:
    config = StorageConfig.from_env()
    if config is None:
        LOG.info(
            "GoFile state sync disabled; set GOFILE_API_TOKEN and "
            "GOFILE_FOLDER_ID to enable it"
        )
    return config


def run_daemon(data_dir: Path, config: StorageConfig) -> None:
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    LOG.info("GoFile state sync enabled; interval=%ss", config.interval)

    # Let the upstream entrypoint finish its first-boot directory setup before
    # the initial upload. Restore happens before setup in bootstrap.sh.
    if stop.wait(min(15, config.interval)):
        return
    while not stop.is_set():
        try:
            sync_once(data_dir, config)
        except (GofileError, OSError, HTTPError, URLError, tarfile.TarError, ValueError) as exc:
            LOG.warning("GoFile state upload failed; will retry: %s", exc)
        stop.wait(config.interval)

    # Render normally sends SIGTERM to the process group. Make a best-effort
    # final upload so a recent chat/config change is not lost.
    try:
        sync_once(data_dir, config)
    except (GofileError, OSError, HTTPError, URLError, tarfile.TarError, ValueError) as exc:
        LOG.warning("final GoFile state upload failed: %s", exc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("restore", "sync", "daemon"))
    parser.add_argument("data_dir", type=Path)
    args = parser.parse_args()
    logging.basicConfig(
        level=os.environ.get("GOFILE_LOG_LEVEL", "INFO").upper(),
        format="[hermes-gofile] %(message)s",
    )
    config = _configured_or_log()
    if config is None:
        return 0
    args.data_dir.mkdir(parents=True, exist_ok=True)
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
