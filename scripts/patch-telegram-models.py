"""Give Telegram the full paginated catalog and make /model --refresh probe live."""
from pathlib import Path
import sys


GATEWAY_REPLACEMENTS = (
    ('list_picker_providers, max_models=50, include_moa=True, **listing_kwargs',
     'list_picker_providers, max_models=None, include_moa=True, **listing_kwargs'),
    ('self, event: MessageEvent, ctx: _ModelSwitchContext, profile_home\n',
     'self, event: MessageEvent, ctx: _ModelSwitchContext, profile_home, *, force_refresh: bool = False\n'),
    ('non_blocking_catalogs=True, probe_custom_providers=False, probe_current_custom_provider=True,',
     'non_blocking_catalogs=not force_refresh, probe_custom_providers=force_refresh,\n'
     '            probe_current_custom_provider=True, refresh=force_refresh,'),
    ('return await self._model_listing_reply(event, ctx, profile_home)',
     'return await self._model_listing_reply(event, ctx, profile_home, force_refresh=request.force_refresh)'),
)
PROVIDER_REPLACEMENTS = (
    ('    probe_current_custom_provider: bool = False) -> list[dict]:\n',
     '    probe_current_custom_provider: bool = False, refresh: bool = False) -> list[dict]:\n'),
    ('        probe_current_custom_provider=probe_current_custom_provider)\n',
     '        probe_current_custom_provider=probe_current_custom_provider, refresh=refresh)\n'),
)


def replace_all(text, replacements):
    for old, new in replacements:
        if new in text:
            continue
        if text.count(old) != 1:
            raise ValueError('Telegram model catalog patch does not match upstream: ' + old[:70])
        text = text.replace(old, new, 1)
    return text


def patch(root):
    root = Path(root)
    changes = []
    for name, replacements in (
        ('gateway/slash_commands_model.py', GATEWAY_REPLACEMENTS),
        ('hermes_cli/model_switch_providers.py', PROVIDER_REPLACEMENTS),
    ):
        path = root / name
        before = path.read_text()
        after = replace_all(before, replacements)
        compile(after, str(path), 'exec')
        changes.append((path, before, after))
    # Validate both files before modifying either one.
    for path, before, after in changes:
        if before != after:
            temporary = path.with_suffix('.telegram-models.tmp')
            temporary.write_text(after)
            temporary.chmod(path.stat().st_mode & 0o777)
            temporary.replace(path)
    return any(before != after for _, before, after in changes)


if __name__ == '__main__':
    changed = patch(sys.argv[1] if len(sys.argv) > 1 else '/opt/hermes')
    print('[telegram-models] full catalog and live refresh enabled' if changed else '[telegram-models] already enabled')
