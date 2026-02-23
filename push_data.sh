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

# ── 2. Generate cache (samples, workflows, properties JSON) ──────────────────
echo "==> Generating JSON cache files..."
DATA_DIR="$BUILD_DIR" python "$(dirname "$0")/generate_cache.py"

# ── 3. Close the live app's DB connection ────────────────────────────────────
echo "==> Closing app DB connection on VM..."
ssh -i "$SSH_KEY" "$VM" \
  "curl -sf -X POST -H 'X-Reload-Token: $RELOAD_TOKEN' http://localhost:8000/api/admin/close && echo '  closed'"

# ── 4. Resolve DB / store / manifest filenames (full rebuild uses _new suffix) ─
if [[ -f "$BUILD_DIR/graph_new.db" ]]; then
  LOCAL_DB="$BUILD_DIR/graph_new.db"
  LOCAL_STORE="$BUILD_DIR/structure_store_new"
  LOCAL_MANIFEST="$BUILD_DIR/parsed_manifest_new.json"
else
  LOCAL_DB="$BUILD_DIR/graph.db"
  LOCAL_STORE="$BUILD_DIR/structure_store"
  LOCAL_MANIFEST="$BUILD_DIR/parsed_manifest.json"
fi

echo "==> Uploading $LOCAL_DB to VM..."
scp -i "$SSH_KEY" "$LOCAL_DB" "$VM:/data/graph_new.db"

if [[ -d "$LOCAL_STORE" ]]; then
  echo "==> Uploading structure_store to VM..."
  scp -i "$SSH_KEY" -qr "$LOCAL_STORE" "$VM:/data/structure_store_new"
fi

if [[ -d "$BUILD_DIR/cache" ]]; then
  echo "==> Uploading cache dir to VM..."
  scp -i "$SSH_KEY" -qr "$BUILD_DIR/cache" "$VM:/data/cache_new"
fi

# ── 5. Atomic swap on VM ─────────────────────────────────────────────────────
echo "==> Swapping DB on VM..."
ssh -i "$SSH_KEY" "$VM" "
  rm -f /data/graph.db
  mv /data/graph_new.db /data/graph.db
  if [ -d /data/structure_store_new ]; then
    rm -rf /data/structure_store
    mv /data/structure_store_new /data/structure_store
  fi
  if [ -d /data/cache_new ]; then
    rm -rf /data/cache
    mv /data/cache_new /data/cache
  fi
  echo '  swapped'
"

# ── 6. Upload manifest ───────────────────────────────────────────────────────
if [[ -f "$LOCAL_MANIFEST" ]]; then
  scp -i "$SSH_KEY" "$LOCAL_MANIFEST" "$VM:/data/parsed_manifest.json"
fi

# ── 7. Patch structure-store paths in the DB ────────────────────────────────
# The locally-built DB contains Mac absolute paths pointing to $BUILD_DIR.
# patch_paths.py rewrites all CMSO.hasPath triples to /data/structure_store/.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "==> Patching structure-store paths in DB on VM..."
scp -i "$SSH_KEY" "$SCRIPT_DIR/patch_paths.py" "$VM:/tmp/patch_paths.py"
ssh -i "$SSH_KEY" "$VM" \
  "docker cp /tmp/patch_paths.py kg_frontend_app:/tmp/patch_paths.py && \
   docker exec kg_frontend_app /opt/conda/bin/python /tmp/patch_paths.py && \
   echo '  paths patched'"

# ── 7. Signal app to reload ──────────────────────────────────────────────────
echo "==> Reloading KG in app..."
ssh -i "$SSH_KEY" "$VM" \
  "curl -sf -X POST -H 'X-Reload-Token: $RELOAD_TOKEN' http://localhost:8000/api/admin/reload && echo '  reloaded'"

echo "==> Done."
