#!/data/data/com.termux/files/usr/bin/bash
# Android fallback: a normal Ubuntu userspace, not the currently broken native Hermes APT package.
set -euo pipefail
command_name="${1:-help}"
if [[ -z "${PREFIX:-}" || "$PREFIX" != /data/data/com.termux/files/usr ]]; then
  echo 'Run this script inside the standard Termux app on Android.' >&2; exit 1
fi
login() { proot-distro login --user hermes ubuntu -- /bin/bash -lc 'export PATH="$HOME/.local/bin:$PATH"; exec "$@"' bash "$@"; }
require_install() {
  command -v proot-distro >/dev/null || { echo 'Run: bash run-android.sh install' >&2; exit 1; }
  login test -x /home/hermes/.local/bin/hermes || { echo 'Complete install first.' >&2; exit 1; }
}
backup_command() {
  local helper
  helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/android-backup.py"
  [[ -f "$helper" ]] || { echo 'Clone the complete repository first.' >&2; return 1; }
  login mkdir -p /home/hermes/.local/share/hermes-backup
  login /bin/bash -c 'umask 077; cat > "$HOME/.local/share/hermes-backup/android-backup.py"' < "$helper"
  login python3 /home/hermes/.local/share/hermes-backup/android-backup.py "$1"
}
case "$command_name" in
backup-setup|backup|restore)
  require_install
  if [[ "$command_name" == restore ]]; then
    if tmux has-session -t '=hermes-android' 2>/dev/null; then
      echo 'Stop the gateway and close dashboard/CLI before restoring.' >&2; exit 1
    fi
    read -r -p 'Close dashboard/CLI. Type RESTORE to replace local state (previous files retained): ' answer
    [[ "$answer" == RESTORE ]] || exit 1
  fi
  case "$command_name" in
    backup-setup) backup_command configure ;;
    *) backup_command "$command_name" ;;
  esac
  ;;
install)
  pkg update -y
  pkg install -y proot-distro tmux curl
  if ! proot-distro login ubuntu -- /bin/true >/dev/null 2>&1; then
    proot-distro install ubuntu
  fi
  proot-distro login ubuntu -- /bin/bash -se <<'UBUNTU'
apt-get update
apt-get install -y ca-certificates curl git python3 python3-venv python3-cryptography build-essential ripgrep
id hermes >/dev/null 2>&1 || useradd --create-home --shell /bin/bash hermes
UBUNTU
  login /bin/bash -se <<'INSTALL'
umask 077
mkdir -p "$HOME/.cache"
curl -fsSL https://hermes-agent.nousresearch.com/install.sh -o "$HOME/.cache/hermes-install.sh"
bash "$HOME/.cache/hermes-install.sh" --non-interactive --skip-browser --skip-computer-use
export PATH="$HOME/.local/bin:$PATH"
hermes --version
INSTALL
  echo 'Installed. Next: bash run-android.sh setup'
  ;;
setup|cli|doctor|update|dashboard)
  require_install
  case "$command_name" in
    setup) login hermes setup ;;
    cli) login hermes ;;
    doctor) login hermes doctor ;;
    update)
      if tmux has-session -t '=hermes-android' 2>/dev/null; then
        echo 'Stop the Android gateway before updating.' >&2; exit 1
      fi
      login hermes update ;;
    dashboard) login hermes dashboard --host 127.0.0.1 --port 9119 --no-open ;;
  esac
  ;;
start)
  require_install
  if tmux has-session -t '=hermes-android' 2>/dev/null; then echo 'Gateway already running. Use status or logs.'; exit 0; fi
  echo 'Stop any other host using the same Telegram bot after its successful backup.'
  read -r -p 'Type SWITCH to confirm: ' answer
  [[ "$answer" == SWITCH ]] || { echo 'Cancelled.'; exit 1; }
  runner="$HOME/.local/share/hermes-android-gateway.sh"
  mkdir -p "$(dirname "$runner")"
  cat > "$runner" <<'RUNNER'
#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
mkdir -p "$HOME/.local/state/hermes-android"
exec proot-distro login --user hermes ubuntu -- /bin/bash -lc \
  'export PATH="$HOME/.local/bin:$PATH"; exec hermes gateway run' \
  >> "$HOME/.local/state/hermes-android/gateway.log" 2>&1
RUNNER
  chmod 700 "$runner"
  if command -v termux-wake-lock >/dev/null; then termux-wake-lock; fi
  tmux new-session -d -s hermes-android "bash '$runner'"
  echo 'Gateway launched. Check: bash run-android.sh status (then logs).'
  ;;
stop)
  if tmux has-session -t '=hermes-android' 2>/dev/null; then
    backup_command backup  # Failure aborts stop; gateway stays available.
    tmux send-keys -t '=hermes-android' C-c
    echo 'Graceful stop requested. Check status before switching hosts.'
  else echo 'Gateway is stopped.'; fi
  ;;
status)
  if tmux has-session -t '=hermes-android' 2>/dev/null; then echo 'Gateway process session exists; inspect logs for Telegram connection.'
  else echo 'Gateway is stopped.'; fi
  ;;
logs) tail -n 100 -F "$HOME/.local/state/hermes-android/gateway.log" ;;
help|-h|--help)
  echo 'Usage: bash run-android.sh install|setup|start|stop|status|logs|cli|dashboard|doctor|update|backup-setup|backup|restore'
  ;;
*) echo "Unknown command: $command_name" >&2; exit 2 ;;
esac
