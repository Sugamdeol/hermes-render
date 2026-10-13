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
