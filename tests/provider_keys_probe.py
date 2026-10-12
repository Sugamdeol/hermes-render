"""Exercise dashboard key lists through the real installed Hermes runtime."""
import os
import tempfile


def main():
    with tempfile.TemporaryDirectory(prefix="hermes-provider-probe-") as home:
        os.environ["HERMES_HOME"] = home
        from hermes_cli.config import save_config, get_compatible_custom_providers
        from hermes_cli.runtime_provider import resolve_runtime_provider
        from agent.credential_pool import load_pool
        config = {"custom_providers": [], "providers": {
            "probe": {"name": "probe", "base_url": "https://probe.invalid/v1",
                      "api_keys": ["probe-one", "probe-two", "probe-three"],
                      "default_model": "manual-model", "models": {"manual-model": {}},
                      "discover_models": False},
            "separate": {"name": "separate", "base_url": "https://probe.invalid/v1",
                         "api_keys": ["separate-key"], "discover_models": False},
        }}
        save_config(config)
        compatible = get_compatible_custom_providers(config)
        assert compatible[0]["api_keys"] == ["probe-one", "probe-two", "probe-three"]
        runtime = resolve_runtime_provider(requested="custom:probe")
        pool = runtime["credential_pool"]
        assert runtime["api_key"] == "probe-one"
        assert len(pool.entries()) == 3
        assert pool.mark_exhausted_and_rotate(status_code=429).runtime_api_key == "probe-two"
        assert pool.mark_exhausted_and_rotate(status_code=401).runtime_api_key == "probe-three"
        assert pool.mark_exhausted_and_rotate(status_code=402) is None
        separate = resolve_runtime_provider(requested="custom:separate")
        assert separate["api_key"] == "separate-key"
        config["providers"]["probe"]["api_keys"] = ["replacement-key"]
        save_config(config)
        pool = load_pool("custom:probe")
        assert [entry.runtime_api_key for entry in pool.entries()] == ["replacement-key"]
        assert pool.select().runtime_api_key == "replacement-key"
        from hermes_cli import model_switch, models
        original = models.fetch_api_models
        try:
            models.fetch_api_models = lambda *a, **k: ["advertised-model"]
            config["providers"]["probe"]["discover_models"] = True
            rows = model_switch.list_authenticated_providers(
                user_providers=config["providers"], custom_providers=[], max_models=500)
            row = next(row for row in rows if row["slug"] == "custom:probe")
            assert "manual-model" in row["models"]
            assert "advertised-model" in row["models"]
        finally:
            models.fetch_api_models = original
    print("Native provider keys: resolution, 429/401/402 failover, removal, isolation, manual IDs OK")


if __name__ == "__main__":
    main()
