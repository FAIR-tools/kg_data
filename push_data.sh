#!/usr/bin/env bash
# push_data.sh
# Deploy a pre-built Oxigraph KG (combined_KG/) to the VM.
# The KG must already be built locally via atomRDF_usecases/build_combined_kg.py.
#
# Usage: ./push_data.sh [--kg-dir /path/to/combined_KG]
#   --kg-dir  Override the path to the pre-built KG directory
#             (default: ../atomRDF_usecases/combined_KG)
set -e

VM="atomrdf@34.77.151.119"
SSH_KEY="$HOME/.ssh/atomrdf_gcp"
RELOAD_TOKEN="bae2bf985952a281bb28eea7ec6e78e2914ce103f41016ed99c0b0e43572b6d5"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Resolve KG directory ─────────────────────────────────────────────────────
KG_DIR=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --kg-dir) KG_DIR="$2"; shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done
[[ -z "$KG_DIR" ]] && KG_DIR="$SCRIPT_DIR/../atomRDF_usecases/combined_KG"
KG_DIR="$(cd "$KG_DIR" && pwd)"

LOCAL_DB="$KG_DIR/oxigraph.db"
LOCAL_STORE="$KG_DIR/rdf_structure_store"

if [[ ! -d "$LOCAL_DB" ]]; then
  echo "ERROR: Oxigraph store not found at $LOCAL_DB"
  echo "  Run: python atomRDF_usecases/build_combined_kg.py"
  exit 1
fi

# ── 1. Generate cache (samples, workflows, properties JSON) ──────────────────
echo "==> Generating JSON cache files..."
mkdir -p "$KG_DIR/cache"
DATA_DIR="$KG_DIR" DB_PATH="$LOCAL_DB" python "$SCRIPT_DIR/generate_cache.py"

# ── 2. Close the live app's DB connection ────────────────────────────────────
echo "==> Closing app DB connection on VM..."
ssh -i "$SSH_KEY" "$VM" \
  "curl -sf -X POST -H 'X-Reload-Token: $RELOAD_TOKEN' http://localhost:8000/api/admin/close && echo '  closed'" || true

# ── 3. Upload Oxigraph store (directory) to VM ───────────────────────────────
echo "==> Uploading oxigraph.db/ to VM..."
ssh -i "$SSH_KEY" "$VM" "mkdir -p /data/oxigraph.db_new"
rsync -az --delete -e "ssh -i $SSH_KEY" \
  "$LOCAL_DB/" "$VM:/data/oxigraph.db_new/"

# ── 4. Upload rdf_structure_store to VM ──────────────────────────────────────
if [[ -d "$LOCAL_STORE" ]]; then
  echo "==> Uploading rdf_structure_store/ to VM..."
  ssh -i "$SSH_KEY" "$VM" "mkdir -p /data/rdf_structure_store_new"
  rsync -az --delete -e "ssh -i $SSH_KEY" \
    "$LOCAL_STORE/" "$VM:/data/rdf_structure_store_new/"
fi

# ── 5. Upload cache dir ───────────────────────────────────────────────────────
if [[ -d "$KG_DIR/cache" ]]; then
  echo "==> Uploading cache dir to VM..."
  rsync -az --delete -e "ssh -i $SSH_KEY" \
    "$KG_DIR/cache/" "$VM:/data/cache_new/"
fi

# ── 6. Atomic swap on VM ─────────────────────────────────────────────────────
echo "==> Swapping data on VM..."
ssh -i "$SSH_KEY" "$VM" "
  rm -rf /data/oxigraph.db
  mv /data/oxigraph.db_new /data/oxigraph.db
  if [ -d /data/rdf_structure_store_new ]; then
    rm -rf /data/rdf_structure_store
    mv /data/rdf_structure_store_new /data/rdf_structure_store
  fi
  if [ -d /data/cache_new ]; then
    rm -rf /data/cache
    mv /data/cache_new /data/cache
  fi
  echo '  swapped'
"

# ── 7. Signal app to reload ──────────────────────────────────────────────────
echo "==> Reloading KG in app..."
ssh -i "$SSH_KEY" "$VM" \
  "curl -sf -X POST -H 'X-Reload-Token: $RELOAD_TOKEN' http://localhost:8000/api/admin/reload && echo '  reloaded'"

echo "==> Done."
