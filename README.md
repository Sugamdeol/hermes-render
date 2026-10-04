# Hermes on Render Free

Native Hermes Agent and its own web dashboard, pinned to v2026.5.7
(`498bfc7bc12a937621b4215312049b1000726df3`), with a small image profile
and private GitHub persistence. This replaces the Nanobot deployment.

[Deploy with Render Blueprint](https://dashboard.render.com/select-repo?type=blueprint)

## Setup

1. Select this repository in the Render account where you want the service.
2. Enter `GIT_STATE_TOKEN`: a fine-grained GitHub token scoped to the private
   `Sugamdeol/hermes-storage` repository, with Contents read/write and Metadata read.
   The existing `state` branch is restored; memories, skills and plugins are kept.
3. Render generates `HERMES_GATEWAY_TOKEN` and `STORAGE_ENCRYPTION_KEY`.
   Save both securely. Keep the same encryption key when recreating this service;
   changing it prevents restoring encrypted settings.
4. Open the service URL. Sign in as **hermes**, using `HERMES_GATEWAY_TOKEN`
   as the password (find its value in Render Environment).
5. In **Models → API Providers**, enter a provider name, custom endpoint,
   OpenAI/Anthropic mode, API key or environment variable name, and model.
   Select that provider/model in the native model picker.
6. Use **Environment** to save `TELEGRAM_BOT_TOKEN` and
   `TELEGRAM_ALLOWED_USERS` (your numeric Telegram user ID). Click the native
   gateway restart action after changing Telegram settings.

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
private repository. `.env`, `config.yaml` and `auth.json` are authenticated-encrypted
with `STORAGE_ENCRYPTION_KEY` before new backups. Other files, including memories
and chat history, remain private-repository plaintext. Old plaintext secrets in
existing Git history are not automatically erased. Legacy age encryption remains
supported when its matching private key is provided.

Git operations use one packing thread, a 16 MB pack window and a 32 MB HTTP buffer.
History is compacted after 200 commits. Root-level `archives/` is preserved by
normal state synchronization. Only one live writer should use this branch.

## Small-memory profile

The Docker build installs core Hermes, native dashboard, native TUI, MCP, PTY and
Telegram support. It does not install the `[all]` extra, Chromium, local Whisper,
GPU/ML runtimes, or unrelated channel SDKs. Those optional tools may need a larger
instance and additional packages. Existing user skills remain available; a skill
that depends on an omitted optional package cannot run until that package is added.

One native browser chat is allowed at a time. The agent cache holds one session
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

The runtime now shares Git delta sync and memory diagnostics in one Python process, uses a shell supervisor for Telegram, starts only one cron job at a time, uses smaller Python thread-stack reservations and one glibc allocation arena, and releases idle gateway agents after 30 seconds. It retains one cached agent and one native browser chat. State encryption, saved memories/skills, provider settings and native chat remain supported.

An unconfigured image-managed Render MCP entry is removed so it does not trigger unnecessary SDK imports or discovery. User-configured MCP servers and authenticated Render entries remain available.

Ordinary browser chat starts the full slash-command CLI subprocess only on the first slash command, rather than eagerly duplicating its imports for every chat.

Memory logs report `mode=observe`. They measure usage without killing agents. CI includes a real native browser message and model response against a local deterministic test provider, thread creation under heap load, gateway initialization, Git storage tests, process-tree cleanup and a 512 MB container without swap. These checks do not prove every research task or tool fits the free instance.

Verified lightweight run on 2026-10-04: the native browser sent a message and received the local test-provider reply under 512 MiB with no swap, peaking at **313.7 MiB**. Chat stayed connected during the pressure test; the gateway initialized without allocation caps. This run did not include a live Telegram connection or the private Git backend.

Git backup subprocesses use small delta and packed-file caches (4 MiB each, 32 MiB mapped-pack cache). HTTP transport buffering stays unchanged to preserve the existing push compatibility. CI also runs browser chat alongside a resident initialized GatewayRunner and a real local Git backup daemon with 1,724 fixture files plus a 16 MiB session file; this does not exercise a live Telegram connection or GitHub HTTP transport.

## Interrupted gateway task recovery

Telegram/gateway turns now record their original request and session ID before allocating the agent. A gateway restart reloads explicitly unfinished tasks regardless of the native two-minute crash-detection window and schedules an internal continuation in the same conversation lane. Native transcripts and tool results remain the progress checkpoints. Completed turns remove the intent record. Explicit stop, new-session and session-selection actions take precedence.

All unfinished gateway lanes are queued one at a time. Waiting does not consume retries. A live recovery scanner runs every ten seconds and retries failures without another user message or gateway restart. Retry delays grow from 20 seconds to a maximum of five minutes; there is no permanent three-attempt pause. Less-tried lanes go first after a crash. Explicit stop/reset still takes precedence. Recovery asks the model to verify uncertain tool outcomes before repeating an action; this is not an exactly-once guarantee for external side effects.

The intent journal is encrypted by Git storage. A killed gateway process can recover its locally committed journal immediately. A full Render-instance replacement can recover only state that reached GitHub before the instance disappeared; unsynced progress cannot be guaranteed. Browser TUI and cron-job runners are separate execution paths from this gateway turn recovery.

Unfinished task records protect their session IDs from idle/daily expiry. Manual `continue` retains the original task. Context compression updates both the recovery ID and existing Telegram topic binding. Completed recovery records are removed only after native transcript persistence.
