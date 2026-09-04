"""The MCP tool-output cap patch (scripts/patch-mcp-output-cap.py).

Upstream caps tool output through ``tool_output.max_bytes`` but only wires it
into ``terminal_tool`` and ``file_operations``. MCP results are the one path
that returns at full size, so a chatty MCP tool can put megabytes into the
agent's history -- resident in the dashboard process and re-sent to the model
on every later turn. On a 512 MB instance that is the whole ballgame.

These tests pin the contract: the patch applies to the pinned source shape,
the truncation matches ``terminal_tool``'s 40/60 head-tail convention, results
under the limit are byte-identical, and a missing limit helper means "leave it
alone" rather than "guess a cap".
"""
from __future__ import annotations

import ast
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "patch-mcp-output-cap.py"
)
_spec = importlib.util.spec_from_file_location("patch_mcp_output_cap", MODULE_PATH)
assert _spec is not None and _spec.loader is not None
_patch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_patch)


# Upstream nests the assembly twelve spaces deep (inside `_call` inside
# `_build_tool`), and the patch anchors on that exact indentation -- so the
# fixture has to reproduce the nesting or the anchor will not match.
def _fixture() -> str:
    """The shape of the pinned mcp_tool.py result-assembly site."""
    return (
        "import json\n"
        "\n"
        "\n"
        "def _outer(result):\n"
        "    def _call():\n"
        "        def _inner():\n"
        "            parts = []\n"
        '            text_result = "\\n".join(parts) if parts else ""\n'
        "\n"
        "            structured = getattr(result, 'structuredContent', None)\n"
        "            if structured is not None:\n"
        "                return json.dumps({'result': text_result,\n"
        "                                   'structuredContent': structured})\n"
        "            return json.dumps({'result': text_result})\n"
        "\n"
        "        return _inner\n"
        "\n"
        "    return _call\n"
    )


def _injected_block(patched: str) -> str:
    """Pull the block this patch injects back out of a patched file, dedented
    so it can be exec'd on its own. Testing the real injected text rather than
    a copy of it is what keeps this honest."""
    start = patched.index(_patch.MARKER)
    start = patched.index("if text_result:", start)
    end = patched.index("structured = getattr(result,", start)
    lines = patched[start:end].splitlines()
    # drop trailing blank/comment-only lines belonging to the next block
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(
        line[12:] if line.startswith(" " * 12) else line for line in lines
    )


def _stub_limits(max_bytes):
    """Install a tools.tool_output_limits stub returning `max_bytes`."""
    pkg = types.ModuleType("tools")
    pkg.__path__ = []
    mod = types.ModuleType("tools.tool_output_limits")
    mod.get_max_bytes = lambda: max_bytes
    sys.modules["tools"] = pkg
    sys.modules["tools.tool_output_limits"] = mod


class PatchMcpOutputCapTests(unittest.TestCase):
    def setUp(self):
        # keep the real modules (if any) restorable
        self._saved = {
            name: sys.modules.get(name)
            for name in ("tools", "tools.tool_output_limits")
        }

    def tearDown(self):
        for name, mod in self._saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod

    def test_patch_applies_idempotently_and_result_parses(self):
        source = _fixture()
        ast.parse(source)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "mcp_tool.py"
            target.write_text(source, encoding="utf-8")
            self.assertEqual(_patch.main(["patch-mcp-output-cap.py", str(target)]), 0)
            once = target.read_text(encoding="utf-8")
            ast.parse(once)
            self.assertIn(_patch.MARKER, once)
            # re-running must be a no-op, not a double truncation
            self.assertEqual(_patch.main(["patch-mcp-output-cap.py", str(target)]), 0)
            self.assertEqual(target.read_text(encoding="utf-8"), once)
            self.assertEqual(once.count(_patch.MARKER), 1)

    def test_patch_fails_loudly_when_the_anchor_moves(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "mcp_tool.py"
            target.write_text("import json\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                _patch.main(["patch-mcp-output-cap.py", str(target)])

    def test_patch_refuses_to_guess_between_two_assembly_sites(self):
        """If upstream grows a second text_result site, truncating the wrong
        one would silently corrupt results -- so refuse instead of guessing."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "mcp_tool.py"
            target.write_text(_fixture() + "\n\n" + _fixture(), encoding="utf-8")
            with self.assertRaises(SystemExit):
                _patch.main(["patch-mcp-output-cap.py", str(target)])

    # ── behaviour of the injected block ────────────────────────────────────

    def _run_block(self, text_result, max_bytes):
        """Apply the patch to the fixture, extract the injected block, and run
        it against `text_result` with get_max_bytes stubbed to `max_bytes`."""
        _stub_limits(max_bytes)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "mcp_tool.py"
            target.write_text(_fixture(), encoding="utf-8")
            _patch.main(["patch-mcp-output-cap.py", str(target)])
            patched = target.read_text(encoding="utf-8")
        namespace = {"text_result": text_result}
        exec(compile(_injected_block(patched), "<injected>", "exec"), namespace)
        return namespace["text_result"]

    def test_result_under_the_limit_is_untouched(self):
        small = "x" * 50
        self.assertEqual(self._run_block(small, 100), small)

    def test_result_exactly_at_the_limit_is_untouched(self):
        exact = "y" * 100
        self.assertEqual(self._run_block(exact, 100), exact)

    def test_oversized_result_is_truncated_head_and_tail(self):
        """40% head, 60% tail, with terminal_tool's notice -- the point is that
        an error at the start and the relevant output at the end both survive."""
        huge = "A" * 500 + "Z" * 500
        out = self._run_block(huge, 100)
        self.assertLess(len(out), 200, f"not bounded: {len(out)} chars")
        self.assertTrue(out.startswith("A" * 40), "40% head not preserved")
        self.assertTrue(out.endswith("Z" * 60), "60% tail not preserved")
        self.assertIn("OUTPUT TRUNCATED - 900 chars omitted out of 1000 total", out)

    def test_missing_limit_helper_means_leave_it_alone(self):
        """A removed/renamed helper must not invent a cap -- silently dropping
        most of a tool result is worse than returning it whole."""
        for name in ("tools", "tools.tool_output_limits"):
            sys.modules.pop(name, None)
        huge = "B" * 5000
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "mcp_tool.py"
            target.write_text(_fixture(), encoding="utf-8")
            _patch.main(["patch-mcp-output-cap.py", str(target)])
            patched = target.read_text(encoding="utf-8")
        namespace = {"text_result": huge}
        exec(compile(_injected_block(patched), "<injected>", "exec"), namespace)
        self.assertEqual(namespace["text_result"], huge)

    def test_empty_result_is_left_alone(self):
        self.assertEqual(self._run_block("", 100), "")


if __name__ == "__main__":
    unittest.main()
