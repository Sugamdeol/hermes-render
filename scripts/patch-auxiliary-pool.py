"""Keep custom auxiliary compression on the same working credential pool as chat."""
from pathlib import Path


def patch(root):
    root = Path(root)
    path = root / 'agent/auxiliary_client.py'
    text = path.read_text()
    marker = '# Dashboard auxiliary multi-key recovery'
    if marker in text:
        return
    replacements = [
        ('    custom_key: Any = (custom_entry.get("api_key") or "").strip()',
         '''    # Dashboard auxiliary multi-key recovery
    from agent.dashboard_aux_pool import configured_pool, selected_key
    binding = configured_pool(provider, custom_base)
    if binding is not None:
        return selected_key(binding)
    custom_key: Any = (custom_entry.get("api_key") or "").strip()'''),
        ('    resolved_provider = main_provider\n    explicit_base_url = runtime_base_url or None',
         '''    from agent.dashboard_aux_pool import configured_pool, selected_key
    binding = configured_pool(main_provider, runtime_base_url)
    if binding is not None:
        runtime_api_key = selected_key(binding)
    resolved_provider = main_provider
    explicit_base_url = runtime_base_url or None'''),
        ('    if main_chain_label and _is_provider_unhealthy(main_chain_label, health_base_url):',
         '    if binding is None and main_chain_label and _is_provider_unhealthy(main_chain_label, health_base_url):'),
        ('    if normalized not in {"", "auto", "custom"}:\n        return normalized',
         '''    from agent.dashboard_aux_pool import configured_pool
    binding = configured_pool(_effective_provider_for_client(client, normalized), base)
    if binding is not None:
        return binding[0]
    if normalized not in {"", "auto", "custom"}:
        return normalized'''),
        ('        pool = load_pool(normalized)\n    except Exception as load_exc:',
         '        pool = load_pool(provider)\n    except Exception as load_exc:'),
        ('        _evict_cached_clients(normalized)\n        return True\n\n    if _is_auth_error(exc):',
         '''        _evict_cached_clients(normalized)
        # Auto clients retain the concrete provider's old key too. Eviction is
        # profile-scoped and retires clients without closing concurrent requests.
        _evict_cached_clients("auto")
        return True

    if _is_auth_error(exc):'''),
    ]
    for old, new in replacements:
        if text.count(old) != 1:
            raise ValueError('Auxiliary pool compatibility anchor changed: ' + old[:70])
        text = text.replace(old, new, 1)
    compile(text, str(path), 'exec')
    helper = Path(__file__).with_name('auxiliary-provider-pool.py').read_text()
    compile(helper, 'dashboard_aux_pool.py', 'exec')
    (root / 'agent/dashboard_aux_pool.py').write_text(helper)
    path.write_text(text)


if __name__ == '__main__':
    import sys
    patch(sys.argv[1])
