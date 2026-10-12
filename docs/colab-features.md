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
