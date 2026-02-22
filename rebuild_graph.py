#!/usr/bin/env python3
"""
rebuild_graph.py
----------------
Incrementally updates the knowledge graph: only parses YAML files that are
new or have changed since the last run, leaving the existing graph intact.

For full rebuilds (--full or first run with existing DB), rebuilds into a
temporary DB file first, then the CI atomically swaps it into place via
/api/admin/close → mv → /api/admin/reload, so the app is never left without
a valid DB.

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
from pathlib import Path

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

# Temp paths used during full rebuilds (avoids touching the live DB until done)
DB_NEW_PATH    = os.path.join(DATA_DIR, "graph_new.db")
STORE_NEW_PATH = os.path.join(DATA_DIR, "structure_store_new")

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

# ── Determine build mode ──────────────────────────────────────────────────────
no_manifest = not os.path.exists(MANIFEST_PATH)
full_rebuild = args.full or (no_manifest and os.path.exists(DB_PATH))

if full_rebuild:
    reason = "--full flag" if args.full else "no manifest (first incremental run)"
    log.info("Full rebuild (%s) — building into temp DB to avoid disrupting live app", reason)

    # Clean up any leftover temp files from a previously interrupted run
    if os.path.exists(DB_NEW_PATH):
        os.remove(DB_NEW_PATH)
    if os.path.isdir(STORE_NEW_PATH):
        shutil.rmtree(STORE_NEW_PATH)
    os.makedirs(STORE_NEW_PATH, exist_ok=True)

    # Build a fresh KG into the temp paths
    kg = KnowledgeGraph(store="SQLAlchemy", store_file=DB_NEW_PATH, structure_store=STORE_NEW_PATH)
    wp = WorkflowParser(kg=kg)
    errors = []
    manifest: dict = {}

    for yf in yaml_files:
        mtime = os.path.getmtime(yf)
        log.info("Parsing %s", yf)
        try:
            result = wp.parse(yf)
            n_samples = len(result.get("sample_map", {}))
            log.info("  → %d sample(s)", n_samples)
            manifest[yf] = mtime
        except Exception as exc:
            log.error("  ✗ Failed: %s", exc)
            errors.append((yf, str(exc)))

    # Write the new manifest alongside the new DB
    MANIFEST_NEW_PATH = os.path.join(DATA_DIR, "parsed_manifest_new.json")
    Path(MANIFEST_NEW_PATH).write_text(json.dumps(manifest, indent=2))

    total = len(kg.sample_ids)
    log.info("─" * 60)
    log.info("Full rebuild complete — %d sample(s)", total)
    if errors:
        log.warning("%d error(s):", len(errors))
        for yf, err in errors:
            log.warning("  %s: %s", yf, err)

    # ── Signal the CI script to do the atomic swap + reload ──────────────────
    # The CI script is responsible for:
    #   curl .../api/admin/close
    #   mv graph_new.db graph.db && mv structure_store_new structure_store
    #   mv parsed_manifest_new.json parsed_manifest.json
    #   curl .../api/admin/reload
    # We just print a clear marker so the CI knows the build succeeded.
    print("FULL_REBUILD_DONE")
    sys.exit(1 if errors else 0)

# ── Incremental mode ──────────────────────────────────────────────────────────
manifest = {}
try:
    manifest = json.loads(Path(MANIFEST_PATH).read_text())
except Exception:
    manifest = {}

to_parse = [(yf, os.path.getmtime(yf)) for yf in yaml_files
            if manifest.get(yf) != os.path.getmtime(yf)]

if not to_parse:
    log.info("All files already up-to-date — nothing to do.")
    sys.exit(0)

log.info("%d file(s) to parse (new or changed)", len(to_parse))

os.makedirs(STORE_PATH, exist_ok=True)
kg = KnowledgeGraph(store="SQLAlchemy", store_file=DB_PATH, structure_store=STORE_PATH)
wp = WorkflowParser(kg=kg)
errors = []

for yf, mtime in to_parse:
    log.info("Parsing %s", yf)
    try:
        result = wp.parse(yf)
        n_samples = len(result.get("sample_map", {}))
        log.info("  → %d sample(s)", n_samples)
        manifest[yf] = mtime
    except Exception as exc:
        log.error("  ✗ Failed: %s", exc)
        errors.append((yf, str(exc)))

Path(MANIFEST_PATH).write_text(json.dumps(manifest, indent=2))

log.info("─" * 60)
log.info("Incremental update complete — %d total sample(s)", len(kg.sample_ids))
if errors:
    log.warning("%d error(s):", len(errors))
    for yf, err in errors:
        log.warning("  %s: %s", yf, err)
    sys.exit(1)
else:
    sys.exit(0)
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
# Also force a full wipe if the DB exists but the manifest doesn't — this means
# the DB was built by the old scratch-rebuild code and re-parsing into it would
# create duplicate triples.
no_manifest = not os.path.exists(MANIFEST_PATH)
if args.full or (no_manifest and os.path.exists(DB_PATH)):
    reason = "--full flag" if args.full else "no manifest found (first incremental run)"
    log.info("Wiping existing graph (%s): %s", reason, DB_PATH)
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
