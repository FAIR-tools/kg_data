#!/usr/bin/env python3
"""
generate_cache.py
-----------------
Pre-computes JSON responses for the three slowest API endpoints:
  /api/samples      → cache/samples.json
  /api/workflows    → cache/workflows.json
  /api/properties   → cache/properties.json

Run this after every rebuild so the frontend loads instantly.

Usage:
    DATA_DIR=/path/to/data python generate_cache.py
    # or inside the container:
    docker exec kg_frontend_app /opt/conda/bin/python /kg_data/generate_cache.py

Environment variables:
    DATA_DIR  — directory containing graph.db (default: /data)
    DB_FILE   — override the SQLite DB path
"""

import os
import sys
import json
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Paths ─────────────────────────────────────────────────────────────────────
DATA_DIR  = os.environ.get("DATA_DIR", "/data")
DB_FILE   = os.environ.get("DB_FILE",  os.path.join(DATA_DIR, "graph.db"))
CACHE_DIR = os.path.join(DATA_DIR, "cache")

# Support locally built DB with _new suffix
if not os.path.exists(DB_FILE):
    alt = os.path.join(DATA_DIR, "graph_new.db")
    if os.path.exists(alt):
        DB_FILE = alt
        log.info("Using alternate DB: %s", DB_FILE)

if not os.path.exists(DB_FILE):
    log.error("DB not found at %s — run rebuild_graph.py first.", DB_FILE)
    sys.exit(1)

os.makedirs(CACHE_DIR, exist_ok=True)

# ── Imports ───────────────────────────────────────────────────────────────────
try:
    from atomrdf import KnowledgeGraph
    from rdflib import URIRef, RDF, RDFS, Literal
    from rdflib.namespace import XSD
except ImportError as e:
    log.error("atomrdf not installed: %s", e)
    sys.exit(1)

# ── Open KG ───────────────────────────────────────────────────────────────────
log.info("Opening KG at %s …", DB_FILE)
kg = KnowledgeGraph(store="SQLAlchemy", store_file=DB_FILE)
g  = kg.graph
log.info("KG loaded — %d triples", sum(1 for _ in g.triples((None, None, None))))


# ══════════════════════════════════════════════════════════════════════════════
# SAMPLES
# ══════════════════════════════════════════════════════════════════════════════
_CMSO_HAS_SPECIES = URIRef(f"{_CMSO_NS}hasSpecies")
_CMSO_HAS_ELEMENT = URIRef(f"{_CMSO_NS}hasElement")
_CMSO_HAS_CHEM_SYM = URIRef(f"{_CMSO_NS}hasChemicalSymbol")
_CMSO_HAS_ELEM_RATIO = URIRef(f"{_CMSO_NS}hasElementRatio")


def _build_element_map() -> dict:
    """Return {sample_uri_str: {"Fe": 0.5, "C": 0.5, ...}} for all samples."""
    result: dict[str, dict[str, float]] = {}
    for sample, _, species in g.triples((None, _CMSO_HAS_SPECIES, None)):
        er: dict[str, float] = {}
        for _, _, element in g.triples((species, _CMSO_HAS_ELEMENT, None)):
            symbol = g.value(element, _CMSO_HAS_CHEM_SYM)
            ratio  = g.value(element, _CMSO_HAS_ELEM_RATIO)
            if symbol is not None:
                try:
                    er[str(symbol)] = float(ratio) if ratio is not None else 1.0
                except (ValueError, TypeError):
                    er[str(symbol)] = 1.0
        if er:
            result[str(sample)] = er
    return result


def _make_formula(er: dict) -> str:
    """Convert {"Fe": 0.5, "C": 0.5} → "C0.5Fe0.5" (alphabetical order)."""
    if not er:
        return "?"
    parts = []
    for el in sorted(er.keys()):
        ratio = er[el]
        if abs(ratio - 1.0) < 0.001:
            parts.append(el)
        else:
            r_str = f"{ratio:.3f}".rstrip("0").rstrip(".")
            parts.append(f"{el}{r_str}")
    return "".join(parts)


