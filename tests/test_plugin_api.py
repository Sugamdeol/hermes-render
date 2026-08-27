from __future__ import annotations

import asyncio
import copy
import importlib.util
import sys
import types
import unittest
from pathlib import Path


# ---------------------------------------------------------------------------
# Loading the plugin backend with stubbed third-party modules (fastapi and
# hermes_cli), mirroring the pattern in test_patch_config.py. The stubs are
# forced so the tests stay hermetic whether or not those packages happen to
# be installed in the test environment.
# ---------------------------------------------------------------------------

class _FakeRouter:
    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], object] = {}

    def _register(self, method: str, path: str):
        def decorator(fn):
            self.routes[(method, path)] = fn
            return fn

        return decorator

    def get(self, path: str):
        return self._register("GET", path)

    def post(self, path: str):
        return self._register("POST", path)

    def delete(self, path: str):
        return self._register("DELETE", path)


class _FakeHTTPException(Exception):
    def __init__(self, status_code: int, detail: str = "") -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _install_stubs(
    env: dict,
    *,
    token_ok: bool = True,
    config_state: dict | None = None,
    auth_available: bool = True,
) -> None:
    env["saved"] = []
    env["token_ok"] = token_ok
    env["config_state"] = env.get("config_state", copy.deepcopy(config_state or {}))

    fastapi_stub = types.ModuleType("fastapi")
    fastapi_stub.APIRouter = _FakeRouter
    fastapi_stub.HTTPException = _FakeHTTPException
    fastapi_stub.Request = object
    sys.modules["fastapi"] = fastapi_stub

    hermes_cli = types.ModuleType("hermes_cli")
    web_server = types.ModuleType("hermes_cli.web_server")
    config_mod = types.ModuleType("hermes_cli.config")

    if auth_available:
        web_server._has_valid_session_token = lambda request: token_ok

    def load_config() -> dict:
        return copy.deepcopy(env["config_state"])

    def save_config(config: dict) -> None:
        env["saved"].append(copy.deepcopy(config))
        env["config_state"] = copy.deepcopy(config)

    config_mod.load_config = load_config
    config_mod.save_config = save_config
    hermes_cli.config = config_mod
    hermes_cli.web_server = web_server
    sys.modules["hermes_cli"] = hermes_cli
    sys.modules["hermes_cli.web_server"] = web_server
    sys.modules["hermes_cli.config"] = config_mod


