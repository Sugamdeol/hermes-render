"""Apply the model-picker repair through Hermes' live plugin reload control socket."""
import __future__
import ast
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import tempfile
import urllib.request

PATCH_URL = ('https://raw.githubusercontent.com/Sugamdeol/hermes-render/'
             'e6f3a5712adea3b26902b142b4db23dd542172aa/scripts/patch-telegram-models.py')
PATCH_SHA256 = 'd9f5e789e7fceec6b15c3c97f6a187ca0f728b357c67d46cd66f332b166c7eeb'
REVISION = 'telegram-model-refresh-v1'


def compile_functions(source, names, namespace, *, class_name=None, filename='<model-hotfix>'):
    tree = ast.parse(source, filename=filename)
    body = tree.body
    if class_name is not None:
        matches = [node for node in body if isinstance(node, ast.ClassDef) and node.name == class_name]
        if len(matches) != 1: raise RuntimeError('Model-command class changed; live repair cancelled')
        body = matches[0].body
    nodes = [node for node in body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    if {node.name for node in nodes} != set(names) or any(node.decorator_list for node in nodes):
        raise RuntimeError('Model-command methods changed; live repair cancelled')
    # Keep the module's existing runtime objects, rather than reloading a class or session module.
    temporary = dict(namespace)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), filename, 'exec',
                 flags=__future__.annotations.compiler_flag), temporary)
    return {name: temporary[name] for name in names}


def apply_live(install):
    import importlib
    gateway = sys.modules.get('gateway.slash_commands_model')
    if gateway is None or 'gateway.run' not in sys.modules:
        return None  # Discovery in a CLI/helper process is not the running gateway.
    providers = importlib.import_module('hermes_cli.model_switch_providers')
    cls = gateway.GatewayModelCommandsMixin
    methods = ('_send_model_picker', '_model_listing_reply', '_handle_model_command_locked')
    replacements = compile_functions((install/'gateway/slash_commands_model.py').read_text(), methods,
        gateway.__dict__, class_name='GatewayModelCommandsMixin')
    listing = compile_functions((install/'hermes_cli/model_switch_providers.py').read_text(),
        ('list_picker_providers',), providers.__dict__)['list_picker_providers']
    previous = {name: getattr(cls, name) for name in methods}
    old_listing = providers.list_picker_providers
    try:
        # New optional parameters are installed before the caller starts passing them.
        providers.list_picker_providers = listing
        for name in methods: setattr(cls, name, replacements[name])
    except BaseException:
        providers.list_picker_providers = old_listing
        for name, function in previous.items(): setattr(cls, name, function)
        raise
    return {'revision': REVISION, 'pid': os.getpid(), 'updated_functions': 4}


def register(ctx):
    install = Path(Path(__file__).with_name('install-path').read_text().strip())
    result = apply_live(install)
    if result is not None:
        directory = Path(__file__).parent
        fd, name = tempfile.mkstemp(dir=directory, prefix='.result-')
        try:
            with os.fdopen(fd, 'w') as handle: json.dump(result, handle)
            os.replace(name, directory/'result.json')
        finally: Path(name).unlink(missing_ok=True)


def main():
    install = Path(os.environ.get('HERMES_INSTALL_DIR', '/opt/hermes'))
    home = Path(os.environ.get('HERMES_HOME', '/opt/data'))
    sys.path.insert(0, str(install))
    from gateway.control_socket import reload_gateway_plugins
    from hermes_cli.plugins import get_bundled_plugins_dir
    with urllib.request.urlopen(PATCH_URL, timeout=30) as response: patcher = response.read()
    if hashlib.sha256(patcher).hexdigest() != PATCH_SHA256:
        raise RuntimeError('Model repair download did not verify; nothing was changed')
    namespace = {'__name__':'model_repair', '__file__':str(install/'model-repair.py')}
    exec(compile(patcher, '<verified-model-patch>', 'exec'), namespace)
    namespace['patch'](install)
    root = get_bundled_plugins_dir()
    if not root.is_dir(): raise RuntimeError('Native plugin directory is unavailable; no live repair attempted')
    plugin = root/('telegram-model-repair-' + secrets.token_hex(6))
    plugin.mkdir(mode=0o700)
    (plugin/'__init__.py').write_text(Path(__file__).read_text())
    (plugin/'install-path').write_text(str(install))
    (plugin/'plugin.yaml').write_text('name: ' + plugin.name + '\nversion: "1.0.0"\nkind: backend\n'
                                     'description: Temporary live model-picker compatibility repair\n')
    result = reload_gateway_plugins(home, timeout=60)
    try: proof = json.loads((plugin/'result.json').read_text())
    except (OSError, ValueError): proof = {}
    if (not result or not result.get('reloaded') or proof.get('revision') != REVISION
            or not isinstance(proof.get('pid'), int) or proof['pid'] == os.getpid()):
        raise RuntimeError('Live repair could not be confirmed. No restart was performed. '
                           'Repair details remain at ' + str(plugin))
    shutil.rmtree(plugin)
    print('Applied to the running gateway without restarting. Send /model --refresh in Telegram; old picker messages keep their old lists.')


if __name__ == '__main__':
    main()
