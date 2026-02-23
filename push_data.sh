#!/usr/bin/env bash
# push_data.sh
# Build the KG locally from YAML files in ./data/, then deploy the DB to the VM.
# Usage: ./push_data.sh [--full]
#   --full  Force a full wipe + rebuild (default on first run; auto-detected otherwise)
set -e

VM="atomrdf@34.77.151.119"
SSH_KEY="$HOME/.ssh/atomrdf_gcp"
RELOAD_TOKEN="bae2bf985952a281bb28eea7ec6e78e2914ce103f41016ed99c0b0e43572b6d5"

BUILD_DIR="$(cd "$(dirname "$0")" && pwd)/build"
FULL_FLAG=""
[[ "$1" == "--full" ]] && FULL_FLAG="--full"

# ── 1. Build KG locally ──────────────────────────────────────────────────────
echo "==> Building KG locally (output: $BUILD_DIR)..."
mkdir -p "$BUILD_DIR"
DATA_DIR="$BUILD_DIR" python "$(dirname "$0")/rebuild_graph.py" $FULL_FLAG

# ── 2. Close the live app's DB connection ────────────────────────────────────
echo "==> Closing app DB connection on VM..."
ssh -i "$SSH_KEY" "$VM" \
  "curl -sf -X POST -H 'X-Reload-Token: $RELOAD_TOKEN' http://localhost:8000/api/admin/close && echo '  closed'"

# ── 3. Upload new DB and structure store ─────────────────────────────────────
echo "==> Uploading graph.db to VM..."
scp -i "$SSH_KEY" "$BUILD_DIR/graph.db" "$VM:/data/graph_new.db"

if [[ -d "$BUILD_DIR/structure_store" ]]; then
  echo "==> Uploading structure_store to VM..."
  scp -i "$SSH_KEY" -qr "$BUILD_DIR/structure_store" "$VM:/data/structure_store_new"
fi

# ── 4. Atomic swap on VM ─────────────────────────────────────────────────────
echo "==> Swapping DB on VM..."
ssh -i "$SSH_KEY" "$VM" "
  rm -f /data/graph.db
  mv /data/graph_new.db /data/graph.db
  if [ -d /data/structure_store_new ]; then
    rm -rf /data/structure_store
    mv /data/structure_store_new /data/structure_store
  fi
  echo '  swapped'
"

# ── 5. Upload manifest ───────────────────────────────────────────────────────
if [[ -f "$BUILD_DIR/parsed_manifest.json" ]]; then
  scp -i "$SSH_KEY" "$BUILD_DIR/parsed_manifest.json" "$VM:/data/parsed_manifest.json"
fi

# ── 6. Signal app to reload ──────────────────────────────────────────────────
echo "==> Reloading KG in app..."
ssh -i "$SSH_KEY" "$VM" \
  "curl -sf -X POST -H 'X-Reload-Token: $RELOAD_TOKEN' http://localhost:8000/api/admin/reload && echo '  reloaded'"

echo "==> Done."
