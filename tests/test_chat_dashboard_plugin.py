import importlib.util
import sys
import types
from pathlib import Path


def load_chat_plugin():
    # Minimal upstream/FastAPI stubs; these tests exercise the pure helper layer only.

    fastapi = types.ModuleType("fastapi")
    class HTTPException(Exception):
        def __init__(self, status_code=500, detail=""):
            super().__init__(detail); self.status_code = status_code; self.detail = detail
    class APIRouter:
        def get(self, *a, **k): return lambda fn: fn
        def post(self, *a, **k): return lambda fn: fn
        def put(self, *a, **k): return lambda fn: fn
        def delete(self, *a, **k): return lambda fn: fn
    fastapi.APIRouter = APIRouter
    fastapi.HTTPException = HTTPException
    fastapi.Request = object
    fastapi.UploadFile = object
    fastapi.File = lambda *a, **k: None
    fastapi.Form = lambda *a, **k: None
    responses = types.ModuleType("fastapi.responses")
    responses.PlainTextResponse = lambda content, media_type=None: content
    sys.modules["fastapi"] = fastapi
    sys.modules["fastapi.responses"] = responses
    web_server = types.ModuleType("hermes_cli.web_server")
    web_server._has_valid_session_token = lambda request: True
    config_mod = types.ModuleType("hermes_cli.config")
    config_mod.load_config = lambda: {}
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.web_server = web_server
    hermes_cli.config = config_mod
    sys.modules["hermes_cli"] = hermes_cli
    sys.modules["hermes_cli.web_server"] = web_server
    sys.modules["hermes_cli.config"] = config_mod
    module_path = (
        Path(__file__).resolve().parents[1]
        / "dashboard-plugins"
        / "hermes-chat-dashboard"
        / "dashboard"
        / "plugin_api.py"
    )
    spec = importlib.util.spec_from_file_location("hermes_chat_dashboard", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_default_modes_are_capability_descriptors_not_frontend_literals():
    mod = load_chat_plugin()
    modes = mod._default_modes([{"name": "web"}, {"name": "terminal"}])
    ids = [m["id"] for m in modes]
    assert ids[:4] == ["fast", "reasoning", "research", "coding"]
    assert all(m.get("strategy") for m in modes)
    assert "web" in modes[2]["recommended_tools"]


def test_configured_modes_override_defaults():
    mod = load_chat_plugin()
    cfg = {"chat_dashboard": {"modes": [{"id": "custom", "label": "Custom"}]}}
    assert mod._configured_modes(cfg) == [{"id": "custom", "label": "Custom"}]


def test_safe_filename_blocks_path_traversal_and_weird_chars():
    mod = load_chat_plugin()
    assert mod._safe_filename("../../secret?.txt") == "secret_.txt"
    assert mod._safe_filename("!!!") == "upload"


# ---------------------------------------------------------------------------
# Transcript builder: chronological turns with tool steps attached to the
# assistant turn that issued them (the sidebar/history view depends on this).
# ---------------------------------------------------------------------------

def _rows():
    return [
        {"id": 1, "role": "user", "content": "check the service", "timestamp": 100.0},
        {"id": 2, "role": "assistant", "content": None, "timestamp": 101.0,
         "tool_calls": [{"id": "c1", "function": {"name": "terminal", "arguments": "{\"command\": \"ls -la\"}"}}]},
        {"id": 3, "role": "tool", "content": "total 0", "tool_call_id": "c1", "tool_name": "terminal", "timestamp": 102.5},
        {"id": 4, "role": "assistant", "content": "", "timestamp": 103.0,
         "tool_calls": [{"id": "c2", "function": {"name": "web_search", "arguments": "{\"query\": \"render status\"}"}}]},
        {"id": 5, "role": "tool", "content": "x" * 5000, "tool_call_id": "c2", "timestamp": 104.0},
        {"id": 6, "role": "assistant", "content": "All good.", "timestamp": 105.0, "reasoning": "checked twice"},
        {"id": 7, "role": "user", "content": [{"type": "text", "text": "thanks"}, {"type": "image_url", "image_url": {"url": "data:..."}}], "timestamp": 106.0},
    ]


def test_build_transcript_groups_tools_in_call_order_under_visible_reply():
    mod = load_chat_plugin()
    turns = mod._build_transcript(_rows())
    assert [t["role"] for t in turns] == ["user", "assistant", "user"]
    reply = turns[1]
    assert reply["content"] == "All good."
    assert reply["reasoning"] == "checked twice"
    names = [t["name"] for t in reply["tools"]]
    assert names == ["terminal", "web_search"]  # oldest first, never newest-on-top
    first = reply["tools"][0]
    assert first["status"] == "complete"
    assert first["result"] == "total 0"
    assert first["context"]  # argument preview present
    assert abs(first["duration_s"] - 1.5) < 1e-6
    assert "args" not in first  # raw args not shipped to the browser
    # long tool output is clipped with a marker
    assert reply["tools"][1]["result"].endswith("more chars)")
    assert len(reply["tools"][1]["result"]) < 5000
    # multi-part content flattened
    assert turns[2]["content"] == "thanks\n[image]"


def test_build_transcript_surfaces_tool_only_turns_without_reply():
    mod = load_chat_plugin()
    rows = _rows()[:3] + [{"id": 9, "role": "user", "content": "you stopped?", "timestamp": 110.0}]
    turns = mod._build_transcript(rows)
    assert [t["role"] for t in turns] == ["user", "assistant", "user"]
    assert turns[1]["incomplete"] is True
    assert turns[1]["content"] == ""
    assert turns[1]["tools"][0]["name"] == "terminal"


def test_build_transcript_handles_orphan_tool_rows_and_bad_json():
    mod = load_chat_plugin()
    rows = [
        {"id": 1, "role": "user", "content": "hi", "timestamp": 1.0},
        {"id": 2, "role": "assistant", "content": "", "timestamp": 2.0,
         "tool_calls": [{"id": "c9", "function": {"name": "todo", "arguments": "{not json"}}]},
        {"id": 3, "role": "tool", "content": "orphan", "tool_call_id": "missing", "tool_name": "mystery", "timestamp": 3.0},
        {"id": 4, "role": "assistant", "content": "done", "timestamp": 4.0},
    ]
    turns = mod._build_transcript(rows)
    tools = turns[1]["tools"]
    assert [t["name"] for t in tools] == ["todo", "mystery"]
    assert tools[1]["result"] == "orphan"


def test_args_preview_prefers_meaningful_keys():
    mod = load_chat_plugin()
    assert mod._args_preview("terminal", {"command": "echo   hi\nthere"}) == "echo hi there"
    long = mod._args_preview("x", {"query": "q" * 500})
    assert len(long) <= 160 and long.endswith("…")
    assert mod._args_preview("x", {}) == ""


def test_content_text_flattens_parts():
    mod = load_chat_plugin()
    assert mod._content_text([{"type": "text", "text": "a"}, {"type": "input_audio"}]) == "a\n[audio]"
    assert mod._content_text(None) == ""
    assert mod._content_text(3) == "3"
