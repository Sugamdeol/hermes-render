from __future__ import annotations

import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path


def load_patch_config():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "patch-config.py"
    spec = importlib.util.spec_from_file_location("patch_config", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules.setdefault("yaml", types.SimpleNamespace())
    spec.loader.exec_module(module)
    return module


class PatchConfigTests(unittest.TestCase):
    def test_default_render_mcp_entry_does_not_filter_tools(self):
        patch_config = load_patch_config()

        render_entry = patch_config._render_entry()

        self.assertNotIn("tools", render_entry)

    def test_render_mcp_entry_bounds_the_connect_timeout(self):
        """tui_gateway/entry.py calls discover_mcp_tools() synchronously
        before emitting `gateway.ready` -- the event the browser Chat tab
        waits on. Upstream's connect timeout is 60 s, measured to block
        discover_mcp_tools() for 60.05 s against an unreachable server, which
        would leave chat unusable for a full minute of a cold boot. The
        injected entry must therefore carry an explicit bound."""
        patch_config = load_patch_config()

        entry = patch_config._render_entry()

        self.assertIn("connect_timeout", entry)
        self.assertLess(entry["connect_timeout"], 60)
        self.assertGreater(entry["connect_timeout"], 0)
        # the server itself is untouched -- this bounds the wait, it does not
        # disable MCP
        self.assertEqual(entry["url"], "https://mcp.render.com/mcp")

    def test_render_mcp_connect_timeout_is_env_configurable(self):
        patch_config = load_patch_config()
        key = patch_config.RENDER_MCP_CONNECT_TIMEOUT_ENV
        previous = os.environ.get(key)
        try:
            os.environ[key] = "4"
            self.assertEqual(patch_config._render_entry()["connect_timeout"], 4.0)

            # an unparsable or non-positive value must fall back rather than
            # raise: this runs at boot, and a typo in one environment variable
            # should not stop the container from starting
            for bad in ("nonsense", "0", "-3", ""):
                os.environ[key] = bad
                self.assertEqual(
                    patch_config._render_entry()["connect_timeout"],
                    patch_config.RENDER_MCP_CONNECT_TIMEOUT_DEFAULT,
                    f"{bad!r} should fall back to the default",
                )
        finally:
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous

    def test_bynara_provider_uses_environment_key(self):
        patch_config = load_patch_config()

        entry = patch_config._bynara_entry()

        self.assertEqual(entry["name"], "bynara")
        self.assertEqual(entry["base_url"], "https://router.bynara.id/v1")
        self.assertEqual(entry["key_env"], "BYNARA_API_KEY")

    def test_bynara_default_only_replaces_upstream_default(self):
        patch_config = load_patch_config()
        original = os.environ.get("BYNARA_API_KEY")
        os.environ["BYNARA_API_KEY"] = "test-only-placeholder"
        try:
            config = {"model": {"default": "anthropic/claude-opus-4.6", "provider": "auto"}}
            self.assertTrue(patch_config.ensure_bynara_default(config))
            self.assertEqual(config["model"]["default"], "qwen-3.8-max-free")
            self.assertEqual(config["model"]["provider"], "custom:bynara")

            explicit = {"model": {"default": "some/model", "provider": "custom:other"}}
            self.assertFalse(patch_config.ensure_bynara_default(explicit))
            self.assertEqual(explicit["model"]["default"], "some/model")
        finally:
            if original is None:
                os.environ.pop("BYNARA_API_KEY", None)
            else:
                os.environ["BYNARA_API_KEY"] = original

    def test_free_resource_defaults_are_conservative_and_preserve_explicit_values(self):
        patch_config = load_patch_config()

        config = {}
        changed = patch_config.ensure_free_resource_defaults(config)

        self.assertIn("agent.max_turns = 30", changed)
        self.assertEqual(config["agent"]["max_turns"], 30)
        self.assertEqual(config["agent"]["api_max_retries"], 1)
        self.assertEqual(config["delegation"]["max_concurrent_children"], 1)
        self.assertEqual(config["delegation"]["max_iterations"], 20)
        self.assertEqual(config["delegation"]["max_spawn_depth"], 1)
        self.assertFalse(config["delegation"]["orchestrator_enabled"])
        self.assertEqual(config["auxiliary"]["session_search"]["max_concurrency"], 1)
        self.assertEqual(config["compression"]["threshold"], 0.40)
        self.assertEqual(config["compression"]["hygiene_hard_message_limit"], 250)
        self.assertEqual(config["code_execution"]["max_tool_calls"], 30)
        self.assertEqual(config["browser"]["inactivity_timeout"], 60)
        self.assertEqual(config["file_read_max_chars"], 50_000)

        upstream_template = {
            "agent": {"max_turns": 60},
            "delegation": {"max_iterations": 50},
            "compression": {"threshold": 0.50},
        }
        patch_config.ensure_free_resource_defaults(upstream_template, fresh=True)
        self.assertEqual(upstream_template["agent"]["max_turns"], 30)
        self.assertEqual(upstream_template["delegation"]["max_iterations"], 20)
        self.assertEqual(upstream_template["compression"]["threshold"], 0.40)

        explicit = {
            "agent": {"max_turns": 90},
            "delegation": {"max_concurrent_children": 2},
            "compression": {"threshold": 0.75},
        }
        patch_config.ensure_free_resource_defaults(explicit)
        self.assertEqual(explicit["agent"]["max_turns"], 90)
        self.assertEqual(explicit["delegation"]["max_concurrent_children"], 2)
        self.assertEqual(explicit["compression"]["threshold"], 0.75)

    def test_render_blueprint_is_free_and_has_no_persistent_disk(self):
        blueprint = (Path(__file__).resolve().parents[1] / "render.yaml").read_text()
        service = blueprint.split("services:", 1)[1]

        self.assertRegex(service, r"(?m)^    plan: free$")
        self.assertNotRegex(service, r"(?m)^    disk:")
        self.assertIn("key: HERMES_HOME", service)
        self.assertIn("key: BYNARA_API_KEY", service)
        self.assertIn("key: OPENROUTER_API_KEY", service)
        self.assertIn("key: GIT_STATE_REPO", service)
        # The in-browser Chat tab must be enabled: the hermes-chat-dashboard
        # plugin drives tui_gateway over /api/ws, which upstream gates behind
        # HERMES_DASHBOARD_TUI (the Node PTY path it also gates is never used
        # by the plugin and only spawns on demand).
        self.assertIn("key: HERMES_DASHBOARD_TUI", service)
        self.assertIn("key: HERMES_DASHBOARD_TUI\n        value: \"1\"", service)
        # The per-chat second Python interpreter (slash-command worker) stays
        # off so open conversations cannot accumulate tens of MB each.
        self.assertIn("key: HERMES_TUI_DISABLE_SLASH_WORKER", service)
        self.assertIn("key: HERMES_AGENT_CACHE_MAX_SIZE", service)
        # Git pushes are memory-capped (post buffer eagerly mallocs; pack
        # objects defaults to unbounded windows) so a sync cannot OOM the box.
        self.assertIn("key: GIT_STATE_HTTP_POST_BUFFER_MB", service)
        self.assertIn("key: GIT_STATE_PACK_WINDOW_MEMORY_MB", service)


class ToolsetDedupeTests(unittest.TestCase):
    def _config(self, enabled):
        return {"tools": {"enabled_toolsets": list(enabled)}}

    def test_web_removed_when_web_search_also_enabled(self):
        patch_config = load_patch_config()
        cfg = self._config(["code", "web", "web-search", "memory"])

        changed = patch_config.dedupe_enabled_toolsets(cfg)

        self.assertEqual(changed, ["tools.enabled_toolsets -= web (shadowed by web-search)"])
        self.assertEqual(cfg["tools"]["enabled_toolsets"], ["code", "web-search", "memory"])

    def test_web_kept_when_web_search_absent(self):
        patch_config = load_patch_config()
        cfg = self._config(["web", "code"])

        changed = patch_config.dedupe_enabled_toolsets(cfg)

        self.assertEqual(changed, [])
        self.assertEqual(cfg["tools"]["enabled_toolsets"], ["web", "code"])

    def test_noop_without_tools_section(self):
        patch_config = load_patch_config()
        self.assertEqual(patch_config.dedupe_enabled_toolsets({}), [])
        self.assertEqual(patch_config.dedupe_enabled_toolsets({"tools": {}}), [])

    def test_opt_out_env(self):
        patch_config = load_patch_config()
        old = os.environ.get("HERMES_DEDUPE_TOOLSETS")
        try:
            os.environ["HERMES_DEDUPE_TOOLSETS"] = "0"
            cfg = self._config(["web", "web-search"])
            self.assertEqual(patch_config.dedupe_enabled_toolsets(cfg), [])
            self.assertEqual(cfg["tools"]["enabled_toolsets"], ["web", "web-search"])
        finally:
            if old is None:
                os.environ.pop("HERMES_DEDUPE_TOOLSETS", None)
            else:
                os.environ["HERMES_DEDUPE_TOOLSETS"] = old
