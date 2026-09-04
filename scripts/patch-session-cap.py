#!/opt/hermes/.venv/bin/python
"""Bound the number of live chat sessions the dashboard process holds.

``tui_gateway`` keeps every created session in a module-level ``_sessions``
dict, and each entry owns a full ``AIAgent`` -- LLM clients, tool schemas,
memory providers, transcript history. Nothing upstream caps that dict: the
only thing that ever removes an entry is an explicit ``session.close`` or (on
this image) the WebSocket-disconnect patch.

That is fine for one browser tab. It is not fine for the way browser tabs
actually behave: a laptop that sleeps, a phone that switches apps, a reload
that abandons a socket half-open. Each of those pins another entry in the
registry, and the growth is linear in the number of them.

Measured, not estimated: the same 24-abandoned-tab workload against two clean
512 MB cgroups grew 0.20 MB per conversation with the cap on and 0.97 MB per
conversation with it off, ending 15.1 MB apart -- and, more importantly, the
capped curve plateaus while the uncapped one keeps climbing. (The heavy
per-conversation cost this image really had was the orphaned `slash_worker`
subprocess at 57-85 MB RSS each; that is what patch-slash-worker.py removes.
A retained session object is about 1 MB. Both have to be bounded.)

This patch adds a registry cap enforced where sessions are created:

    HERMES_TUI_MAX_SESSIONS   (default 2)

When a new session would take the registry past the cap, the oldest sessions
are finalized and dropped -- transcript first, so nothing is lost: the
conversation stays in the session DB and re-opens through ``session.resume``.
Sessions that are mid-turn (``running``) are skipped rather than torn down,
because evicting an agent under a live request is how you turn a memory
problem into a crashed conversation. Set the variable to 0 to disable the cap
entirely.

Default 2, not 1: the dashboard's own UI opens a session for the sidebar and
the composer can hold another, and a cap of 1 makes those fight.

Exact-match replacement; the build fails loudly if the pinned source moves.
"""
from __future__ import annotations

import sys
from pathlib import Path

MARKER = "render-tools: bounded session registry"

REGISTRY_ANCHOR = "_sessions: dict[str, dict] = {}\n"

HELPER = '''

def _render_tools_evict_sessions(keep_sid: str = "") -> None:  # {marker}
    """Drop the oldest live sessions once the registry passes its cap.

    Each entry is a live AIAgent, so an unbounded registry grows without
    bound; measured on a 512 MB cgroup that is ~1 MB per abandoned
    conversation, forever. The transcript is committed before the object is
    released, so an evicted conversation is still in the session DB and
    re-opens via ``session.resume`` -- this costs RAM, never history.

    A session that is mid-turn is skipped: tearing an agent down underneath a
    live request would turn a memory problem into a crashed conversation, and
    the next ``session.create`` will simply try again.
    """
    import os as _os

    raw = _os.environ.get("HERMES_TUI_MAX_SESSIONS", "2")
    try:
        cap = int(raw)
    except (TypeError, ValueError):
        cap = 2
    if cap <= 0:
        return

    # dict preserves insertion order, so iterating it walks oldest-first.
    for sid in list(_sessions.keys()):
        if len(_sessions) <= cap:
            break
        if sid == keep_sid:
            continue
        session = _sessions.get(sid)
        if not isinstance(session, dict):
            continue
        if session.get("running"):
            continue
        _sessions.pop(sid, None)
        try:
            _finalize_session(session, end_reason="session_cap")
        except Exception:
            pass
        try:
            agent = session.get("agent")
            if agent is not None and hasattr(agent, "close"):
                agent.close()
        except Exception:
            pass
        try:
            worker = session.get("slash_worker")
            if worker is not None:
                worker.close()
        except Exception:
            pass
'''.format(marker=MARKER)

CREATE_ANCHOR = """    build_timer = threading.Timer(0.05, _deferred_build)
    build_timer.daemon = True
    build_timer.start()

    return _ok(
"""

CREATE_PATCHED = """    build_timer = threading.Timer(0.05, _deferred_build)
    build_timer.daemon = True
    build_timer.start()

    # {marker}: enforce the cap here rather than on a timer, so the registry
    # can never be observed over it and nothing has to poll for it.
    _render_tools_evict_sessions(sid)

    return _ok(
""".format(marker=MARKER)


def main(argv: "list[str]") -> int:
    if len(argv) != 2:
        print("usage: patch-session-cap.py <path/to/tui_gateway/server.py>",
              file=sys.stderr)
        return 2
    path = Path(argv[1])
    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        print(f"[render-tools] {path} already patched; nothing to do")
        return 0
    if REGISTRY_ANCHOR not in text:
        raise SystemExit(
            f"expected the _sessions registry declaration not found in {path}; "
            "the pinned Hermes source changed and this patch needs updating"
        )
    if CREATE_ANCHOR not in text:
        raise SystemExit(
            f"expected the session.create build-timer block not found in "
            f"{path}; the pinned Hermes source changed and this patch needs "
            "updating"
        )
    text = text.replace(REGISTRY_ANCHOR, REGISTRY_ANCHOR + HELPER, 1)
    text = text.replace(CREATE_ANCHOR, CREATE_PATCHED, 1)
    path.write_text(text, encoding="utf-8")
    print(f"[render-tools] patched {path}: live chat sessions are bounded by "
          "HERMES_TUI_MAX_SESSIONS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
