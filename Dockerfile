# syntax=docker/dockerfile:1.7
#
# Hermes Agent on Render, pre-baked with Render tooling.
#
# Extends the upstream NousResearch/hermes-agent image with:
#   - A bundle of Render-focused skills mounted via skills.external_dirs
#   - A boot-time patcher that registers the Render MCP server and Bynara
#     custom provider in config.yaml (idempotent; never overwrites user edits)
#   - Free-tier resource guardrails for the gateway, dashboard, and GoFile sync
#   - An optional GoFile state sync for Render's Free filesystem
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


_AGENT_CACHE_MAX_SIZE = _resource_int_env("HERMES_AGENT_CACHE_MAX_SIZE", 16)
_AGENT_CACHE_IDLE_TTL_SECS = _resource_float_env(
    "HERMES_AGENT_CACHE_IDLE_TTL_SECONDS", 900.0
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
COPY --chown=root:root scripts/free-storage.py /opt/render-tools/free-storage.py
RUN chmod 0755 /opt/render-tools/bootstrap.sh /opt/render-tools/patch-config.py \
             /opt/render-tools/free-storage.py /opt/render-tools/seed-env.py

# Pre-create the dir the patcher writes to so chown works cleanly on
# first boot. Render Free has no persistent disk, so this image directory
# is intentionally disposable. A paid deployment may mount a disk here
# without requiring a different image.
RUN install -d -o hermes -g hermes -m 0755 /opt/data

# Stay as root so the bootstrap can chown the mounted /opt/data on first
# boot, then `gosu hermes` for the config patch, then exec the upstream
# entrypoint (which also runs as root and does its own gosu drop).
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/opt/render-tools/bootstrap.sh"]
CMD ["gateway", "run"]
