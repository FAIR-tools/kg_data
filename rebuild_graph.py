#!/usr/bin/env python3
"""
rebuild_graph.py
----------------
Incrementally updates the knowledge graph: only parses YAML files that are
new or have changed since the last run, leaving the existing graph intact.

Pass --full to force a complete wipe + rebuild from scratch.

Run inside the Docker container:
    docker compose exec app python /kg_data/rebuild_graph.py

Environment variables:
    DATA_DIR   — override the data directory (default: /data)
    YAML_DIR   — override the YAML source directory (default: ./data relative to this script)
"""

import os
import sys
import glob
import json
import shutil
import logging
import argparse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

parser_args = argparse.ArgumentParser()
parser_args.add_argument("--full", action="store_true", help="Wipe and rebuild from scratch")
args = parser_args.parse_args()

# ── Paths ────────────────────────────────────────────────────────────────────
DATA_DIR      = os.environ.get("DATA_DIR",  "/data")
DB_PATH       = os.path.join(DATA_DIR, "graph.db")
STORE_PATH    = os.path.join(DATA_DIR, "structure_store")
MANIFEST_PATH = os.path.join(DATA_DIR, "parsed_manifest.json")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
YAML_DIR   = os.environ.get("YAML_DIR", os.path.join(SCRIPT_DIR, "data"))

# ── Imports ──────────────────────────────────────────────────────────────────
try:
    from atomrdf import KnowledgeGraph
    from atomrdf.io.workflow_parser import WorkflowParser
except ImportError as e:
    log.error("atomrdf is not installed: %s", e)
    sys.exit(1)

# ── Collect YAML files ────────────────────────────────────────────────────────
yaml_files = sorted(
    glob.glob(os.path.join(YAML_DIR, "**", "*.yaml"), recursive=True) +
    glob.glob(os.path.join(YAML_DIR, "**", "*.yml"),  recursive=True)
)
log.info("Found %d YAML file(s) in %s", len(yaml_files), YAML_DIR)

# ── Full rebuild: wipe everything ─────────────────────────────────────────────
if args.full:
    log.info("--full flag set: wiping existing graph at %s", DB_PATH)
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    if os.path.isdir(STORE_PATH):
        shutil.rmtree(STORE_PATH)
    if os.path.exists(MANIFEST_PATH):
        os.remove(MANIFEST_PATH)

os.makedirs(STORE_PATH, exist_ok=True)

# ── Load manifest (path → mtime) ──────────────────────────────────────────────
manifest: dict[str, float] = {}
if os.path.exists(MANIFEST_PATH):
    try:
        manifest = json.loads(Path(MANIFEST_PATH).read_text())
    except Exception:
        manifest = {}

from pathlib import Path

# ── Determine which files need parsing ────────────────────────────────────────
to_parse = []
for yf in yaml_files:
    mtime = os.path.getmtime(yf)
    if manifest.get(yf) != mtime:
        to_parse.append((yf, mtime))

if not to_parse:
    log.info("All files already up-to-date — nothing to do.")
    sys.exit(0)

log.info("%d file(s) to parse (new or changed)", len(to_parse))

# ── Open or create KnowledgeGraph ─────────────────────────────────────────────
log.info("Opening KnowledgeGraph at %s", DB_PATH)
kg = KnowledgeGraph(
    store="SQLAlchemy",
    store_file=DB_PATH,
    structure_store=STORE_PATH,
)

# ── Parse only new/changed files ─────────────────────────────────────────────
wp = WorkflowParser(kg=kg)
errors = []

for yf, mtime in to_parse:
    log.info("Parsing %s", yf)
    try:
        result = wp.parse(yf)
        n_samples = len(result.get("sample_map", {}))
        log.info("  → %d sample(s) added/deduplicated", n_samples)
        manifest[yf] = mtime          # mark as successfully parsed
    except Exception as exc:
        log.error("  ✗ Failed to parse %s: %s", yf, exc)
        errors.append((yf, str(exc)))

# ── Save updated manifest ─────────────────────────────────────────────────────
Path(MANIFEST_PATH).write_text(json.dumps(manifest, indent=2))

# ── Summary ───────────────────────────────────────────────────────────────────
total_samples = len(kg.sample_ids)
log.info("─" * 60)
log.info("Done. Total samples in graph: %d", total_samples)
if errors:
    log.warning("%d file(s) had errors:", len(errors))
    for yf, err in errors:
        log.warning("  %s: %s", yf, err)
    sys.exit(1)
else:
    log.info("All new files parsed successfully.")
    sys.exit(0)
