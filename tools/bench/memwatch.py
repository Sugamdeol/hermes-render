#!/usr/bin/env python3
"""Low-frequency memory telemetry for a cgroup-limited Hermes run.

Samples the container budget (cgroup v2 ``memory.current`` / ``memory.max``),
the per-process RSS of every process in the slice, and the thread count, then
prints one compact line per sample and a summary at exit:

    MEMORY: total=214MB/512MB peak=238MB gateway=96MB dashboard=71MB \
children=47MB procs=9 threads=54 sessions=0

Nothing here is a logging framework: it is one line every N seconds, and the
same reader is reused by scripts/benchmark-memory.py so production telemetry
and the bench report the same numbers.

Usage:
    memwatch.py --cgroup /sys/fs/cgroup/hermes-bench --interval 5 --duration 120
    memwatch.py --json out.jsonl ...
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

CG_ROOT_DEFAULT = "/sys/fs/cgroup"

# argv fragments that identify the roles we report separately. Kept in the same
# shape as bootstrap.sh's proc_pids_matching needles so a bench line and a
# production log line mean the same thing.
ROLES = (
    ("gateway", ("hermes gateway", "hermes_cli.main gateway", "gateway run")),
    ("dashboard", ("hermes dashboard", "hermes_cli.main dashboard",
                   "web_server", "uvicorn")),
    ("sync", ("git-storage", "git_state", "git-storage.py")),
    ("tui", ("tui_gateway", "slash_worker")),
)


def _read_int(path: Path, default: int = 0) -> int:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return default
    if raw in ("", "max"):
        return default if raw == "max" else 0
    try:
        return int(raw)
    except ValueError:
        return default


def cgroup_memory(cg_path: str | None) -> tuple[int, int, int]:
    """(used_bytes, limit_bytes, peak_bytes) for the slice, or (0, 0, 0)."""
    if not cg_path:
        return 0, 0, 0
    base = Path(cg_path)
    used = _read_int(base / "memory.current")
    limit = _read_int(base / "memory.max")
    peak = _read_int(base / "memory.peak")
    if not peak:
        # memory.peak is kernel 5.19+; memory.events' high/max counters at
        # least tell us whether we ever hit the ceiling.
        peak = used
    return used, limit, peak


def _proc_cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            return handle.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def _proc_rss_kb(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/status", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _proc_threads(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/status", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("Threads:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def processes_in(cg_path: str | None) -> list[dict]:
    """Every process whose cgroup is (or is under) the slice."""
    out: list[dict] = []
    needle = cg_path.rstrip("/") if cg_path else None
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if needle:
            try:
                with open(f"/proc/{pid}/cgroup", "r", encoding="utf-8") as handle:
                    cg_text = handle.read()
            except OSError:
                continue
            slice_rel = needle[len(CG_ROOT_DEFAULT):] or "/"
            if slice_rel != "/" and f"::{slice_rel}" not in cg_text:
                continue
        cmd = _proc_cmdline(pid)
        out.append({
            "pid": pid,
            "rss_kb": _proc_rss_kb(pid),
            "threads": _proc_threads(pid),
            "cmd": cmd[:160],
        })
    return out


def classify(procs: list[dict]) -> dict[str, int]:
    """RSS by role. ``children`` is everything that is not gateway/dashboard."""
    totals = {name: 0 for name, _ in ROLES}
    other = 0
    for proc in procs:
        cmd = proc["cmd"]
        for name, needles in ROLES:
            if any(needle in cmd for needle in needles):
                totals[name] += proc["rss_kb"]
                break
        else:
            other += proc["rss_kb"]
    totals["children"] = other
    return totals


def sample(cg_path: str | None) -> dict:
    used, limit, peak = cgroup_memory(cg_path)
    procs = processes_in(cg_path)
    roles = classify(procs)
    return {
        "ts": time.time(),
        "used_mb": round(used / 1048576, 1),
        "limit_mb": round(limit / 1048576, 1) if limit else 0,
        "peak_mb": round(peak / 1048576, 1),
        "rss_sum_mb": round(sum(p["rss_kb"] for p in procs) / 1024, 1),
        "procs": len(procs),
        "threads": sum(p["threads"] for p in procs),
        "roles_mb": {k: round(v / 1024, 1) for k, v in roles.items()},
        "top": sorted(
            ({"pid": p["pid"], "rss_mb": round(p["rss_kb"] / 1024, 1),
              "cmd": p["cmd"][:90]} for p in procs),
            key=lambda p: -p["rss_mb"],
        )[:5],
    }


def format_line(s: dict) -> str:
    roles = s["roles_mb"]
    limit = f"/{int(s['limit_mb'])}MB" if s["limit_mb"] else ""
    return (
        f"MEMORY: total={s['used_mb']:.0f}MB{limit} peak={s['peak_mb']:.0f}MB "
        f"gateway={roles.get('gateway', 0):.0f}MB dashboard={roles.get('dashboard', 0):.0f}MB "
        f"sync={roles.get('sync', 0):.0f}MB tui={roles.get('tui', 0):.0f}MB "
        f"children={roles.get('children', 0):.0f}MB "
        f"procs={s['procs']} threads={s['threads']}"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cgroup", default=os.environ.get("BENCH_CGROUP_PATH", ""))
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--duration", type=float, default=0.0,
                    help="seconds to sample; 0 = until SIGINT/EOF on stdin")
    ap.add_argument("--json", default="", help="append each sample as JSONL")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    cg = args.cgroup or None
    samples: list[dict] = []
    started = time.time()
    out = open(args.json, "a", encoding="utf-8") if args.json else None
    try:
        while True:
            s = sample(cg)
            samples.append(s)
            if out:
                out.write(json.dumps(s) + "\n")
                out.flush()
            if not args.quiet:
                print(format_line(s), flush=True)
            if args.duration and (time.time() - started) >= args.duration:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        if out:
            out.close()

    if samples:
        worst = max(samples, key=lambda s: s["used_mb"] or s["rss_sum_mb"])
        peak_field = max(s["peak_mb"] for s in samples)
        print(
            f"SUMMARY: samples={len(samples)} "
            f"idle={samples[0]['used_mb']:.0f}MB "
            f"max={worst['used_mb']:.0f}MB "
            f"cgroup_peak={peak_field:.0f}MB "
            f"max_procs={max(s['procs'] for s in samples)} "
            f"max_threads={max(s['threads'] for s in samples)}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
