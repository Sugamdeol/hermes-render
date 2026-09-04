# syntax=docker/dockerfile:1.7
#
# Hermes Agent on Render, pre-baked with Render tooling.
#
# Extends the upstream NousResearch/hermes-agent image with:
#   - A bundle of Render-focused skills mounted via skills.external_dirs
#   - A boot-time patcher that registers the Render MCP server and Bynara
#     custom provider in config.yaml (idempotent; never overwrites user edits)
#   - Free-tier resource guardrails for the gateway, dashboard, and state sync
#   - An optional git-backed state sync for Render's Free filesystem
#
# We deliberately do NOT install the `render` CLI. This image is configured
# around the Render MCP server; installing extra CLIs should be a conscious
# operator choice, not something the agent does as an automatic fallback.
#
# Pin the upstream tag here. Bump and redeploy to upgrade Hermes.
ARG HERMES_IMAGE=docker.io/nousresearch/hermes-agent:v2026.5.7
FROM ${HERMES_IMAGE}

# Workarounds for upstream issues that prevent the dashboard's Chat tab
# from connecting on hosted deploys. Baked into the image so the runtime
# command stays simple. See render.yaml comments + the README for context.
#   - chown: dashboard runs as `hermes` but ui-tui/ + node_modules/ ship root-owned
#   - touch ink-bundle.js: short-circuits _hermes_ink_bundle_stale()
#   - touch entry.js: bumps mtime above source .ts files so _tui_build_needed() returns False
USER root
RUN chown -R hermes:hermes /opt/hermes/ui-tui /opt/hermes/node_modules \
 && mkdir -p /opt/hermes/ui-tui/packages/hermes-ink/dist /opt/hermes/ui-tui/dist \
 && touch /opt/hermes/ui-tui/packages/hermes-ink/dist/ink-bundle.js \
          /opt/hermes/ui-tui/dist/entry.js \
 && chown -R hermes:hermes /opt/hermes/ui-tui

# The pinned gateway release keeps up to 128 session agents cached for one
# hour. That is reasonable on a workstation but can exhaust Render Free RAM.
# Make those constants environment-configurable without changing upstream
# behavior for other deployments. The build fails loudly if the pinned source
# changes, so a Hermes image upgrade cannot silently lose this guardrail.
RUN /opt/hermes/.venv/bin/python - <<'PY'
from pathlib import Path

path = Path("/opt/hermes/gateway/run.py")
text = path.read_text(encoding="utf-8")
old = "_AGENT_CACHE_MAX_SIZE = 128\n_AGENT_CACHE_IDLE_TTL_SECS = 3600.0  # evict agents idle for >1h\n"
new = '''def _resource_int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _resource_float_env(name: str, default: float) -> float:
    try:
        return max(1.0, float(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


_AGENT_CACHE_MAX_SIZE = _resource_int_env("HERMES_AGENT_CACHE_MAX_SIZE", 8)
_AGENT_CACHE_IDLE_TTL_SECS = _resource_float_env(
    "HERMES_AGENT_CACHE_IDLE_TTL_SECONDS", 600.0
)
'''
if old not in text:
    raise SystemExit("expected gateway cache constants not found")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
PY

# The upstream model picker historically only queried /models for custom
# providers with an inline api_key. That omits key_env and keyless endpoints,
# and uses Bearer auth even for Anthropic-compatible custom providers. Patch
# the pinned picker so every custom provider added by the dashboard can be
# discovered. The patch script is exact and fails the build if the upstream
# source changes underneath the pin.
COPY scripts/patch-model-discovery.py /opt/render-tools/patch-model-discovery.py
RUN /opt/hermes/.venv/bin/python /opt/render-tools/patch-model-discovery.py \
    /opt/hermes/hermes_cli/model_switch.py

# The in-browser chat (the bundled hermes-chat-dashboard plugin drives
# tui_gateway over a pure-Python WebSocket) spawns a *second* full Python
# interpreter per conversation -- tui_gateway.slash_worker, the complete
# cli.HermesCLI stack -- just to serve the interactive TUI's slash menu.
# That is tens of MB of RSS per open chat, with no eviction when a browser
# tab closes, so a couple of conversations on a 512 MB instance OOM-kill the
# dashboard. The web chat never drives that menu. Patch the pinned
# tui_gateway so the worker is not spawned when HERMES_TUI_DISABLE_SLASH_WORKER
# is set (the env default in env/common.env turns it on); slash.exec keeps
# its existing no-worker fallback and normal chat/tool traffic never uses the
# worker. The patch is exact and fails the build if upstream shifts.
COPY scripts/patch-slash-worker.py /opt/render-tools/patch-slash-worker.py
RUN /opt/hermes/.venv/bin/python /opt/render-tools/patch-slash-worker.py \
    /opt/hermes/tui_gateway/server.py

