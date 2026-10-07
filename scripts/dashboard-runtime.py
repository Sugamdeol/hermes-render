"""Read-only analytics and safe dashboard plugin asset discovery."""
import json
import logging
from pathlib import Path
import shutil
import sqlite3
import subprocess

log = logging.getLogger(__name__)


class AnalyticsDB:
    """Never run schema migrations or WAL checkpoints while viewing charts."""
    def __init__(self, path=None):
        if path is None:
            from hermes_state import DEFAULT_DB_PATH
            path = DEFAULT_DB_PATH
        self._conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro',
                                     uri=True, timeout=5)
        self._conn.row_factory = sqlite3.Row
        try:
            columns = {r['name'] for r in self._conn.execute('PRAGMA table_info(sessions)')}
            if not columns:
                raise sqlite3.OperationalError('sessions table is unavailable')
            # Old backups may predate optional cost/usage counters. Supply zero
            # only for absent columns, in a temporary view; never alter state.db.
            numeric = ('input_tokens', 'output_tokens', 'cache_read_tokens',
                       'cache_write_tokens', 'reasoning_tokens', 'estimated_cost_usd',
                       'actual_cost_usd', 'api_call_count', 'tool_call_count')
            optional = {c: '0' for c in numeric}
            optional.update(model='NULL', billing_provider='NULL')
            extras = [f'{value} AS "{name}"' for name, value in optional.items()
                      if name not in columns]
            if extras:
                self._conn.execute('CREATE TEMP VIEW sessions AS SELECT *, ' +
                                   ', '.join(extras) + ' FROM main.sessions')
        except BaseException:
            self._conn.close()
            raise

    def close(self):
        self._conn.close()


def skill_usage(db, cutoff):
    """Chart only skill calls, without computing an entire insights report."""
    skills = {}
    columns = {r['name'] for r in db._conn.execute('PRAGMA table_info(messages)')}
    if not {'session_id', 'role', 'tool_calls'}.issubset(columns):
        return {'summary': {'total_skill_loads': 0, 'total_skill_edits': 0,
                            'total_skill_actions': 0, 'distinct_skills_used': 0},
                'top_skills': [], 'available': False}
    timestamp = 'm.timestamp' if 'timestamp' in columns else 'NULL AS timestamp'
    for row in db._conn.execute(f'''SELECT m.tool_calls, {timestamp} FROM messages m
            JOIN sessions s ON s.id=m.session_id WHERE s.started_at >= ?
            AND m.role='assistant' AND m.tool_calls IS NOT NULL''', (cutoff,)):
        try:
            calls = json.loads(row['tool_calls'])
        except (ValueError, TypeError):
            continue
        if not isinstance(calls, list):
            continue
        for call in calls:
            fn = call.get('function') if isinstance(call, dict) else None
            if not isinstance(fn, dict) or fn.get('name') not in ('skill_view', 'skill_manage'):
                continue
            args = fn.get('arguments')
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    continue
            name = args.get('name') if isinstance(args, dict) else None
            if not isinstance(name, str) or not name.strip():
                continue
            item = skills.setdefault(name, dict(skill=name, view_count=0,
                                               manage_count=0, last_used_at=None))
            item['view_count' if fn['name'] == 'skill_view' else 'manage_count'] += 1
            stamp = row['timestamp']
            if isinstance(stamp, (float, int)):
                item['last_used_at'] = max(item['last_used_at'] or 0, stamp)
    rows = sorted(skills.values(), key=lambda s: s['view_count'] + s['manage_count'], reverse=True)
    loads = sum(r['view_count'] for r in rows)
    edits = sum(r['manage_count'] for r in rows)
    for r in rows:
        r['total_count'] = r['view_count'] + r['manage_count']
        r['percentage'] = r['total_count'] / (loads + edits) * 100 if loads + edits else 0
    return {'summary': {'total_skill_loads': loads, 'total_skill_edits': edits,
                        'total_skill_actions': loads + edits, 'distinct_skills_used': len(rows)},
            'top_skills': rows[:20]}


_syntax_cache = {}


def asset_path(base, relative):
    if not isinstance(relative, str) or not relative or '\\' in relative:
        return None
    target = (base / relative).resolve()
    return target if target.is_relative_to(base.resolve()) and target.is_file() else None


def syntax_error(path):
    node = shutil.which('node')
    if not node:
        return None
    info = path.stat()
    if info.st_size > 2 * 1024 * 1024:
        return 'Plugin bundle exceeds the 2 MiB validation limit; split or reduce its build.'
    key = (str(path), info.st_mtime_ns, info.st_size)
    if key in _syntax_cache:
        return _syntax_cache[key]
    # vm.Script parses browser classic scripts; node --check permits top-level
    # return in CommonJS, which the browser rejects. Do not execute plugin code.
    result = subprocess.run([node, '-e',
        "new (require('vm').Script)(require('fs').readFileSync(process.argv[1],'utf8'))",
        str(path)], capture_output=True, text=True, timeout=5)
    error = 'JavaScript bundle has invalid syntax; rebuild or repair it.' if result.returncode else None
    if len(_syntax_cache) >= 256:
        _syntax_cache.clear()
    _syntax_cache[key] = error
    return error


def validate_plugin(plugin):
    plugin = dict(plugin)
    base = Path(plugin['_dir'])
    entry = asset_path(base, plugin.get('entry'))
    if entry is None:
        # Recover a stale dist/ manifest only when the replacement is unambiguous.
        candidates = [p for name in ('bundle/index.js', 'dist/index.js', 'plugin.js', 'index.js')
                      if (p := asset_path(base, name)) is not None]
        if len(candidates) == 1:
            entry = candidates[0]
            plugin['entry'] = str(entry.relative_to(base.resolve()))
        else:
            plugin['_asset_error'] = 'Plugin JavaScript bundle is missing. Build the plugin and rescan.'
    if entry is not None:
        try:
            error = syntax_error(entry)
        except (OSError, subprocess.TimeoutExpired):
            error = 'Plugin JavaScript could not be validated; rescan after checking its bundle.'
        if error:
            plugin['_asset_error'] = error
    if plugin.get('css') and asset_path(base, plugin['css']) is None:
        plugin['css'] = None
    if plugin.get('_asset_error'):
        log.warning('Dashboard plugin %s: %s', plugin['name'], plugin['_asset_error'])
        # Keep its tab with a useful diagnostic instead of a 404/blank screen.
        plugin['entry'] = '__hermes_plugin_error__.js'
        plugin['css'] = None
        plugin['load_error'] = plugin['_asset_error']
    return plugin


def error_bundle(plugin):
    return '''(function(){var sdk=window.__HERMES_PLUGIN_SDK__;
    if(!sdk)return;var h=sdk.React.createElement;
    window.__HERMES_PLUGINS__.register(%s,function(){return h('section',
    {style:{padding:'24px'}},h('h2',null,%s),h('p',null,%s));});})();''' % (
        json.dumps(plugin['name']), json.dumps(plugin.get('label', plugin['name'])),
        json.dumps(plugin['_asset_error']))


def validate_plugins(plugins):
    result = []
    routes = set()
    for original in plugins:
        plugin = validate_plugin(original)
        tab = dict(plugin.get('tab') or {})
        route = tab.get('path')
        if route and route in routes:
            # Generated Debug Doctor and Debug Master both used /debug.
            # Give each component a distinct route instead of opening another tab.
            route = '/plugin-' + str(plugin['name'])
            while route in routes:
                route += '-tab'
            tab['path'] = route
            tab.pop('override', None)
            plugin['tab'] = tab
        if route:
            routes.add(route)
        result.append(plugin)
    return result
