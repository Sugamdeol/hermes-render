# October 15 repository audit fixes

All 12 findings have code changes and regression coverage in this release:

| Finding | Change |
|---|---|
| First chat WebSocket fails | Initialize bridge cleanup using ASGI lifespan before requests |
| Restore follows paths outside the home | No-follow directory descriptors, validated paths and atomic file publication |
| Active-chat deletion bypass | One checked REST operation, pending-turn checks, native ownership leases and shared admission lock |
| False deletion failure | Idempotent deletion; no unconditional second request |
| Incorrect history branch | Stable stored message IDs and a transactional SQL copy preserving reasoning/tool fields; source stays open |
| False dependency readiness | Run-specific installer completion records; monitor waits and preserves errors |
| Bad JSON disconnects chat | JSON-RPC parse/shape errors keep the connection usable |
| Unbounded unanswered requests | Outstanding request cap, expiry and detached old socket references |
| Unrelated search results | Recent rows must match before content hits are merged |
| Removed packages return | Successful inventory replaces previous packages; partial restore cannot erase desired extras |
| Different upload limits | Proxy derives the body limit from the app setting plus multipart overhead |
| Deleted remote files return | Proven obsolete managed files move to an excluded local quarantine; unknown local files stay |

The standalone shell archive is refreshed and checked against source. The Render dependency versions, npm install and image digests are pinned. CI is configured for pull requests, real ASGI/SQLite regressions, native Colab patch compatibility on Python 3.14, and desktop/mobile Chromium layout checks. CI execution and deployment still require publishing the changes.

After merging: save and stop the existing Colab host, then run its automatically updating Start cell. Keep it running if backup fails. Docker/Render requires a rebuilt image. Refreshing only the dashboard does not replace bootstrap/storage/gateway code. Historical pinned hot-updaters should not be used to apply this combined release.

Dependency status remains false until the installer completes successfully. Failed desired recipes are kept. Quarantined files remain under `.restore-quarantine/` locally and are not backed up again. Older restore checkpoints migrate the managed inventory only when their exact proven prior clone is available; otherwise unknown leftovers are retained safely.

The compatibility job checks patch application and recovery regressions. It does not certify a full live Colab installation, every optional extra/provider or Google's runtime lifetime.
