"""Use the selected custom provider's multi-key pool for auxiliary requests."""


def configured_pool(provider, base_url):
    from hermes_cli.runtime_provider import _get_named_custom_provider
    from agent.credential_pool import custom_provider_pool_key_candidates, load_pool
    entry = _get_named_custom_provider(provider)
    if not isinstance(entry, dict) or entry.get('key_cmd'):
        return None
    keys = entry.get('api_keys')
    if not isinstance(keys, list) or not any(isinstance(k, str) and k.strip() for k in keys):
        return None
    expected = str(entry.get('base_url') or '').strip().rstrip('/')
    # Exact endpoint matching: never borrow a configured key for an override URL.
    if not expected or str(base_url or '').strip().rstrip('/') != expected:
        return None
    name = entry.get('provider_key') or entry.get('name') or provider
    for pool_key in custom_provider_pool_key_candidates(expected, name):
        pool = load_pool(pool_key)
        if pool and pool.has_credentials():
            return pool_key, pool
    return None


def selected_key(binding):
    entry = binding[1].select()
    if entry is None:
        raise RuntimeError('All configured keys for this provider are unavailable. Check provider credit or key status.')
    key = getattr(entry, 'runtime_api_key', None) or getattr(entry, 'access_token', '')
    if not isinstance(key, str) or not key.strip():
        raise RuntimeError('Selected auxiliary credential has no API key')
    return key.strip()
