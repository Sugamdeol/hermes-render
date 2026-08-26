#!/opt/hermes/.venv/bin/python
"""Optional S3-compatible state sync for Render's Free filesystem.

Render Free services cannot attach persistent disks. When configured with an
S3-compatible object store (for example, a Cloudflare R2 bucket), this module
restores a compressed archive before Hermes starts and periodically uploads the
current `/opt/data` state while Hermes is running. Runtime logs are left out
because Render already retains service logs and log files can grow without
bound.

The archive deliberately excludes `/opt/data/.env`. Render environment variables
are the durable source for secrets, and copying .env to object storage would
make an otherwise safe backup unexpectedly contain provider keys.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import io
import logging
import os
from pathlib import Path
import signal
import tarfile
import tempfile
import threading
from typing import BinaryIO
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

LOG = logging.getLogger("hermes-free-storage")


class StorageConfig:
    def __init__(
        self,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str,
        prefix: str,
        interval: int,
        max_archive_bytes: int,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.prefix = prefix.strip("/")
        self.interval = interval
        self.max_archive_bytes = max_archive_bytes

    @property
    def object_key(self) -> str:
        return f"{self.prefix}/state.tar.gz" if self.prefix else "state.tar.gz"

    @classmethod
    def from_env(cls) -> "StorageConfig | None":
        values = {
            "endpoint": os.environ.get("HERMES_STORAGE_ENDPOINT_URL", "").strip(),
            "bucket": os.environ.get("HERMES_STORAGE_BUCKET", "").strip(),
            "access_key": os.environ.get("HERMES_STORAGE_ACCESS_KEY_ID", "").strip(),
            "secret_key": os.environ.get("HERMES_STORAGE_SECRET_ACCESS_KEY", "").strip(),
        }
        if not all(values.values()):
            return None

        def positive_int(name: str, default: int) -> int:
            try:
                return max(1, int(os.environ.get(name, str(default))))
            except ValueError:
                LOG.warning("invalid %s; using %s", name, default)
                return default

        return cls(
            **values,
            region=os.environ.get("HERMES_STORAGE_REGION", "auto").strip() or "auto",
            prefix=os.environ.get("HERMES_STORAGE_PREFIX", "hermes"),
            interval=positive_int("HERMES_STORAGE_SYNC_INTERVAL_SECONDS", 60),
            max_archive_bytes=positive_int(
                "HERMES_STORAGE_MAX_ARCHIVE_MB", 25
            )
            * 1024
            * 1024,
        )


def _hmac(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, date: str, region: str) -> bytes:
    date_key = _hmac(("AWS4" + secret).encode("utf-8"), date)
    region_key = _hmac(date_key, region)
    service_key = _hmac(region_key, "s3")
    return _hmac(service_key, "aws4_request")


def _object_url(config: StorageConfig) -> tuple[str, str]:
    parsed = urlsplit(config.endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("HERMES_STORAGE_ENDPOINT_URL must be an http(s) URL")
    bucket = quote(config.bucket, safe="-_.~")
    key = quote(config.object_key, safe="/-_.~")
    path = f"{parsed.path.rstrip('/')}/{bucket}/{key}"
    url = urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    return url, path or "/"


def _request(config: StorageConfig, method: str, body: bytes | None = None) -> bytes:
    url, canonical_uri = _object_url(config)
    parsed = urlsplit(url)
    payload_hash = hashlib.sha256(body or b"").hexdigest()
    now = dt.datetime.now(dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    short_date = now.strftime("%Y%m%d")
    headers = {
        "Host": parsed.netloc,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if body is not None:
        headers["Content-Type"] = "application/gzip"

    canonical_headers = "".join(
        f"{name.lower()}:{' '.join(value.strip().split())}\n"
        for name, value in sorted(headers.items(), key=lambda item: item[0].lower())
    )
    signed_headers = ";".join(name.lower() for name in sorted(headers))
    canonical_request = "\n".join(
        [
            method,
            canonical_uri,
            "",
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )
    scope = f"{short_date}/{config.region}/s3/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    signature = hmac.new(
        _signing_key(config.secret_key, short_date, config.region),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    headers["Authorization"] = (
        "AWS4-HMAC-SHA256 "
        f"Credential={config.access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    request = Request(url, data=body, headers=headers, method=method)
    with urlopen(request, timeout=30) as response:
        return response.read()


def _archive_filter(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
    name = member.name.removeprefix("./").rstrip("/")
    if name == ".env" or name.startswith(".env/"):
        return None
    # Render already keeps service logs, and log files can grow without bound.
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


def restore(data_dir: Path, config: StorageConfig) -> bool:
    try:
        payload = _request(config, "GET")
    except HTTPError as exc:
        if exc.code == 404:
            LOG.info("no remote Hermes state found; starting with a clean instance")
            return False
        raise
    if not payload:
        LOG.warning("remote Hermes state archive was empty; ignoring it")
        return False
    if len(payload) > config.max_archive_bytes:
        raise ValueError(
            f"remote state archive is larger than the {config.max_archive_bytes // (1024 * 1024)} MB safety limit"
        )
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        _safe_extract(archive, data_dir)
    LOG.info("restored Hermes state from %s/%s", config.bucket, config.object_key)
    return True


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
    _request(config, "PUT", payload)
    LOG.info("uploaded Hermes state archive (%d bytes)", len(payload))
    return True


def _configured_or_log() -> StorageConfig | None:
    config = StorageConfig.from_env()
    if config is None:
        LOG.info(
            "remote state sync disabled; set HERMES_STORAGE_ENDPOINT_URL, "
            "HERMES_STORAGE_BUCKET, HERMES_STORAGE_ACCESS_KEY_ID, and "
            "HERMES_STORAGE_SECRET_ACCESS_KEY to enable it"
        )
    return config


def run_daemon(data_dir: Path, config: StorageConfig) -> None:
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    LOG.info("remote state sync enabled; interval=%ss", config.interval)

    # Let the upstream entrypoint finish its first-boot directory setup before
    # the initial upload. Restore happens before setup in bootstrap.sh.
    if stop.wait(min(15, config.interval)):
        return
    while not stop.is_set():
        try:
            sync_once(data_dir, config)
        except (OSError, ValueError, HTTPError, URLError, tarfile.TarError) as exc:
            LOG.warning("state upload failed; will retry: %s", exc)
        stop.wait(config.interval)

    # Render normally sends SIGTERM to the process group. Make a best-effort
    # final upload so a recent chat/config change is not lost.
    try:
        sync_once(data_dir, config)
    except (OSError, ValueError, HTTPError, URLError, tarfile.TarError) as exc:
        LOG.warning("final state upload failed: %s", exc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("restore", "sync", "daemon"))
    parser.add_argument("data_dir", type=Path)
    args = parser.parse_args()
    logging.basicConfig(
        level=os.environ.get("HERMES_STORAGE_LOG_LEVEL", "INFO").upper(),
        format="[hermes-storage] %(message)s",
    )
    config = _configured_or_log()
    if config is None:
        return 0
    args.data_dir.mkdir(parents=True, exist_ok=True)
    if args.command == "restore":
        try:
            restore(args.data_dir, config)
        except (OSError, ValueError, HTTPError, URLError, tarfile.TarError) as exc:
            LOG.warning("state restore failed; continuing with local state: %s", exc)
            # Tell bootstrap not to upload a fresh empty instance over a
            # remote archive that could not be read.
            return 2
        return 0
    if args.command == "sync":
        try:
            return 0 if sync_once(args.data_dir, config) else 1
        except (OSError, ValueError, HTTPError, URLError, tarfile.TarError) as exc:
            LOG.error("state upload failed: %s", exc)
            return 1
    run_daemon(args.data_dir, config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