def load_plugin_api(env: dict):
    _install_stubs(env, config_state=env.get("config_state", {}))
    module_path = (
        Path(__file__).resolve().parents[1]
        / "dashboard-plugins"
        / "render-api-providers"
        / "dashboard"
        / "plugin_api.py"
    )
    spec = importlib.util.spec_from_file_location("render_api_providers", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def call_route(handler, *args):
    return asyncio.run(handler(*args))


class ProviderKeyTests(unittest.TestCase):
    def setUp(self):
        self.env = {}
        self.mod = load_plugin_api(self.env)

    def test_kebab_case_from_display_name(self):
        self.assertEqual(self.mod.provider_key_from_name("My Local LLM"), "my-local-llm")

    def test_plain_name_passes_through(self):
        self.assertEqual(self.mod.provider_key_from_name("bynara"), "bynara")

    def test_no_usable_characters_rejected(self):
        with self.assertRaises(ValueError):
            self.mod.provider_key_from_name("!!!")

    def test_too_long_rejected(self):
        with self.assertRaises(ValueError):
            self.mod.provider_key_from_name("a" * 128)


class NormalizationTests(unittest.TestCase):
    def setUp(self):
        self.env = {}
        self.mod = load_plugin_api(self.env)

    def test_base_url_trailing_slash_stripped(self):
        self.assertEqual(
            self.mod.normalize_base_url("https://api.example.com/v1/"),
            "https://api.example.com/v1",
        )

    def test_base_url_requires_http_scheme_and_host(self):
        for bad in ("", "not-a-url", "ftp://example.com/v1", "https://"):
            with self.assertRaises(ValueError, msg=bad):
                self.mod.normalize_base_url(bad)

    def test_api_mode_validation(self):
        self.assertEqual(self.mod.normalize_api_mode("Chat_Completions"), "chat_completions")
        self.assertEqual(self.mod.normalize_api_mode(""), "")
        with self.assertRaises(ValueError):
            self.mod.normalize_api_mode("gpt")

    def test_key_env_validation(self):
        self.assertEqual(self.mod.normalize_key_env("MY_KEY"), "MY_KEY")
        self.assertEqual(self.mod.normalize_key_env(""), "")
        with self.assertRaises(ValueError):
            self.mod.normalize_key_env("1KEY")
        with self.assertRaises(ValueError):
            self.mod.normalize_key_env("MY-KEY")


class ListEntriesTests(unittest.TestCase):
    def setUp(self):
        self.env = {}
        self.mod = load_plugin_api(self.env)

    def test_providers_dict_and_legacy_list_dedup(self):
        config = {
            "providers": {
                "bynara": {
                    "name": "bynara",
                    "base_url": "https://router.bynara.id/v1",
                    "key_env": "BYNARA_API_KEY",
                }
            },
            "custom_providers": [
                # Same (name, base_url) pair in the legacy list — must be
                # skipped so the runtime never sees a stale duplicate.
                {
                    "name": "bynara",
                    "base_url": "https://router.bynara.id/v1",
                    "key_env": "BYNARA_API_KEY",
                }
            ],
        }
        entries = self.mod.list_custom_provider_entries(config)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["key"], "bynara")
        self.assertEqual(entries[0]["source"], "providers")
        self.assertFalse(entries[0]["has_api_key"])

    def test_legacy_entry_normalizes_aliases(self):
        config = {
            "custom_providers": [
                {
                    "name": "Work",
                    "url": "https://gpu.example.com/v1",
                    "api_key_env": "CORP_KEY",
                    "api_mode": "anthropic_messages",
                    "default_model": "claude-sonnet-4",
                    "api_key": "sk-abc",
                }
            ]
        }
        entries = self.mod.list_custom_provider_entries(config)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["key"], "work")
        self.assertEqual(entry["base_url"], "https://gpu.example.com/v1")
        self.assertEqual(entry["key_env"], "CORP_KEY")
        self.assertEqual(entry["api_mode"], "anthropic_messages")
        self.assertEqual(entry["model"], "claude-sonnet-4")
        self.assertTrue(entry["has_api_key"])
        # Raw keys never surface in the listing shape.
        self.assertNotIn("api_key", entry)

    def test_entries_without_url_or_name_are_skipped(self):
        config = {
            "providers": {
                "no-url": {"name": "No URL"},
                "broken": "not-a-dict",
            },
            "custom_providers": [{"base_url": "https://x.example/v1"}],
        }
        self.assertEqual(self.mod.list_custom_provider_entries(config), [])