def build_samples():
    el_map = _build_element_map()
    log.info("  Element map built for %d samples", len(el_map))
    ids   = kg.sample_ids    # list[URIRef]
    names = kg.sample_names  # list[str | None]
    out = []
    for sid, sname in zip(ids, names):
        er = el_map.get(str(sid), {})
        out.append({
            "id":            str(sid),
            "name":          sname or "",
            "elements":      sorted(er.keys()),
            "element_ratio": er,
            "formula":       _make_formula(er),
        })
    return out


# ══════════════════════════════════════════════════════════════════════════════
# WORKFLOWS  (mirrors app/routes/workflows.py logic)
# ══════════════════════════════════════════════════════════════════════════════
_ASMO_NS   = "http://purls.helmholtz-metadaten.de/asmo/"
_PROV_NS   = "http://www.w3.org/ns/prov#"
_CMSO_NS   = "http://purls.helmholtz-metadaten.de/cmso/"

_ENERGY_CALC  = URIRef(f"{_ASMO_NS}EnergyCalculation")
_SIMULATION   = URIRef(f"{_ASMO_NS}Simulation")
_PROV_ASSOC   = URIRef(f"{_PROV_NS}wasAssociatedWith")
_PROV_GEN_BY  = URIRef(f"{_PROV_NS}wasGeneratedBy")
_PROV_DERIVED = URIRef(f"{_PROV_NS}wasDerivedFrom")
_ASMO_POT     = URIRef(f"{_ASMO_NS}hasInteratomicPotential")
_ASMO_METHOD  = URIRef(f"{_ASMO_NS}hasComputationalMethod")
_CMSO_PATH    = URIRef(f"{_CMSO_NS}hasPath")
_CMSO_REF     = URIRef(f"{_CMSO_NS}hasReference")
_CMSO_SAMPLE  = URIRef(f"{_CMSO_NS}AtomicScaleSample")

def _local(uri: str) -> str:
    return uri.rstrip("/").split("/")[-1].split("#")[-1]

def _exists_as_sample(node) -> bool:
    if node is None:
        return False
    return any(True for _ in g.triples((URIRef(str(node)), RDF.type, _CMSO_SAMPLE)))

def _build_workflow_record(wf_uri, type_name, type_uri):
    software_uris = [str(o) for _, _, o in g.triples((wf_uri, _PROV_ASSOC, None)) if str(o).startswith("http")]
    software = software_uris[0] if software_uris else ""

    pot_node = g.value(wf_uri, _ASMO_POT)
    potential = ""
    potential_uri = ""
    if pot_node:
        pot_label = g.value(pot_node, RDFS.label)
        pot_ref   = g.value(pot_node, _CMSO_REF)
        pot_type  = g.value(pot_node, RDF.type)
        if pot_ref:
            potential_uri = str(pot_ref)
        if pot_label:
            potential = str(pot_label)
        elif pot_type:
            potential = _local(str(pot_type))
        else:
            potential = _local(str(pot_node))

    method_node = g.value(wf_uri, _ASMO_METHOD)
    method = ""
    if method_node:
        mt = g.value(method_node, RDF.type)
        method = _local(str(mt)) if mt else _local(str(method_node))

    output_samples = [str(s) for s, _, _ in g.triples((None, _PROV_GEN_BY, wf_uri)) if s is not None and _exists_as_sample(s)]
    input_set = set()
    for out_s in output_samples:
        for _, _, in_s in g.triples((URIRef(out_s), _PROV_DERIVED, None)):
            if in_s is not None and _exists_as_sample(in_s):
                input_set.add(str(in_s))

    path_lit = g.value(wf_uri, _CMSO_PATH)
    path = str(path_lit) if path_lit else ""

    return {
        "id":            str(wf_uri),
        "type":          type_name,
        "type_uri":      type_uri,
        "method":        method,
        "software":      software,
        "potential":     potential,
        "potential_uri": potential_uri,
        "path":          path,
        "input_samples": sorted(input_set),
        "output_samples": output_samples,
        "samples":       output_samples,
    }

def build_workflows():
    records = []
    for type_ref, type_label in (
        (_ENERGY_CALC, "Energy Calculation"),
        (_SIMULATION,  "Simulation"),
    ):
        for wf_uri, _, _ in g.triples((None, RDF.type, type_ref)):
            records.append(_build_workflow_record(wf_uri, type_label, str(type_ref)))
    records.sort(key=lambda r: r["id"])
    return {"workflows": records, "total": len(records)}


