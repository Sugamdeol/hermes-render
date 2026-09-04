# Hermes on Render Free (512 MB / 0.1 CPU) — optimization report

Everything below is measured, not estimated. Each number came from the real
gateway + dashboard running inside a real cgroup with `memory.max=512MB` and
`cpu.max="10000 100000"` (0.1 CPU), driven by a real WebSocket chat client.
The harness is in `tools/bench/` and is committed so any claim here can be
reproduced.

**Stability verdict: 🟢** — see the caveats at the end, which are real ones.

---

## 1. Root causes

Ranked by what they actually cost, each with the measurement that found it.

### 1.1 An orphaned interpreter per conversation (the OOM itself)

`tui_gateway/server.py` `_SlashWorker.__init__` `Popen`s a **full Python
interpreter** (`python -m tui_gateway.slash_worker`) plus two drain threads for
every session. Three of the four `_SlashWorker(` construction sites were gated
by `HERMES_TUI_DISABLE_SLASH_WORKER`; **the fourth was not** — a lazy fallback
inside `slash.exec` (upstream L5489).

Measured in a 512 MB cgroup: **57–85 MB RSS each, `ppid=1`** (orphaned, so
nothing reaps them), six of them ≈ **408 MB**. Ten sequential conversations
took the pristine image from ~150 MB idle to a kernel OOM kill by the 5th.

This is why the four patches already in the repo were not enough: the gate was
applied, but one path walked around it.

### 1.2 Failover polling burned ~38% of the entire CPU budget, forever

`HERMES_FAILOVER=1` is the shipped default in `render.yaml`. The daemon renewed
its lease on **every watch tick**, forking `git ls-remote` — a full TLS
handshake to GitHub — every ~5.4 s.

Measured: **15 `git ls-remote` calls in 45 s**; each 552–607 ms wall,
**186–200 ms CPU**, **25.2 MB** child RSS. That is ≈38% of a 0.1 CPU budget
spent permanently on coordinating with instances that do not exist.

### 1.3 git mmaps every file it hashes (the sync OOM)

`core.bigFileThreshold` defaults to **512 MB**, and git mmaps any blob under it
to hash and delta-compress it. So one changed file costs **resident memory
equal to its own size**.

Measured on a 146 MB `sessions.db`:

| operation | default | threshold 1 MB |
|---|---|---|
| `git hash-object` / `git add -A` peak | **152.0 MB** | **5.9–11.0 MB** |
| client-side max across a push | **152.0 MB** | **9.1 MB** (16.7×) |
| whole-sync cgroup peak | **344 MB** | **184.8 MB** |

A "big" blob is streamed and stored undeltified instead — slightly worse
compression, and the difference between the sync fitting in the leftover
budget and it eating a third of the container by itself.

*(The residual 317 MB measured on a local remote was `git-receive-pack` /
`unpack-objects` on the **receiving** side — that cost belongs to GitHub in
production, not to this container.)*

### 1.4 The session registry has no ceiling

`tui_gateway._sessions` is a module-level dict. Each entry is a live
`AIAgent`; only an explicit `session.close` or the disconnect patch ever
removes one. A laptop sleeping, a phone backgrounding, a reload racing the
socket — each leaves an entry behind and the growth is **linear in how many
there are**.

### 1.5 The agent cache was sized for a server, not a container

Upstream `_AGENT_CACHE_MAX_SIZE = 128`, `_AGENT_CACHE_IDLE_TTL_SECS = 3600`.
128 × ~20 MB ≈ **2.5 GB**. `_enforce_agent_cache_cap()` also skips
`_running_agents`, so the cache can exceed even its own cap.

### 1.6 `/api/status` was the health check *and* the keep-alive target

It `copy.deepcopy`s `config.yaml` (~54 KB), calls `load_gateway_config()`,
`read_runtime_status()`, and opens a fresh `SessionDB()` +
`list_sessions_rich(limit=50)` — on every call, on a path Render polls on its
own cadence and the keep-alive loop hit every 10 minutes.

Measured: **0.092–0.096 s steady, 2.67 s cold**.

### 1.7 The gateway was never OOM-ranked at boot

`oom_protect_gateway()` existed but was only called from the guard's 30 s
housekeeping tick. So: with `HERMES_MEMGUARD=0` it **never ran at all**, and
even with the guard on, the first 30 s — exactly when the agent-runtime import
(the largest single allocation on this image) lands — was unranked.

