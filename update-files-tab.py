"""Run inside the existing Colab cell to restore Files Search/Analysis without restoring data."""
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import urllib.request

# Public source assets only. Hashes prevent partial or mismatched downloads.
ASSETS = {'original-files-tab/manifest.json': '4c9c9e20bcddeed7ee57632540628928f2e8bf66747446586715a8760ed684cd', 'original-files-tab/tests/hermes_cli/test_web_server_fs_analysis.py': '379abbc28b6f3f9d84b4c2ee7f1b66f332435196b633215dd169ab74b4401fcc', 'original-files-tab/web/src/lib/api-fs-analysis.ts': 'fa17c200f4f092638b12e2e28c24ab8a2684358f6c071f89ab596ec5c7ae2bd7', 'original-files-tab/web/src/lib/fs-format.ts': 'a490a51ecd951d3f53f33f8acb7922162bab1afab0641e421a75af6933613bd5', 'original-files-tab/web/src/pages/FilesPage.tsx': '6300d40f340ad7540167e00cc851d6ba85ac79b84d4f50a1191c84cc3193e362', 'original-files-tab/web/src/components/FilesSearchDialog.tsx': '7046c6f8707c68b0424792e5f07ae02b726a1a7333acfd2736203b27354d55d9', 'original-files-tab/web/src/components/FilesAnalysisCard.tsx': '2682df45b6c017a1fd8da340e7215fd5e5da9296fc6ef57847fd1939c37cdf12', 'original-files-tab/hermes_cli/web_routers/fs_analysis.py': 'b7e0eeeda4842f0d5a5ccf5520028229bcc86af7a2bf562a02293b742eb88561', 'patch-files-analysis.py': '6fa63b831eac874858f25ddf88406376c0d2fa83b13803ce376cf649c8cb120d'}


def main(agent):
    root = Path('/opt/hermes')
    if not (root / '.venv/bin/python').is_file():
        raise RuntimeError('Start the Hermes Colab runtime first')
    env = {**os.environ, **agent.env}
    env['HERMES_HOME'] = '/opt/data'
    log = Path('/content/hermes-files-tab-repair.log')
    with tempfile.TemporaryDirectory(prefix='hermes-files-tab-') as directory:
        assets = Path(directory)
        for name, expected in ASSETS.items():
            url = 'https://raw.githubusercontent.com/Sugamdeol/hermes-render/main/scripts/' + name
            with urllib.request.urlopen(url, timeout=60) as response:
                content = response.read()
            if hashlib.sha256(content).hexdigest() != expected:
                raise RuntimeError('Files tab repair assets changed; download the latest updater and retry')
            target = assets / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        print('Building restored Files Search/Analysis. Build output: ' + str(log), flush=True)
        with log.open('w') as output:
            result = subprocess.run([str(root / '.venv/bin/python'), str(assets / 'patch-files-analysis.py'), str(root), '--build'],
                                    cwd=root, env=env, stdout=output, stderr=subprocess.STDOUT)
    redact = agent.backup.__func__.__globals__['redact_secrets']
    print(redact('\n'.join(log.read_text(errors='replace').splitlines()[-60:]), agent.env))
    if result.returncode:
        raise RuntimeError('Files tab repair failed; send the redacted output above')
    # Restart only the native dashboard worker; its existing supervisor respawns it.
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            args = (proc / 'cmdline').read_bytes().decode(errors='replace').split('\0')
            if 'dashboard' in args and any(arg.startswith('/opt/hermes/') for arg in args):
                os.kill(int(proc.name), signal.SIGTERM)
        except (OSError, ValueError):
            pass
    print('Files tab assets installed. Dashboard restart requested; refresh after 15 seconds.')


if __name__ == '__main__':
    agent = globals().get('HERMES_COLAB') or globals().get('HERMES')
    if agent is None:
        raise RuntimeError('Run this updater in the same Colab notebook as HERMES_COLAB')
    main(agent)
