# Repository audit — 2026-10-07

Reviewed the deployment/bootstrap scripts, Colab and local launchers, chat UI and API, provider configuration, Git storage, memory supervision and task recovery against the pinned Hermes runtime. This is a review of this integration repository, not a claim that every upstream Hermes dependency is bug-free.

## Confirmed issues fixed

| Issue | Fix |
| --- | --- |
| New chat retained temporary gateway ID, breaking later resume | Resolve persistent database ID after prompt acceptance; migrate draft key |
| Reconnect left the composer generating indefinitely | Mark interrupted output and release the composer; preserve saved session identity |
| RPC timers lingered after disconnect or a failed send | Clear pending timers and entries |
| Secret and sudo inputs displayed plain text | Password inputs |
| Explicit Markdown links could be processed again as bare URLs | Protect inline code and links before prose formatting |
| Huge code blocks caused expensive syntax scanning | Preserve full text but skip highlighting above 40,000 characters |
| Damaged metadata silently became empty defaults | Fail visibly; keep damaged file for recovery |
| Concurrent file writes reused one temporary filename | Unique private temporary files, fsync and atomic replacement |
| Failed or oversized uploads left partial files | Clean temporary uploads; publish completed files atomically |
| Shared transcript loaded an entire conversation | Default 80 rows, maximum 500, pagination metadata |
| Blocking export/share database reads ran on the event loop | Use synchronous FastAPI handlers executed in its thread pool |
| Notebook startup printed restored dashboard credential | Explicit password() method |
| Colab updater modified files before validating the installed bridge | Preflight bridge and launcher, verify download hashes, atomic writes |
| Local launcher accepted invalid ports | Validate 1–65535 before starting Docker |

## Validation and remaining limits

Regression tests cover state corruption, concurrent atomic writes, upload failures, private completed files, Markdown and failed WebSocket sends, alongside existing backup, multi-session recovery, cancellation, model routing, history paging and live-stream checks. Desktop/mobile browser checks exercise live partial replies and final responses without reopening, long history, focus controls, keyboard input and reduced motion.

No real Docker image build or live Colab VM was available for this audit. Provider rate limits and outages require provider recovery. A forcibly deleted runtime can lose progress not yet uploaded. Dashboard transport reconnect preserves conversation identity but does not automatically replay a potentially side-effecting interrupted prompt. Concurrent Telegram and dashboard writes to the same conversation still need a shared conversation lock. Google determines Colab lifetime; code cannot guarantee 24/7 hosting or zero OOM events.