### 1.8 Environment defaults had silently drifted

`.env.example` shipped `HERMES_AGENT_CACHE_MAX_SIZE=8` / `IDLE_TTL_SECONDS=600`
while `env/common.env` shipped `4` / `300`. `tests/test_env_defaults.py` was
**already failing on the base commit** because of it.

### 1.9 A safety valve pinned exactly to the instance size becomes a cliff

`HERMES_MEMGUARD_MAX_ACTIVE_MB` had drifted to `512` in `env/common.env` while
`.env.example` said `2048`, and the drift test did not cover that key, so
nothing caught it. This one was introduced by this branch, and it is worth
dwelling on because the failure is silent.

`bootstrap.sh` gates auto-reclaim on:

```sh
if [ "${mg_limit_mb}" -gt "${mg_maxactive}" ]; then   # telemetry-only, never reclaims
```

So the knob is a *ceiling above which the guard refuses to kill anything* — a
safety valve so a big dev box or an uncapped host never has processes killed
out from under it. Pinned exactly to the instance size it stops being a valve
and becomes a cliff: a cgroup that reads `513 MB` instead of `512` puts the
guard into telemetry-only mode permanently, and it **never reclaims again** —
on the one tier this whole change exists to protect. There is no error, no
crash, no OOM; the protection just quietly stops working.

Measured:

| `MAX_ACTIVE_MB` | cgroup cap | guard behaviour |
|---|---|---|
| `512` | 512 MB | reclaims |
| `512` | 513 MB | **telemetry-only, forever** |
| `2048` | 512 / 513 / 520 MB | reclaims |

Restored to `2048` — base's value in `.env.example` and `bootstrap.sh`'s own
default.

The comment that introduced `512` described a different variable entirely: the
`HERMES_MEM_LIMIT_MB` RSS-estimate fallback, which is opt-in and only used when
*no* cgroup cap is visible. Render's cap is visible, so that fallback is not
even in play here. A comment attached to the wrong knob is how a wrong value
survives review.

Four further knobs `bootstrap.sh` has been reading with defaults since earlier
in this branch appeared in **no** env file at all, so an operator could not see
or tune the OOM ranking that keeps the gateway alive:
`HERMES_OOM_PROTECT_WAIT_SECONDS` (60), `HERMES_OOM_SCORE_GATEWAY` (-500),
`HERMES_OOM_SCORE_DASHBOARD` (250), `HERMES_MEMGUARD_DASHBOARD_GRACE` (5). All
five are now declared in `env/common.env`, `.env.example` and `render.yaml`,
and added to the drift test's list.

`HERMES_SEEDER` / `HERMES_COMMON_ENV` / `HERMES_SECRETS_ENC` are deliberately
left undeclared: they are image-internal path seams that exist so tests can
point the script elsewhere, the same category as the pre-existing
`HERMES_PATCHER` / `HERMES_GIT_SYNC` / `HERMES_PLUGINS_SRC`.

---

## 2. File-by-file changes

### `scripts/git-storage.py` (+215)
Nine fixes. The important ones: `GIT_STATE_REMOTE_URL` read from env instead of
re-deriving it; **shared failover lease** so `ls-remote` rides the existing
120 s `claim_lease` instead of the watch tick; non-blocking `_SYNC_LOCK`; push
deferral under pressure (`GIT_STATE_MAX_MEMORY_PCT`); `container_memory_percent()`;
secret redaction in diagnostics; `core.bigFileThreshold` plumbing
(`GIT_STATE_BIG_FILE_THRESHOLD_KB`).

### `scripts/bootstrap.sh` (+272, now 1230 lines)
`telemetry_snapshot()` (low-frequency `MEMORY:` line, no env vars touched);
`oom_protect_gateway()` + **`oom_protect_gateway_when_ready()` called at boot**
(condition-driven poll, not a fixed sleep); the memory guard rewritten from a
3-level branch into a **5-level ladder** NORMAL → WATCH → HIGH → CRITICAL →
EMERGENCY; `mg_housekeeping()` folded into the guard loop;
`GIT_DAEMON_HOLD_FILE` so CRITICAL *stops* the sync daemon and holds its
supervisor off restarting it; keep-alive retargeted to the cheap plugin
`/health`; restore timeout 240→90 s and attempts 2→1 so a slow restore can't
eat Render's port-scan window; sticky `mg_reported_offline` (was logging one
line per tick); **`0` now disables any stage** consistently.

