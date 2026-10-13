"""Keep manually configured model IDs in the native custom endpoint picker."""
from pathlib import Path
import sys


REPLACEMENTS = (
    (
        '    _extend_unique(grp["models"], [active_model])\n    models_field = entry.get("models")\n',
        '    _extend_unique(grp["models"], [active_model])\n'
        '    # Hermes adapter: preserve manually registered IDs across discovery.\n'
        '    manual = grp.setdefault("manual_models", [])\n'
        '    _extend_unique(manual, [active_model])\n'
        '    models_field = entry.get("models")\n'
        '    if not _entry_models_discovered(entry):\n'
        '        _extend_unique(manual, _declared_model_ids(models_field))\n',
    ),
    (
        '        if discovered is not None:\n            models_list = discovered\n',
        '        if discovered is not None:\n'
        '            models_list = list(dict.fromkeys([*grp.get("manual_models", []), *discovered]))\n'
        '            native_catalog_empty = native_catalog_empty and not grp.get("manual_models")\n',
    ),
    (
        '        if discovered is not None:\n            grp["models"] = discovered\n',
        '        if discovered is not None:\n'
        '            grp["models"] = list(dict.fromkeys([*grp.get("manual_models", []), *discovered]))\n'
        '            native_catalog_empty = native_catalog_empty and not grp.get("manual_models")\n',
    ),
)


def patch_text(text):
    for old, new in REPLACEMENTS:
        if new in text:
            continue
        if text.count(old) != 1:
            raise ValueError('Custom model picker source changed; expected manual/discovery merge anchor missing')
        text = text.replace(old, new, 1)
    return text


def patch(root):
    path = Path(root) / 'hermes_cli/model_switch_providers.py'
    text = path.read_text()
    updated = patch_text(text)
    compile(updated, str(path), 'exec')
    if updated != text:
        temporary = path.with_suffix('.custom-models.tmp')
        temporary.write_text(updated)
        temporary.chmod(path.stat().st_mode & 0o777)
        temporary.replace(path)
    return updated != text


if __name__ == '__main__':
    changed = patch(sys.argv[1] if len(sys.argv) > 1 else '/opt/hermes')
    print('[custom-models] native picker repaired' if changed else '[custom-models] native picker already repaired')
