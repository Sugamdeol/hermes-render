# Hermes on Render Free

Native Hermes Agent and its own web dashboard, pinned to v2026.5.7
(`498bfc7bc12a937621b4215312049b1000726df3`), with a small image profile
and private GitHub persistence. This replaces the Nanobot deployment.

[Deploy with Render Blueprint](https://dashboard.render.com/select-repo?type=blueprint)

## Single-file Colab and PC launchers

Download just **run-colab.py** or **run-local.py**. Each automatically downloads
the current application code and restores the same private
`Sugamdeol/hermes-storage` repository and `state` branch. Supply your GitHub
storage token and the **existing** encryption key through the hidden prompts.
Providers, Telegram credentials, memories, skills, plugins and chat history
come from that backup. Neither launcher creates an empty replacement when
the existing backup cannot be restored.

Before switching hosts, finish/stop active tasks, wait for a successful backup,
and stop the old Render/local/Colab copy. Type `SWITCH` in the new launcher to
confirm. Run only one copy against this bot and snapshot repository. Both new
launchers use Telegram polling instead of the saved Render webhook.

**Colab:** paste the entire `run-colab.py` file into one code cell and run it.
Use a normal current Linux Python runtime; no GPU or Docker is needed. The first
install builds the native web UI and can take several minutes. Open the printed
dashboard link through Colab's browser proxy. Username is `hermes`; the saved
password is shown only when you call `HERMES_COLAB.password()`. Keep the notebook private.

In another cell, use `HERMES_COLAB.status()`, `HERMES_COLAB.backup()` or
`HERMES_COLAB.stop()`. Stop saves through the existing storage daemon and refuses
to shut down on a failed backup. Colab can end a runtime without warning, so it
is temporary hosting: work not uploaded before termination can be lost.

Free Colab runs for **at most 12 hours**, and can end earlier due to idle timeouts,
usage and availability. Pro+ supports up to **24 hours** of continuous execution
with sufficient compute units. Primarily using an external web UI on the free
tier without a positive compute balance can trigger termination without warning;
Colab is not a reliable always-on Telegram/web host. See the
[official Colab FAQ](https://research.google.com/colaboratory/faq.html).
`HERMES_COLAB.status()` reports agent uptime, not guaranteed remaining VM time.

**Your PC:** install Python 3.9+, Git and Docker Desktop (Windows/macOS) or
Docker Engine (Linux). Start Docker, then run:

```sh
python run-local.py
python run-local.py logs
python run-local.py backup
python run-local.py stop
```

On Windows, `py run-local.py` works too. The dashboard binds only to
`http://127.0.0.1:10000`; use `--port 10001` if needed. Username and saved password
are shown after startup. `python run-local.py password` shows the password again.
The local data and Git clone live in persistent Docker volumes, which are kept
when stopping. Stop the container before restarting Render or Colab. A failed
backup leaves it running so you can inspect logs and retry.

## Setup

### Updated chat workspace

The bundled `hermes-chat-dashboard` plugin keeps the conversation central:
readable system fonts, narrower history, optional details, Focus mode and a
Latest messages button. Existing model selection, attachments, tool activity,
steering, history and exports stay in the same plugin. Submission locks prevent
rapid duplicate sends; IME typing, read-only sessions and pending uploads are
handled before submission. No extra agent process is added.

For an already running Colab notebook, run `update-chat-ui.py` in that notebook.
It verifies downloaded plugin files, updates the frontend and paged history
backend, then saves through the existing daemon before restarting the Colab
agent with its current credentials. Backup failure leaves it running. After
it reports a healthy restart, hard-refresh the dashboard. History loads in
80-message pages; earlier pages stay accessible. Completed messages are memoized
and streaming chunks are batched every 80ms. Slow provider discovery does not
block history or the dashboard event loop. A new launcher boot installs the
bundled version.


1. Select this repository in the Render account where you want the service.
2. Enter `GIT_STATE_TOKEN`: a fine-grained GitHub token scoped to the private
   `Sugamdeol/hermes-storage` repository, with Contents read/write and Metadata read.
   The existing `state` branch is restored; memories, skills and plugins are kept.
3. Enter the **existing** `STORAGE_ENCRYPTION_KEY` when restoring an existing backup.
   Never generate a different key for a migration. Only this key and
   `GIT_STATE_TOKEN` are required: a private GitHub repository cannot be fetched
   using its encryption key alone. The repository and branch have built-in defaults.
   Saved Telegram credentials, allowed users, provider endpoints/keys, dashboard
   password, retry counts and custom settings load before services start.
   A new runtime snapshot is created on the next successful encrypted backup.
   Older backups can restore their `.env` values but cannot contain settings that
   were never captured. Run the updated source service and wait for a successful
   backup before migrating. Host paths, Render URLs/IDs, and the encryption key
   are intentionally excluded. The Telegram webhook uses the new service URL.
   Saved settings override image defaults. To override a saved value explicitly,
   set its new value and list its name in `HERMES_ENV_OVERRIDE_KEYS` (comma-separated).
4. Open the service URL. Sign in as **hermes**, using `HERMES_GATEWAY_TOKEN`
   as the password (restored from the encrypted backup; on a fresh setup it is generated in the private `.env`).
5. In **Models → API Providers**, enter a provider name, custom endpoint,
   OpenAI/Anthropic mode, API key or environment variable name, and model.
   Select that provider/model in the native model picker.
6. Use **Environment** to save `TELEGRAM_BOT_TOKEN` and
   `TELEGRAM_ALLOWED_USERS` (your numeric Telegram user ID). Click the native
   gateway restart action after changing Telegram settings. Dashboard-saved
   values are encrypted in Git storage; Render Environment values remain the
   recommended setup for provider and chat credentials.

Telegram uses a secret-validated webhook on Render's public `/telegram` route,
so an incoming message can wake a sleeping free service. The first message after
sleep can be delayed while the container restores its private state. Free Render
is not an always-on service. Stop other instances using the same bot/state repo
before starting this one.

## GitHub storage

Startup shallow-clones the private repository, restores `data/`, then starts
Hermes. If an existing backup cannot be read or decrypted, startup fails without
writing an empty replacement. Changes are backed up after a short quiet period,
with at least 30 seconds between pushes. Unsynced edits can still be lost if the
free container is killed before a successful backup.

Memories, skills, workspace, sessions, plugin files and settings persist in the
private repository. `.env`, `config.yaml`, `auth.json`, the recovery journal and
SQLite databases are encrypted before new backups when `GIT_STATE_ENV_MODE=encrypt`.
SQLite session databases use a transactionally consistent online snapshot; WAL
and SHM sidecars are not copied as independent files. Other files, including
memories and workspace content, remain private-repository plaintext. Old plaintext
secrets in existing Git history are not automatically erased. Legacy age
encryption remains supported when its matching private key or recipient is set.

The Models page shows the last successful GitHub state sync, current sync errors,
and the count of interrupted gateway tasks queued for automatic recovery. It
does not expose saved task text or chat IDs.
Provider listing does not probe remote endpoints; use **Test & refresh models**
on a provider row to check its model endpoint and refresh IDs. A branch conflict
is reported and held until restart restores the latest state; it never replaces
the remote branch. Keep one active writer per state branch.

Git operations use one packing thread, a 16 MB pack window and a 32 MB HTTP buffer.
History is compacted after 200 commits. Root-level `archives/` is preserved by
normal state synchronization. Only one live writer should use this branch.

## Small-memory profile

The Docker build installs core Hermes, native dashboard, native TUI, MCP, PTY and
Telegram support. It does not install the `[all]` extra, Chromium, local Whisper,
GPU/ML runtimes, or unrelated channel SDKs. Those optional tools may need a larger
instance and additional packages. Existing user skills remain available; a skill
that depends on an omitted optional package cannot run until that package is added.

One native browser chat is allowed at a time. Gateway agent turns also run one
at a time across chats; incoming sessions wait with their own transcript and
task state intact, then run when the active turn releases the slot. This keeps
several simultaneous Telegram sessions from multiplying the agent working set.
The agent cache holds one session
with a 30-second idle timeout, and native library threads default to one. These settings reduce baseline memory; arbitrary terminal jobs,
large uploads, plugins or tool workloads can still exceed 512 MB.

The GitHub Actions workflow builds the actual image and boots it under a 512 MB
Docker memory limit with no swap, then checks dashboard authentication, provider
creation, environment writes and native browser chat. See the workflow logs for
measured peak memory; a boot smoke test is not a full workload benchmark.

Verified run on 2026-10-03: 145 unit tests passed; native dashboard authentication,
custom provider creation, environment saving and one browser chat passed with
no swap under 512 MB, peaking at **250.4 MiB**. At 100 MB, native browser chat
was OOM-killed. Dashboard-only mode (`HERMES_DASHBOARD_TUI=0`) booted at
98.9 MiB, but that is not a working 100 MB agent. This deployment keeps native
Hermes chat enabled on Render Free's 512 MB service as requested.

The experimental 100 MB checks are informational and allowed to fail in CI;
a green workflow does not mean full Hermes is compatible with 100 MB.

## Native plugins

Hermes's original dashboard plugin system is retained. Create plugins under
`$HERMES_HOME/plugins/<name>/dashboard/` with a `manifest.json`, frontend bundle,
and optional `plugin_api.py`. They are included in GitHub backups. The bundled
`render-api-providers` plugin is image-managed and refreshed on startup; use a
separate name for your own plugins.

## Development

```sh
python -m unittest discover -s tests -q
docker build -t hermes-lite .
docker run --rm -p 10000:10000 --memory=512m --memory-swap=512m \
  -e HERMES_GATEWAY_TOKEN=local-password \
  -e STORAGE_ENCRYPTION_KEY=local-storage-key hermes-lite
```

Provider and Telegram secrets go in the authenticated dashboard or Render
Environment, never in this public source repository.

### Lightweight runtime

Python allocation caps, worker cgroup caps and automatic memory-based cancellations have been removed. Old `HERMES_PYTHON_DATA_MB`, `HERMES_PYTHON_AS_MB` and `HERMES_AGENT_RAM_MB` values are ignored. The adapter's previous `NODE_OPTIONS=--max-old-space-size=64` is cleared for agent workers. Render still enforces its own 512 MB instance limit.

The runtime now shares Git delta sync and memory diagnostics in one Python process, runs one Telegram gateway under a foreground shell supervisor, starts only one cron job at a time, uses smaller Python thread-stack reservations and one glibc allocation arena, and releases idle gateway agents after 30 seconds. The foreground supervisor restarts a failed gateway and causes the container to exit if the supervisor itself is killed, so Render can replace the instance. Recovery scans only write session state when it actually changes, avoiding pointless Git snapshots every few seconds. It retains one cached agent and one native browser chat. State encryption, saved memories/skills, provider settings and native chat remain supported.

An unconfigured image-managed Render MCP entry is removed so it does not trigger unnecessary SDK imports or discovery. User-configured MCP servers and authenticated Render entries remain available.

Ordinary browser chat starts the full slash-command CLI subprocess only on the first slash command, rather than eagerly duplicating its imports for every chat.

Memory logs report `mode=observe`. They measure usage without killing agents. CI includes a real native browser message and model response against a local deterministic test provider, thread creation under heap load, gateway initialization, Git storage tests, process-tree cleanup and a 512 MB container without swap. These checks do not prove every research task or tool fits the free instance.

Verified lightweight run on 2026-10-04: the native browser sent a message and received the local test-provider reply under 512 MiB with no swap, peaking at **313.7 MiB**. Chat stayed connected during the pressure test; the gateway initialized without allocation caps. This run did not include a live Telegram connection or the private Git backend.

Git backup subprocesses use small delta and packed-file caches (4 MiB each, 32 MiB mapped-pack cache). HTTP transport buffering stays unchanged to preserve the existing push compatibility. CI also runs browser chat alongside a resident initialized GatewayRunner and a real local Git backup daemon with 1,724 fixture files plus a 16 MiB session file; this does not exercise a live Telegram connection or GitHub HTTP transport.

## Interrupted gateway task recovery

Telegram/gateway turns now record their original request and session ID before allocating the agent. A gateway restart reloads explicitly unfinished tasks regardless of the native two-minute crash-detection window and schedules an internal continuation in the same conversation lane. Native transcripts and tool results remain the progress checkpoints. Completed turns remove the intent record. Explicit stop, new-session and session-selection actions take precedence.

All unfinished gateway lanes are queued one at a time. Waiting does not consume retries. A live recovery scanner runs every ten seconds and retries failures without another user message or gateway restart. Retry delays grow from 20 seconds to a maximum of five minutes; there is no permanent three-attempt pause. Less-tried lanes go first after a crash. Explicit stop/reset still takes precedence. Recovery asks the model to verify uncertain tool outcomes before repeating an action; this is not an exactly-once guarantee for external side effects.

The intent journal is encrypted by Git storage. With private Git storage configured, each gateway turn waits for the existing storage daemon to upload its task intent and saved session state before allocating the agent. This upload bypasses the normal debounce and push interval. Backup failure or a 180-second acknowledgement timeout leaves the task queued for a later recovery attempt instead of starting work without a durable checkpoint. The journal includes the session entry and routing source, so startup can reconstruct a missing session-index entry with its original ID. A full Render-instance replacement can recover the acknowledged checkpoint; tool progress written after that checkpoint still depends on subsequent backups. Browser TUI and cron-job runners are separate execution paths from this gateway turn recovery.

Unfinished task records protect their session IDs from idle/daily expiry. Manual `continue` retains the original task. Context compression updates both the recovery ID and existing Telegram topic binding. Completed recovery records are removed only after native transcript persistence.
