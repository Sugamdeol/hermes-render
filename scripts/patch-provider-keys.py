"""Connect dashboard key lists to the pinned Hermes credential pool."""
from pathlib import Path


def replace_once(text, old, new, label):
    if new in text:
        return text
    if old in text:
        return text.replace(old, new, 1)
    raise ValueError(f"provider-key patch no longer matches: {label}")


def patch_config(text):
    text = replace_once(text,
        '"name", "api", "url", "base_url", "api_key", "key_env", "api_key_env",',
        '"name", "api", "url", "base_url", "api_key", "api_keys", "key_env", "api_key_env",',
        "known config keys")
    old = '''    api_key = entry.get("api_key")
    if isinstance(api_key, str) and api_key.strip():
        normalized["api_key"] = api_key.strip()
'''
    new = old + '''
    api_keys = entry.get("api_keys")
    if isinstance(api_keys, list):
        normalized["api_keys"] = list(dict.fromkeys(
            key.strip() for key in api_keys if isinstance(key, str) and key.strip()
        ))
'''
    return replace_once(text, old, new, "normalize key lists")


def patch_pool(text):
    old = '''    custom_providers = config.get("custom_providers")
    if not isinstance(custom_providers, list):
        # Fall back to the v12+ providers dict via the compatibility layer
        try:
            from hermes_cli.config import get_compatible_custom_providers

            custom_providers = get_compatible_custom_providers(config)
        except Exception:
            return
'''
    new = '''    try:
        from hermes_cli.config import get_compatible_custom_providers
        custom_providers = get_compatible_custom_providers(config)
    except Exception:
        custom_providers = config.get("custom_providers")
        if not isinstance(custom_providers, list):
            return
'''
    text = replace_once(text, old, new, "canonical and legacy provider pools")
    old = '''            entry.source.startswith("env:")
            or entry.source in {"claude_code", "hermes_pkce"}
'''
    new = '''            entry.source.startswith("env:")
            or entry.source.startswith("config:")
            or entry.source in {"claude_code", "hermes_pkce", "model_config"}
'''
    text = replace_once(text, old, new, "removed key pruning")
    if "# Seed from the dashboard multi-key pool" in text:
        return text
    start = text.index("    # Seed from the custom_providers config entry's api_key field")
    end = text.index("    # Seed from model.api_key", start)
    if "dashboard multi-key pool" not in text[start:end]:
        new = '''    # Seed from the dashboard multi-key pool as well as legacy single keys.
    cp_config = _get_custom_provider_config(pool_key)
    if cp_config:
        raw_keys = cp_config.get("api_keys")
        if not isinstance(raw_keys, list):
            raw_keys = [cp_config.get("api_key")]
        keys = list(dict.fromkeys(
            key.strip() for key in raw_keys if isinstance(key, str) and key.strip()
        ))
        base_url = str(cp_config.get("base_url") or "").strip().rstrip("/")
        name = str(cp_config.get("name") or "").strip()
        import hashlib
        for index, api_key in enumerate(keys):
            fingerprint = hashlib.sha256(api_key.encode()).hexdigest()[:16]
            source = f"config:{name}:key:{fingerprint}"
            if not _is_suppressed(pool_key, source):
                active_sources.add(source)
                changed |= _upsert_entry(
                    entries, pool_key, source,
                    {"source": source, "auth_type": AUTH_TYPE_API_KEY,
                     "access_token": api_key, "base_url": base_url,
                     "label": f"{name} key {index + 1}"},
                )

'''
        text = text[:start] + new + text[end:]
    return text


def patch(root):
    root = Path(root)
    for relative, transform in (("hermes_cli/config.py", patch_config),
                                ("agent/credential_pool.py", patch_pool)):
        path = root / relative
        text = transform(path.read_text())
        compile(text, str(path), "exec")
        path.write_text(text)


if __name__ == "__main__":
    import sys
    patch(sys.argv[1])
