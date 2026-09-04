#!/opt/hermes/.venv/bin/python
"""Route MCP tool results through the tool-output cap the other tools honour.

This image already caps tool output: ``scripts/patch-config.py`` injects
``tool_output.max_bytes`` (default 30000) into config.yaml, resolved through
``tools/tool_output_limits.py``. But in the pinned upstream only two tools
consult it -- ``tools/terminal_tool.py`` and ``tools/file_operations.py``.
``tools/mcp_tool.py`` never imports the limits module, and
``handle_function_call`` in ``model_tools.py`` applies no central truncation
either, so an MCP tool result is returned at full size.

The consequence is an inconsistency rather than a missing feature: ``cat`` of
a 5 MB file through the terminal tool comes back trimmed to 30 KB, while an
MCP tool returning the same 5 MB lands whole in the dashboard process *and* in
the conversation history, where it is re-sent to the model on every subsequent
turn. On a 512 MB instance that is the difference between a large tool call
costing 30 KB and costing megabytes that never get released.

This patch makes MCP behave like the other tools: same config key, same
40%/60% head-tail split, same ``[OUTPUT TRUNCATED ...]`` notice, so an
operator who has already tuned ``tool_output.max_bytes`` gets MCP included
rather than exempt. No new knob is introduced and no limit is invented -- the
operator's existing setting is the only one that applies.

Only the model-facing ``content`` text is trimmed. ``structuredContent`` is
left alone: it is machine-oriented metadata, and silently rewriting it could
break a caller that parses it.

Exact-match replacement; the build fails loudly if the pinned source moves.
"""
from __future__ import annotations

import sys
from pathlib import Path

MARKER = "render-tools: MCP results honour tool_output.max_bytes"

# The line that assembles the model-facing text from the MCP content blocks.
ANCHOR = '            text_result = "\\n".join(parts) if parts else ""\n'

REPLACEMENT = '''            text_result = "\\n".join(parts) if parts else ""

            # {marker}
            #
            # terminal_tool and file_operations both trim their output to
            # tool_output.max_bytes; MCP results were the one path that did
            # not, so a chatty MCP tool could put megabytes into the agent's
            # history -- resident in the dashboard process and re-sent to the
            # model on every later turn. Same key, same 40/60 head-tail split
            # and same notice as terminal_tool, so one setting governs every
            # tool. structuredContent is deliberately untouched: it is
            # machine-oriented and a caller may parse it.
            if text_result:
                try:
                    from tools.tool_output_limits import get_max_bytes

                    _mcp_max_chars = get_max_bytes()
                except Exception:
                    # No configured limit (or the helper moved): leave the
                    # result alone rather than guessing a cap.
                    _mcp_max_chars = 0
                if _mcp_max_chars and len(text_result) > _mcp_max_chars:
                    _head = int(_mcp_max_chars * 0.4)
                    _tail = _mcp_max_chars - _head
                    _omitted = len(text_result) - _head - _tail
                    text_result = (
                        text_result[:_head]
                        + f"\\n\\n... [OUTPUT TRUNCATED - {{_omitted}} chars "
                        f"omitted out of {{len(text_result)}} total] ...\\n\\n"
                        + text_result[-_tail:]
                    )
'''.format(marker=MARKER)


def main(argv: "list[str]") -> int:
    if len(argv) != 2:
        print("usage: patch-mcp-output-cap.py <path/to/tools/mcp_tool.py>",
              file=sys.stderr)
        return 2
    path = Path(argv[1])
    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        print(f"[render-tools] {path} already patched; nothing to do")
        return 0
    occurrences = text.count(ANCHOR)
    if occurrences == 0:
        raise SystemExit(
            f"expected the MCP text_result assembly not found in {path}; the "
            "pinned Hermes source changed and this patch needs updating"
        )
    if occurrences > 1:
        # The truncation is only correct at the model-facing assembly site.
        # If upstream grows a second one, a human has to decide which needs it
        # rather than the patch guessing.
        raise SystemExit(
            f"{path} assembles text_result in {occurrences} places; this patch "
            "expected exactly one and refuses to guess which to truncate"
        )
    path.write_text(text.replace(ANCHOR, REPLACEMENT, 1), encoding="utf-8")
    print(f"[render-tools] patched {path}: MCP results now honour "
          "tool_output.max_bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