class UpsertTests(unittest.TestCase):
    def setUp(self):
        self.env = {}
        self.mod = load_plugin_api(self.env)

    def test_add_writes_canonical_providers_entry(self):
        config: dict = {}
        key = self.mod.upsert_custom_provider_entry(
            config,
            {
                "name": "Together",
                "base_url": "https://api.together.xyz/v1",
                "api_key": "sk-123",
                "key_env": "",
                "api_mode": "chat_completions",
                "model": "MiniMaxAI/MiniMax-M2.7",
                "key": "",
            },
        )
        self.assertEqual(key, "together")
        entry = config["providers"]["together"]
        self.assertEqual(entry["name"], "Together")
        self.assertEqual(entry["base_url"], "https://api.together.xyz/v1")
        self.assertEqual(entry["api_key"], "sk-123")
        self.assertEqual(entry["api_mode"], "chat_completions")
        self.assertEqual(entry["default_model"], "MiniMaxAI/MiniMax-M2.7")

    def test_update_preserves_unknown_fields_and_blank_key(self):
        config = {
            "providers": {
                "together": {
                    "name": "Together",
                    "base_url": "https://old.example/v1",
                    "api_key": "sk-old",
                    "models": {"gpt-4o": {"context_length": 128000}},
                }
            }
        }
        key = self.mod.upsert_custom_provider_entry(
            config,
            {
                "name": "Together",
                "base_url": "https://new.example/v1",
                "api_key": "",  # blank = keep current key
                "key_env": "TOGETHER_API_KEY",
                "api_mode": "",
                "model": "",
                "key": "",
            },
        )
        self.assertEqual(key, "together")
        entry = config["providers"]["together"]
        self.assertEqual(entry["base_url"], "https://new.example/v1")
        self.assertEqual(entry["api_key"], "sk-old")
        self.assertEqual(entry["key_env"], "TOGETHER_API_KEY")
        self.assertEqual(entry["models"], {"gpt-4o": {"context_length": 128000}})

    def test_key_collision_with_different_name_rejected(self):
        config = {"providers": {"other": {"name": "Other", "base_url": "https://a.example/v1"}}}
        with self.assertRaises(LookupError):
            self.mod.upsert_custom_provider_entry(
                config,
                {"name": "other!!", "base_url": "https://b.example/v1", "api_key": "",
                 "key_env": "", "api_mode": "", "model": "", "key": "other"},
            )

    def test_legacy_list_entry_updated_in_place(self):
        config = {
            "custom_providers": [
                {"name": "Local", "base_url": "http://127.0.0.1:11434/v1"}
            ]
        }
        key = self.mod.upsert_custom_provider_entry(
            config,
            {"name": "Local", "base_url": "http://127.0.0.1:8080/v1", "api_key": "",
             "key_env": "", "api_mode": "", "model": "qwen3.5:27b", "key": ""},
        )
        self.assertEqual(key, "local")
        self.assertIn("providers", config)
        legacy = config["custom_providers"]
        self.assertEqual(len(legacy), 1)
        self.assertEqual(legacy[0]["base_url"], "http://127.0.0.1:8080/v1")
        self.assertEqual(legacy[0]["model"], "qwen3.5:27b")


class RemoveTests(unittest.TestCase):
    def setUp(self):
        self.env = {}
        self.mod = load_plugin_api(self.env)

    def test_removes_from_both_schemas(self):
        config = {
            "providers": {"local": {"name": "Local", "base_url": "https://a.example/v1"}},
            "custom_providers": [{"name": "Local", "base_url": "https://a.example/v1"}],
        }
        self.assertTrue(self.mod.remove_custom_provider_entry(config, "local"))
        self.assertNotIn("local", config["providers"])
        self.assertEqual(config["custom_providers"], [])

    def test_missing_provider_is_noop(self):
        config = {"providers": {"keep": {"name": "Keep", "base_url": "https://k.example/v1"}}}
        self.assertFalse(self.mod.remove_custom_provider_entry(config, "nope"))
        self.assertIn("keep", config["providers"])


class MainUsageTests(unittest.TestCase):
    def setUp(self):
        self.env = {}
        self.mod = load_plugin_api(self.env)

    def test_main_provider_reference_matches_by_key_and_name(self):
        config = {"model": {"provider": "custom:bynara", "default": "qwen"}}
        self.assertTrue(self.mod.provider_in_use_as_main(config, "bynara"))
        self.assertFalse(self.mod.provider_in_use_as_main(config, "together"))

        # Display-name reference (spaces) normalizes to the same key.
        config2 = {"model": {"provider": "custom:My Local LLM"}}
        self.assertTrue(self.mod.provider_in_use_as_main(config2, "my-local-llm"))

        config3 = {"model": {"provider": "openrouter"}}
        self.assertFalse(self.mod.provider_in_use_as_main(config3, "bynara"))


