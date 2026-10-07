"""Idempotent patch for pinned native analytics and plugin discovery."""
import ast


def patch(source):
    if '# render dashboard reliability v1' in source:
        return source
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    edits = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        old = ''.join(lines[node.lineno-1:node.end_lineno])
        new = old
        if node.name in ('get_usage_analytics', 'get_models_analytics'):
            new = new.replace('async def ', 'def ', 1)
            new = new.replace('    from hermes_state import SessionDB',
                              '    from _hermes_dashboard_runtime import AnalyticsDB, skill_usage')
            new = new.replace('    from agent.insights import InsightsEngine\n', '')
            new = new.replace('    db = SessionDB()', '    days = max(1, min(int(days), 3650))\n    db = AnalyticsDB()')
            if node.name == 'get_usage_analytics':
                start = new.index('        insights_report =')
                end = new.index('\n        return {', start)
                new = new[:start] + '        skills = skill_usage(db, cutoff)\n' + new[end:]
        elif node.name == '_discover_dashboard_plugins':
            new = new.replace('    return plugins', '    return _dashboard_runtime.validate_plugins(plugins)')
        elif node.name == 'serve_plugin_asset':
            needle = '    base = Path(plugin["_dir"])'
            replacement = '''    if file_path == '__hermes_plugin_error__.js' and plugin.get('_asset_error'):
        return Response(_dashboard_runtime.error_bundle(plugin), media_type='application/javascript')

''' + needle
            assert needle in new
            new = new.replace(needle, replacement, 1)
        if new != old:
            edits.append((node.lineno-1, node.end_lineno, new))
    assert len(edits) == 4, 'Dashboard reliability patch does not match pinned source'
    for start, end, new in reversed(edits):
        lines[start:end] = [new]
    header = '''# render dashboard reliability v1
import importlib.util as _dash_import
_dash_spec = _dash_import.spec_from_file_location('_hermes_dashboard_runtime', '/opt/render-tools/dashboard-runtime.py')
_dashboard_runtime = _dash_import.module_from_spec(_dash_spec)
sys.modules['_hermes_dashboard_runtime'] = _dashboard_runtime
_dash_spec.loader.exec_module(_dashboard_runtime)

'''
    result = ''.join(lines)
    anchor = 'app = FastAPI(title="Hermes Agent", version=__version__)\n'
    assert anchor in result
    result = result.replace(anchor, anchor + '\n' + header, 1)
    compile(result, 'web_server.py', 'exec')
    return result
