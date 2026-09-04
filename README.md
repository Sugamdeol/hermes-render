# Hermes Agent on Render, free-tier friendly

Deploy [Hermes Agent](https://github.com/NousResearch/hermes-agent) (the self-improving AI agent from Nous Research) on Render as a single **Free web service**, already wired up to your Render account. The image extends the upstream Hermes container with:

- The [Render MCP server](https://render.com/docs/mcp-server) registered in `config.yaml` at boot, so MCP tools appear as `mcp_render_list_services`, `mcp_render_get_metrics`, `mcp_render_list_logs`, etc. The agent gets the full MCP tool catalog that your API key can use.
- The official [render-oss/skills](https://github.com/render-oss/skills) bundle (22 Render skills) pinned at a commit and exposed via `skills.external_dirs`.
- A `render-on-hermes` overlay skill that tells the agent the MCP server is already wired up, that the CLI is not installed, and how to behave when an upstream skill expects either.
- A [dashboard plugin](#adding-api-providers-from-the-dashboard) that adds a "Custom API providers" card to the dashboard's Models page, so new OpenAI/Anthropic-compatible API providers can be added from the web UI instead of hand-editing `config.yaml`.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy-template/api/github/start?template_repo=hermes-render)

The Hermes release and the skills commit are both pinned in the `Dockerfile` for reproducible deploys. This template intentionally has **no persistent disk**, because Render Free does not support one. Hermes runtime state is disposable; keep API keys and other configuration in Render environment variables so they survive restarts and redeploys. The dashboard at the service URL is the primary setup surface.

> **Use at your own risk:** The agent can use every Render MCP tool allowed by `RENDER_MCP_API_KEY`, including tools that mutate resources. Lock down dashboard access and use the least-privileged Render account you can.

## Architecture

```
                            ┌──────────────────────────────────────────────┐
                            │ Render web service (Docker, plan: free)      │
                            │                                              │
   you / external clients   │  ┌────────────────────────────────────────┐  │
   ─────────HTTPS──────────►│  │  hermes dashboard (port 10000)         │  │
                            │  │  - /api/status (healthcheck)            │  │
                            │  │  - browser UI: config / keys / chat    │  │
                            │  └────────────────────────────────────────┘  │
                            │                  │                           │
                            │  ┌────────────────────────────────────────┐  │
   Telegram / Discord /  ◄──┤  │  hermes gateway run (foreground)       │  │
   Slack / etc. (outbound)  │  │  - registers Render MCP @ boot         │  │
   Render MCP @ mcp.render  │  │  - calls mcp_render_* tools            │  │
   ◄──────HTTPS────────────►│  │  - long-polls chat platforms           │  │
                            │  │  - spawns subagents per task           │  │
                            │  └────────────────────────────────────────┘  │
                            │                  │                           │
                            │                  ▼                           │
                            │  ┌────────────────────────────────────────┐  │
                            │  │  /opt/data (ephemeral Free filesystem) │  │
                            │  │  config.yaml, sessions/,               │  │
                            │  │  skills/, memories/, logs/             │  │
                            │  └────────────────────────────────────────┘  │
                            │                                              │
                            │  Image-baked, read-only:                     │
                            │   /opt/render-tools/skills-upstream (skills) │
                            │   /opt/render-tools/skills-local    (overlay)│
                            └──────────────────────────────────────────────┘
```

A single container runs both Hermes processes. The dashboard ([upstream docs](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/web-dashboard.md)) is a side-process that the upstream entrypoint backgrounds whenever `HERMES_DASHBOARD=1` is set; the gateway is the foreground PID. They share `/opt/data` and a PID namespace, which is required for the dashboard's gateway-liveness checks.

The Free service has an ephemeral filesystem. `/opt/data` holds the config (`config.yaml`), FTS5 session database, installed skills, Honcho user models, agent memories, cron job definitions, and logs only for the current instance. Render environment variables are the default durable source for API keys and other secrets; when the git state sync is enabled, the complete `/opt/data` tree, including `.env` and logs, is also restored and backed up. The `render-oss/skills` bundle and the bootstrap that registers the Render MCP server are baked into the image (versioned with each deploy).

## What's pre-baked for Render

The `Dockerfile` adds three layers on top of `nousresearch/hermes-agent`:

| Layer | Path in container | Source | Pinned via |
|---|---|---|---|
| Render skill bundle | `/opt/render-tools/skills-upstream/` | [render-oss/skills](https://github.com/render-oss/skills) tarball | `RENDER_SKILLS_REF` ARG (commit SHA) |
| Hermes-on-Render overlay | `/opt/render-tools/skills-local/` | [`./skills/`](./skills) in this repo | This repo's commits |
| Dashboard plugins | `/opt/render-tools/dashboard-plugins/` | [`./dashboard-plugins/`](./dashboard-plugins) in this repo | This repo's commits |

On every boot, [`scripts/bootstrap.sh`](scripts/bootstrap.sh) restores optional remote state, installs the bundled dashboard plugins into `$HERMES_HOME/plugins/`, and runs an idempotent patcher ([`scripts/patch-config.py`](scripts/patch-config.py)) that adds the Render MCP server, Bynara provider, and external skill directories to `/opt/data/config.yaml` if they're missing:

```yaml
mcp_servers:
  render:
    url: https://mcp.render.com/mcp
    headers:
      Authorization: "Bearer ${RENDER_MCP_API_KEY}"

custom_providers:
  - name: bynara
    base_url: https://router.bynara.id/v1
    key_env: BYNARA_API_KEY

skills:
  external_dirs:
    - /opt/render-tools/skills-local
    - /opt/render-tools/skills-upstream
```

The patcher is **insert-only**: it never overwrites edits you make from the dashboard. The `${RENDER_MCP_API_KEY}` placeholder is resolved lazily at gateway startup, so you can rotate the key from Render's **Environment** tab without rebuilding the image — just restart the service.

> **Why `RENDER_MCP_API_KEY` and not `RENDER_API_KEY`?** The standard name is what the `render` CLI looks for. We deliberately don't ship the CLI in this image (see **Security: agent capabilities**). This is still a normal Render API key with the permissions of the user who created it. The nonstandard env var name avoids accidental CLI auto-auth if you later install the CLI manually. Name your CLI key separately.

## Bynara router support

The Blueprint includes the Bynara OpenAI-compatible router as a custom Hermes
provider. Add `BYNARA_API_KEY` in Render's **Environment** tab (or provide it
when the Deploy button prompts for it). Never commit the key to this
repository; rotate it if it has been shared publicly.

On a fresh instance, when `BYNARA_API_KEY` is present, the boot patcher adds:

```yaml
custom_providers:
  - name: bynara
    base_url: https://router.bynara.id/v1
    key_env: BYNARA_API_KEY

model:
  default: qwen-3.8-max-free
  provider: custom:bynara
```

The default is selected only when the config still has Hermes' upstream
model default. An explicit model/provider choice, or a config restored from
remote storage, is left alone. From the dashboard Chat tab you can change
models with `/model custom:bynara:<alias>`. Available aliases currently
include `agnes-2.0-flash`, `agnes-2.5-flash`, `deepseek-v4-flash`,
`laguna-s-2.1`, `minimax-m3-free`, `mistral-large`, `mistral-medium-3-5`,
`nemotron-3-ultra`, `ox-alpha`, `ox-alpha-bynara`, `qwen-3.8-max-free`,
`qwen3.8-27b`, `stepfun-3.7-flash`, and `tencent-hy3-free`.

The key is referenced through `key_env`, so it is never written into
`config.yaml` by this repository. To reach the agent from Telegram, also add
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_USERS` in Render's **Environment**
tab. Use a comma-separated allowlist if you need more than one Telegram user.

## Adding API providers from the dashboard

By default, adding a new LLM provider means editing `config.yaml` by hand.
This image ships a [dashboard plugin](./dashboard-plugins/render-api-providers)
(`render-api-providers`) that adds a **Custom API providers** card to the top
of the dashboard's **Models** page so you can do it from the browser instead.

From that card you can:

- **Add provider** — name, base URL, API key *or* a `key_env` var, API mode
  (OpenAI `chat_completions` / Anthropic `anthropic_messages`, or auto), and an
  optional default model. The plugin discovers models from every provider's
  `/models` endpoint server-side, including keyless local endpoints and
  Anthropic-compatible endpoints, so browser CORS settings do not matter.
- **Refresh models** — re-query any provider and use the discovered model IDs
  when setting it as the main model. A provider that does not implement
  `/models` keeps its configured default model as a fallback.
- **Set as main** — point `model.provider` at the new provider
  (`custom:<name>`) and pick the model, without leaving the page.
- **Edit / Remove** — update or delete a provider. Removing a provider that is
  currently the main model is blocked until you switch models first.

Each add/edit writes the canonical `providers:` entry in
`/opt/data/config.yaml` (falling back to the legacy `custom_providers:` list
for pre-v12 configs) and applies to **new sessions** — the currently running
chat tab is not affected, matching Hermes' normal model-switch behavior. The
provider immediately shows up in the Models page picker and the `/model`
command.

> **Keys:** prefer `key_env` over pasting an inline API key. Render
> environment variables survive restarts and redeploys; an inline key lives
> only in the (ephemeral) `config.yaml` unless you have [git state
> sync](#keeping-files-between-restarts) enabled. The card lists which
> source each provider uses and never echoes a stored key back to the browser.

The plugin is image-managed: [`scripts/bootstrap.sh`](scripts/bootstrap.sh)
reinstalls it into `$HERMES_HOME/plugins/` on every boot, so the bundled
version always wins over a restored or locally edited copy. To hide the card,
use the visibility toggle on the dashboard's Plugins page — the plugin stays
installed but its slot stops rendering.

## Prerequisites

You need:

- **An LLM provider API key.** [OpenRouter](https://openrouter.ai/keys) is the easiest because it routes to most providers behind a single key. Direct keys for Anthropic, OpenAI, Google, or Hugging Face also work.
- **A Render account** with access to the Free web-service instance type. This template uses `plan: free` and has no persistent disk. Free services spin down after inactivity, can be restarted by Render at any time, and lose their local filesystem state when they stop. That is why provider keys belong in Render's Environment tab, not only in the Hermes dashboard.

The upstream Hermes image is resource-intensive. This Free configuration is intended for personal testing and light, text-only use. The Blueprint caps concurrency and cache growth, keeps native worker pools to one thread, and bounds every known spike a push or a chat session can make on the 512 MB instance. Avoid browser automation and parallel subagents, or upgrade the service plan for heavier use. The image build can also take several minutes on a first deploy.

On first boot, the patcher adds conservative Free-tier defaults: 30 agent
turns, one API retry, one delegation worker, an 8-entry cached-agent cap with
10-minute idle eviction, one session-search worker, earlier context
compression, shorter browser lifetimes, and smaller tool-output/code-
execution caps. Edit `config.yaml` from the dashboard or the restored state if
you deliberately need higher budgets; the profile marker prevents later boots
from overwriting those explicit values.

Optional, depending on which channels you want Hermes to listen on:

- **A Render API key**, if you want the bundled MCP server to inspect or manage Render resources. Generate one at [`dashboard.render.com/u/*/settings#api-keys`](https://dashboard.render.com/u/*/settings#api-keys) and add it as `RENDER_MCP_API_KEY` in Render's **Environment** tab. The agent runs without it, but can't see anything on your Render account.
- **Telegram bot token** from [@BotFather](https://t.me/BotFather), plus your Telegram user ID from [@userinfobot](https://t.me/userinfobot).
- **Discord bot token** from [discord.com/developers/applications](https://discord.com/developers/applications) (enable the Message Content Intent).
- **Slack bot + app-level tokens** from [api.slack.com/apps](https://api.slack.com/apps) (Socket Mode requires both `xoxb-...` and `xapp-...`).

> [!WARNING]
> **A Render API key can expose every workspace linked to your account.**
>
> Hermes can use the key through MCP to inspect any workspace the key's owner can access. Some MCP tools can mutate resources today, and more write-capable tools may be added over time. Use a dedicated low-privilege Render user when possible, and do not paste a personal Owner key unless you accept that risk.

You don't need the optional Render MCP key to deploy. The Blueprint prompts for `OPENROUTER_API_KEY` and `RENDER_MCP_API_KEY` without committing either value. For any other provider or channel token, add the variable in the Render **Environment** tab; Render-managed environment variables survive Free service restarts and redeploys.

## Deploy

### Option 1: Deploy button

1. Click the **Deploy to Render** button above.
2. Pick a workspace and a service name.
3. Render reads `render.yaml`, creates a Free web service, and generates a value for `HERMES_GATEWAY_TOKEN`. The deploy flow can prompt for `OPENROUTER_API_KEY` and `RENDER_MCP_API_KEY`; leave either blank if you do not need it. If you use another LLM provider, add its key in the Render Environment tab.
4. The first deploy builds the image from the `Dockerfile`. Expect several minutes for the upstream pull plus our thin Render tooling and skills layers, followed by a cold-start delay while the gateway boots. Free services can take about a minute to wake after being idle.

### Option 2: Manual Blueprint sync

1. Fork this repo.
2. In the Render Dashboard, go to **Blueprints** → **New Blueprint Instance** and point at your fork.
3. Confirm and apply.

### Protect the URL before configuring

The Hermes dashboard has no built-in authentication. Anyone who knows the service URL can read and write your API keys. Before you visit the dashboard for the first time, choose how you want to protect it:

- Put the service behind an external auth gateway that verifies a bearer token, OAuth session, or trusted identity provider.
- Use an external access layer such as Cloudflare Access or Tailscale Funnel. Render Free services cannot receive private-network traffic.
- Accept the risk for a demo, use low-privilege keys, and delete the service when you're done.

Read the **Security** section before you paste production API keys.

## Post-deploy setup

Once the service is healthy (the **Events** tab shows "Deploy live"), open the URL Render assigned (it ends in `.onrender.com`). You'll see the Hermes dashboard.

The Free instance has no persistent disk. The dashboard writes `/opt/data/.env`, which disappears on its own when Render spins the service down or redeploys it. Enable the optional git state sync to restore that file and the rest of `/opt/data`; Render's **Environment** tab remains the preferred source for secrets because it is injected on every boot even if the state repo is unavailable.

Walk through these tabs in order:

1. **Render Environment**. Set a key for at least one LLM provider. Pick one:
   - `OPENROUTER_API_KEY` from [openrouter.ai/keys](https://openrouter.ai/keys) routes to most providers behind a single key
   - `ANTHROPIC_API_KEY` from [console.anthropic.com](https://console.anthropic.com) for Claude models direct
   - `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `HF_TOKEN`, etc. for the others
   Add your chat platform tokens (`TELEGRAM_BOT_TOKEN`, `DISCORD_BOT_TOKEN`, `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN`, etc.) here as well. Saving Environment changes restarts the service.
2. **Config**. Set the `model` field at the top of the list. The upstream image's default is `anthropic/claude-opus-4.6`, which works as soon as you've set `ANTHROPIC_API_KEY`. Otherwise pick a model your provider supports (for example, `anthropic/claude-sonnet-4.6` for Anthropic, or any OpenRouter model ID like `openai/gpt-5.5`). Config edits are temporary on Free unless you reproduce them after a redeploy.
3. **Status**. Confirm the gateway is running and the model is reachable. The "Connected platforms" list will be empty until you add a chat platform.
4. **Chat**. The in-browser TUI is the easiest way to talk to the agent. Use the **Restart gateway** button on the Status tab after changing keys outside Render's Environment tab.

The dashboard API Keys tab writes to `/opt/data/.env`. That file is durable across replacements only when git state sync is enabled; otherwise use Render's **Environment** tab for secrets. The `RENDER_MCP_API_KEY` exception is still important: set it from Render's Environment tab (not only the Hermes dashboard), since `config.yaml` reads `${RENDER_MCP_API_KEY}` from the gateway process environment.

### Verify the Render tools are wired up

From the dashboard's **Chat** tab, ask Hermes to verify the tools:

```
What Render services are running in my account?
```

The agent should call `mcp_render_list_services` and respond with the list. If it instead tells you "I don't have access to Render tools" or similar, the gateway didn't see `RENDER_MCP_API_KEY` at startup — set it under **Environment** and click **Restart gateway** on the Status tab.

Before you ask the agent to mutate Render resources, read the **Security: agent capabilities** section below. The agent can use every Render MCP tool allowed by your API key.

### Where the "gateway token" fits

The Blueprint generates a `HERMES_GATEWAY_TOKEN` for you. Today, upstream Hermes doesn't read this variable directly at runtime: it's a placeholder for the OpenAI-compatible API server's bearer key. If you opt into the API server, set `API_SERVER_ENABLED=true` and `API_SERVER_KEY` in Render's **Environment** tab, then external HTTP clients can authenticate against `/v1/chat/completions` using `Authorization: Bearer <that value>`. Keeping these values in Render's environment is important on Free because dashboard edits to `/opt/data` are ephemeral.

## Chatting with the agent

The dashboard's **Chat** tab is enabled by default. It is served by the bundled `hermes-chat-dashboard` plugin — a ChatGPT-style web workspace that talks to Hermes' `tui_gateway` over a pure-Python WebSocket (`/api/ws`). Upstream gates that WebSocket behind the same `HERMES_DASHBOARD_TUI` flag as its older terminal-PTY fallback, so the Blueprint sets `HERMES_DASHBOARD_TUI=1`; the memory-heavy path people usually mean by "the TUI" — a per-chat Node/esbuild PTY process behind `/api/pty` plus an xterm.js terminal — is never started by the plugin and only spawns if a client actually opens `/api/pty`. Two more guardrails keep chat cheap on 512 MB: the Dockerfile patches `tui_gateway` so `HERMES_TUI_DISABLE_SLASH_WORKER=1` leaves its per-conversation second HermesCLI Python interpreter (the interactive TUI's slash-menu worker, tens of MB of RSS that is never evicted on tab close) unspawned, and the RPC thread pool is pinned to two workers. If you do not want chat at all, set `HERMES_DASHBOARD_TUI=0` in the Environment tab; for the lightest always-on deployment, connect Telegram or another chat platform instead. You do not need Render Shell or SSH (neither is available for Free web services). A Free service only stays awake while it receives traffic, so an outbound-only long-polling or gateway connection may not keep a bot running continuously; use the dashboard to wake it or upgrade for an always-on service.

The in-container `hermes` binary remains available in the image, but running it requires a local terminal or a paid Render service with shell access. The Free service cannot be used for interactive CLI sessions.

## Run it locally

[`run-local.sh`](./run-local.sh) is a single self-contained script that runs the
same agent on your own machine: the same `Dockerfile` (same pinned Hermes tag
and `render-oss/skills` commit), the same boot patcher, the same overlay skill
and dashboard plugin, and the same environment variables `render.yaml`
declares. It needs only Docker (or Podman).

```bash
OPENROUTER_API_KEY=sk-or-... ./run-local.sh      # build + start on http://127.0.0.1:10000
./run-local.sh logs -f                            # follow the gateway
./run-local.sh cli                                # interactive hermes CLI in the container
./run-local.sh down                               # stop (data volume kept)
./run-local.sh --help                             # all commands and flags
```

Secrets come from a local `.env` (copy `.env.example`) or from exported shell
variables; nothing is written back into the repo. Two deliberate differences
from Render Free: `/opt/data` is a persistent Docker volume, so config,
sessions, memories, and skills survive restarts (add `--data-dir ./hermes-data`
to bind-mount a directory instead), and the dashboard binds to `127.0.0.1`
because it has no authentication. Use `--free-limits` to reproduce Free's
512MB/0.5-CPU squeeze, `--tui` to enable the in-browser chat TUI, and
`--rebuild` after changing anything in `scripts/`, `skills/`, or
`dashboard-plugins/`.

### Running local and Render at the same time

There are two ways to do this. **Coordinated** is the one you want.

#### Coordinated handoff (`--takeover`)

Both instances point at the same state repo and publish a tiny *lease* ref
there. The highest-priority instance with a fresh lease is **active**; everyone
else is **standby**.

```bash
./run-local.sh --takeover      # local is priority 100, Render is 50
```

What happens:

1. Local starts and claims the lease. Within ~30s Render notices, pushes its
   state one last time, and restarts as standby: its chat tokens are stripped
   at boot, so Telegram/Discord messages come only to your laptop. Its
   dashboard stays up.
2. While local runs, **Render never pushes**, so it cannot clobber your state.
3. `./run-local.sh down` sends SIGTERM and waits up to 60s. Local pushes a
   final commit and *then* releases its lease — that order matters, so Render
   restores complete state rather than a stale snapshot.
4. Render sees the lease vanish, restarts as active, restores from the state
   repo, and carries on answering messages.

Failover **fails open**: if the state repo is unreachable or the lease can't be
read, an instance stays active. A coordination outage can leave you with two
agents answering, never zero.

| Variable | Default | Meaning |
|---|---|---|
| `HERMES_FAILOVER` | `1` on Render | Enable lease coordination |
| `HERMES_INSTANCE_PRIORITY` | `50` Render / `100` local | Higher wins |
| `HERMES_LEASE_TTL_SECONDS` | `600` | How long a lease stays valid without a heartbeat |
| `HERMES_LEASE_HEARTBEAT_SECONDS` | `120` | How often the active instance refreshes it |
| `HERMES_LEASE_POLL_SECONDS` | `30` | How often a standby re-checks |
| `HERMES_ROLE_SWITCH_MIN_SECONDS` | `300` | Hysteresis, to stop restart flapping |

Checking costs one `git ls-remote` — priority, identity, and freshness are
encoded in the lease *ref name* — so coordination adds essentially nothing to
bandwidth.

#### Uncoordinated (what happens if you don't)

Two agents that only look like one:

| Shared value | Result |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram allows one poller per token. The second takes over and the other gets HTTP 409; messages land wherever, unpredictably. |
| `DISCORD_BOT_TOKEN` | Both connections get every event, so users get **two replies**. |
| `SLACK_*` / IMAP | Events split across connections, or races decide who claims each message. |
| `GIT_STATE_REPO` | Both instances push to the same state branch. Last writer wins; the other's sessions and `.env` can be overwritten. |

`/opt/data` is per-instance regardless, so the two have separate session
databases, memories, and configs. `run-local.sh up` warns when it is about to
claim a shared identity without `--takeover`; `--isolate` drops those values
entirely and gives you a local-only agent.

### Backup bandwidth

Backing up state is the part of this template that spends bandwidth, because
it happens over and over while the agent runs. The git backend uploads only
what changed: a save costs a few kilobytes instead of a whole `/opt/data`
archive. Measured on a 412 KB SQLite session database with 20 new messages
added between saves, the first save is ~30 KB and each later one **0.6 KB**.

Two things make that work, and both matter if you are tempted to "improve" it:
git compares each save against the previous one and ships only the difference,
and a real SQLite file is mostly unchanged pages between saves, so that
difference is tiny. (Random or already-compressed data does not compress or
diff, so it would transfer in full — agent state is neither.)

The practical effect on a 5 GB/month allowance: git syncing on every change
costs on the order of tens of megabytes a month — a save is only a few KB, so
saving more often is not what costs you. Logs and caches are excluded from the
backup and from the change fingerprint, so log churn never triggers an upload.
See [Keeping files between restarts](#keeping-files-between-restarts) for setup.

The script also works away from the repo: it carries a self-extracting copy of
the build context, so a single copied `run-local.sh` can build and run on its
own. Maintainers refresh that copy with `./run-local.sh update-embed` after
changing the baked-in files.

## Environment variables in the repo

Secrets can live in this repo instead of only in Render's Environment tab —
encrypted, never in plaintext. Two files under [`env/`](./env) hold everything:

| File | Contents | Committed |
|---|---|---|
| `env/common.env` | Non-secret config (`HERMES_*`, `PORT`, resource guardrails) | Plaintext |
| `env/secrets.enc.env` | API keys and tokens, values encrypted with [SOPS](https://github.com/getsops/sops) + [age](https://github.com/FiloSottile/age) | Encrypted |

The encrypted file is a dotenv whose **keys stay readable and whose values are
ciphertext**, so `git diff` shows *which* variable changed without leaking it.

### Setup

```bash
./run-local.sh secrets init     # generate an age key, record the public recipient
./run-local.sh secrets edit     # opens $EDITOR; saves re-encrypted
git add .sops.yaml env/secrets.enc.env && git commit
```

`secrets init` writes the private key to `~/.config/sops/age/keys.txt` and puts
only the **public** recipient in `.sops.yaml`. To let the deployed service
decrypt, paste the `AGE-SECRET-KEY-...` line into Render → **Environment** as
`SOPS_AGE_KEY` (or mount it as a Secret File at `/etc/secrets/age.key`). That
one variable replaces pasting every individual key into the dashboard.

`run-local.sh` needs no extra flags: it decrypts `env/secrets.enc.env`
automatically when the age key is present, and warns and carries on when it
isn't, so a collaborator without the key can still run the agent from their own
`.env`.

### Precedence

Identical locally and on Render, highest first:

1. **The process environment** — Render's Environment tab, or your shell. The
   repo never overrides a live deploy knob, so the dashboard stays the
   emergency override.
2. **An existing `$HERMES_HOME/.env`** — written by the dashboard's API Keys tab
   or restored from the state repo. A key set from the UI is not reverted to
   the committed one.
3. **`env/secrets.enc.env`** — fills in whatever is still missing.

Set `RENDER_TOOLS_SECRETS_FORCE=1` to swap 2 and 3, which is what you want after
rotating a key in git. On boot, [`scripts/seed-env.py`](scripts/seed-env.py)
merges the decrypted values into `$HERMES_HOME/.env` insert-only and exports the
ones the process environment lacks, so `config.yaml`'s `${RENDER_MCP_API_KEY}`
substitution resolves from a committed secret too. Only variable *names* are
logged, never values.

The non-secret knobs in `env/common.env` (`HERMES_DASHBOARD_TUI`, ports, thread
caps, cache sizes) take a different path on purpose: they are exported for the
gateway and dashboard but **never written into `$HERMES_HOME/.env`**, and any
copy of them already in that file is removed at boot. Upstream Hermes loads
`.env` with `override=True` every time a process starts, so a knob persisted
there would outrank Render's Environment tab — a `HERMES_DASHBOARD_TUI=0`
seeded by an older image and carried along in the state backup is exactly how
the Chat tab stayed switched off after the variable had been set to `1`. For
knobs the precedence is therefore just: process environment, then the repo.

### What this does and does not protect

Committing encrypted secrets means the ciphertext is in git history forever. It
is safe against the repo being read — by a collaborator, a fork, or an
accidental public flip — and it is *not* safe against the age private key
leaking, which retroactively exposes every version of every secret ever
committed. Rotate the key by re-running `secrets init` after deleting
`~/.config/sops/age/keys.txt`, re-encrypting, and updating `SOPS_AGE_KEY`.

Never commit: `~/.config/sops/age/keys.txt`, anything matching `*.key`, or a
decrypted copy of the secrets file. `.gitignore` covers the usual names.

## Cost expectations

This Blueprint uses Render's **Free** web-service instance. It has no service charge, but Free usage is subject to Render's monthly instance-hour allowance and the service spins down after inactivity. The next request may wait for a cold start. There is no persistent-disk charge because Free services cannot attach persistent disks.

| Component                      | Plan   | Cost |
|--------------------------------|--------|------|
| Web service (`runtime: docker`) | `free` | $0   |
| Persistent disk (`/opt/data`)  | none   | $0   |
| **Subtotal (this template)**    |        | **$0** |

LLM costs are separate and depend entirely on your provider and usage. OpenRouter and Anthropic both report usage in their respective dashboards; Hermes also surfaces per-model usage on its **Analytics** page. Upgrade the Render service if Free's memory limit causes OOMs or if you need persistent local state.

## Keeping the service awake (keep-alive)

A Render Free web service spins down after roughly 15 minutes without
**inbound** HTTP traffic, and a cold start takes tens of seconds. That is a
poor fit for a chat agent: Telegram, Discord and Slack messages are
*outbound* connections from the container, so they do not count as traffic —
with no help, the service falls asleep in the middle of a conversation and
the agent stops answering until something happens to wake it again.

The image ships with a keep-alive for exactly this. When Render injects
`RENDER_EXTERNAL_URL` (it does for every web service), the boot wrapper
starts a tiny loop that requests the dashboard's own `/api/status` over the
public URL every `HERMES_KEEP_ALIVE_SECONDS` (600 by default — well inside
the 15-minute idle window). The request goes through Render's proxy, so it
counts as traffic and the service stays awake. It runs only where that
variable exists, so local `run-local.sh` containers are unaffected
automatically.

| Setting | Default | Effect |
|---|---|---|
| `HERMES_KEEP_ALIVE` | `1` | Set to `0` to let the service sleep when idle |
| `HERMES_KEEP_ALIVE_SECONDS` | `600` | Seconds between keep-alive requests |

The trade-off is instance hours: awake around the clock, one Free service
uses roughly 720 of the ~750 monthly Free instance hours, which fits, but a
second always-awake Free service would not. If you would rather let the
agent sleep between messages — accepting a cold start on the next one — set
`HERMES_KEEP_ALIVE=0` in Render's **Environment** tab. Sessions and files
still survive the sleep either way when the git state sync is enabled.

## Keeping files between restarts

Render Free cannot attach a persistent disk: when the service restarts, the
container is rebuilt from the image and everything the agent wrote —
conversations, memories, dashboard settings, installed skills — is gone. So the
state has to be copied somewhere else while the agent runs, and copied back at
boot.

One backend does that: a private GitHub repository. It is optional and off
by default — enable nothing and the agent runs with disposable state, which is
fine for trying the template out.

| | Git state backend |
|---|---|
| Uploads | only the bytes that changed |
| Typical save | a few KB |
| When it saves | seconds after anything changes |
| Deletions | mirrored |
| Needs | a private GitHub repo + token |

**Boot order, and the one exception.** At boot the agent restores from GitHub
whenever the state branch already holds anything. The exception is the very
first launch: that branch is empty, so there is nothing to restore and the
tree this instance builds locally is the only copy that exists. The boot
wrapper flags the situation and hands the branch to the git sync daemon, which
seeds it with that state once the gateway is up. GitHub is the durable store
from then on.

The seed runs in the daemon rather than in the boot wrapper on purpose. A
first state push is the largest upload this service ever makes, and everything
the wrapper does happens before the container binds a port — Render fails a
deploy whose service never opens one (`Port scan timeout reached, no open
ports detected`). A slow GitHub response during that push used to be enough to
push the dashboard past the scan window and take the deploy with it. Now the
port binds first and the seed follows a few seconds later, and a failed attempt
costs a retry on the daemon's next tick rather than a redeploy.

That handoff is guarded: only a branch the daemon has confirmed is empty gets
seeded, and never one it simply could not reach. If GitHub is unreachable at
boot, the local tree is not pushed over state this instance never restored
from — the daemon waits for a boot that restores cleanly.

**Shutdown order, and the flush window.** The mirror works the same way in
reverse when the service stops. A stop signals the whole process group: the
gateway exits almost immediately, and without help the container would be torn
down with it — taking the sync daemon down mid-push and silently dropping
whatever had changed in the previous few seconds. That is the bug behind
"the GitHub files are not synced after restart": every restart lost the tail
of the last session. The boot wrapper now supervises the upstream entrypoint
instead of replacing itself with it, and after the gateway exits it holds the
container open for `HERMES_SHUTDOWN_FLUSH_SECONDS` (20 by default — keep it
below Render's 30s stop grace period) while the daemon lands its final push
and releases its failover lease. The final push is deliberately not forced:
when everything is already on the branch it is a no-op, so a quiet restart
costs a second or two, not a commit.

A failed restore at boot also no longer poisons the whole runtime. Restoring
is retried while boot time allows (`HERMES_RESTORE_ATTEMPTS`, 2 by default),
because a single transient GitHub blip at boot used to leave the instance
running with state sync disarmed until the next restart. The daemon process
itself is supervised twice over: unexpected errors are caught and logged
inside the daemon (one bad tick costs a backoff, not the backup), and if the
daemon *process* ever dies, the boot wrapper restarts it
(`HERMES_SYNC_DAEMON_RESTARTS` restarts before giving up).

where the branch points before believing the failure — which is what the
`Everything up-to-date` in that same error message is telling you.

### Configure the git backup

**1. Create a private repository for the state.** On GitHub, make a new repo —
`hermes-storage` is a good name — and set it to **Private**. It must hold
nothing else: this backend force-pushes and rewrites the branch, so it is not a
repo you also commit to by hand.

> The backup contains your agent's chat history, memories, and potentially API
> keys. The backend refuses to push at all unless GitHub confirms the repo is
> private, so a mistake here stops the backup rather than publishing your data.

**2. Create a token.** A *fine-grained personal access token* is a password
scoped to specific repositories, so it cannot touch the rest of your account.
Go to **GitHub → Settings → Developer settings → Personal access tokens →
Fine-grained tokens → Generate new token**, then:

- **Repository access:** Only select repositories → your `hermes-storage`.
- **Permissions:** Repository permissions → **Contents: Read and write**.
  Nothing else is needed.
- Set an expiry you are willing to renew, and copy the token once — GitHub
  will not show it again.

**3. Add two variables in Render → Environment:**

```
GIT_STATE_REPO   = yourname/hermes-storage
GIT_STATE_TOKEN  = github_pat_...
```

That is the whole setup. On the next deploy the boot log will show
`started git state sync (delta uploads on change)`.

#### First launch: filling an empty repo

The state repo starts out empty, so there is nothing in it to restore. On that
first boot the wrapper notices, keeps whatever this instance builds locally,
and leaves the branch to the sync daemon, which seeds it once the gateway is
up:

```
[render-tools] github state branch is empty (first launch); the sync daemon seeds it after boot
[render-tools] state restore finished in 2s (source=fresh)
[render-tools] github has no state yet; the sync daemon seeds it from this instance after boot
[render-tools] started git state sync (delta uploads on change)
...
[hermes-git-state] state branch state does not exist yet; starting a new one
[hermes-git-state] seeded yourname/hermes-storage@state; github is now the primary state store
```

The last two lines land a few seconds after the dashboard starts rather than
before it, which is the point: the port is already bound, so a slow first push
cannot fail the deploy.

Every later boot restores from GitHub. The seed refuses to run against a
branch that already holds state, so a stale local tree can never overwrite
newer GitHub state — `GIT_STATE_SEED_FORCE=1` is the deliberate override if
you ever want the local copy to win.

#### When a push does not get through

A state push is one HTTP request, and GitHub's front end drops big ones:

```
[hermes-git-state] git push failed (attempt 1/3); retrying in 5s: error: RPC failed; HTTP 408 curl 22 ...
```

Three things keep that from becoming a lost backup. The request is sent
unchunked, with a `http.postBuffer` large enough for a whole state tree
(`GIT_STATE_HTTP_POST_BUFFER_MB`, 250 by default), because git chunks anything
bigger and chunked uploads are what draw the 408. A stalled transfer is
abandoned after `GIT_STATE_HTTP_LOW_SPEED_TIME` seconds instead of hanging, and
no git command may run longer than `GIT_STATE_GIT_TIMEOUT_SECONDS`. And because
a cut-off response often means the push *did* land, the backend asks the remote
where the branch points before believing the failure — which is what the
`Everything up-to-date` in that same error message is telling you.

#### Saving on change instead of on a timer

Saves are driven by what the agent does, not by a clock. The daemon
fingerprints `/opt/data` every few seconds and pushes once the tree has been
quiet for `GIT_STATE_DEBOUNCE_SECONDS`, so a new message or a dashboard edit
reaches GitHub within seconds. One message can touch the session database, the
memory index and the config within a second, so the debounce turns that burst
into a single commit rather than four half-written ones.

| Setting | Default | Effect |
|---|---|---|
| `GIT_STATE_WATCH_SECONDS` | `5` | How often the tree is fingerprinted |
| `GIT_STATE_DEBOUNCE_SECONDS` | `10` | How long state must stay quiet before it is pushed |
| `GIT_STATE_MIN_PUSH_INTERVAL_SECONDS` | `20` | Hard floor between two pushes, so a frantic agent cannot churn commits |
| `GIT_STATE_INTERVAL_SECONDS` | `300` | Safety-net sync, in case a change slips past the fingerprint |
| `GIT_STATE_WATCH` | `1` | Set to `0` for interval-only saves |

Lower `GIT_STATE_DEBOUNCE_SECONDS` (and the min gap) for near-instant saves;
raise them if you would rather trade freshness for fewer commits. The
`GIT_STATE_INTERVAL_SECONDS` sync is deliberately still there: a fingerprint
is metadata-based, so an edit that rewrites a file to the same size within one
mtime tick can slip past it.

The upload is a real delta, not just a delta commit. The daemon remembers what
it copied last time (size, mtime and mode per file) and re-copies only what
changed, so a 30 MB session database that gained one message costs one small
copy instead of re-reading the whole tree on every push. On a 512 MB Free
instance that matters beyond bandwidth: re-reading every file per push spikes
the daemon's memory by the size of the largest file, and when the OOM killer
comes to reclaim it, it can just as well take the gateway — which is one way
an agent ends up "stopping mid-session".

#### What gets backed up, and what deliberately does not

Everything under `/opt/data` is mirrored into a `data/` folder in the repo,
minus logs and caches (logs, `__pycache__`, `node_modules`, tmp files, and the
like), plus a `MANIFEST.json` recording what was saved and when.

A restore is meant to hand back the tree that was saved, so three things git
cannot carry on its own ride in the manifest instead: **file permissions**
beyond the executable bit (a `0600` key would otherwise come back as a
world-readable `0644`), **empty directories** (git has no such thing), and
**relative symlinks** that stay inside the data directory. A symlink with an
absolute target, or one that climbs out with `..`, is skipped and logged —
restoring it would be a way to write outside the restored tree.

**Deleting a file locally deletes it from the repo.** The backend rebuilds the
mirror from scratch each time rather than adding to it, so a memory you delete
does not quietly live on in the backup. (Older *versions* still exist in git
history until the next squash — see below.)

**`.env` is included, config files and all.** The point of the backup is a
copy you can restart from, and a state branch without the dotenv is missing
the keys the agent runs on. `GIT_STATE_ENV_MODE` decides how it is stored:

| `GIT_STATE_ENV_MODE` | What lands in the branch |
|---|---|
| `plaintext` (default) | `.env` committed verbatim |
| `encrypt` | `.env` sealed to `GIT_STATE_AGE_RECIPIENT` with `age`, stored as `.env.enc` |
| `omit` | `.env` left out of the backup entirely |

The default is what it is because it is what makes the backup complete — and it
is only ever pushed to a repository the backend has
[confirmed is private](#configure-the-git-backup). Be deliberate
about it anyway: a private
repo is not an encrypted one. Anyone who later gains read access to the repo,
or any token with access to it, can read a plaintext `.env` out of git history,
and history survives deletion until the next squash. The backend logs a warning
on every start that commits one.

`encrypt` is the middle ground: the branch still carries your keys, but nobody
who reads it can use them without the age private key (keep it in Render's
Environment tab as `SOPS_AGE_KEY`). Under `encrypt`, if no recipient is
configured or the `age` binary is missing, `.env` is **omitted rather than
committed in the clear** — the one thing that never happens silently.

#### Keeping the repo from growing forever

Git never forgets, which is usually the point and here is a liability: every
version of every file stays in history, and GitHub starts complaining past
about 5 GB. After `GIT_STATE_MAX_COMMITS` saves (200 by default) the backend
squashes the branch down to a single commit containing only the current state
and force-pushes it. Old versions are discarded; the latest state is untouched.

That squash re-uploads everything, so it is a deliberate trade: 200 cheap saves,
then one expensive one. Raising the number makes the repo bigger between
squashes; lowering it makes the expensive save more frequent.

#### Failover

If you run a second instance (say a laptop with `./run-local.sh --takeover`),
the two coordinate through the state repo — see
[Running local and Render at the same time](#running-local-and-render-at-the-same-time).
Checking who is in charge is a single `git ls-remote`, a few hundred bytes, so
polling for it is not what costs you bandwidth.

## Updating

Both pinned versions live in the [`Dockerfile`](Dockerfile) as build args:

```dockerfile
ARG HERMES_IMAGE=docker.io/nousresearch/hermes-agent:v2026.5.7
ARG RENDER_SKILLS_REF=1b8496570748203351f628b2ae738805ac2c23d5
```

Bump either, commit, and push. Render won't auto-deploy (the Blueprint sets `autoDeployTrigger: off`); trigger a manual deploy from the Dashboard or the [Render CLI](https://render.com/docs/cli) on your own machine:

```bash
render deploys create <service-id>
```

Free has no persistent disk: `/opt/data` is recreated from the image whenever the service is redeployed or restarted after a spin-down. Without the optional git state sync, sessions, memories, installed skills, and dashboard config are disposable. With the state repo configured, those files are restored from and backed up to it. The upstream entrypoint still runs its manifest-based `skills_sync.py` on each boot, and the `render-oss/skills` bundle plus the `render-on-hermes` overlay live under `/opt/render-tools/` (the image layer), so the bundled skills are restored on every boot. Keep secrets outside the backup in Render environment variables.

Hermes ships fast: roughly weekly tagged releases, each with around 180 commits. Check [the upstream releases page](https://github.com/NousResearch/hermes-agent/releases) before bumping `HERMES_IMAGE`. The [skills repo's commit log](https://github.com/render-oss/skills/commits/main) is the source of truth for `RENDER_SKILLS_REF`.

## Troubleshooting

### Logs

Render keeps logs in the **Logs** tab of your service. Filter by stream:

- The dashboard side-process prefixes its lines with `[dashboard]`.
- Gateway and agent logs are unprefixed.
- For deeper inspection, log files also live on disk at `/opt/data/logs/` (`agent.log`, `errors.log`, `gateway.log`).

These logs are also ephemeral and disappear when the Free instance is replaced. Use Render's **Logs** tab for the current instance; Free web services do not provide Dashboard Shell or SSH access.

### Shell access

Free web services do not include Render Shell or SSH. Use the dashboard's **Chat** tab, Render's **Logs** tab, or a configured chat platform instead. The CLI can be run from a local Hermes installation or after upgrading this service to a paid instance with shell access.

The container runs the gateway as the `hermes` user (UID 10000), not root.

### Service won't start

Check the **Events** tab for the deploy that failed, then the **Logs** tab around that timestamp.

| Symptom                                              | Likely cause                                                                 |
|------------------------------------------------------|------------------------------------------------------------------------------|
| `Refusing to start: binding to 0.0.0.0 requires API_SERVER_KEY` | You set `API_SERVER_ENABLED=true` and `API_SERVER_HOST=0.0.0.0` without an `API_SERVER_KEY`. Set the key or flip back to `127.0.0.1`. |
| Health check fails on `/api/status`                  | `HERMES_DASHBOARD` is unset or the dashboard crashed. Check `[dashboard]` lines for a Python traceback. |
| `Port scan timeout reached, no open ports detected`  | The container never bound `$PORT` inside Render's scan window, because the boot wrapper was still working when it expired. `[render-tools] state restore finished in Ns (source=...)` in the logs says how long the blocking part took; `HERMES_RESTORE_TIMEOUT_SECONDS` (240) caps it. The GitHub state seed is no longer part of it — a slow first push there used to eat the whole window, so it now runs in the sync daemon after the dashboard is up. |
| Container OOM-killed                                 | Free has 512 MB and Hermes is a heavy Python image. This template bounds the known spikes: git state pushes run with a 64 MB `http.postBuffer` and capped `pack.windowMemory`/single-threaded pack (`GIT_STATE_HTTP_POST_BUFFER_MB`, `GIT_STATE_PACK_WINDOW_MEMORY_MB`, `GIT_STATE_PACK_THREADS`), the state-sync daemon is OOM-scored so the kernel reclaims it before the gateway, the gateway caches at most 8 session agents (`HERMES_AGENT_CACHE_MAX_SIZE`), and dashboard chat never spawns the slash-command worker subprocess. If light text-only use still OOMs — typically browser/Playwright tasks or parallel subagents — raise those limits back only if you know you need them, or upgrade the service plan. |
| The agent stops answering mid-session, and the dashboard is slow or 502s until you open it | On Free, the service spun down: chat platforms are outbound-only, so a quiet dashboard lets the ~15-minute idle timer expire even while you are chatting. The bundled keep-alive prevents it (see **Keeping the service awake**); check the boot log for `keep-alive: requesting /api/status`. If it is present but the service still sleeps, make sure `HERMES_KEEP_ALIVE` has not been set to `0`. |
| The last chat messages or file edits are missing after a restart | The final state push was cut off. Since the fix, the container stays up for `HERMES_SHUTDOWN_FLUSH_SECONDS` (20s) after the gateway exits so the sync daemon can land its last push — if you forked an older version of `bootstrap.sh`, the wrapper must supervise the entrypoint instead of `exec`-ing it. Anything older than that window was already on the branch; check `pushed state:` lines in the logs for the last successful save. |
| `[render-tools] warning: sync daemon exited (status N); restarting` | The sync daemon process crashed; the wrapper restarted it (up to `HERMES_SYNC_DAEMON_RESTARTS` times). If it repeats, look one line up in the logs for the actual error. |
| API keys or sessions disappear                      | Render's **Environment** tab is the durable fallback. If dashboard-managed keys, sessions, logs, and files must survive a cold start/redeploy, configure the git state sync (`GIT_STATE_REPO` + `GIT_STATE_TOKEN`). |
| `Warning: Input is not a terminal (fd=0)` then `Goodbye!` when running `hermes` | Free services have no shell/SSH. Chat from the dashboard's **Chat** tab or a configured platform; run the CLI locally instead. |
| `Goodbye! ⚕` in the deploy logs followed by 502s on the URL | The Dockerfile's `ENTRYPOINT` got bypassed somehow (forked the template and overrode it, or set a `dockerCommand` in `render.yaml` without the full upstream chain). The default `ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/opt/render-tools/bootstrap.sh"]` + `CMD ["gateway", "run"]` must stay intact. |
| `Refusing to run the Hermes gateway as root` | Same root cause as above. Restore the Dockerfile's `ENTRYPOINT`/`CMD` so the upstream `entrypoint.sh` can do its `gosu` drop. |
| **Chat** tab missing from the sidebar, opening **Chat** from the **Plugins** page lands on **Sessions**, or chat won't connect ("gateway WebSocket failed" / 4403) | The bundled `hermes-chat-dashboard` plugin serves the Chat tab (it overrides `/chat`) and talks to `/api/ws`. Upstream only mounts a `/chat` route — built-in or plugin override — and only answers that WebSocket when the dashboard process started with `HERMES_DASHBOARD_TUI=1`; otherwise the Plugins page still lists Chat with an "Open tab" link, but `/chat` has no route and the dashboard's catch-all redirects it to **Sessions**. The plugin now shows an orange banner on the Sessions/Plugins pages when it detects this state. Two causes: (1) the variable is `0` or unset in Render's Environment tab — set it to `1` and restart; (2) **a stale `HERMES_DASHBOARD_TUI=0` in `/opt/data/.env`** — upstream loads that file with `override=True` at every start, so it silently beats the Environment tab. Images built from this repo before the fix seeded that file with the old `common.env` defaults, and the git state backup restored it on every boot. Current images remove deploy knobs from `.env` at boot (look for `removed N deploy knob(s) from /opt/data/.env` in the logs), so **redeploy on the latest image** and the setting from the Environment tab wins again. The plugin itself must also be present in `$HERMES_HOME/plugins/hermes-chat-dashboard/` — bootstrap installs it from the image on every boot. If you disabled `HERMES_DASHBOARD`, the tab cannot exist. |
| Dashboard **Chat** terminal (xterm PTY) shows "Chat unavailable: 1" or hangs / 500s on `/api/pty` | Only relevant to the old in-terminal chat (`/api/pty`), which the bundled plugin does not use. Two upstream bugs combined to break it on hosted deploys: (1) [#20500](https://github.com/NousResearch/hermes-agent/issues/20500): `/opt/hermes/ui-tui/` ships root-owned but the dashboard runs as the `hermes` user, so the runtime esbuild rebuild fails with `EACCES`. (2) Separate filename mismatch: `_hermes_ink_bundle_stale()` in `hermes_cli/main.py` looks for `packages/hermes-ink/dist/ink-bundle.js`, but `@hermes/ink`'s build script (`esbuild src/entry-exports.ts --outdir=dist`) only produces `entry-exports.js`. The bundle the staleness check expects is never created, so every `/api/pty` connect runs a 28-second `npm run build` that exceeds Render's WebSocket-upgrade timeout. The Dockerfile chowns the directories AND `touch`es the two expected paths at build time so both checks short-circuit. If you've forked the template and removed those lines, restore them. |
| `mcp_render_*` tools missing from Hermes' tool list | The gateway started without `RENDER_MCP_API_KEY`. Add it under the service's **Environment** tab and click **Restart gateway** from the dashboard's Status tab. |
| Agent says it tried to run `render <something>` and got `command not found` | Working as designed — the Render CLI is not installed in this image (see **Security: agent capabilities**). Most CLI capabilities have an MCP equivalent the agent should use instead; the rest (live log streaming, `render psql`, SSH) the user runs from their own machine. |
| `[render-tools] config patch failed; continuing` in the boot logs | Non-fatal. The agent still runs; you just won't see the Render MCP server until you fix it. Usually means `/opt/data/config.yaml` isn't valid YAML — fix it from the dashboard or wipe it (see "Forcing a clean rebuild"). |
| `tirith security scanner enabled but not available`  | Harmless. Tirith is an optional Rust-based command scanner; without it, Hermes uses pattern matching. Ignore unless you specifically want native scanning. |

### Changing env vars

Set, change, or delete env vars under the service's **Environment** tab. Render restarts the container after a save. Hermes also exposes a `/reload` slash command for in-session reloads if you've already started chatting from the CLI; it's not relevant for the gateway, which restarts cleanly.

### Forcing a clean rebuild

Free instances are disposable by design. If the Hermes data directory gets into a bad state (corrupt session DB, partial skill install), trigger a new deploy from the Render Dashboard. With the state sync disabled, the new instance starts with a clean `/opt/data` directory and reseeds defaults. With it enabled, the bad state is restored again on boot — delete the `data/` tree on the state branch (or the whole branch) before redeploying if you also need to discard the saved state. Render Environment variables are re-injected automatically.

There is no persistent-disk snapshot to restore on Free; the private state repo is the available backup.

## Security

There are two distinct security surfaces in this template, and they compound:

1. **Dashboard auth.** Hermes' web dashboard has no authentication. Anyone who reaches the URL can read your provider keys, change configuration, and chat with the agent.
2. **Agent capabilities.** The agent has access to a Render workspace API key via MCP. Depending on that key's role, it can restart services, change env vars, trigger deploys, and run SQL against Render Postgres.

The two compose into a worst case: an unauthenticated user reaches the dashboard, chats with the agent, and asks it to "delete all services in this workspace." This template registers the full Render MCP tool catalog and **does not install the `render` CLI**. The dashboard lock is on you.

### Agent capabilities

The agent can reach Render through MCP. The boot-time patcher registers `mcp_servers.render` without a `tools.include` filter, so Hermes sees every tool exposed by the Render MCP server. The effective permission boundary is the Render role behind `RENDER_MCP_API_KEY`, across every workspace that key can access.

This is intentionally permissive. It avoids tool visibility surprises, but it means the agent can call write-capable tools when the API key allows them. Even if most MCP usage is read-oriented today, treat the dashboard URL and API key like an admin surface.

#### Why we don't ship the Render CLI

The [`render` CLI](https://render.com/docs/cli) is useful for local operator workflows, but this image does not install it. MCP is the supported in-container Render integration. If you need the CLI, install it deliberately and inspect any installer before running it.

The variable bound in the gateway environment is named `RENDER_MCP_API_KEY` rather than the stock `RENDER_API_KEY` so a manually installed CLI does not auto-authenticate from this var. This does not create a different kind of API key. The Render account role behind the key limits agent capabilities.

This trade-off is worth revisiting once Render adds scoped API keys. A read-only-scoped key for routine inspection and a write-scoped key for deliberate actions would be a better posture.

#### Concrete steps to harden further

- **Scope the API key with a workspace member role.** Create a separate Render workspace member with the minimum role you need and use that user's API key for `RENDER_MCP_API_KEY` instead of an Owner key. The agent inherits whatever role the key grants. This is the closest thing to scoped keys available today.
- **Lock the dashboard.** Put authentication or private-network access in front of the service. Without that, anyone reaching the URL can ask the agent to do anything within whatever caps you've set above.

The bundled `render-on-hermes` overlay skill tells the agent that MCP is already configured and that CLI installation is not an automatic fallback. But **do not rely on agent-side guardrails for safety**. An LLM cannot meaningfully self-restrict. Dashboard access control and a least-privileged API key are the real defenses.

### Dashboard access

Even if the Render API key cannot mutate resources, the dashboard still leaks your LLM provider keys to whoever reaches it. Anyone who can chat with the agent can ask it to do anything the API key allows. Lock the dashboard down before pasting any keys.

Two practical options.

#### Option A: Auth gateway

Expose a small authenticated proxy in front of Hermes. The proxy verifies a bearer token, OAuth session, or identity-provider token, then forwards approved traffic to the Hermes URL. If the proxy is another Render service, remember that a Free Hermes service cannot receive private-network traffic; use an external proxy or upgrade Hermes to a paid instance for private networking.

This is the most portable option because it does not depend on static client IPs.

#### Option B: Tailscale

On Render Free, a Tailscale sidecar/private-network path is not supported because Free web services cannot receive private-network traffic. Use an external access layer such as Cloudflare Access or Tailscale Funnel, or upgrade Hermes to a paid instance before using a private Render-sidecar design.

#### Notes

- These options compose. For example, an auth gateway can still sit behind a private network path.
- The bundled [render-api-providers](#adding-api-providers-from-the-dashboard) plugin is one extension of the same unauthenticated dashboard surface. Upstream excludes `/api/plugins/*` from the dashboard's session-token middleware, so the plugin's backend re-checks that token on every route and refuses to serve (503) rather than run ungated if the check is unavailable. Still, this does not replace locking the dashboard down.
- The OpenAI-compatible API server (`API_SERVER_ENABLED=true`) is separate from the dashboard. It uses a bearer token (`API_SERVER_KEY`), so it's safe to expose with a long random key, but this Blueprint doesn't route it publicly.
- For broader Hermes security guidance see the [upstream security doc](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/security.md).

## What this template does and doesn't do

What it does:

- Pins a specific upstream Hermes image and `render-oss/skills` commit for reproducible deploys.
- Runs the Hermes gateway and dashboard inside one container, the way upstream supports.
- Uses the upstream-default `HERMES_HOME` path (`/opt/data`) on the Free service's ephemeral filesystem.
- Bakes the official Render skill bundle into the image, plus a small `render-on-hermes` overlay skill that tells the agent how to behave on this host.
- Idempotently patches `config.yaml` on each boot to register the Render MCP server, the Bynara custom provider, the full MCP tool catalog available to your API key, and conservative Free-tier concurrency/cache defaults, without overwriting explicit edits.
- Backs up every regular file under `/opt/data` to a private GitHub repo within seconds of any change, uploading only the bytes that changed, and restores from it at boot.
- Seeds an empty state branch from the tree the instance builds on a first launch, once the gateway is up, so the port bind never waits on a push.
- Refuses to seed a state branch it could not confirm was empty, and refuses to push a tree it never restored from over GitHub state the branch already holds, so an unproven copy cannot silently replace a newer one.
- Generates a `HERMES_GATEWAY_TOKEN` and marks `BYNARA_API_KEY`, `OPENROUTER_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS`, `GIT_STATE_REPO`, and `GIT_STATE_TOKEN` as `sync: false` so secrets never sync from the repo.
- Sets a healthcheck that probes the dashboard.

What it deliberately doesn't do:

- **It doesn't install the `render` CLI.** MCP is the supported in-container Render integration. Install the CLI only as a deliberate operator choice.
- It doesn't try to add authentication on top of the dashboard. Use an auth gateway, private network path, or another access-control layer you trust.
- It doesn't enable the OpenAI-compatible API server. Flip `API_SERVER_ENABLED=true` and supply `API_SERVER_KEY` if you need it.
- It doesn't hardcode a model API key. When `BYNARA_API_KEY` is configured, the patcher selects Bynara's `qwen-3.8-max-free` default on a fresh/upstream-default config; otherwise Hermes uses its upstream model default. The setting lives in ephemeral `/opt/data` unless git state storage is enabled.
- It doesn't encrypt the state branch by default. `.env` is committed verbatim in plaintext mode (`GIT_STATE_ENV_MODE`); use `encrypt` or `omit` if you would rather not, and protect the state repo like a secrets backup either way.
- It doesn't configure browser automation tweaks (`--shm-size`, GPU access). Those need an instance type with more RAM, not extra Render config.
- It doesn't fork or modify the upstream `render-oss/skills` content. The overlay in `skills/render-on-hermes/` is the only Hermes-specific addition; everything else is the canonical Render skill bundle.

## License

This template is MIT licensed (see [`LICENSE`](./LICENSE)). Hermes Agent itself is also MIT licensed; see [the upstream LICENSE](https://github.com/NousResearch/hermes-agent/blob/main/LICENSE).
