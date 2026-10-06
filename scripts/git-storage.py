#!/opt/hermes/.venv/bin/python
"""Git-backed state sync for Hermes on Render.

Render's Free tier has no persistent disk, so /opt/data has to live somewhere
else. This backend mirrors it into a private GitHub repository and transfers
only changed files. Encrypted database snapshots are compressed before sealing;
their ciphertext must be uploaded again when the database changes.

Layout in the state repo (default branch ``state``):

    data/            mirror of $HERMES_HOME, minus excluded paths
    data/.env.enc    encrypted dotenv when GIT_STATE_ENV_MODE=encrypt
    MANIFEST.json    instance id, timestamp, and what transforms were applied

The manifest also carries the parts of the tree git cannot hold by itself: the
file modes git does not record (everything except the executable bit), the
empty directories, and the relative symlinks that stay inside the data
directory. Without them a restore quietly returns a different tree from the
one that was saved -- a hook that no longer runs, a ``memories/`` that is gone,
a private key that is suddenly world-readable.

Deletions are mirrored. The working tree is rebuilt from the data directory on
every sync, so ``git add -A`` records removals as well as additions and the
repo never accumulates files you deleted locally.

GitHub is the durable store. The one wrinkle is the very first launch, when
the state repo is still empty: there is nothing to restore from it, so
bootstrap.sh flags the situation (GIT_STATE_SEED_ON_BOOT) and the daemon then
pushes whatever this instance built locally as the first commit with ``seed``.
``seed`` refuses to write over a branch that already holds state, so a stale
local tree can never be pushed on top of newer GitHub state.

The seed belongs to the daemon, not to the boot wrapper. A first state push is
the largest upload this service ever makes, and Render fails a deploy whose
container has not bound a port within its scan window ("no open ports
detected"). Running the seed inline meant one slow GitHub response -- an HTTP
408 on a chunked upload, retried -- could push the dashboard past that window
and take the whole deploy with it. In the daemon it runs after the gateway is
up, so a slow or failed push costs a retry on the next tick instead of a
redeploy.

Saves are change-driven rather than clock-driven: the daemon fingerprints
``/opt/data`` every GIT_STATE_WATCH_SECONDS and pushes as soon as the tree has
been quiet for GIT_STATE_DEBOUNCE_SECONDS, so a new message or a dashboard edit
lands in GitHub within seconds instead of waiting for the next interval. The
interval (GIT_STATE_INTERVAL_SECONDS) stays as a safety net, and
GIT_STATE_MIN_PUSH_INTERVAL_SECONDS stops a very chatty agent from turning one
busy minute into dozens of commits.

Safety rules this module enforces rather than documents:

  * It refuses to push to a public repository. /opt/data contains .env, chat
    history, and memories.
  * Sensitive settings follow GIT_STATE_ENV_MODE. The Render Blueprint uses
    authenticated encryption; plaintext mode is supported for private repos
    and logs a warning. Omit leaves the values out, never falling back to
    plaintext when encryption cannot run.
  * SQLite databases are backed up with SQLite's online snapshot API so WAL
    pages are included atomically. Encrypt mode seals snapshots in bounded
    chunks; omit mode leaves them out.
  * It refuses to seed GitHub over a branch that already holds state, unless
    GIT_STATE_SEED_FORCE=1 says the operator meant it.
  * Tokens are stripped from every log line and exception message.

History is bounded: after GIT_STATE_MAX_COMMITS commits the branch is squashed
to a single orphan commit and replaced with a force-with-lease push, so the repo
cannot grow without limit and concurrent changes are not overwritten.

Failover leases ride on Git refs (refs/hermes-lease/<priority>-<id>-<epoch>),
so checking the cluster is one `git ls-remote` -- no clone, no checkout, a few
kilobytes. The highest-priority instance with a fresh lease is ACTIVE;
everyone else is STANDBY and does not push state or answer chat platforms.

The dotenv is part of the backup. Render uses GIT_STATE_ENV_MODE=encrypt;
plaintext remains available for a repository proven private, while omit leaves
the file out. Old plaintext commits cannot be erased by changing this setting.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import signal
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

LOG = logging.getLogger("hermes-git-storage")

LEASE_REF_PREFIX = "refs/hermes-lease/"
DATA_SUBDIR = "data"
MANIFEST_NAME = "MANIFEST.json"
ENCRYPTED_SUFFIX = ".enc"
ROLE_ACTIVE = "active"
ROLE_STANDBY = "standby"

# What the remote state branch looks like to us. "unknown" is the answer for
# a repo we could not reach at all, which is deliberately NOT the same as
# "empty": only an empty branch is safe to seed.
REMOTE_HAS_STATE = "has-state"
REMOTE_EMPTY = "empty"
REMOTE_UNKNOWN = "unknown"

# git's http.postBuffer defaults to 1 MiB, and anything larger than that is
# sent as a chunked request. GitHub's HTTP front end answers a big chunked
# `git-receive-pack` with "RPC failed; HTTP 408 ... send-pack: unexpected
# disconnect" -- which is exactly what a first state push (the whole restored
# tree, tens of MB) looks like. A buffer big enough for one request makes git
# send it with a Content-Length instead, in a single shot.
DEFAULT_HTTP_POST_BUFFER_BYTES = 32 * 1024 * 1024
# A transfer slower than this for this long is stalled, not slow. git gives up
# and we retry, instead of the process sitting on a dead connection.
DEFAULT_HTTP_LOW_SPEED_LIMIT = 1000
DEFAULT_HTTP_LOW_SPEED_TIME = 60
# No git command here should ever run forever: the boot wrapper and the daemon
# both wait on them.
DEFAULT_GIT_TIMEOUT_SECONDS = 600
DEFAULT_PUSH_ATTEMPTS = 3
DEFAULT_PUSH_RETRY_SECONDS = 5

# Files that hold live credentials. Matched on the data-dir relative path.
# Whether they are committed in the clear is GIT_STATE_ENV_MODE's decision,
# not a hard-coded one: the operator asked for a complete, restartable copy of
# the instance, and .env is part of that.
SENSITIVE_FILES = (".env", "config.yaml", "auth.json", ".render-recovery.json", ".render-runtime-env.json")
_SEALED_PREFIX = b"HERMES-FERNET-v1\n"
_SEALED_CACHE = {}

# How a sensitive file reaches the branch.
#   plaintext - committed as-is (default). The branch can restore the
#               instance on its own, at the cost of putting live API keys in
#               git history. Only ever pushed to a proven-private repo.
#   encrypt   - sealed with age first and stored as <name>.enc, so the branch
#               holds the secrets but cannot read them.
#   omit      - left out of the backup entirely.
ENV_MODE_PLAINTEXT = "plaintext"
ENV_MODE_ENCRYPT = "encrypt"
ENV_MODE_OMIT = "omit"
ENV_MODES = (ENV_MODE_PLAINTEXT, ENV_MODE_ENCRYPT, ENV_MODE_OMIT)
DEFAULT_ENV_MODE = ENV_MODE_PLAINTEXT

# Paths that change constantly and are worthless in a restored instance.
# Excluding them from the mirror keeps the fingerprint stable (log churn
# cannot trigger a push) and the backup small.
DEFAULT_EXCLUDES = (
    "logs",
    "*.log",
    "*.log.*",
    "__pycache__",
    "*.pyc",
    ".cache",
    "node_modules",
    ".venv",
    "venv",
    "site-packages",
    "tmp",
    "*.tmp",
    "*.sock",
    ".git",
)


class GitStateError(RuntimeError):
    """A git or GitHub operation failed."""


def sanitize_instance_id(raw: str) -> str:
    """Reduce a hostname to something safe to embed in a ref name."""
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in raw.strip())
    cleaned = cleaned.strip("-") or "instance"
    return cleaned[:40]


def is_excluded(relative: str, excludes: "tuple[str, ...]" = DEFAULT_EXCLUDES) -> bool:
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


def redact(text: str) -> str:
    """Strip credentials from anything we are about to log or raise.

    git puts the remote URL in its error messages, and the remote URL carries
    the token. Without this, a failed push writes the token into Render's logs.
    """
    text = re.sub(r"(https://)[^@/\s]+(@)", r"\1***\2", text)
    text = re.sub(r"gh[pousr]_[A-Za-z0-9]{20,}", "***", text)
    text = re.sub(r"github_pat_[A-Za-z0-9_]{20,}", "***", text)
    return text


class GitConfig:
    def __init__(
        self,
        repo: str,
        token: str,
        branch: str = "state",
        interval: int = 300,
        max_commits: int = 200,
        age_recipient: str = "",
        age_key_file: str = "",
        instance_id: str = "instance",
        priority: int = 50,
        failover: bool = False,
        lease_ttl: int = 600,
        heartbeat_interval: int = 120,
        poll_interval: int = 30,
        role_switch_min: int = 300,
        allow_public: bool = False,
        env_mode: str = DEFAULT_ENV_MODE,
        workdir: "Path | None" = None,
        watch: bool = True,
        watch_seconds: int = 5,
        debounce_seconds: int = 10,
        min_push_interval: int = 20,
        retry_seconds: int = 30,
        seed_on_boot: bool = False,
        seed_force: bool = False,
        push_attempts: int = DEFAULT_PUSH_ATTEMPTS,
        push_retry_seconds: int = DEFAULT_PUSH_RETRY_SECONDS,
        git_timeout_seconds: int = DEFAULT_GIT_TIMEOUT_SECONDS,
        http_post_buffer: int = DEFAULT_HTTP_POST_BUFFER_BYTES,
        http_low_speed_limit: int = DEFAULT_HTTP_LOW_SPEED_LIMIT,
        http_low_speed_time: int = DEFAULT_HTTP_LOW_SPEED_TIME,
    ) -> None:
        self.repo = repo
        self.token = token
        self.branch = branch or "state"
        self.interval = interval
        self.max_commits = max_commits
        self.age_recipient = age_recipient
        self.age_key_file = age_key_file
        self.instance_id = instance_id
        self.priority = priority
        self.failover = failover
        self.lease_ttl = lease_ttl
        self.heartbeat_interval = heartbeat_interval
        self.poll_interval = poll_interval
        self.role_switch_min = role_switch_min
        self.allow_public = allow_public
        self.workdir = workdir or Path(
            os.environ.get("GIT_STATE_WORKDIR", "/tmp/hermes-git-state")
        )
        self.watch = watch
        self.watch_seconds = watch_seconds
        self.debounce_seconds = debounce_seconds
        self.min_push_interval = min_push_interval
        self.retry_seconds = retry_seconds
        # Set when this instance booted without restoring from GitHub -- the
        # branch was empty, or unreachable. Until the seed succeeds, it is a
        # promise not to overwrite GitHub state we never restored from.
        self.seed_on_boot = seed_on_boot
        self.seed_force = seed_force
        # An unrecognised mode is a typo, not a request to guess: fall back to
        # the default rather than to something more permissive.
        self.env_mode = env_mode if env_mode in ENV_MODES else DEFAULT_ENV_MODE
        self._env_mode_warned = False
        # How hard a push is worth trying before the caller gives up on this
        # round. GitHub's HTTP front end drops large uploads often enough that
        # one attempt is not a real answer.
        self.push_attempts = max(1, int(push_attempts))
        self.push_retry_seconds = max(0, int(push_retry_seconds))
        self.git_timeout_seconds = max(1, int(git_timeout_seconds))
        self.http_post_buffer = max(1024 * 1024, int(http_post_buffer))
        self.http_low_speed_limit = max(0, int(http_low_speed_limit))
        self.http_low_speed_time = max(1, int(http_low_speed_time))
        self.role = ROLE_ACTIVE
        self._visibility_checked = False
        self._last_push_at = 0.0

    @classmethod
    def from_env(cls) -> "GitConfig | None":
        repo = os.environ.get("GIT_STATE_REPO", "").strip()
        token = (
            os.environ.get("GIT_STATE_TOKEN")
            or os.environ.get("GITHUB_TOKEN")
            or ""
        ).strip()
        if not repo or not token:
            return None

        def positive_int(name: str, default: int) -> int:
            try:
                return max(1, int(os.environ.get(name, str(default))))
            except (TypeError, ValueError):
                LOG.warning("invalid %s; using %s", name, default)
                return default

        def non_negative_int(name: str, default: int) -> int:
            """Like positive_int, but 0 is a meaningful value ("no wait")."""
            try:
                return max(0, int(os.environ.get(name, str(default))))
            except (TypeError, ValueError):
                LOG.warning("invalid %s; using %s", name, default)
                return default

        def flag(name: str) -> bool:
            return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")

        def flag_on(name: str) -> bool:
            """On unless explicitly turned off."""
            raw = os.environ.get(name, "").strip().lower()
            if not raw:
                return True
            return raw in ("1", "true", "yes", "on")

        return cls(
            repo=repo,
            token=token,
            branch=os.environ.get("GIT_STATE_BRANCH", "state"),
            interval=positive_int("GIT_STATE_INTERVAL_SECONDS", 300),
            max_commits=positive_int("GIT_STATE_MAX_COMMITS", 200),
            age_recipient=os.environ.get("GIT_STATE_AGE_RECIPIENT", "").strip(),
            age_key_file=os.environ.get("SOPS_AGE_KEY_FILE", "").strip(),
            instance_id=sanitize_instance_id(
                os.environ.get("HERMES_INSTANCE_ID", "") or os.uname().nodename
            ),
            priority=max(0, min(999, positive_int("HERMES_INSTANCE_PRIORITY", 50))),
            failover=flag("HERMES_FAILOVER"),
            lease_ttl=positive_int("HERMES_LEASE_TTL_SECONDS", 600),
            heartbeat_interval=positive_int("HERMES_LEASE_HEARTBEAT_SECONDS", 120),
            poll_interval=positive_int("HERMES_LEASE_POLL_SECONDS", 30),
            role_switch_min=positive_int("HERMES_ROLE_SWITCH_MIN_SECONDS", 300),
            allow_public=flag("GIT_STATE_ALLOW_PUBLIC"),
            # "even the config env files": the dotenv is committed as-is so
            # the branch is a complete copy of the instance. encrypt/omit are
            # still available for anyone who would rather not.
            env_mode=os.environ.get(
                "GIT_STATE_ENV_MODE", DEFAULT_ENV_MODE).strip().lower(),
            watch=flag_on("GIT_STATE_WATCH"),
            # How often the tree is fingerprinted, how long it has to stay
            # quiet before that change is pushed, and the hard floor between
            # two pushes. Defaults put a change in GitHub within ~20s while
            # capping a frantic agent at ~3 commits a minute.
            watch_seconds=positive_int("GIT_STATE_WATCH_SECONDS", 5),
            debounce_seconds=non_negative_int("GIT_STATE_DEBOUNCE_SECONDS", 10),
            min_push_interval=non_negative_int("GIT_STATE_MIN_PUSH_INTERVAL_SECONDS", 20),
            retry_seconds=positive_int("GIT_STATE_RETRY_SECONDS", 30),
            seed_on_boot=flag("GIT_STATE_SEED_ON_BOOT"),
            seed_force=flag("GIT_STATE_SEED_FORCE"),
            # Push robustness. A first state push is the biggest upload this
            # service makes and GitHub answers an oversized or slow one with
            # "RPC failed; HTTP 408", so the request is sent unchunked, a
            # stalled transfer is abandoned instead of waited on, and the push
            # itself is retried a couple of times before the round is lost.
            push_attempts=positive_int("GIT_STATE_PUSH_ATTEMPTS",
                                       DEFAULT_PUSH_ATTEMPTS),
            push_retry_seconds=non_negative_int("GIT_STATE_PUSH_RETRY_SECONDS",
                                                DEFAULT_PUSH_RETRY_SECONDS),
            git_timeout_seconds=positive_int("GIT_STATE_GIT_TIMEOUT_SECONDS",
                                             DEFAULT_GIT_TIMEOUT_SECONDS),
            http_post_buffer=positive_int("GIT_STATE_HTTP_POST_BUFFER_MB",
                                          DEFAULT_HTTP_POST_BUFFER_BYTES
                                          // (1024 * 1024)) * 1024 * 1024,
            http_low_speed_limit=non_negative_int("GIT_STATE_HTTP_LOW_SPEED_LIMIT",
                                                  DEFAULT_HTTP_LOW_SPEED_LIMIT),
            http_low_speed_time=positive_int("GIT_STATE_HTTP_LOW_SPEED_TIME",
                                             DEFAULT_HTTP_LOW_SPEED_TIME),
        )

    @property
    def remote_url(self) -> str:
        repo = self.repo
        if repo.startswith("http://") or repo.startswith("https://"):
            host_path = repo.split("://", 1)[1]
        else:
            host_path = f"github.com/{repo.strip('/')}"
        if not host_path.endswith(".git"):
            host_path += ".git"
        return f"https://x-access-token:{self.token}@{host_path}"

    @property
    def api_repo(self) -> str:
        repo = self.repo
        if "://" in repo:
            repo = repo.split("://", 1)[1].split("/", 1)[1]
        return repo.strip("/").removesuffix(".git")


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------


def clean_stale_index_lock(workdir):
    """Remove an orphaned index lock, never one owned by a live Git process."""
    lock = Path(workdir) / '.git/index.lock'
    if not lock.exists():
        return
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            command = (proc / 'cmdline').read_bytes().split(b'\0')[0].decode()
            if Path(command).name.startswith('git') and (proc / 'cwd').resolve() == Path(workdir).resolve():
                return
        except (OSError, UnicodeError):
            continue
    lock.unlink(missing_ok=True)
    LOG.warning('Removed orphaned Git index lock after process exit')


def run_git(args: "list[str]", cwd: "Path | None" = None, check: bool = True,
            config: "GitConfig | None" = None) -> subprocess.CompletedProcess:
    if cwd is not None and args and args[0] in ('add', 'checkout', 'commit', 'reset'):
        clean_stale_index_lock(cwd)
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_CONFIG_NOSYSTEM", "1")
    env.setdefault("HOME", str(Path(tempfile.gettempdir())))
    post_buffer = (config.http_post_buffer if config
                   else DEFAULT_HTTP_POST_BUFFER_BYTES)
    low_speed_limit = (config.http_low_speed_limit if config
                       else DEFAULT_HTTP_LOW_SPEED_LIMIT)
    low_speed_time = (config.http_low_speed_time if config
                      else DEFAULT_HTTP_LOW_SPEED_TIME)
    base = [
        "git",
        "-c", "user.name=hermes-state",
        "-c", "user.email=hermes-state@localhost",
        "-c", "core.autocrlf=false",
        "-c", "gc.auto=0",
        "-c", "pack.threads=1",
        "-c", "pack.windowMemory=4m",
        "-c", "pack.window=0",
        "-c", "core.bigFileThreshold=1m",
        "-c", "pack.deltaCacheSize=4m",
        "-c", "core.deltaBaseCacheLimit=4m",
        "-c", "core.packedGitWindowSize=4m",
        "-c", "core.packedGitLimit=32m",
        # Transport tuning for the large pushes this backend makes. See
        # DEFAULT_HTTP_POST_BUFFER_BYTES: without it git chunks the request
        # body and GitHub's front end answers "RPC failed; HTTP 408".
        "-c", f"http.postBuffer={post_buffer}",
        "-c", "http.version=HTTP/1.1",
        "-c", f"http.lowSpeedLimit={low_speed_limit}",
        "-c", f"http.lowSpeedTime={low_speed_time}",
    ]
    timeout = (config.git_timeout_seconds if config
               else DEFAULT_GIT_TIMEOUT_SECONDS)
    try:
        proc = subprocess.run(
            base + args,
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        # A hung git is worse than a failed one: the caller is either the boot
        # wrapper or the daemon, and both have other work to do.
        raise GitStateError(
            f"git {' '.join(redact(a) for a in args[:3])} timed out "
            f"after {timeout}s"
        )
    if check and proc.returncode != 0:
        raise GitStateError(
            f"git {' '.join(redact(a) for a in args[:3])} failed: "
            f"{redact(proc.stderr.strip() or proc.stdout.strip())}"
        )
    return proc


def repo_is_private(config: GitConfig) -> "bool | None":
    """Ask the GitHub API whether the state repo is private.

    Returns None when the answer cannot be determined (network trouble, a
    non-GitHub remote). Callers treat None as "not proven private".
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        f"https://api.github.com/repos/{config.api_repo}",
        headers={
            "Authorization": f"Bearer {config.token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "hermes-git-state",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if isinstance(payload, dict) and "private" in payload:
            return bool(payload["private"])
    except (urllib.error.URLError, OSError, ValueError) as exc:
        LOG.warning("could not verify repo visibility: %s", redact(str(exc)))
    return None


def ensure_safe_to_push(config: GitConfig) -> None:
    """Refuse to push agent state to a repository that is not proven private."""
    if config._visibility_checked:
        return
    if config.allow_public:
        config._visibility_checked = True
        return
    private = repo_is_private(config)
    if private is False:
        raise GitStateError(
            f"{config.api_repo} is PUBLIC. /opt/data contains .env, chat history "
            "and memories. Make the repository private (or set "
            "GIT_STATE_ALLOW_PUBLIC=1 if you truly intend this)."
        )
    if private is None:
        # Fail closed: never assume privacy we could not verify. The flag stays
        # unset so the next sync retries -- a transient GitHub API blip delays
        # the backup by one interval instead of leaking state to a public repo.
        raise GitStateError(
            f"could not confirm {config.api_repo} is private (GitHub API "
            "unreachable or token lacks access); not pushing state this round"
        )
    config._visibility_checked = True


def origin_matches(workdir: Path, config: GitConfig) -> bool:
    """Is the clone's ``origin`` the repository we are configured for?

    The working clone lives in a tmpdir that outlives a redeploy, and
    ``GIT_STATE_REPO`` can change between boots. Every fetch and push in this
    module goes through ``origin``, so a clone left over from the previous
    repository keeps quietly reading and writing the *old* one: the operator
    moves the state repo, the logs still say "pushed state", and the backup is
    going somewhere they are no longer looking.

    Repointing the URL is not by itself enough either -- the checked-out
    history and the index still describe the old repository, so the next sync
    finds "nothing to commit" and the new repository stays empty forever. The
    clone is thrown away instead and rebuilt against the configured remote.
    """
    proc = run_git(["remote", "get-url", "origin"], cwd=workdir,
                   check=False, config=config)
    return proc.returncode == 0 and proc.stdout.strip() == config.remote_url


def ensure_clone(config: GitConfig) -> Path:
    """Return a working clone of the state branch, creating it if needed."""
    workdir = config.workdir
    if (workdir / ".git").is_dir():
        if origin_matches(workdir, config):
            current = head_commit(workdir, config)
            # Most saves are by the current writer. Compare a tiny ref reply
            # before asking GitHub to negotiate another object transfer.
            if current and remote_ref_sha(config) == current:
                return workdir
            proc = run_git(["fetch", "--depth", "1", "origin", config.branch],
                           cwd=workdir, check=False, config=config)
            if proc.returncode != 0 and not (not head_commit(workdir, config) and remote_branch_state(config) == "absent"):
                raise GitStateError("state fetch failed; refusing to classify a stale clone as empty: " + redact(proc.stderr.strip()))
            if proc.returncode == 0:
                fetched = run_git(["rev-parse", "--verify", "FETCH_HEAD"],
                                  cwd=workdir, check=False, config=config)
                current = head_commit(workdir, config)
                if fetched.returncode == 0 and current == fetched.stdout.strip():
                    # Do not make Git rewrite a tree that already matches the
                    # remote. SQLite can create transient -wal/-shm sidecars
                    # beside an older mirrored database; a no-op checkout can
                    # still refuse because one of those files looks modified.
                    # The next sync rebuilds data/ from the source snapshot.
                    LOG.debug("state branch %s is already current; skipping checkout",
                              config.branch)
                else:
                    run_git(["checkout", "-B", config.branch, "FETCH_HEAD"],
                            cwd=workdir, config=config)
            return workdir
        LOG.info(
            "state workdir holds a clone of a different repository; starting a "
            "fresh one for %s", config.api_repo,
        )
        shutil.rmtree(workdir, ignore_errors=True)

    workdir.parent.mkdir(parents=True, exist_ok=True)
    if workdir.exists():
        shutil.rmtree(workdir, ignore_errors=True)
    proc = run_git(
        ["clone", "--depth", "1", "--branch", config.branch,
         config.remote_url, str(workdir)],
        check=False, config=config,
    )
    if proc.returncode != 0:
        # Failed transfers are not evidence of an absent branch.
        if remote_branch_state(config) != "absent":
            raise GitStateError("state clone failed; refusing to initialise empty state: " + redact(proc.stderr.strip()))
        # The remote explicitly confirmed that the branch does not exist.
        stderr = redact(proc.stderr)
        if "not found" in stderr.lower() and "repository" in stderr.lower():
            raise GitStateError(
                f"repository {config.api_repo} not found, or the token cannot "
                "reach it. Create it (private) and give the token "
                "contents:read/write on it."
            )
        LOG.info("state branch %s does not exist yet; starting a new one", config.branch)
        workdir.mkdir(parents=True, exist_ok=True)
        run_git(["init", "-q", "-b", config.branch, "."], cwd=workdir, config=config)
        run_git(["remote", "add", "origin", config.remote_url], cwd=workdir, config=config)
    return workdir


def head_commit(workdir: Path, config: GitConfig) -> str:
    """The checked-out commit, or "" when the branch has no commits yet.

    ``git rev-parse HEAD`` cannot answer that question on its own: in a repo
    with no commits it prints the literal string "HEAD" and exits non-zero,
    which reads like a perfectly good (and completely unusable) refspec.
    ``--verify`` prints nothing instead, which is what callers actually want.
    """
    proc = run_git(["rev-parse", "--verify", "HEAD"], cwd=workdir,
                   check=False, config=config)
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def ensure_initial_commit(workdir: Path, config: GitConfig) -> str:
    """Create a first commit when the branch is empty. Returns the sha."""
    existing = head_commit(workdir, config)
    if existing:
        return existing
    (workdir / MANIFEST_NAME).write_text("{}\n", encoding="utf-8")
    run_git(["add", "-A"], cwd=workdir, config=config)
    run_git(["commit", "-q", "-m", "initialise state branch"], cwd=workdir,
            check=False, config=config)
    run_git(["push", "origin", config.branch], cwd=workdir, check=False, config=config)
    return head_commit(workdir, config)


def remote_has_data(workdir: Path) -> bool:
    """Does the checked-out branch carry a state tree?"""
    source_root = workdir / DATA_SUBDIR
    if not source_root.is_dir():
        return False
    return any(path.is_file() for path in source_root.rglob("*"))


def remote_branch_state(config: GitConfig) -> str:
    """Is the state branch on the remote? "present", "absent" or "unknown".

    One `git ls-remote --heads`, so a few hundred bytes: cheap enough to run
    on every boot before deciding where to restore from.
    """
    proc = run_git(
        ["ls-remote", "--exit-code", "--heads", config.remote_url, config.branch],
        check=False, config=config,
    )
    if proc.returncode == 0:
        return "present"
    if proc.returncode == 2:
        # --exit-code: "no matching refs", i.e. the branch really is absent.
        return "absent"
    LOG.debug("ls-remote failed (%s): %s", proc.returncode,
              redact(proc.stderr.strip()))
    return "unknown"


def remote_ref_sha(config: GitConfig) -> str:
    """The commit the state branch points at on the remote, or "" if none.

    One `git ls-remote`, so a few hundred bytes. Used to answer "did that push
    actually land?" after git itself reported a failure.
    """
    proc = run_git(
        ["ls-remote", config.remote_url, f"refs/heads/{config.branch}"],
        check=False, config=config,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return ""
    return proc.stdout.split()[0]


def push_branch(workdir: Path, config: GitConfig, *, force: bool = False,
                force_lease_sha: str | None = None) -> None:
    """Push the state branch, retrying, and trusting the remote over git.

    Two things make a bare `git push` unreliable for the payload this backend
    sends. First, GitHub's HTTP front end times out large uploads: the state
    push that seeds a branch is the whole restored tree, and a single dropped
    request is a normal event rather than a reason to give up on the backup.
    Second, the failure is not always a failure. A push whose response was cut
    off ("send-pack: unexpected disconnect", "Everything up-to-date" in the
    same breath) often *did* move the ref server-side; retrying blind would
    report a lost backup that is in fact safe. So after a failed attempt we ask
    the remote where the branch is, and only call it lost when the answer
    disagrees with our commit.
    """
    detail = ""
    for attempt in range(1, config.push_attempts + 1):
        args = ["push"]
        if force_lease_sha:
            args.append(
                f"--force-with-lease=refs/heads/{config.branch}:{force_lease_sha}"
            )
        elif force:
            args.append("--force")
        args += ["origin", config.branch]
        proc = run_git(args, cwd=workdir, check=False, config=config)
        if proc.returncode == 0:
            return
        detail = redact((proc.stderr or proc.stdout).strip())
        wanted = head_commit(workdir, config)
        if wanted and remote_ref_sha(config) == wanted:
            LOG.warning(
                "git push reported a failure but %s@%s already points at our "
                "commit %s; treating the push as landed (%s)",
                config.api_repo, config.branch, wanted[:12],
                detail.splitlines()[0] if detail else "no detail",
            )
            return
        if attempt < config.push_attempts:
            LOG.warning("git push failed (attempt %d/%d); retrying in %ds: %s",
                        attempt, config.push_attempts, config.push_retry_seconds,
                        detail.splitlines()[0] if detail else "no detail")
            if config.push_retry_seconds:
                time.sleep(config.push_retry_seconds)
    raise GitStateError(f"git push origin {config.branch} failed: {detail}")


def probe_state(config: GitConfig) -> str:
    """REMOTE_HAS_STATE, REMOTE_EMPTY or REMOTE_UNKNOWN.

    "Empty" and "unreachable" must never be confused: only a branch we can see
    is empty is safe to seed, because seeding force-pushes.
    """
    branch = remote_branch_state(config)
    if branch == "absent":
        return REMOTE_EMPTY
    if branch == "unknown":
        return REMOTE_UNKNOWN
    try:
        workdir = ensure_clone(config)
    except GitStateError as exc:
        LOG.warning("could not read the state branch: %s", redact(str(exc)))
        return REMOTE_UNKNOWN
    tree = run_git(["ls-tree", "-r", "--name-only", "HEAD", "--", DATA_SUBDIR], cwd=workdir, check=False, config=config)
    if tree.returncode != 0:
        return REMOTE_UNKNOWN
    if tree.stdout.strip():
        # Repair an interrupted checkout before restore reads filesystem bytes.
        run_git(["reset", "--hard", "HEAD"], cwd=workdir, config=config)
        return REMOTE_HAS_STATE
    return REMOTE_EMPTY


def state_fingerprint(data_dir: Path) -> str:
    """Cheap metadata fingerprint of the state directory.

    Metadata only (paths, sizes, mtimes), with volatile paths excluded. It is
    what lets the daemon notice a change in a few milliseconds instead of
    copying the whole tree to find out. Excluded paths are skipped so that log
    churn -- which is never backed up -- cannot trigger a push of everything
    else.
    """
    digest = hashlib.blake2b(digest_size=16)
    for root, dirs, files in os.walk(data_dir, topdown=True, followlinks=False):
        dirs[:] = sorted(
            name
            for name in dirs
            if not (Path(root) / name).is_symlink()
            and not is_excluded(
                (Path(root) / name).relative_to(data_dir).as_posix(), DEFAULT_EXCLUDES
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
            if is_excluded(relative, DEFAULT_EXCLUDES):
                continue
            digest.update(
                f"F\\0{relative}\\0{info.st_size}\\0{info.st_mtime_ns}\\0{info.st_mode & 0o7777}\\n".encode()
            )
    return digest.hexdigest()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _sqlite_fingerprints(data_dir: Path) -> dict[str, str]:
    planned, _empty, _links = plan_tree(data_dir, DEFAULT_EXCLUDES)
    databases, _sidecars = sqlite_files(data_dir, planned)
    result = {}
    for relative in sorted(databases):
        try:
            result[relative] = _hash_file(data_dir / relative)
        except OSError:
            continue
    return result


def _sync_base_path(config: GitConfig) -> Path:
    """The restore/push checkpoint shared by bootstrap and the sync daemon."""
    override = os.environ.get("GIT_STATE_BASE_FILE", "").strip()
    return Path(override) if override else Path(str(config.workdir) + ".base.json")


def _remote_identity(config: GitConfig) -> str:
    """Stable remote identity without the token embedded in HTTPS URLs."""
    raw = config.remote_url
    try:
        parsed = urlsplit(raw)
        if parsed.scheme in ("http", "https") and parsed.hostname:
            host = parsed.hostname.lower()
            if parsed.port:
                host += f":{parsed.port}"
            return f"{parsed.scheme.lower()}://{host}{parsed.path}"
    except ValueError:
        pass
    return raw


def _read_sync_base(config: GitConfig) -> dict | None:
    try:
        value = json.loads(_sync_base_path(config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (not isinstance(value, dict) or value.get("repo") != config.api_repo
            or value.get("remote") != _remote_identity(config)
            or value.get("branch") != config.branch):
        return None
    return value


def _write_sync_base(config: GitConfig, remote_sha: str, local_fingerprint: str,
                     sqlite_hashes: dict[str, str] | None = None) -> None:
    """Atomically record which remote revision this local tree came from."""
    path = _sync_base_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=".state-base-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"repo": config.api_repo, "remote": _remote_identity(config),
                       "branch": config.branch,
                       "remote_sha": remote_sha, "local_fingerprint": local_fingerprint,
                       "sqlite_hashes": sqlite_hashes or {}}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


class ChangeWatcher:
    """Decides when a change is old enough to be worth pushing.

    Agent state changes in bursts: one message can touch the session database,
    the memory index and a config file within a second. Pushing on the first
    write would commit a half-finished burst, so the watcher waits for the tree
    to stay quiet for `debounce` seconds and then pushes once -- close enough
    to "immediately" to be useful, and one commit instead of five.

    The clock and the fingerprint sampler are injectable so this is testable
    without sleeping or writing real files.
    """

    def __init__(self, data_dir: Path, config: GitConfig, *,
                 clock=time.time, sampler=None) -> None:
        self.data_dir = data_dir
        self.debounce = max(0, config.debounce_seconds)
        self._clock = clock
        self._sampler = sampler or (lambda: state_fingerprint(data_dir))
        self.baseline = self._sample()
        self.pending_since: "float | None" = None
        self.not_before = 0.0

    def _sample(self) -> "str | None":
        try:
            return self._sampler()
        except OSError as exc:  # a directory vanished mid-scan
            LOG.warning("could not fingerprint %s: %s", self.data_dir, exc)
            return None

    def poll(self) -> bool:
        """Call once per watch tick. True means "push now".

        A change is noticed on one tick and pushed on a later one, once the
        tree has held still for `debounce` seconds -- so the delay between the
        last write and the push is roughly `watch_seconds + debounce`.
        """
        now = self._clock()
        current = self._sample()
        if current is not None and current != self.baseline:
            self.baseline = current
            self.pending_since = now
        if self.pending_since is None or now < self.not_before:
            return False
        return now - self.pending_since >= self.debounce

    def snapshot(self) -> "str | None":
        """The tree as it is right now, to use as the baseline after a push.

        Taken *before* the push starts. A push can take many seconds, and
        anything written during it is not in the commit that is on its way --
        so the post-push baseline has to be the tree the push actually saw, not
        the tree that exists once the push returns.
        """
        return self._sample()

    def mark_pushed(self, baseline: "str | None" = None) -> None:
        """Re-baseline after a push so an unchanged tree stays quiet.

        ``baseline`` should be the snapshot taken before the push; re-sampling
        here instead would adopt writes made during the push and never back
        them up until some later, unrelated change happened to wake the
        watcher.
        """
        self.baseline = baseline if baseline is not None else self._sample()
        self.pending_since = None
        self.not_before = 0.0

    def defer(self, seconds: float) -> None:
        """A push failed: leave the change pending and back off before retrying."""
        self.not_before = self._clock() + max(0.0, seconds)


# ---------------------------------------------------------------------------
# Mirroring
# ---------------------------------------------------------------------------


def _readlink_or_blank(link: Path) -> str:
    """The link target for a log message, or "" if it cannot be read."""
    try:
        return os.readlink(link)
    except OSError:
        return ""


def safe_symlink_target(link: Path, data_dir: Path) -> "str | None":
    """The symlink target worth carrying in the backup, or None to drop it.

    A symlink is data -- dropping one silently changes what a restored
    instance points at -- but it is also the one kind of entry that can name a
    path outside the tree. So only a *relative* target that resolves inside the
    data directory is carried; an absolute target or one that climbs out with
    ``..`` is dropped, because restoring it would be a way to write outside the
    restored tree.
    """
    try:
        raw = os.readlink(link)
    except OSError:
        return None
    candidate = PurePosixPath(raw)
    if candidate.is_absolute():
        return None
    try:
        # Non-strict: a dangling link is still a link worth restoring.
        resolved = (link.parent / candidate).resolve()
        resolved.relative_to(data_dir.resolve())
    except (OSError, ValueError):
        return None
    return raw


def _mark_populated(populated: "set[str]", relative: str) -> None:
    """Record every directory that holds something, walking up to the root."""
    parent = PurePosixPath(relative).parent
    while str(parent) not in ("", "."):
        populated.add(str(parent))
        parent = parent.parent


def plan_tree(data_dir: Path,
              excludes) -> "tuple[list[str], list[str], dict[str, str]]":
    """What under data_dir belongs in the backup.

    Returns ``(files, empty_dirs, symlinks)``.

    A file walk on its own loses two things silently: git has no way to record
    an empty directory, and a symlink is not a regular file. Both are real
    state -- an agent whose ``memories/`` directory vanished on restore, or a
    config that was a link and came back as nothing -- so they are collected
    here and carried in the manifest instead.

    Pure enough to test: it only reads the filesystem and returns names.
    """
    files: list[str] = []
    kept_dirs: list[str] = []
    symlinks: dict[str, str] = {}
    for root, dirs, filenames in os.walk(data_dir, topdown=True, followlinks=False):
        root_path = Path(root)
        keep: list[str] = []
        for name in sorted(dirs):
            path = root_path / name
            relative = path.relative_to(data_dir).as_posix()
            if is_excluded(relative, excludes):
                continue
            if path.is_symlink():
                target = safe_symlink_target(path, data_dir)
                if target is None:
                    LOG.warning(
                        "symlink %s is not backed up (target %r is absolute or "
                        "escapes the data directory)", relative,
                        _readlink_or_blank(path),
                    )
                else:
                    symlinks[relative] = target
                continue
            keep.append(name)
        dirs[:] = keep
        kept_dirs.extend(
            (root_path / name).relative_to(data_dir).as_posix() for name in keep
        )
        for name in sorted(filenames):
            path = root_path / name
            relative = path.relative_to(data_dir).as_posix()
            if is_excluded(relative, excludes):
                continue
            if path.is_symlink():
                target = safe_symlink_target(path, data_dir)
                if target is None:
                    LOG.warning(
                        "symlink %s is not backed up (target %r is absolute or "
                        "escapes the data directory)", relative,
                        _readlink_or_blank(path),
                    )
                    continue
                symlinks[relative] = target
                continue
            if not path.is_file():
                continue
            files.append(relative)

    populated: set[str] = set()
    for relative in files:
        _mark_populated(populated, relative)
    for relative in symlinks:
        _mark_populated(populated, relative)
    for relative in kept_dirs:
        # A directory holding only other directories is not empty, but the
        # innermost one may be, so only the parents are marked here.
        _mark_populated(populated, relative)
    empty_dirs = sorted(d for d in kept_dirs if d not in populated)
    return sorted(files), empty_dirs, symlinks


_SQLITE_HEADER = b"SQLite format 3\x00"
_STREAM_SEALED_PREFIX = b"HERMES-AESGCM-STREAM-v1\n"
_COMPRESSED_STREAM_PREFIX = b"HERMES-AESGCM-ZLIB-v2\n"
_STREAM_CHUNK_BYTES = 1024 * 1024


def sqlite_files(root: Path, relative_paths) -> tuple[set[str], set[str]]:
    """Return SQLite database paths and their live WAL/SHM sidecars."""
    paths = set(relative_paths)
    databases: set[str] = set()
    sidecars: set[str] = set()
    for relative in paths:
        if not relative.lower().endswith((".db", ".sqlite", ".sqlite3")):
            continue
        try:
            with (root / relative).open("rb") as handle:
                if handle.read(len(_SQLITE_HEADER)) == _SQLITE_HEADER:
                    databases.add(relative)
        except OSError:
            continue
    for relative in databases:
        for suffix in ("-wal", "-shm"):
            candidate = relative + suffix
            if candidate in paths:
                sidecars.add(candidate)
    return databases, sidecars


def snapshot_sqlite(source: Path, destination: Path) -> None:
    """Create a transactionally consistent, bounded-memory SQLite snapshot."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".snapshot"
    )
    os.close(fd)
    temporary = Path(temporary_name)
    source_db = target_db = None
    try:
        source_uri = source.resolve().as_uri() + "?mode=ro"
        source_db = sqlite3.connect(source_uri, uri=True, timeout=10)
        target_db = sqlite3.connect(str(temporary), timeout=10)
        source_db.backup(target_db, pages=128, sleep=0.05)
        target_db.close()
        target_db = None
        source_db.close()
        source_db = None
        os.replace(temporary, destination)
    except Exception as exc:
        if target_db is not None:
            target_db.close()
        if source_db is not None:
            source_db.close()
        temporary.unlink(missing_ok=True)
        raise GitStateError(
            f"could not create a consistent SQLite snapshot of {source.name}: "
            f"{type(exc).__name__}"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _stream_cipher(salt: bytes):
    key = os.environ.get("STORAGE_ENCRYPTION_KEY", "")
    if not key:
        raise RuntimeError("STORAGE_ENCRYPTION_KEY is required for streamed state encryption")
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    derived = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=salt,
        info=b"hermes-render-state-stream-v1",
    ).derive(key.encode("utf-8"))
    return AESGCM(derived)


def encrypt_stream_file(source: Path, destination: Path, config: GitConfig, *, compress: bool = True) -> None:
    """Encrypt a large file in 1 MiB authenticated chunks with bounded RAM."""
    key = os.environ.get("STORAGE_ENCRYPTION_KEY", "")
    if key:
        salt, nonce_prefix = secrets.token_bytes(16), secrets.token_bytes(8)
        cipher = _stream_cipher(salt)
        import zlib
        prefix = _COMPRESSED_STREAM_PREFIX if compress else _STREAM_SEALED_PREFIX
        header = prefix + salt + nonce_prefix
        destination.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as inp, destination.open("wb") as out:
            out.write(header)
            index = 0
            while True:
                block = inp.read(_STREAM_CHUNK_BYTES)
                if not block:
                    break
                if compress:
                    block = zlib.compress(block, level=6)
                nonce = nonce_prefix + index.to_bytes(4, "big")
                aad = header + index.to_bytes(8, "big") + b"D"
                sealed = cipher.encrypt(nonce, block, aad)
                out.write(struct.pack(">I", len(block)))
                out.write(sealed)
                index += 1
            # Authenticated end marker detects truncation and reordering.
            nonce = nonce_prefix + index.to_bytes(4, "big")
            aad = header + index.to_bytes(8, "big") + b"F"
            out.write(struct.pack(">I", 0))
            out.write(cipher.encrypt(nonce, b"", aad))
        return

    if not config.age_recipient:
        raise GitStateError(
            "GIT_STATE_ENV_MODE=encrypt needs STORAGE_ENCRYPTION_KEY or "
            "GIT_STATE_AGE_RECIPIENT to back up SQLite session history"
        )
    binary = shutil.which("age") or shutil.which("rage")
    if not binary:
        raise GitStateError("age encryption is configured but age/rage is unavailable")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as inp, destination.open("wb") as out:
        proc = subprocess.run(
            [binary, "-a", "-r", config.age_recipient], stdin=inp,
            stdout=out, capture_output=True,
        )
    if proc.returncode != 0:
        destination.unlink(missing_ok=True)
        raise GitStateError("age failed to encrypt a SQLite session snapshot")


def decrypt_stream_file(source: Path, destination: Path, config: GitConfig) -> None:
    """Decrypt a chunked snapshot, rejecting altered or truncated files."""
    with source.open("rb") as inp:
        prefix = inp.readline(64)
        if prefix not in (_STREAM_SEALED_PREFIX, _COMPRESSED_STREAM_PREFIX):
            raise ValueError("not a streamed Hermes ciphertext")
        salt = inp.read(16)
        nonce_prefix = inp.read(8)
        if len(salt) != 16 or len(nonce_prefix) != 8:
            raise ValueError("truncated Hermes ciphertext header")
        header = prefix + salt + nonce_prefix
        cipher = _stream_cipher(salt)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".restore-tmp")
        index = 0
        try:
            with temporary.open("wb") as out:
                while True:
                    raw_length = inp.read(4)
                    if len(raw_length) != 4:
                        raise ValueError("truncated Hermes ciphertext frame")
                    (length,) = struct.unpack(">I", raw_length)
                    if length > _STREAM_CHUNK_BYTES + 1024:
                        raise ValueError("oversized Hermes ciphertext frame")
                    encrypted = inp.read(length + 16 if length else 16)
                    if len(encrypted) != (length + 16 if length else 16):
                        raise ValueError("truncated Hermes ciphertext data")
                    nonce = nonce_prefix + index.to_bytes(4, "big")
                    if length:
                        aad = header + index.to_bytes(8, "big") + b"D"
                        block = cipher.decrypt(nonce, encrypted, aad)
                        if prefix == _COMPRESSED_STREAM_PREFIX:
                            import zlib
                            decoder = zlib.decompressobj()
                            block = decoder.decompress(block, _STREAM_CHUNK_BYTES + 1)
                            if (len(block) > _STREAM_CHUNK_BYTES or not decoder.eof
                                    or decoder.unused_data or decoder.unconsumed_tail):
                                raise ValueError("invalid compressed Hermes frame")
                        out.write(block)
                        index += 1
                        continue
                    aad = header + index.to_bytes(8, "big") + b"F"
                    cipher.decrypt(nonce, encrypted, aad)
                    if inp.read(1):
                        raise ValueError("trailing bytes after Hermes ciphertext")
                    break
            os.replace(temporary, destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise


def plan_mirror(data_dir: Path, excludes) -> "list[str]":
    """Relative file paths under data_dir that belong in the backup, sorted."""
    return plan_tree(data_dir, excludes)[0]


def age_encrypt(plaintext: bytes, config: GitConfig) -> "bytes | None":
    """Encrypt with `age` if a recipient is configured, else None."""
    key = os.environ.get("STORAGE_ENCRYPTION_KEY")
    if key:
        import base64
        import hashlib
        from cryptography.fernet import Fernet
        cipher_key = base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest())
        cache_key = (cipher_key, hashlib.sha256(plaintext).digest())
        if cache_key not in _SEALED_CACHE:
            if len(_SEALED_CACHE) >= 8:
                _SEALED_CACHE.clear()
            _SEALED_CACHE[cache_key] = _SEALED_PREFIX + Fernet(cipher_key).encrypt(plaintext)
        return _SEALED_CACHE[cache_key]
    if not config.age_recipient:
        return None
    binary = shutil.which("age") or shutil.which("rage")
    if not binary:
        LOG.warning("age binary not found; cannot encrypt sensitive files")
        return None
    proc = subprocess.run(
        [binary, "-a", "-r", config.age_recipient],
        input=plaintext, capture_output=True,
    )
    if proc.returncode != 0:
        LOG.warning(
            "age encryption failed: %s",
            redact(proc.stderr.decode("utf-8", "replace")),
        )
        return None
    return proc.stdout


def age_decrypt(ciphertext: bytes, config: GitConfig) -> "bytes | None":
    if ciphertext.startswith(_SEALED_PREFIX):
        import base64
        import hashlib
        from cryptography.fernet import Fernet, InvalidToken
        key = os.environ.get("STORAGE_ENCRYPTION_KEY")
        if not key:
            raise RuntimeError("STORAGE_ENCRYPTION_KEY is required to restore encrypted settings")
        try:
            cipher_key = base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest())
            return Fernet(cipher_key).decrypt(ciphertext[len(_SEALED_PREFIX):])
        except InvalidToken as exc:
            raise RuntimeError("STORAGE_ENCRYPTION_KEY does not match this backup") from exc
    binary = shutil.which("age") or shutil.which("rage")
    if not binary:
        return None
    args = [binary, "-d"]
    env = dict(os.environ)
    identity_path = None
    if config.age_key_file:
        args += ["-i", config.age_key_file]
    elif os.environ.get("SOPS_AGE_KEY"):
        # age reads identities from a file; materialise the key privately.
        handle = tempfile.NamedTemporaryFile("w", delete=False)
        identity_path = Path(handle.name)
        os.chmod(handle.name, 0o600)
        handle.write(os.environ["SOPS_AGE_KEY"] + "\n")
        handle.close()
        args += ["-i", handle.name]
    try:
        proc = subprocess.run(args, input=ciphertext, capture_output=True, env=env)
    finally:
        if identity_path is not None:
            identity_path.unlink(missing_ok=True)
    if proc.returncode != 0:
        LOG.warning("age decryption failed: %s", redact(proc.stderr.decode("utf-8", "replace")))
        return None
    return proc.stdout


def decrypt_age_file(source: Path, destination: Path, config: GitConfig) -> None:
    """Decrypt an age file to disk without buffering a session DB in RAM."""
    binary = shutil.which("age") or shutil.which("rage")
    if not binary:
        raise GitStateError("age-encrypted SQLite history cannot be restored: age/rage missing")
    args = [binary, "-d"]
    identity_path = None
    temporary = destination.with_name(destination.name + ".restore-tmp")
    if config.age_key_file:
        args += ["-i", config.age_key_file]
    elif os.environ.get("SOPS_AGE_KEY"):
        handle = tempfile.NamedTemporaryFile("w", delete=False)
        identity_path = Path(handle.name)
        os.chmod(handle.name, 0o600)
        handle.write(os.environ["SOPS_AGE_KEY"] + "\n")
        handle.close()
        args += ["-i", str(identity_path)]
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as inp, temporary.open("wb") as out:
            proc = subprocess.run(args, stdin=inp, stdout=out, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            detail = redact(proc.stderr.decode("utf-8", "replace"))[:300]
            raise GitStateError(f"age could not decrypt SQLite history: {detail}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
        if identity_path is not None:
            identity_path.unlink(missing_ok=True)


def _prepare_sensitive(payload: bytes, config: GitConfig) -> "tuple[bytes, str]":
    """Bytes to commit for a sensitive file, and how they were produced.

    The disposition is one of "plaintext", "encrypted" or "omitted". Nothing
    here can return a sensitive file in the clear while the mode forbids it:
    encrypt with no working age setup yields "omitted", never a fallback to
    plaintext.
    """
    if config.env_mode == ENV_MODE_OMIT:
        return b"", ENV_MODE_OMIT
    if config.env_mode == ENV_MODE_PLAINTEXT:
        return payload, ENV_MODE_PLAINTEXT
    sealed = age_encrypt(payload, config)
    if sealed is None:
        return b"", ENV_MODE_OMIT
    return sealed, ENV_MODE_ENCRYPT


def _warn_plaintext_env(names: "list[str]", config: GitConfig) -> None:
    """Say out loud, once per process, that live keys are going into git."""
    if not names or config._env_mode_warned:
        return
    config._env_mode_warned = True
    LOG.warning(
        "committing %s in the clear (GIT_STATE_ENV_MODE=plaintext): every API "
        "key in it becomes part of the history of the private repo %s. Anyone "
        "who can read that repo, now or through a future fork or export, gets "
        "those keys -- rotate them if it is ever shared. Set "
        "GIT_STATE_ENV_MODE=encrypt with GIT_STATE_AGE_RECIPIENT to seal it, "
        "or GIT_STATE_ENV_MODE=omit to leave it out.",
        ", ".join(sorted(names)), config.api_repo,
    )


def build_worktree(data_dir: Path, workdir: Path, config: GitConfig) -> "dict":
    """Rebuild <workdir>/data from data_dir. Returns a manifest dict.

    The tree is rebuilt rather than patched so that files deleted locally
    disappear from the next commit: `git add -A` sees them gone.
    """
    excludes = DEFAULT_EXCLUDES
    # Read the previous generation before the manifest is overwritten. The
    # clone is shallow, so "git rev-list --count" cannot tell us how long the
    # remote history is; we carry the count in the manifest instead.
    generation = read_generation(workdir) + 1
    target = workdir / DATA_SUBDIR
    cache_root = workdir / ".git" if (workdir / ".git").is_dir() else workdir
    previous_target = cache_root / "hermes-state-data-previous"
    if previous_target.exists():
        shutil.rmtree(previous_target, ignore_errors=True)
    if target.exists():
        os.replace(target, previous_target)
    target.mkdir(parents=True, exist_ok=True)

    copied, encrypted, plaintext_env, skipped = 0, [], [], []
    mirrored_sqlite_dbs: set[str] = set()
    sqlite_hashes: dict[str, str] = {}
    previous_base = _read_sync_base(config) or {}
    previous_sqlite_hashes = previous_base.get("sqlite_hashes", {})
    if not isinstance(previous_sqlite_hashes, dict):
        previous_sqlite_hashes = {}
    modes: dict[str, str] = {}
    planned, empty_dirs, symlinks = plan_tree(data_dir, excludes)
    sqlite_dbs, sqlite_sidecars = sqlite_files(data_dir, planned)
    for relative in planned:
        if relative in sqlite_sidecars:
            # A standalone WAL/SHM file cannot be mixed with a different
            # commit of state.db. The database snapshot below includes every
            # committed WAL page through SQLite's online backup API.
            continue
        source = data_dir / relative
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if relative in SENSITIVE_FILES:
            try:
                payload = source.read_bytes()
            except OSError:
                continue
            payload, disposition = _prepare_sensitive(payload, config)
            if disposition == ENV_MODE_OMIT:
                # Either the operator asked for omit, or encrypt was asked for
                # and age could not seal it. Neither falls back to plaintext.
                skipped.append(relative)
                LOG.warning(
                    "%s left OUT of the git backup (GIT_STATE_ENV_MODE=%s%s). "
                    "Keep those values in Render's Environment tab or the "
                    "repo's encrypted secrets file.", relative, config.env_mode,
                    "; age encryption unavailable" if config.env_mode == ENV_MODE_ENCRYPT
                    else "",
                )
                continue
            if disposition == ENV_MODE_ENCRYPT:
                destination = destination.with_name(destination.name + ENCRYPTED_SUFFIX)
                encrypted.append(relative)
            else:
                plaintext_env.append(relative)
            destination.write_bytes(payload)
            copied += 1
            try:
                # A restored .env holds live keys; keep it owner-only on disk
                # even though the copy in the branch is world-readable.
                os.chmod(destination, 0o600)
            except OSError:
                pass
            continue

        if relative in sqlite_dbs:
            if config.env_mode == ENV_MODE_OMIT:
                skipped.append(relative)
                continue
            snapshot_path = destination.with_name(destination.name + ".snapshot-tmp")
            snapshot_sqlite(source, snapshot_path)
            sqlite_digest = _hash_file(snapshot_path)
            sqlite_hashes[relative] = sqlite_digest
            if config.env_mode == ENV_MODE_ENCRYPT:
                sealed_path = destination.with_name(destination.name + ENCRYPTED_SUFFIX)
                try:
                    prior_cipher = previous_target / (relative + ENCRYPTED_SUFFIX)
                    if (previous_sqlite_hashes.get(relative) == sqlite_digest
                            and prior_cipher.is_file()):
                        shutil.copyfile(prior_cipher, sealed_path)
                    else:
                        encrypt_stream_file(snapshot_path, sealed_path, config)
                finally:
                    snapshot_path.unlink(missing_ok=True)
                encrypted.append(relative)
                destination = sealed_path
            else:
                os.replace(snapshot_path, destination)
            try:
                mode = source.stat().st_mode & 0o777
                os.chmod(destination, mode)
            except OSError:
                mode = 0o644
            if mode not in (0o644, 0o755):
                modes[relative] = format(mode, "o")
            copied += 1
            mirrored_sqlite_dbs.add(relative)
            continue

        # Stream ordinary files: workspace artifacts can exceed the entire
        # Render memory budget. Encryption is needed only for small settings.
        try:
            shutil.copyfile(source, destination)
        except OSError:
            continue
        try:
            mode = source.stat().st_mode & 0o777
            os.chmod(destination, mode)
        except OSError:
            mode = 0o644
        if mode not in (0o644, 0o755):
            # git records only the executable bit, so every other mode has to
            # travel in the manifest -- otherwise a 0600 key comes back from a
            # restore as a world-readable 0644.
            modes[relative] = format(mode, "o")
        copied += 1

    _warn_plaintext_env(plaintext_env, config)
    config._sqlite_hashes = sqlite_hashes
    if previous_target.exists():
        shutil.rmtree(previous_target, ignore_errors=True)

    manifest = {
        "instance": config.instance_id,
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "files": copied,
        "generation": generation,
        "encrypted": sorted(encrypted),
        "omitted": sorted(skipped),
        "plaintext_env": sorted(set(plaintext_env)),
        # git cannot hold an empty directory and this walk does not copy
        # symlinks as blobs, so both ride in the manifest instead of being
        # dropped from the backup.
        "empty_dirs": empty_dirs,
        "symlinks": {name: symlinks[name] for name in sorted(symlinks)},
        "sqlite_snapshots": sorted(mirrored_sqlite_dbs),
        # Only the modes git cannot express; 0644/0755 come back from the
        # object mode on their own.
        "modes": {name: modes[name] for name in sorted(modes)},
    }
    (workdir / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def materialize(workdir: Path, data_dir: Path, config: GitConfig) -> int:
    """Copy the repo's data/ tree into data_dir, decrypting what is sealed.

    Existing local files are overwritten, but local files absent from the repo
    are left alone: a restore should not delete state after a partial fetch.
    """
    source_root = workdir / DATA_SUBDIR
    if not source_root.is_dir():
        LOG.info("no data/ in the state repo yet; nothing to restore")
        return 0

    manifest = read_manifest(workdir)
    recorded_modes = manifest.get("modes")
    if not isinstance(recorded_modes, dict):
        recorded_modes = {}

    source_files = [source for source in source_root.rglob("*") if source.is_file()]
    source_relatives = [source.relative_to(source_root).as_posix() for source in source_files]
    sqlite_dbs, sqlite_sidecars = sqlite_files(source_root, source_relatives)
    recorded_sqlite = manifest.get("sqlite_snapshots", [])
    if isinstance(recorded_sqlite, list):
        sqlite_dbs.update(value for value in recorded_sqlite if isinstance(value, str))
    for relative in sqlite_dbs:
        for suffix in ("-wal", "-shm"):
            if relative + suffix in source_relatives:
                sqlite_sidecars.add(relative + suffix)

    restored = 0
    for source in sorted(source_files):
        relative = source.relative_to(source_root).as_posix()
        if relative in sqlite_sidecars:
            continue
        logical_relative = relative[:-len(ENCRYPTED_SUFFIX)] if relative.endswith(ENCRYPTED_SUFFIX) else relative
        is_sqlite = logical_relative in sqlite_dbs
        destination = data_dir / logical_relative
        if is_sqlite:
            # Restore only the database snapshot. A legacy branch may still
            # contain sidecars from the old raw-file mirror; never replay them
            # against this restored image.
            Path(str(destination) + "-wal").unlink(missing_ok=True)
            Path(str(destination) + "-shm").unlink(missing_ok=True)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source.open("rb") as handle:
                prefix = handle.readline(64)
            if prefix in (_STREAM_SEALED_PREFIX, _COMPRESSED_STREAM_PREFIX):
                decrypt_stream_file(source, destination, config)
            elif relative.endswith(ENCRYPTED_SUFFIX):
                decrypt_age_file(source, destination, config)
            else:
                # Older mirrors stored the main file and live WAL/SHM as
                # separate blobs. Fold any same-generation WAL frames into a
                # new DB snapshot instead of discarding the latest commits.
                snapshot_sqlite(source, destination)
            mode = manifest_mode(recorded_modes.get(logical_relative))
            try:
                os.chmod(destination, mode or (source.stat().st_mode & 0o777))
            except OSError:
                pass
            restored += 1
            continue
        payload = None
        if relative.endswith(ENCRYPTED_SUFFIX):
            payload = source.read_bytes()
            relative = relative[: -len(ENCRYPTED_SUFFIX)]
            opened = age_decrypt(payload, config)
            if opened is None:
                LOG.warning(
                    "could not decrypt %s; skipping (is SOPS_AGE_KEY set?)", relative
                )
                continue
            payload = opened
        destination = data_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if relative in sqlite_dbs:
            # Restore happens before Hermes starts. Remove any sidecars from a
            # prior legacy backup so SQLite cannot replay them against this
            # newly restored snapshot.
            Path(str(destination) + "-wal").unlink(missing_ok=True)
            Path(str(destination) + "-shm").unlink(missing_ok=True)
        if payload is None:
            shutil.copyfile(source, destination)
        else:
            destination.write_bytes(payload)
        # git records the executable bit in the object mode, so a script that
        # was 0755 in /opt/data has to come back runnable -- a restored
        # entrypoint or hook that lost its +x simply does not run. Modes git
        # cannot express (a 0600 key) come from the manifest instead. A
        # restored .env holds live keys, so it is owner-only whatever either
        # of them says.
        if relative in SENSITIVE_FILES:
            mode = 0o600
        else:
            mode = manifest_mode(recorded_modes.get(relative))
            if mode is None:
                try:
                    mode = source.stat().st_mode & 0o777
                except OSError:
                    mode = 0o644
        try:
            os.chmod(destination, mode or 0o644)
        except OSError:
            pass
        restored += 1

    # Directories git could not hold, and the symlinks the walk did not copy
    # as blobs. Both come from the manifest the mirror wrote.
    for relative in manifest.get("empty_dirs", []) or []:
        if not isinstance(relative, str) or not relative or relative.startswith("/"):
            continue
        try:
            (data_dir / relative).mkdir(parents=True, exist_ok=True)
            restored += 1
        except OSError as exc:
            LOG.warning("could not restore directory %s: %s", relative, exc)
    for relative, target in (manifest.get("symlinks") or {}).items():
        if restore_symlink(data_dir, relative, target):
            restored += 1
    return restored


def restore_symlink(data_dir: Path, relative: str, target: str) -> bool:
    """Recreate one backed-up symlink inside data_dir.

    The manifest is read back out of a repository, so the target is re-checked
    here rather than trusted: only a relative target that stays inside the data
    directory is created, which is what keeps a tampered manifest from writing
    somewhere outside the restored tree.
    """
    if not isinstance(relative, str) or not relative or relative.startswith("/"):
        return False
    if not isinstance(target, str) or not target or PurePosixPath(target).is_absolute():
        LOG.warning("not restoring symlink %s: target %r is not relative",
                    relative, target)
        return False
    destination = data_dir / relative
    try:
        resolved = (destination.parent / target).resolve()
        resolved.relative_to(data_dir.resolve())
    except (OSError, ValueError):
        LOG.warning("not restoring symlink %s: target %r escapes the data directory",
                    relative, target)
        return False
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink() or destination.exists():
            destination.unlink()
        os.symlink(target, destination)
        return True
    except OSError as exc:
        LOG.warning("could not restore symlink %s -> %s: %s", relative, target, exc)
        return False


def read_manifest(workdir: Path) -> dict:
    """The manifest the last mirror wrote, or {} when it is missing/unreadable."""
    try:
        raw = json.loads((workdir / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, AttributeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def manifest_mode(raw) -> "int | None":
    """A permission recorded in the manifest as an octal string, or None.

    The manifest is read back out of a repository, so the value is parsed
    defensively: anything that is not an octal mode is ignored and the mode git
    recorded for the file is used instead.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str):
        try:
            value = int(raw.strip(), 8)
        except ValueError:
            return None
    else:
        return None
    value &= 0o7777
    return value or None


def read_generation(workdir: Path) -> int:
    """How many state commits the branch has accumulated since the last squash.

    Stored in the manifest because the working clone is shallow and therefore
    cannot count the remote's commits itself.
    """
    try:
        value = int(read_manifest(workdir).get("generation", 0))
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def should_compact(commits: int, max_commits: int) -> bool:
    """History is bounded so the repo cannot grow forever."""
    return max_commits > 0 and commits >= max_commits


def compact_history(workdir: Path, config: GitConfig, *, expected_remote: str | None = None) -> None:
    """Squash history, replacing it only if the remote has not advanced."""
    observed_remote = remote_ref_sha(config)
    expected_remote = expected_remote or observed_remote
    if observed_remote != expected_remote:
        raise GitStateError("backup writer changed during sync; refusing history compaction")
    if not expected_remote:
        raise GitStateError("cannot compact state history without a remote branch checkpoint")
    LOG.info("squashing state history to one commit")
    # The squashed commit becomes generation 1 again, so the counter that
    # triggered this compaction restarts rather than firing on every sync.
    try:
        manifest_path = workdir / MANIFEST_NAME
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        raw["generation"] = 1
        manifest_path.write_text(
            json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except (OSError, ValueError):
        pass
    run_git(["checkout", "--orphan", "__compact"], cwd=workdir, config=config)
    run_git(["add", "-A"], cwd=workdir, config=config)
    run_git(["commit", "-q", "-m", "Hermes state (squashed history)"],
            cwd=workdir, check=False, config=config)
    run_git(["branch", "-M", config.branch], cwd=workdir, config=config)
    push_branch(workdir, config, force_lease_sha=expected_remote)


def capture_runtime_secrets(data_dir: Path, config: GitConfig) -> None:
    """Include live Render credentials in the encrypted dotenv backup."""
    if config.env_mode != ENV_MODE_ENCRYPT:
        return
    excluded = {'STORAGE_ENCRYPTION_KEY', 'SOPS_AGE_KEY', 'SOPS_AGE_KEY_FILE',
                'GIT_STATE_AGE_RECIPIENT'}
    # Capture settings as well as secrets. Host/image paths and Render-generated
    # identities must be taken from the new container, never from an old host.
    host_keys = {"PATH", "HOME", "HOSTNAME", "PWD", "OLDPWD", "SHLVL", "_",
                 "USER", "LOGNAME", "SHELL", "TERM", "LANG", "TZ", "PORT",
                 "VIRTUAL_ENV", "UV_LINK_MODE", "UV_COMPILE_BYTECODE",
                 "HERMES_HOME", "HERMES_TUI_DIR", "GIT_STATE_SEED_ON_BOOT",
                 "TELEGRAM_WEBHOOK_URL", "TELEGRAM_WEBHOOK_PORT", "TELEGRAM_WEBHOOK_SECRET"}
    runtime = {key: value for key, value in os.environ.items()
               if key not in excluded and key not in host_keys
               and not key.startswith(("PYTHON", "LC_", "CODEX_"))
               and (not key.startswith("RENDER_") or key.startswith("RENDER_MCP_"))}
    data_dir.mkdir(parents=True, exist_ok=True)
    runtime_path = data_dir / ".render-runtime-env.json"
    runtime_content = json.dumps({"version": 1, "variables": runtime}, sort_keys=True) + "\n"
    if not runtime_path.exists() or runtime_path.read_text() != runtime_content:
        fd, temporary = tempfile.mkstemp(dir=data_dir, prefix='.runtime-env-', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w') as handle:
                handle.write(runtime_content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, runtime_path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    values = {key: value for key, value in os.environ.items()
              if value and key not in excluded
              and key.endswith(('_KEY', '_TOKEN', '_SECRET', '_PASSWORD', '_CREDENTIALS'))}
    if not values:
        return
    path = data_dir / '.env'
    original = path.read_text() if path.exists() else ''
    remaining = dict(values)
    lines = []
    for line in original.splitlines():
        raw = line.strip()
        raw = raw[7:].lstrip() if raw.startswith('export ') else raw
        name = raw.split('=', 1)[0].strip() if '=' in raw and not raw.startswith('#') else ''
        if name in values:
            if name in remaining:
                lines.append(name + '=' + json.dumps(remaining.pop(name)))
        else:
            lines.append(line)
    lines.extend(name + '=' + json.dumps(value) for name, value in sorted(remaining.items()))
    content = '\n'.join(lines) + '\n'
    if content == original:
        return
    data_dir.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=data_dir, prefix='.runtime-secrets-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def reject_destructive_shrink(previous_files: int, current_files: int) -> None:
    """An empty/partial source must never replace an established backup."""
    if previous_files >= 100 and current_files < previous_files * 0.5:
        raise GitStateError(
            f"backup would shrink from {previous_files} to {current_files} files; "
            "refusing to replace established state with a partial tree"
        )


def sync_once(data_dir: Path, config: GitConfig, *, force: bool = False,
              seed: bool = False) -> bool:
    if config.failover and config.role == ROLE_STANDBY:
        LOG.debug("standby instance; not pushing state")
        return True

    ensure_safe_to_push(config)
    workdir = ensure_clone(config)

    # This instance booted without restoring from GitHub -- the branch was
    # empty or unreachable. If GitHub has since turned out to hold state we
    # never restored from, pushing would replace it with an unproven copy.
    # Refuse and say what to do about it.
    if config.seed_on_boot and not config.seed_force and remote_has_data(workdir):
        raise GitStateError(
            f"{config.api_repo}@{config.branch} already holds state, but this "
            "instance booted without restoring from it, so pushing would "
            "overwrite it with an unproven copy. To adopt the GitHub copy "
            "instead, restart once it is the branch you want restored; to "
            "push this copy anyway, set GIT_STATE_SEED_FORCE=1."
        )

    base = _read_sync_base(config)
    remote_sha = head_commit(workdir, config)
    if base is None:
        if remote_has_data(workdir) and not config.seed_force:
            raise GitStateError(
                f"{config.api_repo}@{config.branch} already has state but this "
                "process has no successful restore checkpoint; refusing to "
                "replace it. Restart through the normal restore path first."
            )
    elif remote_sha != base.get("remote_sha", ""):
        raise GitStateError(
            f"{config.api_repo}@{config.branch} advanced since this instance "
            "restored or last backed up state; refusing to overwrite it. Stop "
            "other writers and restart this service to restore the newer copy."
        )

    saved_manifest = run_git(["show", f"HEAD:{MANIFEST_NAME}"], cwd=workdir, check=False, config=config)
    previous_files = 0
    if saved_manifest.returncode == 0:
        previous_files = int(json.loads(saved_manifest.stdout).get("files", 0) or 0)
    capture_runtime_secrets(data_dir, config)
    manifest = build_worktree(data_dir, workdir, config)
    reject_destructive_shrink(previous_files, int(manifest["files"]))

    run_git(["add", "-A"], cwd=workdir, config=config)
    # Only real state changes are worth a commit. The manifest carries a
    # timestamp and generation counter that differ on every run, so asking
    # about the whole tree would push a pointless commit every interval --
    # exactly the bandwidth waste this backend exists to avoid.
    status = run_git(["status", "--porcelain", "--", DATA_SUBDIR],
                     cwd=workdir, config=config)
    if not status.stdout.strip() and not force:
        LOG.debug("state unchanged; nothing to commit")
        run_git(["checkout", "HEAD", "--", MANIFEST_NAME], cwd=workdir,
                check=False, config=config)
        _write_sync_base(config, remote_sha, state_fingerprint(data_dir),
                         getattr(config, "_sqlite_hashes", {}))
        return True

    message = (
        f"Hermes state from {config.instance_id} "
        f"({manifest['files']} files, {manifest['updated']})"
    )
    proc = run_git(["commit", "-q", "-m", message], cwd=workdir, check=False, config=config)
    if proc.returncode != 0 and "nothing to commit" not in (proc.stdout + proc.stderr):
        raise GitStateError(redact(proc.stderr or proc.stdout))

    if should_compact(manifest.get("generation", 0), config.max_commits):
        compact_history(workdir, config, expected_remote=remote_sha)
        _write_sync_base(config, head_commit(workdir, config), state_fingerprint(data_dir),
                         getattr(config, "_sqlite_hashes", {}))
        return True

    # The remote may have advanced from another writer. Never replace that
    # state as a retry: a normal push is safe to retry, but a force push can
    # destroy newer sessions and memories. `push_branch` already checks
    # whether a response-lost push reached our exact commit.
    push_branch(workdir, config)
    _write_sync_base(config, head_commit(workdir, config), state_fingerprint(data_dir),
                     getattr(config, "_sqlite_hashes", {}))
    LOG.info("pushed state: %s", message)
    return True


def restore(data_dir: Path, config: GitConfig) -> bool:
    try:
        workdir = ensure_clone(config)
        if (os.environ.get('GIT_STATE_FENCE_ON_RESTORE', '0') == '1'
                and not (config.failover and config.role == ROLE_STANDBY)
                and remote_has_data(workdir)):
            ensure_safe_to_push(config)
            # A fast-forward empty commit fences the old deployment before
            # materializing state. Its old base can no longer push; no files
            # are overwritten and in-flight normal pushes cannot replace us.
            run_git(['commit', '--allow-empty', '-q', '-m',
                     'Hermes backup writer handover'], cwd=workdir, config=config)
            push_branch(workdir, config)
            LOG.info('Claimed backup writer before restoring state')
    except GitStateError as exc:
        LOG.warning("git state restore unavailable: %s", redact(str(exc)))
        return False
    restored = materialize(workdir, data_dir, config)
    _write_sync_base(config, head_commit(workdir, config), state_fingerprint(data_dir),
                     _sqlite_fingerprints(data_dir))
    LOG.info("restored %d file(s) from %s@%s", restored, config.api_repo, config.branch)
    return restored > 0


def seed(data_dir: Path, config: GitConfig) -> bool:
    """First launch: put this instance's tree into GitHub.

    The branch is empty on the first boot, so there is nothing to restore from
    it and GitHub would stay empty until the daemon's first tick. Seeding
    closes that gap: whatever this instance has locally becomes the first
    commit, and GitHub is the durable store from then on.

    It refuses to run against a branch that already holds state unless
    GIT_STATE_SEED_FORCE=1, because seeding force-pushes and the local tree
    may be older than what GitHub already has.
    """
    state = probe_state(config)
    if state == REMOTE_HAS_STATE and not config.seed_force:
        raise GitStateError(
            f"{config.api_repo}@{config.branch} already holds state; refusing "
            "to overwrite it. Seeding is only for an empty branch. Set "
            "GIT_STATE_SEED_FORCE=1 if you really want this copy to win."
        )
    if state == REMOTE_UNKNOWN:
        raise GitStateError(
            f"could not reach {config.api_repo}@{config.branch}, so it is not "
            "safe to assume it is empty; not seeding. Nothing is lost and "
            "the daemon will retry."
        )

    pushed = sync_once(data_dir, config, force=True, seed=True)
    if pushed:
        config.seed_on_boot = False
        LOG.info("seeded %s@%s; github is now the primary state store",
                 config.api_repo, config.branch)
    return pushed


def seed_on_startup(data_dir: Path, config: GitConfig) -> bool:
    """Seed an empty state branch from the tree this boot built.

    Called by the daemon once the gateway is up, which is what keeps the port
    bind off the critical path: bootstrap.sh flags the situation with
    GIT_STATE_SEED_ON_BOOT and moves on, and the seed happens here instead.
    Returns True when the branch now carries this instance's state.

    Every answer that is not "the branch is provably empty" leaves the seed for
    a later tick, because the alternative -- guessing -- is how an unproven
    local tree ends up overwriting newer GitHub state.
    """
    if not config.seed_on_boot:
        return False
    if config.failover and config.role == ROLE_STANDBY:
        LOG.info("standby instance; not seeding the state branch")
        return False

    state = probe_state(config)
    if state == REMOTE_UNKNOWN:
        LOG.info("state branch unreachable; leaving the seed for a later tick")
        return False
    if state == REMOTE_HAS_STATE:
        # GitHub holds state this instance never restored from, so its tree is
        # not derived from it. The push guard stays armed and the operator's
        # next clean boot is what settles the question.
        LOG.warning(
            "%s@%s already holds state this instance did not restore from; "
            "not seeding, and not pushing over it",
            config.api_repo, config.branch,
        )
        return False

    try:
        return bool(seed(data_dir, config))
    except GitStateError as exc:
        # Not fatal and not final: the daemon retries on its next tick, which
        # is the whole reason this runs here rather than in the boot wrapper.
        LOG.warning("github seed failed; the push is retried: %s", redact(str(exc)))
        return False


# ---------------------------------------------------------------------------
# Failover leases, carried on git refs
# ---------------------------------------------------------------------------


def lease_ref(priority: int, instance_id: str, when: int) -> str:
    return f"{LEASE_REF_PREFIX}{max(0, min(999, priority)):03d}-{instance_id}-{int(when)}"


def parse_lease_ref(ref: str) -> "tuple[int, str, float] | None":
    if not ref.startswith(LEASE_REF_PREFIX):
        return None
    body = ref[len(LEASE_REF_PREFIX):]
    parts = body.split("-")
    if len(parts) < 3:
        return None
    priority_text, updated_text = parts[0], parts[-1]
    instance_id = "-".join(parts[1:-1])
    if not priority_text.isdigit() or not updated_text.isdigit() or not instance_id:
        return None
    return int(priority_text), instance_id, float(updated_text)


def read_leases(config: GitConfig) -> "list[tuple[int, str, float]]":
    proc = run_git(["ls-remote", config.remote_url, f"{LEASE_REF_PREFIX}*"],
                   check=False, config=config)
    if proc.returncode != 0:
        raise GitStateError(redact(proc.stderr.strip()))
    leases = []
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        parsed = parse_lease_ref(parts[1])
        if parsed:
            leases.append(parsed)
    return leases


def decide_role(leases, *, instance_id: str, priority: int, now: float, ttl: int) -> str:
    """Identical lease decision to the standby/active contract: the highest
    priority with a fresh lease wins; ties break on instance id; anything
    unreadable fails open to ACTIVE so an outage never silences everyone."""
    mine = (priority, instance_id)
    for peer_priority, peer_id, updated in leases:
        if peer_id == instance_id:
            continue
        if now - updated > ttl:
            continue
        if (peer_priority, peer_id) > mine:
            return ROLE_STANDBY
    return ROLE_ACTIVE


def claim_lease(config: GitConfig) -> str:
    now = int(time.time())
    ref = lease_ref(config.priority, config.instance_id, now)
    workdir = ensure_clone(config)
    # On a first launch there is no commit yet, and a lease ref has to point at
    # one, so make the initial commit if needed.
    target = ensure_initial_commit(workdir, config)
    if not target:
        raise GitStateError(
            "no commit to anchor a failover lease on; the state branch could "
            "not be initialised"
        )

    stale = [
        lease_ref(priority, instance, updated)
        for priority, instance, updated in read_leases(config)
        if instance == config.instance_id
    ]
    refspecs = [f"{target}:{ref}"] + [f":{old}" for old in stale if old != ref]
    run_git(["push", "--force", config.remote_url, *refspecs], cwd=workdir, config=config)

    role = decide_role(
        read_leases(config), instance_id=config.instance_id,
        priority=config.priority, now=time.time(), ttl=config.lease_ttl,
    )
    config.role = role
    return role


def release_lease(config: GitConfig) -> None:
    mine = [
        f":{lease_ref(priority, instance, updated)}"
        for priority, instance, updated in read_leases(config)
        if instance == config.instance_id
    ]
    if not mine:
        return
    # "git push" needs to run inside a repository even when the remote is given
    # as an explicit URL, so hand it the local clone.
    proc = run_git(["push", config.remote_url, *mine], cwd=ensure_clone(config),
                   check=False, config=config)
    if proc.returncode != 0:
        LOG.warning("could not release failover lease for %s: %s",
                    config.instance_id, redact(proc.stderr.strip()))
        return
    LOG.info("released failover lease for %s", config.instance_id)


def current_role(config: GitConfig) -> str:
    if not config.failover:
        return ROLE_ACTIVE
    return decide_role(
        read_leases(config), instance_id=config.instance_id,
        priority=config.priority, now=time.time(), ttl=config.lease_ttl,
    )


def _sync_status_path() -> Path:
    return Path(os.environ.get(
        "GIT_STATE_STATUS_FILE", "/tmp/hermes-git-state-status.json"
    ))


def _write_sync_status(config: GitConfig, state: str, error: str = "") -> None:
    """Publish a small, secret-free backup health record for the dashboard."""
    path = _sync_status_path()
    previous = {}
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(previous, dict):
            previous = {}
    except (OSError, ValueError):
        pass
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    status = {
        "state": state,
        "updated_at": now,
        "last_success_at": now if state == "ok" else previous.get("last_success_at"),
        "branch": config.branch,
        "error": redact(error)[:300] if error else "",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=".state-status-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(status, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


def recovery_checkpoint(data_dir: Path, config: GitConfig) -> bool:
    """Acknowledge a turn only after its state has reached the remote branch."""
    request = Path(os.environ.get('HERMES_RECOVERY_CHECKPOINT_FILE', '/tmp/hermes-recovery-checkpoint.json'))
    ack = request.with_suffix('.ack')
    try:
        value = json.loads(request.read_text())
        nonce = value['nonce']
        if not isinstance(nonce, str) or not nonce:
            return False
        try:
            acknowledged = json.loads(ack.read_text())
            if isinstance(acknowledged, dict) and acknowledged.get('nonce') == nonce:
                return False
        except (OSError, ValueError):
            pass
    except (OSError, ValueError, KeyError, TypeError):
        return False
    ok = False
    try:
        if config.env_mode == ENV_MODE_OMIT:
            raise GitStateError('Recovery checkpoint requires encrypted or private plaintext state, not omit mode')
        if config.failover and config.role == ROLE_STANDBY:
            raise GitStateError('A standby service cannot checkpoint active work')
        ok = bool(sync_once(data_dir, config))
        if ok:
            config._last_push_at = time.time()
            LOG.info('recovery checkpoint uploaded')
    except Exception as exc:
        LOG.warning('recovery checkpoint failed: %s', redact(str(exc)))
    fd, name = tempfile.mkstemp(dir=request.parent, prefix='.checkpoint-ack-')
    try:
        # Diagnostics starts as root, while the gateway drops to hermes.
        # Keep the 0600 acknowledgement readable by the requesting user.
        owner = request.stat()
        if os.geteuid() == 0:
            os.fchown(fd, owner.st_uid, owner.st_gid)
        with os.fdopen(fd, 'w') as handle:
            json.dump({'nonce': nonce, 'ok': ok}, handle)
        os.replace(name, ack)
    finally:
        Path(name).unlink(missing_ok=True)
    return True


def run_daemon(data_dir: Path, config: GitConfig,
               stop: "threading.Event | None" = None) -> None:
    stop = stop if stop is not None else threading.Event()

    def request_stop(_signum, _frame):
        stop.set()

    # Only the main thread may install handlers, and a daemon embedded in
    # someone else's process should not take over their signals anyway.
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
    _write_sync_status(config, "starting")
    LOG.info(
        "git state sync enabled; repo=%s branch=%s interval=%ss%s",
        config.api_repo, config.branch, config.interval,
        "" if not config.watch else
        f" watch={config.watch_seconds}s debounce={config.debounce_seconds}s"
        f" min-gap={config.min_push_interval}s",
    )

    last_heartbeat = 0.0
    last_switch = time.time()
    if config.failover:
        try:
            config.role = claim_lease(config)
            last_heartbeat = time.time()
            LOG.info("failover: instance=%s priority=%d role=%s",
                     config.instance_id, config.priority, config.role)
        except GitStateError as exc:
            LOG.warning("could not claim lease; assuming active: %s", redact(str(exc)))

    # Let the upstream entrypoint finish its first-boot directory setup before
    # the first upload. It is also what keeps this off the port-bind path: the
    # dashboard has had its chance to come up before we start moving bytes.
    if stop.wait(min(15, config.interval)):
        return

    # First launch: GitHub's state branch is empty and this instance's tree was
    # built locally rather than restored. bootstrap.sh used to push it inline,
    # before anything bound a port; a slow GitHub response then ate the whole
    # boot and Render failed the deploy with "no open ports detected". The
    # daemon owns the seed now, so a slow or failed push costs a retry instead
    # of a redeploy.
    seed_on_startup(data_dir, config)

    # Change-driven saves. The interval below is only the safety net: a change
    # is normally pushed within `debounce` seconds of the last write, not at
    # the next interval boundary.
    watcher = ChangeWatcher(data_dir, config) if config.watch else None

    def push(reason: str) -> None:
        # Snapshot first: whatever is written while the push is in flight is
        # not part of the commit, so it has to stay pending afterwards.
        snapshot = watcher.snapshot() if watcher is not None else None
        try:
            sync_once(data_dir, config)
        except GitStateError as exc:
            _write_sync_status(config, "error", redact(str(exc)))
            LOG.warning("git state push failed (%s); will retry: %s",
                        reason, redact(str(exc)))
            if watcher is not None:
                watcher.defer(config.retry_seconds)
            return
        config._last_push_at = time.time()
        _write_sync_status(config, "ok")
        if watcher is not None:
            watcher.mark_pushed(snapshot)

    next_sync = 0.0
    last_scan = 0.0
    while not stop.is_set():
        now = time.time()
        # Recovery intent cannot wait for a quiet tree or the normal push gap.
        # The gateway waits for this acknowledgement before allocating an agent.
        recovery_checkpoint(data_dir, config)
        if config.failover:
            if now - last_heartbeat >= config.heartbeat_interval:
                try:
                    claim_lease(config)
                    last_heartbeat = now
                except GitStateError as exc:
                    LOG.warning("lease heartbeat failed: %s", redact(str(exc)))
            try:
                role = current_role(config)
            except GitStateError:
                role = config.role
            if role != config.role and now - last_switch >= config.role_switch_min:
                previous, config.role = config.role, role
                last_switch = now
                if previous == ROLE_ACTIVE:
                    try:
                        sync_once(data_dir, config, force=True)
                    except GitStateError as exc:
                        LOG.warning("handoff push failed: %s", redact(str(exc)))
                _request_restart(f"{previous} -> {role}")
                stop.wait(5)
                continue

        if watcher is not None and now - last_scan >= config.watch_seconds:
            last_scan = now
            # A burst of writes should become one commit, so wait for quiet
            # before pushing -- then respect the minimum gap so a very chatty
            # agent cannot turn one minute into dozens of commits.
            if watcher.poll() and now - config._last_push_at >= config.min_push_interval:
                push("change detected")

        if now >= next_sync:
            next_sync = now + config.interval
            push("safety-net interval")

        stop.wait(min(
            1,
            config.watch_seconds,
            config.poll_interval if config.failover else config.interval,
            config.interval,
        ))

    try:
        sync_once(data_dir, config, force=True)
    except GitStateError as exc:
        LOG.warning("final git state push failed: %s", redact(str(exc)))
    if config.failover:
        try:
            release_lease(config)
        except GitStateError as exc:
            LOG.warning("could not release lease: %s", redact(str(exc)))


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


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("restore", "sync", "seed", "state", "daemon", "role", "release"),
    )
    parser.add_argument("data_dir", type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=os.environ.get("GIT_STATE_LOG_LEVEL", "INFO").upper(),
        format="[hermes-git-state] %(message)s",
    )
    config = GitConfig.from_env()
    if config is None:
        LOG.info("git state sync disabled; set GIT_STATE_REPO and GIT_STATE_TOKEN")
        if args.command == "role":
            print(ROLE_ACTIVE)
        return 0

    args.data_dir.mkdir(parents=True, exist_ok=True)
    try:
        if args.command == "role":
            print(current_role(config))
            return 0
        if args.command == "release":
            release_lease(config)
            return 0
        if args.command == "state":
            # bootstrap.sh branches on this to decide where to restore from,
            # so it is printed even when the answer is a non-zero exit code.
            found = probe_state(config)
            print(found)
            if found == REMOTE_HAS_STATE:
                return 0
            return 3 if found == REMOTE_EMPTY else 4
        if args.command == "seed":
            return 0 if seed(args.data_dir, config) else 1
        if args.command == "restore":
            return 0 if restore(args.data_dir, config) else 2
        if args.command == "sync":
            return 0 if sync_once(args.data_dir, config) else 1
        run_daemon(args.data_dir, config)
        return 0
    except GitStateError as exc:
        LOG.error("%s", redact(str(exc)))
        if args.command == "role":
            print(ROLE_ACTIVE)
            return 0
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
