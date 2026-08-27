#!/opt/hermes/.venv/bin/python
"""Optional GoFile state sync for Render's Free filesystem.

Render Free services cannot attach persistent disks. When configured with a
GoFile account token, this module resolves a state folder and restores the
newest Hermes state archive before Hermes starts and periodically uploads a
fresh archive while Hermes is running. It keeps one current archive by deleting
older matching backups after a successful upload. Syncs are metadata-aware and
run at low priority so idle Free instances do not repeatedly recompress state.

The archive contains the complete Hermes data directory, including
``.env`` and runtime logs, so dashboard settings, credentials, sessions, and
user-created files survive a replacement. Treat the configured GoFile folder
as sensitive because the archive contains secrets. No third-party Python
package is required.
"""
from __future__ import annotations

import argparse
import datetime as dt
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


class GofileError(RuntimeError):
    """A GoFile API response was unsuccessful."""


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
        self.last_fingerprint: str | None = None

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


def _archive_filter(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
    # Archive every regular data file, including dotfiles, .env, and logs.
    # Sockets and device files cannot be meaningfully restored into a new
    # container and must not be copied from a remote archive.
    if not (member.isdir() or member.isfile()):
        return None
    return member


def _create_archive(data_dir: Path, destination: BinaryIO) -> None:
    # Level 3 materially reduces CPU spikes on the single Free CPU while
    # keeping the archive substantially smaller than an uncompressed tar.
    with tarfile.open(fileobj=destination, mode="w:gz", compresslevel=3) as archive:
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


def _state_fingerprint(data_dir: Path) -> str:
    """Return a cheap metadata fingerprint for change-aware syncs.

    Reading metadata is much cheaper than recompressing /opt/data every
    minute. This walk covers every regular file in the data directory,
    including .env and logs, and skips only symlinked/non-directory entries
    that the archive filter cannot restore safely.
    """
    digest = hashlib.blake2b(digest_size=16)
    for root, dirs, files in os.walk(data_dir, topdown=True, followlinks=False):
        dirs[:] = sorted(
            name
            for name in dirs
            if not (Path(root) / name).is_symlink()
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
            digest.update(
                f"F\\0{relative}\\0{info.st_size}\\0{info.st_mtime_ns}\\0{info.st_mode & 0o7777}\\n".encode()
            )
    return digest.hexdigest()


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


def sync_once(data_dir: Path, config: StorageConfig, *, force: bool = False) -> bool:
    fingerprint = _state_fingerprint(data_dir)
    if not force and config.last_fingerprint == fingerprint:
        LOG.debug("state unchanged; skipping GoFile archive")
        return True

    filename = f"{config.prefix}{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}.tar.gz"
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
        old_entries = _file_entries(config)
        metadata = _multipart_upload(config, filename, archive_file, size)

    new_id = metadata.get("id") or metadata.get("fileId")
    if not isinstance(new_id, str) or not new_id:
        raise GofileError(f"GoFile upload did not return a file id: {metadata!r}")
    _delete_old_entries(config, old_entries, new_id)
    config.last_fingerprint = fingerprint
    LOG.info("uploaded Hermes state to GoFile (%d bytes)", size)
    return True


def _configured_or_log() -> StorageConfig | None:
    config = StorageConfig.from_env()
    if config is None:
        LOG.info("GoFile state sync disabled; set GOFILE_API_TOKEN to enable it")
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
        sync_once(data_dir, config, force=True)
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
