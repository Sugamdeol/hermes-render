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


def test_content_text_flattens_openai_style_parts():
    mod = load_chat_plugin()
    assert mod._content_text(None) == ""
    assert mod._content_text("plain") == "plain"
    assert (
        mod._content_text(
            [
                {"type": "text", "text": "hello"},
                {"type": "image_url", "image_url": {"url": "x"}},
                {"type": "input_audio"},
            ]
        )
        == "hello\n[image]\n[audio]"
    )
    assert mod._content_text({"type": "output_text", "text": "ok"}) == "ok"
    assert mod._content_text({"type": "weird"}) == "[weird]"
