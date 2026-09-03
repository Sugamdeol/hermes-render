#!/opt/hermes/.venv/bin/python
"""Release dashboard chat sessions when their WebSocket disconnects.

``tui_gateway`` keeps every created session -- a full ``AIAgent`` with LLM
clients, tool schemas and memory providers -- in a module-level ``_sessions``
dict for the lifetime of the *dashboard* process, and nothing evicts them:
on WS disconnect the handler only detaches the transport. On a 512 MB Free
instance a handful of browser conversations opened over time then pin tens
of MB each in the dashboard until the container restarts, which is a quiet
OOM path on the process that has to stay up for the health check.

The pinned ``tui_gateway/ws.py`` disconnect ``finally`` block currently does::

    for _, sess in list(server._sessions.items()):
        if sess.get("transport") is transport:
            sess["transport"] = server._stdio_transport

This patch turns it into a finalize-and-pop for sessions owned by this
connection when HERMES_TUI_CLOSE_SESSIONS_ON_DISCONNECT is not explicitly
disabled: the persisted transcript stays in the session DB and re-opens via
``session.resume``, while the in-process agent (and its slash worker, when
that is enabled) is released. Exact-match replacement; the build fails loudly
if the pinned source changes.
"""
from __future__ import annotations

import sys
from pathlib import Path

MARKER = "render-tools: ws session cleanup"

OLD = """    finally:
        transport.close()

        # Detach the transport from any sessions it owned so later emits
        # fall back to stdio instead of crashing into a closed socket.
        for _, sess in list(server._sessions.items()):
            if sess.get("transport") is transport:
                sess["transport"] = server._stdio_transport
"""

NEW = """    finally:
        transport.close()

        # Detach the transport from any sessions it owned so later emits
        # fall back to stdio instead of crashing into a closed socket.
        # {marker}: sessions created over this WebSocket are also finalized
        # and removed from the in-process registry on disconnect. Each one
        # holds a full AIAgent (LLM clients, tools, memory) and nothing
        # else ever evicts them, so open browser conversations otherwise
        # accumulate in the dashboard process for its whole lifetime -- a
        # quiet OOM path on small hosted instances. The transcript persists
        # in the session DB and re-opens via session.resume.
        import os as _os

        if _os.environ.get("HERMES_TUI_CLOSE_SESSIONS_ON_DISCONNECT", "1").strip().lower() not in (
            "0", "false", "no", "off",
        ):
            for _sid, _sess in list(server._sessions.items()):
                if _sess.get("transport") is transport:
                    try:
                        server._sessions.pop(_sid, None)
                        server._finalize_session(_sess, end_reason="ws_disconnect")
                    except Exception:
                        pass
        else:
            for _, sess in list(server._sessions.items()):
                if sess.get("transport") is transport:
                    sess["transport"] = server._stdio_transport
""".format(marker=MARKER)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: patch-ws-session-cleanup.py <path/to/tui_gateway/ws.py>",
              file=sys.stderr)
        return 2
    path = Path(argv[1])
    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        print(f"[render-tools] {path} already patched; nothing to do")
        return 0
    if OLD not in text:
        raise SystemExit(
            "expected ws.py disconnect/finally block not found in "
            f"{path}; the pinned Hermes source changed and this patch "
            "needs updating"
        )
    path.write_text(text.replace(OLD, NEW, 1), encoding="utf-8")
    print(f"[render-tools] patched {path}: WebSocket sessions are "
          "released on disconnect")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