# ══════════════════════════════════════════════════════════════════════════════
# PROPERTIES
# ══════════════════════════════════════════════════════════════════════════════
# Scalar ASMO property types we surface in the Properties tab.
# Array-only types (Stress, Strain, etc. from MD) are also included but
# shown as "N-step array" since their values live in structure-store files.
_PROP_TYPES = [
    "TotalEnergy", "Energy", "BulkModulus", "Volume",
    "Pressure", "VirialPressure",
    "FreeEnergy", "Temperature",
    "FlowStress", "Stress", "Strain", "StrainRate",
    "EquationOfStateFit", "ThermodynamicIntegration",
]

_ASMO_HAS_VALUE     = URIRef(f"{_ASMO_NS}hasValue")
_ASMO_HAS_UNIT      = URIRef(f"{_ASMO_NS}hasUnit")
_ASMO_CALC_BY       = URIRef(f"{_ASMO_NS}wasCalculatedBy")
_CMSO_HAS_PATH      = _CMSO_PATH
_CMSO_HAS_ID        = URIRef(f"{_CMSO_NS}hasIdentifier")

def _qudt_label(uri: str) -> str:
    """Extract concise unit string from a QUDT URI like .../unit/EV → EV"""
    return uri.split("/")[-1]

def build_properties():
    # Build a workflow_id → [sample_ids] map for quick lookup
    sim_to_samples: dict[str, list[str]] = {}
    for s, _, wf in g.triples((None, _PROV_GEN_BY, None)):
        if _exists_as_sample(s):
            wf_str = str(wf)
            sim_to_samples.setdefault(wf_str, []).append(str(s))

    records = []
    for pt in _PROP_TYPES:
        pt_uri = URIRef(f"{_ASMO_NS}{pt}")
        for prop_node, _, _ in g.triples((None, RDF.type, pt_uri)):
            label_lit = g.value(prop_node, RDFS.label)
            label = str(label_lit) if label_lit else pt

            value_lit = g.value(prop_node, _ASMO_HAS_VALUE)
            has_path  = g.value(prop_node, _CMSO_HAS_PATH) is not None

            if value_lit is not None:
                try:
                    value = float(value_lit.toPython())
                except Exception:
                    value = str(value_lit)
                value_is_array = False
            elif has_path:
                value = None
                value_is_array = True
            else:
                # no value at all — skip
                continue

            unit_node = g.value(prop_node, _ASMO_HAS_UNIT)
            unit     = _qudt_label(str(unit_node)) if unit_node else ""
            unit_uri = str(unit_node) if unit_node else ""

            wf_node = g.value(prop_node, _ASMO_CALC_BY)
            wf_id   = str(wf_node) if wf_node else ""
            samples  = sim_to_samples.get(wf_id, [])

            records.append({
                "id":           str(prop_node),
                "type":         pt,
                "label":        label,
                "value":        value,
                "value_is_array": value_is_array,
                "unit":         unit,
                "unit_uri":     unit_uri,
                "workflow_id":  wf_id,
                "sample_ids":   samples,
            })

    records.sort(key=lambda r: (r["type"], r["label"], r["id"]))
    log.info("  Properties: %d records", len(records))
    return records


# ══════════════════════════════════════════════════════════════════════════════
# Write cache files
# ══════════════════════════════════════════════════════════════════════════════
log.info("Building samples cache …")
samples_data = build_samples()
log.info("  Samples: %d", len(samples_data))

log.info("Building workflows cache …")
wf_data = build_workflows()
log.info("  Workflows: %d", wf_data["total"])

log.info("Building properties cache …")
prop_data = build_properties()

for fname, data in [
    ("samples.json",    samples_data),
    ("workflows.json",  wf_data),
    ("properties.json", prop_data),
]:
    out = os.path.join(CACHE_DIR, fname)
    with open(out, "w") as f:
        json.dump(data, f)
    sz = os.path.getsize(out) // 1024
    log.info("Wrote %s (%d KB)", out, sz)

log.info("Cache generation complete.")
