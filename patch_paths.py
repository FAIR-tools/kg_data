#!/usr/bin/env python3
"""
patch_paths.py
--------------
Patches CMSO.hasPath literals in the KnowledgeGraph SQLite DB so that the
structure-store path prefix matches the container's /data/structure_store.

Usage (inside the container or with the right env):
    python /kg_data/patch_paths.py

Environment variables:
    DATA_DIR  — override data directory (default: /data)
    OLD_PREFIX — path prefix to replace (default: auto-detected from first match)
    NEW_PREFIX — replacement prefix (default: {DATA_DIR}/structure_store)
"""

import os
import sys
import logging
from rdflib import URIRef, Literal
from rdflib.namespace import XSD

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

DATA_DIR   = os.environ.get("DATA_DIR", "/data")
DB_PATH    = os.path.join(DATA_DIR, "graph.db")
NEW_PREFIX = os.environ.get("NEW_PREFIX", os.path.join(DATA_DIR, "structure_store"))
OLD_PREFIX = os.environ.get("OLD_PREFIX", None)   # auto-detect if not set

try:
    from atomrdf import KnowledgeGraph
    from atomrdf.namespace import CMSO
except ImportError as e:
    log.error("atomrdf not installed: %s", e)
    sys.exit(1)

log.info("Opening KG at %s", DB_PATH)
kg = KnowledgeGraph(store="SQLAlchemy", store_file=DB_PATH)

# ── Gather all CMSO.hasPath triples ─────────────────────────────────────────
all_path_triples = list(kg.triples((None, CMSO.hasPath, None)))
log.info("Found %d hasPath triples", len(all_path_triples))

if not all_path_triples:
    log.warning("No hasPath triples found — nothing to patch.")
    kg.close()
    sys.exit(0)

# ── Auto-detect old prefix from first absolute path that isn't /data ─────────
if OLD_PREFIX is None:
    for subj, pred, obj in all_path_triples:
        val = str(obj)
        if not val.startswith("/data") and os.path.basename(val):
            # strip the basename to get the prefix
            OLD_PREFIX = os.path.dirname(val)
            log.info("Auto-detected OLD_PREFIX: %s", OLD_PREFIX)
            break

if OLD_PREFIX is None:
    log.warning("All paths already start with /data (or no path triples found). Nothing to patch.")
    kg.close()
    sys.exit(0)

# ── Patch ────────────────────────────────────────────────────────────────────
patched = 0
skipped = 0

for subj, pred, obj in all_path_triples:
    val = str(obj)
    if val.startswith(OLD_PREFIX):
        basename = os.path.basename(val)
        new_val  = os.path.join(NEW_PREFIX, basename)
        kg.remove((subj, pred, obj))
        kg.add((subj, pred, Literal(new_val, datatype=XSD.string)))
        patched += 1
    else:
        skipped += 1

log.info("Patched %d path(s), skipped %d (already correct or different prefix)", patched, skipped)

# ── Verify a few ─────────────────────────────────────────────────────────────
log.info("Sample paths after patching:")
for _, _, obj in list(kg.triples((None, CMSO.hasPath, None)))[:5]:
    log.info("  %s", str(obj))

# ── Commit ───────────────────────────────────────────────────────────────────
try:
    kg.graph.commit()
    log.info("Committed changes to DB.")
except Exception as e:
    log.warning("commit() raised %s (may be auto-committed)", e)

# dispose engine before close to release file descriptors
try:
    kg.graph.store._engine.dispose()
except Exception:
    pass
try:
    kg.close()
except TypeError:
    # older atomRDF versions require a filename arg; safe to ignore after commit+dispose
    pass
except Exception as e:
    log.warning("close() warning (non-fatal): %s", e)
log.info("Done. Reload the app with: curl -X POST .../api/admin/reload")
