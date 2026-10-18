# Windows and Android local launchers

Keep only one gateway using your Telegram bot at a time. Stop the previous host after a successful backup. These launchers do not delete Colab or InstaBox data.

## Windows: existing private backup and dashboard

Install Python 3, Git, and Docker Desktop with Linux containers enabled. Start Docker Desktop. Clone this repository, then open PowerShell in its directory:

```powershell
git clone https://github.com/Sugamdeol/hermes-render.git
cd hermes-render
.\run-windows.cmd start
```

The wrapper runs the existing `run-local.py` launcher. It privately prompts for the storage GitHub token and original encryption key, restores the configured backup, and keeps local data in Docker volumes. The first image build and full restore can be slow. The dashboard is at http://127.0.0.1:10000 once ready. Keep enough Docker disk space for the expanded backup, Git cache and image.

```powershell
.\run-windows.cmd logs
.\run-windows.cmd status
.\run-windows.cmd password
.\run-windows.cmd backup
.\run-windows.cmd stop
```

`stop` first requests a backup; backup failure prevents stopping. Sleep/shutdown stops local hosting. The container restarts when Docker starts. Update this checkout with `git pull`; to refresh the runtime, back up and stop before starting again. Optional port: `run-windows.cmd start -Port 10001`.

## Android: Termux with Ubuntu fallback

Install standard Termux from https://termux.dev/ and keep the installation in Termux private storage, not `/sdcard`. The upstream native Termux package is currently marked broken, so this script uses `proot-distro` Ubuntu. No root access or Docker is required. Use a 64-bit phone with enough free storage; 4GB RAM or more is preferable.

```bash
pkg update
pkg install git
git clone https://github.com/Sugamdeol/hermes-render.git
cd hermes-render
bash run-android.sh install
bash run-android.sh setup
bash run-android.sh start
```

`setup` configures your provider and messaging account through the official CLI. Android uses a **separate local Hermes home** inside Ubuntu at `/home/hermes/.hermes`. It does **not automatically restore or write the private GitHub backup**. Do not expect the Windows/Colab chats, memories, files or repository overlays to appear here. Migrating that backup needs a separate verified import and path conversion; this launcher never claims that it completed one.

```bash
bash run-android.sh cli
bash run-android.sh status
bash run-android.sh logs
bash run-android.sh doctor
bash run-android.sh stop
bash run-android.sh update
```

Optional dashboard in a second Termux session:

```bash
cd ~/hermes-render
bash run-android.sh dashboard
```

Open http://127.0.0.1:9119 on that phone. This command stays in the foreground; Ctrl+C stops the dashboard. Initial dashboard build may take time and require extra upstream dependencies. The gateway runs in a separate tmux session. `status` checks that session, not Telegram readiness; inspect logs for connection failures.

Disable battery optimization for Termux and allow background activity. The script requests a wake lock when supported. Android can still kill the process; this is not guaranteed 24/7 hosting. The script does not enable auto-start at boot or install local Chromium/computer-use tools. Proot and individual tools may have Android kernel limitations. It does not apply the repository's notebook compatibility patches to the official Android runtime.

Validation: shell syntax and mocked command-flow checks on Linux. A real Windows/Docker and Android/Termux device run is still required; installation success on those platforms has not been verified here.