class RouteTests(unittest.TestCase):
    """Exercise the FastAPI route handlers against the stubbed backend."""

    def setUp(self):
        self.env = {"config_state": {}}
        self.mod = load_plugin_api(self.env)
        self.request = object()

    def _routes(self):
        return self.mod.router.routes

    def test_list_requires_token(self):
        _install_stubs(self.env, token_ok=False)
        handler = self._routes()[("GET", "/custom-providers")]
        with self.assertRaises(_FakeHTTPException) as caught:
            call_route(handler, self.request)
        self.assertEqual(caught.exception.status_code, 401)

    def test_list_returns_providers_and_main(self):
        self.env["config_state"] = {
            "providers": {
                "bynara": {"name": "bynara", "base_url": "https://router.bynara.id/v1",
                           "key_env": "BYNARA_API_KEY"}
            },
            "model": {"provider": "custom:bynara", "default": "qwen-3.8-max-free"},
        }
        env = self.env
        self.mod = load_plugin_api(env)
        handler = self.mod.router.routes[("GET", "/custom-providers")]
        result = call_route(handler, self.request)
        self.assertEqual(result["main_provider"], "custom:bynara")
        self.assertEqual(len(result["providers"]), 1)
        self.assertEqual(result["providers"][0]["key"], "bynara")

    def test_add_provider_persists_and_reports_ref(self):
        handler = self._routes()[("POST", "/custom-providers")]
        result = call_route(
            handler,
            self.request,
            {
                "name": "Together",
                "base_url": "https://api.together.xyz/v1/",
                "key_env": "TOGETHER_API_KEY",
                "model": "MiniMaxAI/MiniMax-M2.7",
            },
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["provider_ref"], "custom:together")
        self.assertEqual(len(self.env["saved"]), 1)
        self.assertIn("together", self.env["saved"][0]["providers"])
        # Raw keys must never be echoed back.
        self.assertNotIn("api_key", result)

    def test_add_provider_validates_input(self):
        handler = self._routes()[("POST", "/custom-providers")]
        for body in (
            {"name": "x", "base_url": "not-a-url"},
            {"name": "", "base_url": "https://a.example/v1"},
            {"name": "x", "base_url": "https://a.example/v1", "api_mode": "bogus"},
            {"name": "x", "base_url": "https://a.example/v1", "key_env": "9BAD"},
        ):
            with self.assertRaises(_FakeHTTPException) as caught:
                call_route(handler, self.request, body)
            self.assertEqual(caught.exception.status_code, 400)

    def test_add_provider_conflict_on_taken_key(self):
        self.env["config_state"] = {
            "providers": {"other": {"name": "Other", "base_url": "https://a.example/v1"}}
        }
        self.mod = load_plugin_api(self.env)
        handler = self.mod.router.routes[("POST", "/custom-providers")]
        with self.assertRaises(_FakeHTTPException) as caught:
            call_route(
                handler,
                self.request,
                {"name": "other!!", "base_url": "https://b.example/v1"},
            )
        self.assertEqual(caught.exception.status_code, 409)

    def test_delete_in_use_provider_is_rejected(self):
        self.env["config_state"] = {
            "providers": {
                "bynara": {"name": "bynara", "base_url": "https://router.bynara.id/v1"}
            },
            "model": {"provider": "custom:bynara", "default": "qwen"},
        }
        self.mod = load_plugin_api(self.env)
        handler = self.mod.router.routes[("DELETE", "/custom-providers/{key}")]
        with self.assertRaises(_FakeHTTPException) as caught:
            call_route(handler, self.request, "bynara")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.env["saved"], [])

    def test_delete_unknown_provider_is_404(self):
        handler = self._routes()[("DELETE", "/custom-providers/{key}")]
        with self.assertRaises(_FakeHTTPException) as caught:
            call_route(handler, self.request, "ghost")
        self.assertEqual(caught.exception.status_code, 404)

    def test_delete_removes_and_saves(self):
        self.env["config_state"] = {
            "providers": {
                "local": {"name": "Local", "base_url": "http://127.0.0.1:11434/v1"}
            }
        }
        self.mod = load_plugin_api(self.env)
        handler = self.mod.router.routes[("DELETE", "/custom-providers/{key}")]
        result = call_route(handler, self.request, "local")
        self.assertTrue(result["ok"])
        self.assertNotIn("local", self.env["saved"][0]["providers"])

    def test_auth_backend_missing_fails_closed(self):
        # No _has_valid_session_token on the stub → routes must refuse
        # (503) rather than open up.
        web_stub = sys.modules["hermes_cli.web_server"]
        del web_stub._has_valid_session_token
        handler = self._routes()[("GET", "/custom-providers")]
        with self.assertRaises(_FakeHTTPException) as caught:
            call_route(handler, self.request)
        self.assertEqual(caught.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
