"""Compatibility only: native upstream runtime without Render resource patches."""
from pathlib import Path
import sys


def replace(text, old, new):
    if new in text:
        return text
    assert text.count(old) == 1, 'Colab compatibility anchor changed: ' + old[:60]
    return text.replace(old, new, 1)


def patch(root):
    root = Path(root)
    path = root/'plugins/platforms/telegram/adapter.py'
    text = path.read_text()
    text = replace(text, 'self._model_picker_state[str(chat_id)] = {', 'self._model_picker_state[(str(chat_id), msg.message_id)] = {')
    text = replace(text, 'state = self._model_picker_state.get(chat_id)', 'state = self._model_picker_state.get((str(chat_id), query.message.message_id))')
    text = text.replace('self._model_picker_state.pop(chat_id, None)', 'self._model_picker_state.pop((str(chat_id), query.message.message_id), None)')
    compile(text, str(path), 'exec')
    path.write_text(text)


    path = root/'hermes_cli/config_providers.py'
    text = path.read_text()
    text = replace(text, '"name", "api", "url", "base_url", "api_key", "key_env",', '"name", "api", "url", "base_url", "api_key", "api_keys", "key_env",')
    old = '    _put("api_key", _stripped("api_key"))'
    new = old + '\n    keys = entry.get("api_keys")\n    if isinstance(keys, list):\n        normalized["api_keys"] = list(dict.fromkeys(k.strip() for k in keys if isinstance(k, str) and k.strip()))\n        if normalized["api_keys"]: normalized["api_key"] = normalized["api_keys"][0]'
    text = replace(text, old, new)
    compile(text,str(path),'exec');path.write_text(text)
    path = root/'agent/credential_pool.py'
    text = path.read_text()
    start = text.index('    cp_config = _get_custom_provider_config(pool_key)', text.index('def _seed_custom_pool('))
    end = text.index('    # Seed from model.api_key',start)
    old = text[start:end]
    new = '\n'.join([
        '    # Sugam custom provider key list compatibility',
        '    cp_config = _get_custom_provider_config(pool_key)',
        '    if cp_config:',
        '        keys = cp_config.get("api_keys") or [cp_config.get("api_key")]',
        '        name = str(cp_config.get("name") or "").strip()',
        '        for key in dict.fromkeys(k.strip() for k in keys if isinstance(k,str) and k.strip()):',
        '            import hashlib',
        '            source = "config:" + name + ":" + hashlib.sha256(key.encode()).hexdigest()[:16]',
        '            seed.upsert(source, {"auth_type":AUTH_TYPE_API_KEY,"access_token":key,',
        '                "base_url":_norm_url(cp_config.get("base_url")),"label":name})', '', ''])
    if '# Sugam custom provider key list compatibility' not in text:
        text=text[:start]+new+text[end:]
    compile(text,str(path),'exec');path.write_text(text)

    path = root / "scripts/build/web.mjs"
    text = path.read_text()
    recovery = "// Hermes Colab build heap recovery\n{\n  const { getHeapStatistics } = await import('node:v8');\n  if (getHeapStatistics().heap_size_limit < 1024 * 1024 * 1024) {\n    if (process.env.HERMES_COLAB_HEAP_REEXEC === '1') {\n      throw new Error('Colab Node heap remained below 1 GB after restart');\n    }\n    const { spawnSync } = await import('node:child_process');\n    const options = (process.env.NODE_OPTIONS || '')\n      .replace(/--max[-_]old[-_]space[-_]size(?:=|\\s+)\\d+/g, '').trim();\n    const env = { ...process.env,\n      NODE_OPTIONS: (options + ' --max-old-space-size=4096').trim(),\n      HERMES_COLAB_HEAP_REEXEC: '1' };\n    // Drop an inherited command-line heap cap as well as NODE_OPTIONS.\n    const args = process.execArgv.filter(arg => !arg.startsWith('--max-old-space-size='));\n    const result = spawnSync(process.execPath, [...args, ...process.argv.slice(1)],\n      { env, stdio: 'inherit' });\n    if (result.error) throw result.error;\n    process.exit(result.status ?? 1);\n  }\n}\n"
    if "// Hermes Colab build heap recovery" not in text:
        if text.startswith("#!"):
            first, rest = text.split("\n", 1)
            text = first + "\n" + recovery + rest
        else:
            text = recovery + text
        path.write_text(text)

    import runpy
    runpy.run_path(str(Path(__file__).with_name('patch-custom-models.py')))['patch'](root)
    runpy.run_path(str(Path(__file__).with_name('patch-telegram-models.py')))['patch'](root)
    runpy.run_path(str(Path(__file__).with_name('patch-native-task-recovery.py')))['patch'](root)
    dashboard = runpy.run_path(str(Path(__file__).with_name('patch-colab-dashboard.py')))
    dashboard['patch'](root)
    runpy.run_path(str(Path(__file__).with_name('patch-auxiliary-pool.py')))['patch'](root)


if __name__ == '__main__':
    patch(sys.argv[1])