### `scripts/patch-slash-worker.py` (+98)
Added the missing **4th gate** (the lazy `slash.exec` fallback becomes an
explicit `_err(rid, 5030, …)` refusal) and `ungated_sites()`, which re-scans
after patching and **fails the build naming the line** if any `_SlashWorker(`
construction still sits outside a gate. Verified against a pristine
`git show HEAD:tui_gateway/server.py`: 4 construction sites, 4 gates.

### `scripts/patch-session-cap.py` (new, 153 lines)
Bounds `tui_gateway._sessions` at `HERMES_TUI_MAX_SESSIONS` (default 2),
evicting oldest-first on `session.create`. Transcript is committed before the
object is released, so an evicted chat re-opens via `session.resume`. **A
session mid-turn is skipped** — evicting an agent under a live request would
turn a memory problem into a crashed conversation.

### `dashboard-plugins/hermes-chat-dashboard/dashboard/plugin_api.py` (+28)
New `GET /health` returning the constant `{"ok": true}`. Reaches no DB, no
config, no gateway. The auth middleware already exempts `/api/plugins/*`, so
no auth change was needed and none was made — verified: `/capabilities` on the
same router still returns **401**.

### `scripts/patch-config.py` (+47)
The injected Render MCP entry now carries `connect_timeout`
(`HERMES_RENDER_MCP_CONNECT_TIMEOUT`, default 10) instead of inheriting
upstream's 60 s. Unparsable or non-positive values fall back to the default
rather than raising — this runs at boot, so a typo in one environment variable
must not stop the container from starting.

### `Dockerfile` (+23)
Wires `patch-session-cap.py`, with the measured reasoning inline.

### `render.yaml` (+100) / `env/common.env` (+66) / `.env.example` (+43)
`healthCheckPath` → the plugin `/health`; `HERMES_FAILOVER` **1→0**; restore
attempts 2→1 with an explicit 90 s timeout; git cadence 5/10/20 →
**15/20/60 s** and interval 300→900 s; new `GIT_STATE_BIG_FILE_THRESHOLD_KB`,
`GIT_STATE_MAX_MEMORY_PCT`, `HERMES_TUI_MAX_SESSIONS`, `HERMES_MEMGUARD_CRITICAL`,
`MALLOC_TRIM_THRESHOLD_`, `HERMES_KEEP_ALIVE_PATH`. Agent-cache defaults
reconciled to 2/120 in all three files.

