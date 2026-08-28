#!/opt/hermes/.venv/bin/python
"""Git-backed state sync for Hermes on Render.

Why Git instead of the GoFile tarball: Git transfers *deltas*. The GoFile
backend uploads a full /opt/data archive every time anything changes, so a
30 MB session database costs 30 MB per sync even when one conversation moved a
few kilobytes. Git sends roughly the changed bytes, which is typically two to
three orders of magnitude less traffic for the same protection.

Layout in the state repo (default branch ``state``):

    data/            mirror of $HERMES_HOME, minus excluded paths
    data/.env.enc    the dotenv, age-encrypted (never plaintext)
    MANIFEST.json    instance id, timestamp, and what transforms were applied

Deletions are mirrored. The working tree is rebuilt from the data directory on
every sync, so ``git add -A`` records removals as well as additions and the
repo never accumulates files you deleted locally.

GitHub is the primary store and GoFile is the backup. The one wrinkle is the
very first launch, when the state repo is still empty: there is nothing to
restore from it, so bootstrap.sh falls back to the GoFile archive and the
daemon then pushes that restored state back to GitHub with ``seed``. From that
moment GitHub is primary and GoFile drops to a slow second copy. ``seed``
refuses to write over a branch that already holds state, so an old GoFile
archive can never be pushed on top of newer GitHub state.

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
  * It refuses to commit .env in plaintext. Either an age recipient is
    configured and .env is encrypted, or .env is left out of the backup.
  * It refuses to seed GitHub from a GoFile restore when the branch already
    holds state, unless GIT_STATE_SEED_FORCE=1 says the operator meant it.
  * Tokens are stripped from every log line and exception message.

History is bounded: after GIT_STATE_MAX_COMMITS commits the branch is squashed
to a single orphan commit and force-pushed, so the repo cannot grow without
limit even though every sync commits.

Failover leases ride on Git refs (refs/hermes-lease/<priority>-<id>-<epoch>),
so checking the cluster is one `git ls-remote` -- no clone, no checkout, a few
kilobytes. Same semantics as the GoFile lease it mirrors.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

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
DEFAULT_HTTP_POST_BUFFER_BYTES = 250 * 1024 * 1024
# A transfer slower than this for this long is stalled, not slow. git gives up
# and we retry, instead of the process sitting on a dead connection.
DEFAULT_HTTP_LOW_SPEED_LIMIT = 1000
DEFAULT_HTTP_LOW_SPEED_TIME = 60
# No git command here should ever run forever: the boot wrapper and the daemon
# both wait on them.
DEFAULT_GIT_TIMEOUT_SECONDS = 600
DEFAULT_PUSH_ATTEMPTS = 3
DEFAULT_PUSH_RETRY_SECONDS = 5

# Files that must never be committed in the clear. Matched on the data-dir
# relative path.
SENSITIVE_FILES = (".env",)


class GitStateError(RuntimeError):
    """A git or GitHub operation failed."""


def _load_free_storage():
    """Reuse the exclusion rules so both backends agree on what is state."""
    path = Path(__file__).resolve().parent / "free-storage.py"
    spec = importlib.util.spec_from_file_location("_free_storage", path)
    if spec is None or spec.loader is None:  # pragma: no cover - packaging issue
        raise GitStateError("could not load free-storage.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_FS = None


def excludes_and_matcher():
    global _FS
    if _FS is None:
        _FS = _load_free_storage()
    return _FS.DEFAULT_EXCLUDES, _FS.is_excluded


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
        workdir: "Path | None" = None,
        watch: bool = True,
        watch_seconds: int = 5,
        debounce_seconds: int = 10,
        min_push_interval: int = 20,
        retry_seconds: int = 30,
        seed_from_gofile: bool = False,
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
        # Set when this instance booted from the GoFile archive because GitHub
        # was empty or unreachable. Until the seed succeeds, it is a promise
        # not to overwrite GitHub state we never restored from.
        self.seed_from_gofile = seed_from_gofile
        self.seed_force = seed_force
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

        free_storage = _load_free_storage()
        return cls(
            repo=repo,
            token=token,
            branch=os.environ.get("GIT_STATE_BRANCH", "state"),
            interval=positive_int("GIT_STATE_INTERVAL_SECONDS", 300),
            max_commits=positive_int("GIT_STATE_MAX_COMMITS", 200),
            age_recipient=os.environ.get("GIT_STATE_AGE_RECIPIENT", "").strip(),
            age_key_file=os.environ.get("SOPS_AGE_KEY_FILE", "").strip(),
            instance_id=free_storage.sanitize_instance_id(
                os.environ.get("HERMES_INSTANCE_ID", "") or os.uname().nodename
            ),
            priority=max(0, min(999, positive_int("HERMES_INSTANCE_PRIORITY", 50))),
            failover=flag("HERMES_FAILOVER"),
            lease_ttl=positive_int("HERMES_LEASE_TTL_SECONDS", 600),
            heartbeat_interval=positive_int("HERMES_LEASE_HEARTBEAT_SECONDS", 120),
            poll_interval=positive_int("HERMES_LEASE_POLL_SECONDS", 30),
            role_switch_min=positive_int("HERMES_ROLE_SWITCH_MIN_SECONDS", 300),
            allow_public=flag("GIT_STATE_ALLOW_PUBLIC"),
            watch=flag_on("GIT_STATE_WATCH"),
            # How often the tree is fingerprinted, how long it has to stay
            # quiet before that change is pushed, and the hard floor between
            # two pushes. Defaults put a change in GitHub within ~20s while
            # capping a frantic agent at ~3 commits a minute.
            watch_seconds=positive_int("GIT_STATE_WATCH_SECONDS", 5),
            debounce_seconds=non_negative_int("GIT_STATE_DEBOUNCE_SECONDS", 10),
            min_push_interval=non_negative_int("GIT_STATE_MIN_PUSH_INTERVAL_SECONDS", 20),
            retry_seconds=positive_int("GIT_STATE_RETRY_SECONDS", 30),
            seed_from_gofile=flag("GIT_STATE_SEED_FROM_GOFILE"),
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


def run_git(args: "list[str]", cwd: "Path | None" = None, check: bool = True,
            config: "GitConfig | None" = None) -> subprocess.CompletedProcess:
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


def ensure_clone(config: GitConfig) -> Path:
    """Return a working clone of the state branch, creating it if needed."""
    workdir = config.workdir
    if (workdir / ".git").is_dir():
        proc = run_git(["fetch", "--depth", "1", "origin", config.branch],
                       cwd=workdir, check=False, config=config)
        if proc.returncode == 0:
            run_git(["checkout", "-B", config.branch, "FETCH_HEAD"], cwd=workdir,
                    config=config)
        return workdir

    workdir.parent.mkdir(parents=True, exist_ok=True)
    if workdir.exists():
        shutil.rmtree(workdir, ignore_errors=True)
    proc = run_git(
        ["clone", "--depth", "1", "--branch", config.branch,
         config.remote_url, str(workdir)],
        check=False, config=config,
    )
    if proc.returncode != 0:
        # Branch (or repo content) does not exist yet: start an empty history.
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


def push_branch(workdir: Path, config: GitConfig, *, force: bool = False) -> None:
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
        if force:
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
    return REMOTE_HAS_STATE if remote_has_data(workdir) else REMOTE_EMPTY


def state_fingerprint(data_dir: Path) -> str:
    """Cheap metadata fingerprint of the state directory.

    Borrowed from the GoFile backend so both agree on what counts as a change:
    metadata only (paths, sizes, mtimes), with volatile paths excluded. It is
    what lets the daemon notice a change in a few milliseconds instead of
    copying the whole tree to find out.
    """
    excludes, _ = excludes_and_matcher()
    free_storage = _load_free_storage()
    return free_storage._state_fingerprint(data_dir, excludes)


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

    def mark_pushed(self) -> None:
        """Re-baseline after a push so an unchanged tree stays quiet."""
        self.baseline = self._sample()
        self.pending_since = None
        self.not_before = 0.0

    def defer(self, seconds: float) -> None:
        """A push failed: leave the change pending and back off before retrying."""
        self.not_before = self._clock() + max(0.0, seconds)


# ---------------------------------------------------------------------------
# Mirroring
# ---------------------------------------------------------------------------


def plan_mirror(data_dir: Path, excludes) -> "list[str]":
    """Relative paths under data_dir that belong in the backup, sorted.

    Pure enough to test: it only reads the filesystem and returns names.
    """
    _, is_excluded = excludes_and_matcher()
    selected: list[str] = []
    for root, dirs, files in os.walk(data_dir, topdown=True, followlinks=False):
        root_path = Path(root)
        dirs[:] = sorted(
            name for name in dirs
            if not (root_path / name).is_symlink()
            and not is_excluded((root_path / name).relative_to(data_dir).as_posix(), excludes)
        )
        for name in sorted(files):
            path = root_path / name
            if path.is_symlink():
                continue
            relative = path.relative_to(data_dir).as_posix()
            if is_excluded(relative, excludes):
                continue
            selected.append(relative)
    return sorted(selected)


def age_encrypt(plaintext: bytes, config: GitConfig) -> "bytes | None":
    """Encrypt with `age` if a recipient is configured, else None."""
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
    binary = shutil.which("age") or shutil.which("rage")
    if not binary:
        return None
    args = [binary, "-d"]
    env = dict(os.environ)
    if config.age_key_file:
        args += ["-i", config.age_key_file]
    elif os.environ.get("SOPS_AGE_KEY"):
        # age reads identities from a file; materialise the key privately.
        handle = tempfile.NamedTemporaryFile("w", delete=False)
        os.chmod(handle.name, 0o600)
        handle.write(os.environ["SOPS_AGE_KEY"] + "\n")
        handle.close()
        args += ["-i", handle.name]
    proc = subprocess.run(args, input=ciphertext, capture_output=True, env=env)
    if proc.returncode != 0:
        LOG.warning("age decryption failed: %s", redact(proc.stderr.decode("utf-8", "replace")))
        return None
    return proc.stdout


def build_worktree(data_dir: Path, workdir: Path, config: GitConfig) -> "dict":
    """Rebuild <workdir>/data from data_dir. Returns a manifest dict.

    The tree is rebuilt rather than patched so that files deleted locally
    disappear from the next commit: `git add -A` sees them gone.
    """
    excludes, _ = excludes_and_matcher()
    # Read the previous generation before the manifest is overwritten. The
    # clone is shallow, so "git rev-list --count" cannot tell us how long the
    # remote history is; we carry the count in the manifest instead.
    generation = read_generation(workdir) + 1
    target = workdir / DATA_SUBDIR
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    copied, encrypted, skipped = 0, [], []
    for relative in plan_mirror(data_dir, excludes):
        source = data_dir / relative
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            payload = source.read_bytes()
        except OSError:
            continue

        if relative in SENSITIVE_FILES:
            sealed = age_encrypt(payload, config)
            if sealed is None:
                # Never fall back to plaintext: the whole point of the check.
                skipped.append(relative)
                LOG.warning(
                    "%s left OUT of the git backup: no GIT_STATE_AGE_RECIPIENT "
                    "(or no age binary), and committing it in the clear is not "
                    "an option. Keep those values in Render's Environment tab "
                    "or the repo's encrypted secrets file.", relative
                )
                continue
            destination = destination.with_name(destination.name + ENCRYPTED_SUFFIX)
            destination.write_bytes(sealed)
            encrypted.append(relative)
            continue

        destination.write_bytes(payload)
        try:
            os.chmod(destination, source.stat().st_mode & 0o777)
        except OSError:
            pass
        copied += 1

    manifest = {
        "instance": config.instance_id,
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "files": copied,
        "generation": generation,
        "encrypted": sorted(encrypted),
        "omitted": sorted(skipped),
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

    restored = 0
    for source in sorted(source_root.rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(source_root).as_posix()
        payload = source.read_bytes()
        if relative.endswith(ENCRYPTED_SUFFIX):
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
        destination.write_bytes(payload)
        if relative in SENSITIVE_FILES:
            os.chmod(destination, 0o600)
        restored += 1
    return restored


def read_generation(workdir: Path) -> int:
    """How many state commits the branch has accumulated since the last squash.

    Stored in the manifest because the working clone is shallow and therefore
    cannot count the remote's commits itself.
    """
    try:
        raw = json.loads((workdir / MANIFEST_NAME).read_text(encoding="utf-8"))
        value = int(raw.get("generation", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return 0
    return max(0, value)


def should_compact(commits: int, max_commits: int) -> bool:
    """History is bounded so the repo cannot grow forever."""
    return max_commits > 0 and commits >= max_commits


def compact_history(workdir: Path, config: GitConfig) -> None:
    """Squash the branch to a single orphan commit and force-push."""
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
    push_branch(workdir, config, force=True)


def sync_once(data_dir: Path, config: GitConfig, *, force: bool = False,
              seed: bool = False) -> bool:
    if config.failover and config.role == ROLE_STANDBY:
        LOG.debug("standby instance; not pushing state")
        return True

    ensure_safe_to_push(config)
    workdir = ensure_clone(config)

    # This instance booted from the GoFile archive because GitHub was empty or
    # unreachable. If GitHub has since turned out to hold state we never
    # restored from, pushing would replace it with an older copy. Refuse and
    # say what to do about it.
    if config.seed_from_gofile and not config.seed_force and remote_has_data(workdir):
        raise GitStateError(
            f"{config.api_repo}@{config.branch} already holds state, but this "
            "instance booted from the GoFile archive, so pushing would "
            "overwrite it with an older copy. GoFile stays the live backup "
            "here. To adopt the GitHub copy instead, restart once it is the "
            "branch you want restored; to push this copy anyway, set "
            "GIT_STATE_SEED_FORCE=1."
        )

    manifest = build_worktree(data_dir, workdir, config)

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
        return True

    message = (
        f"Hermes state from {config.instance_id} "
        f"({manifest['files']} files, {manifest['updated']})"
    )
    proc = run_git(["commit", "-q", "-m", message], cwd=workdir, check=False, config=config)
    if proc.returncode != 0 and "nothing to commit" not in (proc.stdout + proc.stderr):
        raise GitStateError(redact(proc.stderr or proc.stdout))

    if should_compact(manifest.get("generation", 0), config.max_commits):
        compact_history(workdir, config)
        return True

    try:
        push_branch(workdir, config)
    except GitStateError:
        # Either someone else advanced the branch, or the transport gave up.
        # Ours is a full mirror of local state, so taking our version is
        # correct rather than merging -- and a force push also settles a ref
        # that a half-landed attempt left pointing somewhere older.
        LOG.info("push did not land; re-pushing with force after refetch")
        run_git(["fetch", "--depth", "1", "origin", config.branch], cwd=workdir,
                check=False, config=config)
        push_branch(workdir, config, force=True)
    LOG.info("pushed state: %s", message)
    return True


def restore(data_dir: Path, config: GitConfig) -> bool:
    try:
        workdir = ensure_clone(config)
    except GitStateError as exc:
        LOG.warning("git state restore unavailable: %s", redact(str(exc)))
        return False
    restored = materialize(workdir, data_dir, config)
    LOG.info("restored %d file(s) from %s@%s", restored, config.api_repo, config.branch)
    return restored > 0


def seed(data_dir: Path, config: GitConfig) -> bool:
    """First launch: put the state we just restored from GoFile into GitHub.

    The branch is empty on the first boot, so there is nothing to restore from
    it and GitHub would stay empty until the daemon's first tick. Seeding
    closes that gap: whatever this instance restored (from GoFile, or from
    nothing at all) becomes the first commit, and GitHub is primary from then
    on.

    It refuses to run against a branch that already holds state unless
    GIT_STATE_SEED_FORCE=1, because seeding force-pushes and the GoFile copy
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
            "safe to assume it is empty; not seeding. GoFile stays the live "
            "backup and the daemon will retry."
        )

    pushed = sync_once(data_dir, config, force=True, seed=True)
    if pushed:
        config.seed_from_gofile = False
        LOG.info("seeded %s@%s; github is now the primary state store",
                 config.api_repo, config.branch)
    return pushed


