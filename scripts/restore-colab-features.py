"""Restore installed dashboard extensions across the upstream plugin activation migration."""
import json
from pathlib import Path
import sys
import yaml

MANAGED = {'render-api-providers', 'hermes-chat-dashboard'}


def restore_config(config, installed):
    plugins = config.setdefault('plugins', {})
    if not isinstance(plugins, dict):
        raise ValueError('plugins must remain a mapping')
    enabled, disabled = plugins.get('enabled', []), plugins.get('disabled', [])
    if not isinstance(enabled, list) or not isinstance(disabled, list):
        raise ValueError('plugin enable/disable settings must remain lists')
    # User requested restoration of our bundled extensions. Preserve explicit choices
    # for unrelated plugins and enable other previously visible restored dashboards.
    disabled = [name for name in disabled if name not in MANAGED]
    enabled = list(dict.fromkeys(enabled + sorted(set(installed) - set(disabled))))
    plugins['enabled'], plugins['disabled'] = enabled, disabled
    dashboard = config.get('dashboard')
    if isinstance(dashboard, dict) and isinstance(dashboard.get('hidden_plugins'), list):
        dashboard['hidden_plugins'] = [n for n in dashboard['hidden_plugins'] if n not in MANAGED]
    return config


def installed_dashboards(home):
    names = []
    for manifest in (Path(home) / 'plugins').glob('*/dashboard/manifest.json'):
        try:
            data = json.loads(manifest.read_text())
            name = manifest.parent.parent.name
            entry = data.get('entry', 'dist/index.js')
            target = (manifest.parent / entry).resolve()
            if not target.is_file():
                candidates = [(manifest.parent / value).resolve() for value in
                              ('bundle/index.js', 'dist/index.js', 'plugin.js', 'index.js')
                              if (manifest.parent / value).is_file()]
                if len(candidates) == 1:
                    target = candidates[0]
            if (data.get('name', name) == name and target.is_relative_to(manifest.parent.resolve())
                    and target.is_file()):
                names.append(name)
        except (OSError, ValueError, TypeError):
            continue
    return names


def restore(home):
    home = Path(home)
    marker = home / '.colab-dashboard-features-v1'
    if marker.exists():
        return
    path = home / 'config.yaml'
    config = yaml.safe_load(path.read_text()) if path.exists() else {}
    if not isinstance(config, dict):
        raise ValueError('Saved config is not a mapping')
    names = installed_dashboards(home)
    missing = MANAGED - set(names)
    if missing:
        raise RuntimeError('Managed dashboard bundles missing: ' + ', '.join(sorted(missing)))
    restore_config(config, names)
    temporary = path.with_suffix('.features.tmp')
    temporary.write_text(yaml.safe_dump(config, sort_keys=False))
    temporary.replace(path)
    marker.touch()
    print('[features] Restored dashboard extensions: ' + ', '.join(sorted(names)), flush=True)


if __name__ == '__main__':
    restore(sys.argv[1])
