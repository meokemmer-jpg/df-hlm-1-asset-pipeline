#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_PATH="${1:-$ROOT/config.yaml}"
LOCK_DIR="${DF_HLM_1_LOCK_DIR:-$ROOT/.df-hlm-1.lock}"
STOP_FLAG="${DF_HLM_1_STOP_FLAG:-$ROOT/STOP-DF-HLM-1.flag}"
PGREP_PATTERN="df-hlm-1-asset-pipeline/src/asset_pipeline.py"

if [[ -f "$STOP_FLAG" ]]; then
  echo "DF-HLM-1 stop flag detected: $STOP_FLAG"
  exit 0
fi

if pgrep -f "$PGREP_PATTERN" >/dev/null 2>&1; then
  echo "DF-HLM-1 already running (pgrep guard)"
  exit 0
fi

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "DF-HLM-1 lock active: $LOCK_DIR"
  exit 0
fi

cleanup() {
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

exec python3 "$ROOT/src/asset_pipeline.py" "$CONFIG_PATH"
