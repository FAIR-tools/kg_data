#!/usr/bin/env python3
"""
rebuild_graph.py
----------------
Wipes the existing graph database and rebuilds it from scratch by parsing
every YAML file found under the data/ directory using atomRDF WorkflowParser.

Run inside the Docker container:
    docker exec kg_frontend_app python rebuild_graph.py

Or directly on the VM (with the venv activated):
    python rebuild_graph.py

Environment variables:
    DATA_DIR   — override the data directory (default: /data)
    YAML_DIR   — override the YAML source directory (default: ./data relative to this script)
"""

import os
import sys
import glob
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────────
DATA_DIR   = os.environ.get("DATA_DIR",  "/data")
DB_PATH    = os.path.join(DATA_DIR, "graph.db")
STORE_PATH = os.path.join(DATA_DIR, "structure_store")

# YAML files live in a data/ folder next to this script in the kg_data repo
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

if not yaml_files:
    log.warning("No YAML files found in %s — graph will be empty.", YAML_DIR)
else:
    log.info("Found %d YAML file(s) in %s", len(yaml_files), YAML_DIR)

# ── Wipe and recreate the graph ───────────────────────────────────────────────
log.info("Wiping existing graph at %s", DB_PATH)

# Remove old DB and structure store
import shutil
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
    log.info("Removed %s", DB_PATH)
if os.path.isdir(STORE_PATH):
    shutil.rmtree(STORE_PATH)
    log.info("Removed %s", STORE_PATH)
os.makedirs(STORE_PATH, exist_ok=True)

# ── Create fresh KnowledgeGraph ───────────────────────────────────────────────
log.info("Creating fresh KnowledgeGraph at %s", DB_PATH)
kg = KnowledgeGraph(
    store="SQLAlchemy",
    store_file=DB_PATH,
    structure_store=STORE_PATH,
)

# ── Parse all YAML files ──────────────────────────────────────────────────────
parser = WorkflowParser(kg=kg)
errors = []

for yf in yaml_files:
    log.info("Parsing %s", yf)
    try:
        result = parser.parse(yf)
        n_samples = len(result.get("sample_map", {}))
        log.info("  → %d sample(s) added/deduplicated", n_samples)
    except Exception as exc:
        log.error("  ✗ Failed to parse %s: %s", yf, exc)
        errors.append((yf, str(exc)))

# ── Summary ───────────────────────────────────────────────────────────────────
total_samples = len(kg.sample_ids)
log.info("─" * 60)
log.info("Rebuild complete. Total samples in graph: %d", total_samples)
if errors:
    log.warning("%d file(s) had errors:", len(errors))
    for yf, err in errors:
        log.warning("  %s: %s", yf, err)
    sys.exit(1)
else:
    log.info("All files parsed successfully.")
    sys.exit(0)