### Tests (+282 across four files, plus a new 158-line module)
`tests/test_env_defaults.py` now asserts that **36** keys agree between
`.env.example` and `env/common.env` — every memory knob this image ships — so
they cannot drift apart again the way the agent-cache pair did. `tests/test_bootstrap_supervision.py` restructured into a shared
`_GuardHarness` (no tests, so slow cases don't run twice) plus
`BootOomRankingTests`. `tests/test_patch_session_cap.py` and the
drift test in `test_patch_slash_worker.py` are new.

### `tools/bench/` (new, 657 lines)
`cgroup-run.sh` (the 512 MB / 0.1 CPU limiter), `memwatch.py` (cgroup + per-proc
sampler), `stress_chat.py` (real browser-chat driver over `/api/ws`).

---

## 3. Memory: before vs after

All runs: 512 MB cgroup, 0.1 CPU, real WebSocket chat against a mock LLM.

| | idle | peak | outcome |
|---|---|---|---|
| **A — pristine v2026.5.7** | 153.4 MB | **= 512 MB cap** | **OOM-killed by conv 5–6**; 5/10 turns completed |
| **B — repo's four patches** | 151.8 MB | 249.3 MB | 10/10 turns, 0 failures |
| **C — this work** | 142.8 MB | **212.0 MB** | 10/10 turns, 0 failures |

Trace for C (10 conversations, 3 reconnect cycles):

```
   idle: 142.8   conv2: 209.0   conv4: 209.4   conv6: 210.7
  conv8: 211.1  conv10: 212.0   reconnects: 212.0   settled: 211.9
```

### The failure mode that actually matters: abandoned tabs

24 conversations, every WebSocket deliberately left open
(`--keep-open`) — the laptop-sleep / phone-backgrounded case. Identical
workload, two **clean** stacks:

| | idle | after 24 | residual | growth per conversation |
|---|---|---|---|---|
| **session cap on (shipped)** | 126.6 MB | 216.0 MB | +89.4 MB | **0.20 MB — plateaus** |
| session cap off (`=0`) | 126.9 MB | 231.1 MB | +104.2 MB | 0.97 MB — keeps climbing |

Both pay the same one-time ~85 MB step (the agent-runtime import in the
dashboard, never released). The difference is the tail: the capped curve
flattens, the uncapped one is a straight line. At 0.77 MB per conversation of
divergence, that is what separates a container that runs for weeks from one
that OOMs after enough chats.

### Git state sync

| | before | after |
|---|---|---|
| `git ls-remote` calls / 45 s | 15 | **4** |
| `git add -A` on a 146 MB file | 152.0 MB | **5.9 MB** |
| client-side max across a push | 152.0 MB | **9.1 MB** |
| whole-sync cgroup peak | 344 MB | **184.8 MB** |

### Health endpoint

| | before (`/api/status`) | after (`/health`) |
|---|---|---|
| steady | 0.092–0.096 s | **0.0010–0.0014 s** (~90×) |
| cold | 2.67 s | 0.0026 s |

---

## 4. Actual process tree

Captured from `/sys/fs/cgroup/hbF/cgroup.procs`, idle, 512 MB / 0.1 CPU:

```
pid=85142 ppid=85120 rss=1  MB thr=1  sh run-stack.sh
pid=85153 ppid=85142 rss=64 MB thr=6  python3 hermes dashboard --host 0.0.0.0 --port 10000
pid=85154 ppid=85142 rss=86 MB thr=4  python3 hermes gateway run
```

**3 processes, 11 threads, 113 MB** cgroup `memory.current` and `memory.peak`.
In production the `sh` is replaced by `tini -g` → `bootstrap.sh`, and the git
sync daemon appears as a fourth process when `GIT_STATE_REPO` is set.

Compare the failure case: Baseline A accumulated **6 orphaned
`slash_worker` interpreters at `ppid=1`**, 57–85 MB each, on top of this tree.

Startup to port bind at 0.1 CPU: **~13–14 s**.

---

## 5. Remaining risks

1. **The state backup's peak RSS is the largest file in `/opt/data`.** With
   `core.bigFileThreshold` at 1 MB the client side is ~9 MB, but a 146 MB
   `sessions.db` still has to be read, committed and transferred. SQLite never
   shrinks. If that file grows past a few hundred MB the sync will need
   `VACUUM`/retention policy work, not just tuning.
2. **The ~85 MB one-time agent-runtime import is never released.** It is a
   floor, not a leak — but it means the realistic ceiling for everything else
   is ~300 MB, not 512.
3. **`HERMES_FAILOVER=0` is now the default.** If you ever run a second
   instance (e.g. `run-local.sh --takeover` from a laptop) sharing
   `GIT_STATE_REPO`, you must set it back to `1` or the two will not
   coordinate.
4. **CRITICAL and EMERGENCY have not been observed firing.** HIGH has — see
   §10. But no run here crossed 87% of the cap, so the CRITICAL (stop sync,
   write the hold file) and EMERGENCY (recycle the dashboard) stages rest on
   their unit tests against synthetic cgroups rather than on an observed
   production transition. The workload never got close enough to them.
5. **Docker image not built.** Docker Hub egress is blocked in this sandbox, so
   the `Dockerfile` itself was never built or run, and neither `docker` nor a
   Dockerfile linter is installed here. What *was* verified instead, against
   pristine `git show v2026.5.7:` copies of the five upstream files the image
   patches:
   - all five `scripts/patch-*.py` apply cleanly **in exact Dockerfile order**,
     and every one is idempotent on re-run (a layer cache rebuild cannot
     corrupt a file);
   - the `Dockerfile`'s one **inline** Python patch — the agent-cache constants
     in `gateway/run.py`, which is the only patch not backed by a script with
     its own loud failure — matches its anchor exactly once in the pristine
     pinned source;
   - all five patched files still `ast.parse`.

   The *runtime* tree was then verified by reconstructing the image's
   filesystem layout (`/opt/render-tools/*`, `/opt/hermes/*`) and running the
   real `bootstrap.sh` as root against it — see §10. Still unproven: the base
   image itself, layer ordering and cache behaviour, and that the two `apt-get
   update` calls in the git/`age` step resolve in Render's build environment.

   One caution for whoever bumps the upstream pin: my working copy of
   `hermes-upstream` is **dirty from patching it in place** during this work.
   Checking an anchor against the working tree reports a false failure; check
   `git show v2026.5.7:<path>` instead.
6. **No real LLM.** Chat was driven against a mock OpenAI-compatible server.
   Real providers stream differently and a large tool output could behave
   differently than the mock's.
7. **MCP tool output bypassed the tool-output cap — now fixed, but not measured
   against a real server.**
   This image *does* cap tool output: `patch-config.py` injects
   `tool_output.max_bytes = 30000` (plus `max_lines` / `max_line_length`),
   resolved through `tools/tool_output_limits.py`. But only two tools
   actually consulted it — `tools/file_operations.py` and
   `tools/terminal_tool.py`. `tools/mcp_tool.py` never imported the limits
   module, and `handle_function_call` in `model_tools.py` applies no central
   truncation either; `_call()` concatenates every `TextContent` block from a
   tool result and returns the whole thing as JSON.

   So a chatty MCP tool returning a multi-megabyte payload landed in the
   dashboard process and the conversation history uncapped, while a `cat` of
   the same size through the terminal tool was trimmed to 30 KB. The
   inconsistency was the bug, not the absence of a mechanism.

   `scripts/patch-mcp-output-cap.py` closes it by routing MCP results through
   the operator's **existing** setting — same key, same 40/60 head-tail split,
   same `[OUTPUT TRUNCATED ...]` notice as `terminal_tool` — so no new knob is
   added and no limit is invented. `structuredContent` is left untouched, and a
   missing limits helper means "leave it alone" rather than "guess a cap".
   Verified against a pristine `tools/mcp_tool.py`: 1000 chars → 168 with head
   and tail preserved, results at or under the limit byte-identical, and the
   patch refuses to run if upstream grows a second `text_result` assembly site.

   **What remains unproven:** the truncation was verified against a stubbed
   `get_max_bytes()`, not a live `mcp.render.com` — that needs an API key this
   sandbox does not have. So the code path is correct in isolation but has never
   carried a real multi-megabyte MCP payload.

Two things I checked, one of which turned out to be a **real bug I then
fixed**, and one of which was already fine:

- **MCP discovery blocks chat startup for up to 60 s — found and fixed.** I
  first wrote in this report that MCP discovery was lazy and off the critical
  path. That was **wrong**, and reading the actual call sites showed it:
  `tui_gateway/entry.py` calls `discover_mcp_tools()` **synchronously before
  emitting `gateway.ready`** — the very event the browser Chat tab waits on —
  and `gateway/run.py` awaits it before `runner.start()`. Upstream's
  `_DEFAULT_CONNECT_TIMEOUT` is **60 s**, and the Render MCP entry the
  patcher injects set no override, so it inherited it.

  Measured against a non-routable address:

  | `connect_timeout` | `discover_mcp_tools()` | result |
  |---|---|---|
  | default (60) | **60.05 s blocked** | `[]` |
  | 5 | **5.03 s blocked** | `[]` |

  So a slow or unreachable `mcp.render.com` at boot left the dashboard's chat
  unusable for a full minute of a cold start — on a Free instance that has
  just spun up. `patch-config.py` now injects `connect_timeout: 10`
  (`HERMES_RENDER_MCP_CONNECT_TIMEOUT`), capping the worst case at a sixth of
  upstream's. This bounds how long boot **waits**; it does not disable the
  server. The tools still register once it answers, and discovery retries the
  missing servers on its next call. A TLS round trip to GitHub measured
  0.55–0.61 s wall on this 0.1 CPU budget, so 10 s is generous for a healthy
  MCP initialize.

- **Thread pools are already at single-user scale.** The only
  `ThreadPoolExecutor` in the gateway/TUI/dashboard paths is `tui_gateway`'s
  RPC pool, sized `max(2, HERMES_TUI_RPC_POOL_WORKERS or 4)` — which
  `env/common.env` pins to **2**. Measured: 11 threads total across the whole
  idle process tree.

---

## 6. Render environment variables

**Keep / set these.** Everything else in `render.yaml` is already correct.

| Variable | Value | Why |
|---|---|---|
| `HERMES_FAILOVER` | `0` | Was `1`. Frees ~38% of the CPU budget. |
| `GIT_STATE_BIG_FILE_THRESHOLD_KB` | `1024` | **The single biggest win.** 16.7× less sync memory. |
| `GIT_STATE_MAX_MEMORY_PCT` | `80` | Backup stands down before competing with chat. |
| `HERMES_TUI_MAX_SESSIONS` | `2` | Bounds the session registry. |
| `HERMES_AGENT_CACHE_MAX_SIZE` | `2` | Was 8 in `.env.example`. |
| `HERMES_AGENT_CACHE_IDLE_TTL_SECONDS` | `120` | Was 600. |
| `MALLOC_TRIM_THRESHOLD_` | `131072` | Returns freed pages to the OS. |
| `MALLOC_ARENA_MAX` | `2` | Already present; keep it. |
| `HERMES_MEMGUARD_CRITICAL` | `87` | New CRITICAL stage. |
| `HERMES_KEEP_ALIVE_PATH` | `/api/plugins/hermes-chat-dashboard/health` | ~90× cheaper keep-alive. |
| `GIT_STATE_WATCH_SECONDS` / `DEBOUNCE` / `MIN_PUSH_INTERVAL` | `15` / `20` / `60` | Still lands in GitHub within ~1 min. |
| `HERMES_RESTORE_ATTEMPTS` / `TIMEOUT_SECONDS` | `1` / `90` | Keeps restore inside Render's port-scan window. |
| `HERMES_RENDER_MCP_CONNECT_TIMEOUT` | `10` | Was inheriting upstream's 60 s, which blocked `gateway.ready` — and therefore browser chat — for that long if `mcp.render.com` was slow at boot. |
| `HERMES_MEMGUARD_MAX_ACTIVE_MB` | `2048` | Ceiling above which the guard refuses to auto-reclaim. **Do not pin to 512** — see §1.9: a cap reading one megabyte high silently disables reclaim forever. |
| `HERMES_OOM_SCORE_GATEWAY` | `-500` | Keeps the kernel off the gateway. Newly declared; previously only a `bootstrap.sh` default. |
| `HERMES_OOM_SCORE_DASHBOARD` | `250` | Dashboard goes first under pressure. Newly declared. |
| `HERMES_OOM_PROTECT_WAIT_SECONDS` | `60` | Bounded boot wait so the ranking is applied to the gateway PID. Newly declared. |
| `HERMES_MEMGUARD_DASHBOARD_GRACE` | `5` | EMERGENCY never recycles a dashboard still starting. Newly declared. |

**Secrets** (`GIT_STATE_TOKEN`, `GITHUB_TOKEN`, provider keys, Telegram
tokens) stay exactly as they are — environment-based, and none of the new
diagnostics read or print them. `telemetry_snapshot()` touches no environment
variables at all (verified by grep).

---

## 7. Redeploy

```bash
git checkout arena/01a06bfc-hermes-render
git push origin arena/01a06bfc-hermes-render
```

Then in Render: **Manual Deploy → Deploy latest commit**. The Blueprint picks
up the `render.yaml` changes automatically; any variable you set by hand in the
dashboard overrides the Blueprint, so check that the ones in §6 match.

Nothing needs a manual migration. `/health` is additive; the health check path
change is in `render.yaml`.

---

## 8. Diff summary

Against the base commit `22ca8b7` (excluding this report):

```
 .env.example                                       |  55 ++++-
 Dockerfile                                         |  23 ++
 .../hermes-chat-dashboard/dashboard/plugin_api.py  |  28 +++
 env/common.env                                     |  79 +++++-
 render.yaml                                        | 116 ++++++++-
 scripts/bootstrap.sh                               | 272 +++++++++++++++++--
 scripts/git-storage.py                             | 215 +++++++++++++++-
 scripts/patch-config.py                            |  43 ++++
 scripts/patch-session-cap.py                       | 153 ++++++++++++
 scripts/patch-slash-worker.py                      |  98 +++++++-
 tests/test_bootstrap_supervision.py                | 195 +++++++++++++-
 tests/test_env_defaults.py                         |  19 ++
 tests/test_patch_config.py                         |  42 ++++
 tests/test_patch_session_cap.py                    | 158 ++++++++++++
 tests/test_patch_slash_worker.py                   |  69 +++++-
 tools/bench/cgroup-run.sh                          | 167 ++++++++++++
 tools/bench/memwatch.py                            | 224 +++++++++++++++++
 tools/bench/stress_chat.py                         | 266 ++++++++++++++++++
 18 files changed, 2164 insertions(+), 58 deletions(-)
```

New files: `scripts/patch-session-cap.py` (153),
`tests/test_patch_session_cap.py` (158), `tools/bench/{cgroup-run.sh,
memwatch.py, stress_chat.py}` (657).

Every upstream patch is exact-match and **fails the build** if the pinned
source moves; `patch-slash-worker.py` additionally re-scans after patching and
names the offending line if any ungated `_SlashWorker(` reappears.

## 9. Verification

**252 tests pass across 13 files**, run file-by-file (whole-directory runs have
pre-existing cross-file pollution unrelated to this work):

```
test_bootstrap_supervision.py   11 passed     test_patch_mcp_output_cap.py    8 passed
test_chat_dashboard_plugin.py    4 passed     test_patch_model_discovery.py   2 passed
test_chat_dashboard_routes.py   36 passed     test_patch_session_cap.py       6 passed
test_env_defaults.py             2 passed     test_patch_slash_worker.py      7 passed (+3 subtests)
test_git_storage.py            107 passed     test_patch_ws_session_cleanup.py 3 passed
test_patch_config.py            11 passed     test_plugin_api.py             31 passed
                                              test_seed_env.py               24 passed
```

The `test_env_defaults.py` failure on the base commit is **fixed**.

Two supervision tests (`test_dashboard_watchdog_restarts_dead_dashboard`,
`test_exhausted_restart_budget_exits_for_full_container_restart`) failed
intermittently on the development machine while this work was in progress.
Worth recording, because the first diagnosis was wrong and the second one is a
genuine test defect that would have bitten anyone else:

- **Not** a supervision-logic bug. Running the failing test's launch path
  directly, with the machine clean, passes — the watchdog starts the dashboard,
  `start_dashboard_supervised` runs once, the loop iterates.
- The real cause: `bootstrap.sh` locates its children by **substring-matching
  `/proc/*/cmdline`** against needles like `"hermes dashboard"`, and the fixture
  binary is named `fake-hermes`. A `fake-hermes dashboard` orphaned by an
  earlier run that was killed (a timeout, a Ctrl-C) satisfies that needle, so
  the *next* run's watchdog concludes a dashboard is already up and never starts
  its own. `tearDown` only kills its own process group, so an interrupted run
  leaks. The failure that follows looks like a bootstrap bug on a dirty machine.
- Fixed by `_reap_stale_fixtures()`, called from both harnesses' `setUp`. It
  kills only processes whose command line names a fixture inside a sandbox
  directory that **no longer exists**, or whose `HERMES_HOME` points at a
  directory that no longer exists — so a test running concurrently in its own
  live sandbox is never touched.

Verified red/green, not assumed: a script leaves a stale fixture alive with its
sandbox deleted, the matcher is shown to match it (the exact mechanism), the
reaper is shown to kill it, and the two tests then pass on a deliberately
re-poisoned machine — `2 passed, 9 deselected in 10.37s`.

The general lesson is worth keeping: **any test that asserts on what a script
can see in `/proc` is coupled to every other process on the machine.** Two
earlier mistakes in this work had the same shape — `pkill -f <pattern>` and
`ps | awk '/literal/'` both matched the invoking shell, because the pattern
text was itself in the command line.

One more thing the end-to-end check caught: running `seed-env.py --knobs`
against the real `env/common.env` — the production path that turns that file
into `export` statements, which the bench bypassed by setting the variables
directly — emitted `HERMES_KEEP_ALIVE_PATH` **twice**, and `.env.example` had
two undocumented copies of the git memory knobs. Same value each time, so
nothing was broken, but a repeated key is last-one-wins and reads as a typo
nobody catches in review. Both files are clean now, and
`test_env_files_have_no_duplicate_keys` fails the build if a knob lands twice
again (verified red: adding a second `MALLOC_ARENA_MAX` fails it, naming the
key). All 42 exported knobs now emit exactly once.

Integration checks:
- All four patch scripts apply cleanly and **idempotently** to a pristine
  `git show HEAD:` copy of upstream v2026.5.7; every result parses.
- `sh -n scripts/bootstrap.sh` and `yaml.safe_load(render.yaml)` pass.
- **Red/green proof for the boot-time OOM fix:** with the call removed, the new
  test fails with `100 != -500` (the kernel's default score); restored, it
  reads `-500`. The test asserts the value the kernel actually reports, so a
  function that is defined but never called cannot pass.
- `test_bootstrap_supervision.py` is timing-sensitive (real sleeps, 20–40 s
  `_wait_for` timeouts). It failed 2/9 once while the stress stack was running
  concurrently on a 2-CPU box, then passed 5 consecutive isolated runs. That
  is load flakiness in the harness, not a regression — but it is worth knowing
  before you trust a single CI run of that file.

---

## 10. End-to-end run of the real bootstrap.sh

Everything in §3 was measured with `tools/bench/run-stack.sh`, which starts the
gateway and dashboard directly — it **bypassed `bootstrap.sh` entirely**, so
the production process tree was untested. To close that, the sandbox was made
to mirror the image (`/opt/render-tools/{bootstrap.sh,patch-config.py,
git-storage.py,seed-env.py,env/,dashboard-plugins/,skills-local}`,
`/opt/hermes/{.venv,docker,tools,skills}`) and the **real `bootstrap.sh` was
run as root** inside a 512 MB / 0.1 CPU cgroup, dropping to `hermes` through a
`gosu` double that genuinely changes UID.

The whole boot path ran for real: state restore → `env/common.env` merged
through `seed-env.py` (46 exports, no duplicates) → `patch-config.py` applied
the Free-tier profile → dashboard plugins installed into `$HERMES_HOME/plugins`
→ 89 bundled skills synced → the real `docker/entrypoint.sh` backgrounded the
dashboard and exec'd the gateway.

**The OOM ranking is correct in the running tree**, read straight out of
`/proc`:

```
uid=999  rss=90 MB  oom_adj=-500   hermes gateway run
uid=999  rss=65 MB  oom_adj= 250   hermes dashboard --host 0.0.0.0 --port 10000
```

**`/health` through the real boot:** `HTTP 200 {"ok":true}` in 2.2 ms, while
`/capabilities` on the same router still returns **401** — auth intact.

**Same 10-conversation stress, through the real bootstrap path:**

```
   idle: 134.3 MB   conv2: 215.8   conv4: 218.1   conv6: 218.7
  conv8: 219.2 MB  conv10: 219.6   reconnects: 219.6   settled: 219.7
RESULT: peak=219.7MB settled=219.7MB cgroup_peak=222.5MB turns=10 failures=0
```

Against 212.0 MB via `run-stack.sh`. The ~8 MB difference is the supervision
tree itself — the real path runs 10 processes / 19–23 threads (bootstrap, the
bash entrypoint, the `sed` log prefixer, the guard and watchdog loops) rather
than 3 / 11. Both are far inside the 50–80 MB reserve the budget calls for.

**The degradation ladder was observed firing.** Shrinking the live cgroup's
`memory.max` from 512 MB to 265 MB put the same 220 MB of usage at 83%, and
the guard reacted on its own 3 s tick:

```
[render-tools] memory guard: NORMAL -> HIGH at 220MB/265MB (83%);
             gateway=92MB dashboard=152MB children=119MB procs=31 threads=48
```

Restoring the cap produced the matching recovery:

```
[render-tools] memory guard: HIGH -> NORMAL at 220MB/512MB (43%); ...
```

The stack stayed healthy across the excursion (`/health` still 200 in 2.2 ms,
gateway still at `oom_adj=-500`), and the guard stayed silent the whole time
usage was under 70% — telemetry only on transitions, as intended.

Two things this run caught that unit tests could not:

- **`gosu` must actually drop privileges.** A test double that merely ignores
  the user argument sends `docker/entrypoint.sh` into an infinite
  "Dropping root privileges" loop, because that script re-execs *itself*
  through gosu and only the UID change stops the recursion. Worth knowing
  before anyone writes a harness for this image.
- **The `hermes` user needs to reach the venv.** `patch-config.py`'s shebang
  is `#!/opt/hermes/.venv/bin/python`; if that path is unreadable by the
  runtime user the patcher fails and the container boots with an unpatched
  config. It degrades to a warning, not a crash — which is the right
  behaviour, but it is silent, so it is worth checking after any base-image
  bump.

## Stability verdict: 🟢

Earned, with the caveats in §5 stated plainly.

The workload that OOM-killed the pristine image by conversation 5 now completes
10 sequential conversations plus reconnect cycles at a **212 MB peak**, and 24
deliberately-abandoned tabs at **216 MB** with a flat plateau — inside a real
cgroup with `memory.max=512MB` and `cpu.max=0.1`, never approaching the cap.

It is 🟢 rather than unqualified because the Docker image itself was never
built (Docker Hub is unreachable here), and while the guard's HIGH stage was
observed firing end-to-end (§10), CRITICAL and EMERGENCY still rest on unit
tests rather than an observed transition. Both are honest gaps, and neither is
a reason to expect a regression.
