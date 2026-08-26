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

    def test_render_blueprint_is_free_and_has_no_persistent_disk(self):
        blueprint = (Path(__file__).resolve().parents[1] / "render.yaml").read_text()
        service = blueprint.split("services:", 1)[1]

        self.assertRegex(service, r"(?m)^    plan: free$")
        self.assertNotRegex(service, r"(?m)^    disk:")
        self.assertIn("key: HERMES_HOME", service)
        self.assertIn("key: BYNARA_API_KEY", service)
        self.assertIn("key: OPENROUTER_API_KEY", service)
        self.assertIn("key: HERMES_STORAGE_ENDPOINT_URL", service)
