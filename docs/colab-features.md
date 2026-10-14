# Custom features on current Hermes

Colab enables the bundled `render-api-providers` and `hermes-chat-dashboard` plugins once after restoring saved data. The migration preserves providers, keys, manual models and unrelated explicit plugin disable/hide choices. Later plugin choices remain respected. Other installed dashboard plugins with valid or recoverable assets are enabled unless explicitly disabled.

| Feature | Current integration |
| --- | --- |
| Custom OpenAI/Anthropic providers | Provider panel in Models; add, edit and delete endpoints |
| Multiple API keys | Key editing/reveal plus native credential pool integration |
| Missing discovery models | Manual model IDs per provider |
| Chat workspace | Streaming, reasoning, tools, approvals, attachments and model switching |
| Chat organization | Folders, tags, shares, branching, retry/edit and exports |
| Interactive chat | Isolated HTML previews; mode prompt applies to web chat submissions only |
| Plugin diagnostics | Recover stale entry paths, show broken bundle errors, separate duplicate tabs |
| Topic models | Existing Telegram picker isolation patch remains active |
| Updates and runtime | Native Hermes main updater and full Colab runtime remain active |

The authenticated proxy maps matching public origins to its loopback upstream origin, preserving the native WebSocket origin guard. Cross-origin requests remain rejected.

After merging, use the auto-updating Colab launcher and refresh the dashboard. This restoration does not reintroduce the old Render memory limits, single-client chat bridge or turn serialization. Native Hermes provides session execution and recovery behavior. Third-party plugin functionality depends on its compatibility with current Hermes.


## Colab capacity

The v2 profile also upgrades existing Colab installs that already have the v1 marker.
It sets the native session admission cap to **10** and the recovery worker default to 10.
Startup keeps these Colab settings instead of discarding them after restoring Render state.

| Setting | Colab v2 |
| --- | --- |
| Active sessions | 10 |
| Retained live dashboard/TUI sessions | 32 |
| Agent turns | Native unlimited (`null`) |
| Gateway idle timeout | 3,600 seconds |
| Delegation | 10 children, 250 iterations, depth 2, orchestrator enabled |
| Session search concurrency | 10 |
| Goal turns | 50 |
| Scheduled job concurrency | 10 |
| Terminal command timeout | 600 seconds |
| Session Python kernels | 10 |
| Browser idle / command timeout | 300 / 60 seconds |
| File reads | 200,000 characters |
| Tool output | 100,000 characters, 4,000 lines, 4,000 characters per line |

The migration runs once; later dashboard config edits remain yours. Existing higher budgets
and explicit unlimited values survive. Compression remains at the current upstream settings.
The legacy worker launcher no longer forces one-agent cache and allocator settings in Colab.
Provider rate limits, model context sizes and Colab's actual RAM/runtime lifetime still apply.

After this adapter update is merged, save your state and restart through the auto-updating
Colab start cell. Restarting only the dashboard cannot replace the launcher's environment
or the gateway's existing admission slots. No upstream Hermes manual update is needed.

## Immediate state uploads

Colab enables `GIT_STATE_EVENT_WATCH=1`. Linux inotify wakes the existing storage worker
on writes, creates, atomic renames, deletions and SQLite/WAL changes. The observer stays
active during uploads, so another write queues the next incremental save. No chat-stop
or quiet-tree wait applies with the Colab zero-debounce / zero-push-gap settings.

Idle change detection no longer repeatedly walks the entire state tree. The existing
incremental mirror, encrypted SQLite snapshots and batched Git staging still handle each
save. GitHub transfer time and snapshot preparation remain dependent on state size.

Excluded logs/caches do not wake the uploader. Directory watches follow newly created or
moved directories; queue overflow rebuilds the watches and requests a save. If Linux events
are unavailable or fail, metadata polling resumes. The 60-second safety save stays enabled.
`GIT_STATE_EVENT_WATCH=0` opts back into polling. Restart the updated Colab launcher to apply.

## Bundled npm recovery

Backup retains Node's own npm/corepack under `tools/node-<version>-linux-<arch>/lib/node_modules` while still excluding ordinary project dependencies. Earlier backups omitted npm, which caused the dashboard to repeatedly fail with `npm-cli.js` missing. Before starting the Colab dashboard, `repair-node-npm.py` detects that condition and restores npm from the matching official Node release archive after checking SHA-256. Existing Node binaries and session data are retained. Healthy installs skip the download. Network or checksum failures stop this repair rather than silently declare startup successful.

For Hermes releases using the native PM store, startup first checks the exact Node/npm entries selected by Hermes. Missing bundled npm or a failed package check runs the confirmed repair command `hermes pm install node npm`, then validates the entries again before starting the dashboard. Named installs do not use `--tools-only`. Healthy entries skip installation. The existing legacy tools-folder repair remains available for older releases.

Colab large-backup restores no longer abort after one minute below 1 KB/s: its launcher disables Git's low-speed threshold while retaining a 30-minute Git command/restore/startup bound. Existing cached clones stay reusable. Bootstrap logs the storage reader's redacted error instead of discarding stderr, and still refuses startup/backup seeding on an unknown remote state. The relaxed limits do not accelerate GitHub or guarantee a download finishes; they allow slow server pack preparation and downloads to complete.

## Fixed dashboard hostname

Cloudflare Quick Tunnels assign a different `trycloudflare.com` URL to each new process. A fixed URL uses a remotely managed named Cloudflare Tunnel and a hostname on a domain in your Cloudflare account.

One-time setup:

