#!/opt/hermes/.venv/bin/python
"""Merge repo-managed environment into Hermes' runtime environment.

Two kinds of input, with deliberately different handling:

--secrets FILE   Credentials: the SOPS-encrypted dotenv committed to the repo
                 (`env/secrets.enc.env`), decrypted at boot by bootstrap.sh and
                 piped in here. These are merged into $HERMES_HOME/.env so the
                 dashboard's API Keys tab sees them and they survive restarts.

--knobs FILE     Non-secret deploy knobs (`env/common.env`: ports, dashboard
                 flags, thread caps, cache sizes). These are exported for the
                 gateway/dashboard processes but NEVER written to
                 $HERMES_HOME/.env, and any copy of them already sitting in
                 that file is removed. Upstream Hermes loads .env with
                 override=True on every start, so a knob persisted there would
                 outrank Render's Environment tab -- which is how a stale
                 `HERMES_DASHBOARD_TUI=0`, seeded by an older image and carried
                 along in the state backup, kept the Chat tab disabled after
                 the operator had set the variable to 1.

Precedence for secrets, highest first:

  1. The real process environment. Render's Environment tab and anything you
     export locally always win; repo values never override a live deploy knob.
  2. An existing value in $HERMES_HOME/.env. That file is written by the
     dashboard's API Keys tab and restored from the state repo, so a key
     someone set from the UI is not silently reverted to the committed one.
  3. The repo's encrypted secrets. These fill in whatever is still missing.

Pass --force to invert rule 2 and make the repo authoritative over the .env
file, which is what you want when rotating a key in git.

Precedence for knobs is simply: process environment, then the repo file. The
.env file does not take part, because it is not allowed to hold them.

The secrets merge is line-preserving: comments, ordering, and unmanaged keys in
the existing .env are kept as-is, and only missing keys are appended under a
marked block. The knob eviction is equally surgical: only assignments of the
listed keys are dropped, everything else is left byte-for-byte.

Outputs:
  * --secrets: the merged .env is written back to disk (0600).
  * --knobs: the .env is rewritten (0600) only when a stale knob was removed.
  * With --print-exports, shell-quoted assignments are written to stdout for
    variables the process environment lacks (and, for secrets, that the .env
    file does not already own), so the caller can eval them into the gateway's
    environment. Exporting a repo secret for a key the .env already defines
    would invert rule 2, since a dotenv loader will not override a variable
    that is already set in the environment. config.yaml's ${RENDER_MCP_API_KEY}
    substitution reads the process env, so this is what makes MCP auth work.

Never log the values. stdout is meant for `eval`, not for a terminal.
"""
from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path

MANAGED_HEADER = "# --- added from the repo's encrypted secrets (env/secrets.enc.env) ---"


