#!/bin/sh
set -eu
DATA_DIR="${DATA_DIR:-/data}"
ORCA_DATADIR="${ORCA_DATADIR:-${DATA_DIR}/orca-datadir}"
SEED_VERSION="${SEED_VERSION:-0}"
SEED_SRC=/opt/orca-datadir
MARKER="${ORCA_DATADIR}/.seed-version"

if [ ! -d "$DATA_DIR" ] || [ ! -w "$DATA_DIR" ]; then
  echo "print-portal: $DATA_DIR is not writable by uid $(id -u)." >&2
  echo "print-portal: if ./data is bind-mounted, fix ownership on the host: sudo chown -R 1000:1000 ./data" >&2
  exit 1
fi

mkdir -p "${DATA_DIR}/jobs" "${DATA_DIR}/home" "${DATA_DIR}/tmp" "${ORCA_DATADIR}"
if [ -d "$SEED_SRC" ] && { [ ! -f "$MARKER" ] || [ "$(cat "$MARKER" 2>/dev/null || true)" != "$SEED_VERSION" ]; }; then
  echo "print-portal: seeding OrcaSlicer presets into ${ORCA_DATADIR} (seed ${SEED_VERSION})"
  cp -R -n "${SEED_SRC}/." "${ORCA_DATADIR}/"
  printf '%s\n' "$SEED_VERSION" > "$MARKER"
fi
export HOME="${DATA_DIR}/home"
# Starlette spools large request bodies to disk; keep those temp files on the volume so an
# upload never fills the container's own filesystem.
export TMPDIR="${DATA_DIR}/tmp"
cd /app
python -m config --allow-unconfigured
exec python -m uvicorn main:app --host 0.0.0.0 --port "${PORT:-8088}"