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


if __name__ == '__main__':
    patch(sys.argv[1])