def parse_dotenv(text: str) -> "list[tuple[str, str]]":
    """Parse dotenv text into ordered (key, value) pairs.

    Deliberately small: `KEY=value`, optional `export ` prefix, optional
    matching single/double quotes, `#` comments, blank lines. Values are taken
    literally otherwise -- no interpolation, because an API key containing `$`
    must survive a round trip.
    """
    pairs: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not is_valid_key(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        pairs.append((key, value))
    return pairs


def is_valid_key(key: str) -> bool:
    return key.replace("_", "").isalnum() and not key[0].isdigit()


def existing_keys(text: str) -> "set[str]":
    return {key for key, _ in parse_dotenv(text)}


def merge(env_text: str, secrets: "list[tuple[str, str]]", *, force: bool) -> "tuple[str, list[str]]":
    """Return (new_env_text, keys_added)."""
    present = existing_keys(env_text)
    added: list[str] = []
    kept: list[tuple[str, str]] = []

    for key, value in secrets:
        if key in present and not force:
            continue
        kept.append((key, value))
        added.append(key)

    if not kept:
        return env_text, []

    if force and present:
        # Rewrite in place for keys that already exist, so we don't end up with
        # a duplicate definition where the last one silently wins.
        overridden = {key: value for key, value in kept if key in present}
        if overridden:
            lines_out = []
            for raw in env_text.splitlines():
                stripped = raw.strip()
                candidate = stripped[len("export "):].lstrip() if stripped.startswith("export ") else stripped
                if stripped and not stripped.startswith("#") and "=" in candidate:
                    key = candidate.split("=", 1)[0].strip()
                    if key in overridden:
                        lines_out.append(f"{key}={overridden[key]}")
                        continue
                lines_out.append(raw)
            env_text = "\n".join(lines_out)
            if env_text and not env_text.endswith("\n"):
                env_text += "\n"
            kept = [(k, v) for k, v in kept if k not in overridden]

    if not kept:
        return env_text, added

    if env_text and not env_text.endswith("\n"):
        env_text += "\n"
    block = [MANAGED_HEADER] + [f"{key}={value}" for key, value in kept]
    return env_text + "\n".join(block) + "\n", added


def evict_keys(env_text: str, keys: "set[str]") -> "tuple[str, list[str]]":
    """Return (new_env_text, keys_removed) with every assignment of `keys` dropped.

    Line-preserving otherwise: comments, blank lines, ordering, and every other
    assignment survive untouched. If the managed block header is left with no
    assignments under it, it is dropped too, so repeated boots do not leave a
    trail of orphaned headers behind.
    """
    if not keys:
        return env_text, []

    removed: list[str] = []
    lines_out: list[str] = []
    for raw in env_text.splitlines():
        stripped = raw.strip()
        candidate = stripped[len("export "):].lstrip() if stripped.startswith("export ") else stripped
        if stripped and not stripped.startswith("#") and "=" in candidate:
            key = candidate.split("=", 1)[0].strip()
            if key in keys:
                if key not in removed:
                    removed.append(key)
                continue
        lines_out.append(raw)

    if not removed:
        return env_text, []

    lines_out = _prune_empty_managed_blocks(lines_out)
    new_text = "\n".join(lines_out)
    if not new_text.strip():
        return "", removed
    if not new_text.endswith("\n"):
        new_text += "\n"
    return new_text, removed


def _prune_empty_managed_blocks(lines: "list[str]") -> "list[str]":
    """Drop a MANAGED_HEADER line when no assignment follows it before the next
    header or end of file -- i.e. when eviction emptied the block it introduced."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        if lines[i].strip() == MANAGED_HEADER:
            j = i + 1
            has_assignment = False
            while j < len(lines) and lines[j].strip() != MANAGED_HEADER:
                s = lines[j].strip()
                if s and not s.startswith("#"):
                    has_assignment = True
                    break
                j += 1
            if not has_assignment:
                i += 1
                continue
        out.append(lines[i])
        i += 1
    return out


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError as exc:
        print(f"[render-tools] cannot read {path}: {exc}", file=sys.stderr)
        return ""


def write_env(path: Path, text: str) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        return True
    except OSError as exc:
        print(f"[render-tools] cannot write {path}: {exc}", file=sys.stderr)
        return False


def _read_source(arg: str, label: str) -> "list[tuple[str, str]] | None":
    """Parse a dotenv source given as a path or `-` (stdin). None = nothing to do."""
    if arg == "-":
        text = sys.stdin.read()
    else:
        path = Path(arg)
        if not path.exists():
            print(f"[render-tools] no repo {label} file; skipping", file=sys.stderr)
            return None
        text = read_text(path)
    pairs = parse_dotenv(text)
    # An empty placeholder value means "declared but not set"; skip it so a
    # scaffolded key never blanks out a real one.
    pairs = [(k, v) for k, v in pairs if v != ""]
    if not pairs:
        print(f"[render-tools] repo {label} file has no values; skipping", file=sys.stderr)
        return None
    return pairs


def run_knobs(knobs: "list[tuple[str, str]]", env_path: Path, *, print_exports: bool) -> int:
    """Export non-secret knobs and make sure the .env file does not carry them.

    The .env file is loaded by every Hermes process with override=True, so a
    knob left in there would beat Render's Environment tab on the next start.
    Removing stale copies here is what lets the Environment tab (and this
    repo's env/common.env) actually control values such as
    HERMES_DASHBOARD_TUI on an instance whose .env was seeded by an older
    image or restored from the state backup.
    """
    env_text = read_text(env_path)
    new_text, removed = evict_keys(env_text, {k for k, _ in knobs})
    if removed:
        if not write_env(env_path, new_text):
            return 1
        print(f"[render-tools] removed {len(removed)} deploy knob(s) from {env_path} "
              f"(the process environment owns them): {', '.join(sorted(removed))}",
              file=sys.stderr)

    if print_exports:
        for key, value in knobs:
            if os.environ.get(key):
                continue  # the live environment always wins
            sys.stdout.write(f"export {key}={shlex.quote(value)}\n")
    return 0


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--secrets",
                        help="decrypted dotenv of credentials to merge into .env, or - for stdin")
    source.add_argument("--knobs",
                        help="dotenv of non-secret deploy knobs to export but keep OUT of .env, "
                             "or - for stdin")
    parser.add_argument("--env-file", required=True,
                        help="target .env, normally $HERMES_HOME/.env")
    parser.add_argument("--force", action="store_true",
                        help="let repo secrets override values already in the .env file")
    parser.add_argument("--print-exports", action="store_true",
                        help="write shell-quoted exports for vars missing from the process env")
    args = parser.parse_args(argv)

    if args.knobs is not None:
        knobs = _read_source(args.knobs, "knobs")
        if knobs is None:
            return 0
        return run_knobs(knobs, Path(args.env_file), print_exports=args.print_exports)

    secrets = _read_source(args.secrets, "secrets")
    if secrets is None:
        return 0

    env_path = Path(args.env_file)
    env_text = read_text(env_path)
    # Captured before the merge: these are the keys the dashboard or a state
    # restore already owns, which outrank the repo unless --force.
    preexisting = existing_keys(env_text)
    new_text, added = merge(env_text, secrets, force=args.force)

    if added and not write_env(env_path, new_text):
        return 1

    # Names only -- never values.
    if added:
        print(f"[render-tools] seeded {len(added)} secret(s) into {env_path}: "
              f"{', '.join(sorted(added))}", file=sys.stderr)
    else:
        print("[render-tools] repo secrets already present; nothing to seed", file=sys.stderr)

    if args.print_exports:
        for key, value in secrets:
            if os.environ.get(key):
                continue  # the live environment always wins
            if key in preexisting and not args.force:
                continue  # ...and so does a value already in the .env file
            sys.stdout.write(f"export {key}={shlex.quote(value)}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
