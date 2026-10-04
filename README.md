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

One native browser chat is allowed at a time. Node's heap is capped at 64 MB,
agent cache at two sessions with a 120-second idle timeout, and native library
threads at one. These settings reduce baseline memory; arbitrary terminal jobs,
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

### Memory pressure and production limits

The 512 MB CI measurement covers dashboard and native TUI startup; it does not establish a ceiling for Telegram inference, tools, and GitHub sync running together. Production OOM reports showed this distinction matters.

Ordinary backup and restore files now stream to disk instead of loading entire workspace artifacts into RAM. Browser disconnects terminate the PTY process group, including its Node and Python descendants. Cached gateway agents are swept every 30 seconds. Runtime logs report total cgroup usage and process RSS without command arguments or secrets.

To reserve capacity for Telegram and storage, browser chat refuses to start above 320 MiB of container usage and closes above 400 MiB. This can interrupt an active browser chat; saved session history remains available for resuming. It is a pressure safeguard, not a guarantee that every Hermes tool or workload fits 512 MB. Large local models, browser processes and concurrent agent workloads may still exceed the service budget.

### Shared agent RAM budget

`HERMES_AGENT_RAM_MB=300` gives browser chat, the Telegram gateway, and their child tools a shared 300 MiB budget. Dashboard, proxy and storage are outside that worker budget; the remaining 212 MiB is headroom, not a guaranteed reservation. The supported setting is clamped to 128–350 MiB.

On platforms with delegated writable cgroup v2 memory controls, the adapter creates a worker cgroup with `memory.max`, disables worker swap, and enables group OOM termination. All launched workers enter it before executing Hermes. This is a hard aggregate kernel cap; exceeding it can terminate the agent group. Telegram's supervisor restarts after 20 seconds; interrupted tasks need retrying.

Render may expose cgroups read-only. In that case the startup log explicitly says `mode=watchdog (sampled, not a hard cap)`. The watchdog checks summed worker RSS every 0.2 seconds, including observed descendants; it cancels browser chat first, then Telegram if needed. It also sheds workers when the container passes 430 MiB. RSS conservatively counts shared pages more than once. Fast allocation spikes can still outrun sampling, and tools that escape tracking may not be fully accounted for. This fallback reduces risk but cannot guarantee prevention of OOM. A strict limit requires a host that permits cgroup delegation.

Look for `[agent-budget]` and `[memory]` lines after redeploy to see the enforcement mode, cancellations, and process memory. Native dashboard gateway-restart actions also enter the worker budget.

The memory logger now supervises the budget monitor in the same process and restarts it after unexpected errors. The monitor runs as the Hermes UID after cgroup setup so signals do not depend on root retaining `CAP_KILL`. Each memory log includes `budget_alive`, worker RSS, cgroup anonymous memory, file cache and inactive file cache. Total cgroup usage includes cache and cannot be compared directly with the agent-only budget. Cached files may be reclaimed by Linux, so a high total alone is not proof of an agent heap leak.

### Python allocation cap

Python Telegram and browser agent backends now call `resource.setrlimit(RLIMIT_DATA, ...)` with a default hard and soft limit of **192 MiB per process** (`HERMES_PYTHON_DATA_MB`, clamped to 96–256 MiB). Linux refuses data-memory allocations beyond that limit; Python allocations may raise `MemoryError` and native libraries may terminate the worker. Its scope is data memory, not total RSS or the sum of all children. Child tools inherit the limit. Node's launcher and the dashboard do not receive this Python cap. The shared 300 MiB worker watchdog still handles multiple workers.

`HERMES_PYTHON_AS_MB` optionally enables `RLIMIT_AS`; it defaults to `0` (disabled). Virtual address-space reservations and thread stacks make a small blanket address-space limit unsafe for native Hermes/Node startup. These allocation limits reduce sudden-growth risk without promising that all container memory, file cache, or every native allocation path is bounded to 192 MiB.
