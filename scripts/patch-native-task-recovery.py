"""Install the Colab recovery hooks without changing upstream turn execution."""
from pathlib import Path
import sys

MARKER = '# Colab durable interrupted-chat recovery'
LOADER = '''# Colab durable interrupted-chat recovery
from hermes_cli.native_task_recovery import install as _install_colab_recovery
_install_colab_recovery(GatewayRunner)

'''


def patch_boot_guard(root):
    """Count the first eligible resume scan once for each gateway runner."""
    root = Path(root)
    path = root / 'gateway/run_startup.py'
    if not path.exists():
        path = root / 'gateway/run.py'
    text = path.read_text()
    marker = '# Colab resume scans are not new gateway boots'
    if marker in text:
        return
    old = '                if _rlg.check_and_record(_max_restarts, _window, max_gap_seconds=_max_gap):'
    if '_rlg.check_and_record(' not in text:
        return  # Older layouts/fixtures without this upstream guard.
    if text.count(old) != 1:
        raise ValueError('Native restart-loop guard anchor changed')
    new = """                # Colab resume scans are not new gateway boots
                if not hasattr(self, '_colab_boot_resume_guard_tripped'):
                    self._colab_boot_resume_guard_tripped = bool(
                        _rlg.check_and_record(_max_restarts, _window, max_gap_seconds=_max_gap))
                if self._colab_boot_resume_guard_tripped:"""
    text = text.replace(old, new, 1)
    compile(text, str(path), 'exec')
    path.write_text(text)


def patch(root):
    root = Path(root)
    patch_boot_guard(root)
    path = root / 'gateway/run.py'
    text = path.read_text()
    anchor = 'if __name__ == "__main__":'
    if MARKER not in text:
        if text.count(anchor) != 1:
            raise ValueError('Native gateway recovery loader anchor changed')
        text = text.replace(anchor, LOADER + anchor, 1)
    compile(text, str(path), 'exec')
    for source, destination in (('task-recovery.py', 'render_recovery.py'),
                                ('native-task-recovery.py', 'native_task_recovery.py'),
                                ('native-ui-task-recovery.py', 'native_ui_task_recovery.py')):
        content = Path(__file__).with_name(source).read_text()
        target = root / 'hermes_cli' / destination
        compile(content, str(target), 'exec')
        target.write_text(content)
    path.write_text(text)
    ui = root / 'tui_gateway/server.py'
    content = ui.read_text()
    ui_marker = '# Colab bulk UI interrupted-chat recovery'
    if ui_marker not in content:
        content += '\n' + ui_marker + '\nfrom hermes_cli.native_ui_task_recovery import install as _install_colab_ui_recovery\n_install_colab_ui_recovery(sys.modules[__name__])\n'
    compile(content, str(ui), 'exec')
    ui.write_text(content)


if __name__ == '__main__':
    patch(sys.argv[1] if len(sys.argv) > 1 else '/opt/hermes')