# tui_gateway keeps every dashboard chat session (a full in-process AIAgent)
# in a module-level dict for the dashboard process's whole lifetime, and on a
# WebSocket disconnect it only detaches the transport -- nothing evicts them.
# Open browser conversations then accumulate tens of MB each in the process
# that has to stay up for the health check, a quiet 512 MB OOM path. Patch the
# pinned ws handler so sessions created over a closing socket are finalized
# (the transcript stays in the session DB and re-opens via session.resume).
# Opt out with HERMES_TUI_CLOSE_SESSIONS_ON_DISCONNECT=0 if a client reconnects
# with the same in-memory expectation.
COPY scripts/patch-ws-session-cleanup.py /opt/render-tools/patch-ws-session-cleanup.py
RUN /opt/hermes/.venv/bin/python /opt/render-tools/patch-ws-session-cleanup.py \
    /opt/hermes/tui_gateway/ws.py

# Nothing upstream caps tui_gateway's session registry. Each entry is a live
# in-process AIAgent, and the only things that ever remove one are an explicit
# session.close or the disconnect patch above -- so a browser tab that
# vanishes without a clean close (laptop sleep, phone backgrounded, reload
# racing the socket) leaves its agent pinned in the dashboard process for the
# life of the container.
#
# Measured, same 24-abandoned-tab workload, two clean 512 MB cgroups:
# 0.20 MB per conversation with the cap on, 0.97 MB per conversation with it
# off, ending 15.1 MB apart -- and the capped curve plateaus while the
# uncapped one keeps climbing. The per-entry cost is only ~1 MB; the heavy
# per-conversation cost this image really had was the orphaned slash_worker
# subprocess (57-85 MB RSS each, which is what took the pristine image from
# ~150 MB idle to a kernel OOM kill by the 5th of 10 conversations), and that
# is what patch-slash-worker.py removes. Both have to be bounded.
#
# This patch caps the registry at HERMES_TUI_MAX_SESSIONS (env default 2),
# finalizing the oldest entries -- transcript first, so an evicted chat still
# re-opens through session.resume -- and skips any session that is mid-turn.
COPY scripts/patch-session-cap.py /opt/render-tools/patch-session-cap.py
RUN /opt/hermes/.venv/bin/python /opt/render-tools/patch-session-cap.py \
    /opt/hermes/tui_gateway/server.py

# Pull the official Render skill bundle from github.com/render-oss/skills
# at a pinned commit. Mounted via skills.external_dirs at boot, so the
# upstream Hermes skills-sync flow never touches these files. To upgrade,
# bump RENDER_SKILLS_REF (a commit SHA, tag, or branch) and rebuild.
ARG RENDER_SKILLS_REPO=render-oss/skills
ARG RENDER_SKILLS_REF=1b8496570748203351f628b2ae738805ac2c23d5
RUN set -eu; \
    tmp="$(mktemp -d)"; \
    url="https://codeload.github.com/${RENDER_SKILLS_REPO}/tar.gz/${RENDER_SKILLS_REF}"; \
    curl -fsSL --retry 3 -o "${tmp}/skills.tar.gz" "${url}"; \
    tar -xzf "${tmp}/skills.tar.gz" -C "${tmp}"; \
    extracted="$(find "${tmp}" -maxdepth 2 -type d -name 'skills' | head -n 1)"; \
    test -n "${extracted}" || { echo "could not find skills/ in tarball" >&2; exit 1; }; \
    install -d -o hermes -g hermes -m 0755 /opt/render-tools/skills-upstream; \
    cp -a "${extracted}/." /opt/render-tools/skills-upstream/; \
    chown -R hermes:hermes /opt/render-tools/skills-upstream; \
    rm -rf "${tmp}"; \
    echo "${RENDER_SKILLS_REPO}@${RENDER_SKILLS_REF}" > /opt/render-tools/skills-upstream/.source

# Local overlay: a Hermes-specific `render-on-hermes` skill that tells
# the agent the MCP server is pre-wired (so skip "install MCP" from
# upstream skills) and that the CLI is deliberately absent (so don't
# try to invoke it). Listed FIRST in skills.external_dirs so same-named
# overlays would shadow upstream entries.
COPY --chown=hermes:hermes skills/ /opt/render-tools/skills-local/

# Dashboard plugin(s) installed into $HERMES_HOME/plugins/ at boot by
# bootstrap.sh (image-managed, same convention as the Bynara config
# entry). render-api-providers adds an "Add API provider" card to the
# dashboard Models page so custom providers no longer require
# hand-editing config.yaml.
COPY --chown=hermes:hermes dashboard-plugins/ /opt/render-tools/dashboard-plugins/

