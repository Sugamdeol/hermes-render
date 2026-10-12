"""Port custom dashboard asset diagnostics to the current native module layout."""
from pathlib import Path


def patch(root):
    root = Path(root)
    path = root / 'hermes_cli/web_server_dashboard.py'
    text = path.read_text()
    marker = '# Colab dashboard asset recovery'
    if marker not in text:
        start = text.index('def _discover_dashboard_plugins(')
        end = text.index('\n\n', text.index('    return plugins', start))
        tail = text[start:end]
        replacement = '''    # Colab dashboard asset recovery
    import importlib.util
    import sys
    module = sys.modules.get('_hermes_dashboard_runtime')
    if module is None:
        spec = importlib.util.spec_from_file_location('_hermes_dashboard_runtime', '/opt/render-tools/dashboard-runtime.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules['_hermes_dashboard_runtime'] = module
        spec.loader.exec_module(module)
    return module.validate_plugins(plugins)'''
        assert tail.count('    return plugins') == 1
        text = text[:start] + tail.replace('    return plugins', replacement, 1) + text[end:]
        compile(text, str(path), 'exec')
        path.write_text(text)
    path = root / 'hermes_cli/web_routers/dashboard_ui.py'
    text = path.read_text()
    marker = '# Colab dashboard diagnostic bundle'
    if marker not in text:
        start = text.index('async def serve_plugin_asset(')
        anchor = '    base = Path(plugin["_dir"])'
        pos = text.index(anchor, start)
        addition = '''    # Colab dashboard diagnostic bundle
    if file_path == '__hermes_plugin_error__.js' and plugin.get('_asset_error'):
        from fastapi.responses import Response
        from _hermes_dashboard_runtime import error_bundle
        return Response(error_bundle(plugin), media_type='application/javascript',
                        headers={"Cache-Control": "no-store"})

'''
        text = text[:pos] + addition + text[pos:]
        compile(text, str(path), 'exec')
        path.write_text(text)
