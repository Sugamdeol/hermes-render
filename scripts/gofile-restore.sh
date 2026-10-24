#!/bin/sh
# Restore Hermes state from a Gofile backup tar.gz.
# Current backup source since 2026-10-24, replacing the GitHub state repo.
# Usage: gofile-restore.sh DATA_DIR
# Required env (never commit these):
#   GOFILE_TOKEN, GOFILE_SERVER, GOFILE_FILE_ID, GOFILE_FILENAME
# Optional env:
#   GOFILE_SHA256 - if set, the download is verified against it.
# The backup tar contains a top-level data/ tree, which is the live
# Hermes state root. Its contents are extracted into DATA_DIR.
# Credential files and git history are not in the backup by design.
set -eu
DATA_DIR="${1:-/opt/data}"
: "${GOFILE_TOKEN:?GOFILE_TOKEN is required}"
: "${GOFILE_SERVER:?GOFILE_SERVER is required}"
: "${GOFILE_FILE_ID:?GOFILE_FILE_ID is required}"
: "${GOFILE_FILENAME:?GOFILE_FILENAME is required}"
TMPDIR="${TMPDIR:-/tmp}"
ARCHIVE="${TMPDIR}/gofile-restore.tar.gz"
URL="https://${GOFILE_SERVER}.gofile.io/download/web/${GOFILE_FILE_ID}/${GOFILE_FILENAME}"
echo "[gofile-restore] downloading ${GOFILE_FILENAME}"
curl -fsSL -H "Authorization: Bearer ${GOFILE_TOKEN}" -o "${ARCHIVE}" "${URL}"
if [ ! -s "${ARCHIVE}" ]; then
  echo "[gofile-restore] download is empty" >&2
  exit 1
fi
if [ -n "${GOFILE_SHA256:-}" ]; then
  ACTUAL="$(sha256sum "${ARCHIVE}" | cut -d' ' -f1)"
  if [ "${ACTUAL}" != "${GOFILE_SHA256}" ]; then
    echo "[gofile-restore] sha256 mismatch" >&2
    rm -f "${ARCHIVE}"
    exit 1
  fi
fi
mkdir -p "${DATA_DIR}"
EXTRACT="${TMPDIR}/gofile-restore-extract"
rm -rf "${EXTRACT}"
mkdir -p "${EXTRACT}"
tar -xzf "${ARCHIVE}" -C "${EXTRACT}"
if [ -d "${EXTRACT}/data" ]; then
  cp -a "${EXTRACT}/data/." "${DATA_DIR}/"
else
  cp -a "${EXTRACT}/." "${DATA_DIR}/"
fi
rm -rf "${EXTRACT}" "${ARCHIVE}"
echo "[gofile-restore] restored to ${DATA_DIR}"