1. Create a named tunnel in Cloudflare Networking > Tunnels. Add a published application route for your chosen hostname, pointing to `http://localhost:10000`.
2. Copy its connector tunnel token (the `eyJ...` value in the installation command).
3. In Colab Secrets, enable notebook access for both settings below.

| Colab Secret | Value |
| --- | --- |
| `CLOUDFLARE_TUNNEL_TOKEN` | Connector token for that named tunnel |
| `HERMES_DASHBOARD_URL` | `https://hermes.your-domain.com` |

The auto-updating launcher reconnects the same named tunnel on every start and displays that configured hostname. It checks cloudflared's loopback readiness endpoint before returning the URL. The Cloudflare hostname route itself must be configured correctly in Cloudflare; local connector readiness does not validate that external route.

Tunnel tokens are read directly from Colab Secrets (or the launcher process environment), passed via a private 0600 temporary file, and removed when the tunnel is stopped. They do not enter Hermes' agent environment or private GitHub backup. The account/domain settings persist outside temporary Colab runtimes. Existing dashboard authentication remains active.

With neither setting configured, the existing temporary Quick Tunnel remains available. Partial or invalid fixed-URL settings display a configuration error rather than switching silently to a random URL. A fixed hostname cannot preserve the previously assigned temporary `trycloudflare.com` address.

## Manual models in native pickers

Colab applies `patch-custom-models.py` during installation and on every startup, including a cached install after a native Hermes update. The patch targets the current split `hermes_cli/model_switch_providers.py` module used by web/TUI and Telegram pickers. Manual IDs are kept first and merged with live/cached discovery results in both canonical and legacy custom-provider rows. An empty or failed discovery cannot hide a manually registered ID. Removed auto-discovered IDs remain removable rather than accumulating forever.

Saving a manual list clears the old `models_discovered` ownership flag in both schemas, preserving per-model metadata and preventing automatic refreshes from replacing that list. Internal legacy catalog markers are omitted from the provider editor. The chat workspace can list up to all 500 IDs accepted by the provider editor, instead of stopping at 100.

After applying this update through the launcher, re-save any custom IDs already lost to a prior discovery refresh. Provider endpoints and keys are retained. Showing a custom ID does not guarantee the API account supports it.

## Faster Colab startup

The launcher avoids recursive ownership changes across restored tools and sessions because Colab runs Hermes as root. Chunked restore stages only large files, reads ordinary files directly from the verified Git clone, and still checks every chunk before writing live data.

On a relaunch in the same VM, restore can reuse local state only when its saved checkpoint matches the current GitHub revision, the local metadata fingerprint, and SQLite content hashes. Remote changes, local edits, a missing checkpoint, or a different repository require the normal restore. Backup writer fencing still runs before reuse. A fresh Colab VM still downloads and restores private state before the gateway starts.

Saved extra applications and libraries install in the background by default on Colab, so their downloads do not hold dashboard and Telegram startup. Core Hermes dependencies and Node/npm repair remain on the startup path. Tools requiring extra apps may be unavailable until `/opt/data/.runtime-dependencies-status.json` reports `ready: true`; installer failures remain in that file and the startup log. Set `os.environ['HERMES_RUNTIME_DEPS_BACKGROUND'] = '0'` before the Start cell to wait for extras instead. Render retains its existing blocking behavior.

Package inventory queries are batched, and successful Playwright system dependency installs are remembered outside the backup for the current VM. A fresh VM still installs those libraries, and a missing browser executable triggers repair. Startup time still depends on GitHub transfer speed and the size of the backup; no fixed startup duration is promised.

## Startup loading panel

The Start cell shows the current step, a bar for completed steps, elapsed time, and an estimated time left. The display updates every second while installs, private restore, service startup, or tunnel connection are running. Plain Python environments print periodic status instead.

Successful starts save stage durations in the public runtime cache's `startup-timings.json`, separately for fresh installs and relaunches. A later similar start estimates remaining time from those measurements. The first start in each category says it is calculating rather than inventing a countdown; a stage that exceeds its previous duration says it is taking longer. Drive cache preserves these measurements across Colab VMs when enabled. Only fixed stage names and timings enter the panel and history, never logs, credentials, or session content. Failed or interrupted starts do not replace successful timing history, and backup/stop controls remain available as before.

## Resume interrupted chats after restart

Colab installs recovery hooks into the native gateway and the dashboard/TUI backend on every boot, including after native Hermes updates. Owned human turns write durable intent before agent work begins. Settled turns are retired only after native transcript persistence. Restart recovery restores every eligible interrupted messaging chat/topic, including older interruption markers, with up to the configured 10 chats executing together and remaining chats queued. Native source authorization, reply-delivery recovery, topic routing, transcript loading and model settings remain in charge. A 10-second scan retries late adapters. Stop/reset commands, changed session identities and deleted database sessions are rechecked before a queued continuation runs.

Dashboard/TUI chats use their separate native turn markers. When a capable client reconnects, a background scan resumes all interrupted chats in the launch profile via native `session.resume`, rather than waiting for each chat to be opened. Browser reconnect is required for this UI path so approvals and questions use the normal connected client. Markers written by live or uncertain sibling processes are not replayed; deleted database rows and opted-out markers are skipped. UI recovery keeps unfinished markers across longer downtime and retains a 10-attempt crash-loop bound. Native cross-process active-session leases still enforce the total budget across surfaces.

Recovery continues from the same saved transcript and tells the agent to check existing results before repeating uncertain actions. It cannot recover progress that never reached the private backup, infer unfinished work from an ordinary idle chat, or recreate markers already cleared by an older build. Completed chats and explicit stop/reset/deletion stay completed or stopped.