def seed_on_startup(data_dir: Path, config: GitConfig) -> bool:
    """Seed an empty state branch from the tree this boot restored.

    Called by the daemon once the gateway is up, which is what keeps the port
    bind off the critical path: bootstrap.sh flags the situation with
    GIT_STATE_SEED_FROM_GOFILE and moves on, and the seed happens here instead.
    Returns True when the branch now carries this instance's state.

    Every answer that is not "the branch is provably empty" leaves the seed for
    a later tick, because the alternative -- guessing -- is how an old GoFile
    archive ends up overwriting newer GitHub state.
    """
    if not config.seed_from_gofile:
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
        # Not fatal and not final: GoFile stays the live backup and the daemon
        # retries on its next tick, which is the whole reason this runs here
        # rather than in the boot wrapper.
        LOG.warning("github seed failed; GoFile stays the live backup and the "
                    "push is retried: %s", redact(str(exc)))
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
    """Identical semantics to the GoFile backend's lease decision."""
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


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


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

    # First launch: GitHub's state branch is empty and this instance's tree
    # came from the GoFile archive. bootstrap.sh used to push it inline, before
    # anything bound a port; a slow GitHub response then ate the whole boot and
    # Render failed the deploy with "no open ports detected". The daemon owns
    # the seed now, so a slow or failed push costs a retry instead of a
    # redeploy.
    seed_on_startup(data_dir, config)

    # Change-driven saves. The interval below is only the safety net: a change
    # is normally pushed within `debounce` seconds of the last write, not at
    # the next interval boundary.
    watcher = ChangeWatcher(data_dir, config) if config.watch else None

    def push(reason: str) -> None:
        try:
            sync_once(data_dir, config)
        except GitStateError as exc:
            LOG.warning("git state push failed (%s); will retry: %s",
                        reason, redact(str(exc)))
            if watcher is not None:
                watcher.defer(config.retry_seconds)
            return
        config._last_push_at = time.time()
        if watcher is not None:
            watcher.mark_pushed()

    next_sync = 0.0
    last_scan = 0.0
    while not stop.is_set():
        now = time.time()
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


def _request_restart(reason: str) -> bool:
    free_storage = _load_free_storage()
    return free_storage._request_restart(reason)


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
