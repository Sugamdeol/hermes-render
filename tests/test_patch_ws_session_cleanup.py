from __future__ import annotations

import ast
import importlib.util
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "patch-ws-session-cleanup.py"
)
_spec = importlib.util.spec_from_file_location("patch_ws_cleanup", MODULE_PATH)
assert _spec is not None and _spec.loader is not None
_patch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_patch)


# The disconnect finally-block exactly as the pinned upstream
# tui_gateway/ws.py (v2026.5.7) ships it.
UPSTREAM_FINALLY = """    finally:
        transport.close()

        # Detach the transport from any sessions it owned so later emits
        # fall back to stdio instead of crashing into a closed socket.
        for _, sess in list(server._sessions.items()):
            if sess.get("transport") is transport:
                sess["transport"] = server._stdio_transport

        try:
            await ws.close()
        except Exception:
            pass
"""


class PatchWsSessionCleanupTests(unittest.TestCase):
    def test_patch_releases_sessions_on_disconnect(self):
        out = _patch.NEW
        self.assertIn(_patch.MARKER, out)
        # The patched path finalizes + pops sessions owned by the transport.
        self.assertIn("server._sessions.pop", out)
        self.assertIn("_finalize_session", out)
        # And it is env-gated so a reconnection-dependent client can revert.
        self.assertIn("HERMES_TUI_CLOSE_SESSIONS_ON_DISCONNECT", out)
        # The original detach behaviour survives as the opt-out branch.
        self.assertIn('sess["transport"] = server._stdio_transport', out)
        # The replacement is itself valid Python.
        ast.parse("async def f(ws, transport, server):\n    try:\n        pass\n" + out)

    def test_patch_via_main_is_exact_and_idempotent(self):
        # The real handler, as an async function body at module level.
        source = (
            "import asyncio\n\n\nasync def handle_ws(ws):\n    await ws.accept()\n"
            "    try:\n        raw = await ws.receive_text()\n"
            + UPSTREAM_FINALLY
        )
        ast.parse(source)

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "ws.py"
            target.write_text(source, encoding="utf-8")
            self.assertEqual(
                _patch.main(["patch-ws-session-cleanup.py", str(target)]), 0
            )
            patched = target.read_text(encoding="utf-8")
            ast.parse(patched)
            self.assertEqual(patched.count(_patch.MARKER), 1)

            # Idempotent: second run leaves the file untouched.
            self.assertEqual(
                _patch.main(["patch-ws-session-cleanup.py", str(target)]), 0
            )
            self.assertEqual(target.read_text(encoding="utf-8"), patched)

    def test_main_fails_when_upstream_block_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "ws.py"
            target.write_text("async def handle_ws(ws):\n    pass\n",
                              encoding="utf-8")
            with self.assertRaises(SystemExit):
                _patch.main(["patch-ws-session-cleanup.py", str(target)])


if __name__ == "__main__":
    unittest.main()