# Repo-managed environment: non-secret defaults plus the SOPS-encrypted
# secrets file. Both are decrypted/merged at boot by bootstrap.sh. The
# encrypted file is optional -- the directory is copied whether or not
# `secrets.enc.env` has been created, so a fresh clone still builds.
COPY --chown=hermes:hermes env/ /opt/render-tools/env/
COPY --chown=root:root scripts/seed-env.py /opt/render-tools/seed-env.py

# SOPS decrypts env/secrets.enc.env at boot using the age key supplied as
# SOPS_AGE_KEY. Pinned by version; the checksum is verified against the
# release's own checksums.txt. Set SOPS_SHA256 at build time to pin the exact
# digest instead, which is stronger:
#   docker build --build-arg SOPS_SHA256=<digest> ...
ARG SOPS_VERSION=v3.13.3
ARG SOPS_SHA256=
ARG TARGETARCH
RUN set -eu; \
    arch="${TARGETARCH:-amd64}"; \
    case "${arch}" in \
      amd64|arm64) ;; \
      *) echo "unsupported TARGETARCH=${arch} for sops" >&2; exit 1 ;; \
    esac; \
    tmp="$(mktemp -d)"; \
    base="https://github.com/getsops/sops/releases/download/${SOPS_VERSION}"; \
    curl -fsSL --retry 3 -o "${tmp}/sops" "${base}/sops-${SOPS_VERSION}.linux.${arch}"; \
    if [ -n "${SOPS_SHA256}" ]; then \
      echo "${SOPS_SHA256}  ${tmp}/sops" | sha256sum -c -; \
    else \
      curl -fsSL --retry 3 -o "${tmp}/checksums.txt" \
        "${base}/sops-${SOPS_VERSION}.checksums.txt"; \
      expected="$(grep " sops-${SOPS_VERSION}.linux.${arch}\$" "${tmp}/checksums.txt" \
        | head -n 1 | cut -d' ' -f1)"; \
      test -n "${expected}" || { echo "no checksum for sops linux.${arch}" >&2; exit 1; }; \
      echo "${expected}  ${tmp}/sops" | sha256sum -c -; \
    fi; \
    install -o root -g root -m 0755 "${tmp}/sops" /usr/local/bin/sops; \
    rm -rf "${tmp}"; \
    sops --version --disable-version-check

# Boot-time wrapper: restores optional remote state, patches
# /opt/data/config.yaml, starts optional state sync, then hands off to the
# upstream entrypoint chain (tini → docker/entrypoint.sh).
COPY --chown=root:root scripts/bootstrap.sh /opt/render-tools/bootstrap.sh
COPY --chown=root:root scripts/patch-config.py /opt/render-tools/patch-config.py
COPY --chown=root:root scripts/git-storage.py /opt/render-tools/git-storage.py
RUN chmod 0755 /opt/render-tools/bootstrap.sh /opt/render-tools/patch-config.py \
             /opt/render-tools/git-storage.py \
             /opt/render-tools/seed-env.py

# The git state backend shells out to `git`, so it must exist in the image.
# `age` is optional: without it the backend refuses to include /opt/data/.env
# in the backup rather than committing secrets in the clear, so we install it
# when the distro offers it but never fail the build over it.
RUN set -eu; \
    if command -v git >/dev/null 2>&1; then \
      echo "git already present: $(git --version)"; \
    elif command -v apt-get >/dev/null 2>&1; then \
      apt-get update; \
      apt-get install -y --no-install-recommends git; \
      rm -rf /var/lib/apt/lists/*; \
    elif command -v apk >/dev/null 2>&1; then \
      apk add --no-cache git; \
    else \
      echo "no supported package manager to install git" >&2; exit 1; \
    fi; \
    git --version; \
    if ! command -v age >/dev/null 2>&1; then \
      if command -v apt-get >/dev/null 2>&1; then \
        apt-get update && apt-get install -y --no-install-recommends age \
          && rm -rf /var/lib/apt/lists/* || true; \
      elif command -v apk >/dev/null 2>&1; then \
        apk add --no-cache age || true; \
      fi; \
    fi; \
    command -v age >/dev/null 2>&1 \
      && echo "age present: encrypted .env backup available" \
      || echo "age absent: /opt/data/.env will be omitted from git backups"

# Pre-create the dir the patcher writes to so chown works cleanly on
# first boot. Render Free has no persistent disk, so this image directory
# is intentionally disposable. A paid deployment may mount a disk here
# without requiring a different image.
RUN install -d -o hermes -g hermes -m 0755 /opt/data

# Stay as root so the bootstrap can chown the mounted /opt/data on first
# boot, then `gosu hermes` for the config patch, then run the upstream
# entrypoint (which also runs as root and does its own gosu drop) as a
# supervised child — the supervision is what gives the sync daemon a window
# to land its final state push at shutdown.
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/opt/render-tools/bootstrap.sh"]
CMD ["gateway", "run"]
