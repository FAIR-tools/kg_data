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
    DATA_DIR  — directory containing oxigraph.db (default: /data)
    DB_PATH   — override the Oxigraph store directory path
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
DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.environ.get("DB_PATH", os.path.join(DATA_DIR, "oxigraph.db"))
STRUCT_STORE = os.path.join(DATA_DIR, "rdf_structure_store")
CACHE_DIR = os.path.join(DATA_DIR, "cache")

if not os.path.isdir(DB_PATH):
    log.error("Oxigraph store not found at %s — build the KG first.", DB_PATH)
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
log.info("Opening KG at %s …", DB_PATH)
kg = KnowledgeGraph(store="Oxigraph", store_file=DB_PATH, structure_store=STRUCT_STORE)
g = kg.graph
log.info("KG loaded — %d triples", sum(1 for _ in g.triples((None, None, None))))


# ══════════════════════════════════════════════════════════════════════════════
# SHARED NAMESPACES
# ══════════════════════════════════════════════════════════════════════════════
_ASMO_NS = "http://purls.helmholtz-metadaten.de/asmo/"
_PROV_NS = "http://www.w3.org/ns/prov#"
_CMSO_NS = "http://purls.helmholtz-metadaten.de/cmso/"
_DCTERMS_NS = "http://purl.org/dc/terms/"
_DCAT_NS = "http://www.w3.org/ns/dcat#"
_FOAF_NS = "http://xmlns.com/foaf/0.1/"

# ══════════════════════════════════════════════════════════════════════════════
# SAMPLES
# ══════════════════════════════════════════════════════════════════════════════
_CMSO_HAS_SPECIES = URIRef(f"{_CMSO_NS}hasSpecies")
_CMSO_HAS_ELEMENT = URIRef(f"{_CMSO_NS}hasElement")
_CMSO_HAS_CHEM_SYM = URIRef(f"{_CMSO_NS}hasChemicalSymbol")
_CMSO_HAS_ELEM_RATIO = URIRef(f"{_CMSO_NS}hasElementRatio")
_DCTERMS_IS_PART_OF = URIRef(f"{_DCTERMS_NS}isPartOf")
_DCTERMS_IS_REF_BY = URIRef(f"{_DCTERMS_NS}isReferencedBy")
_DCTERMS_TITLE = URIRef(f"{_DCTERMS_NS}title")
_DCTERMS_IDENTIFIER = URIRef(f"{_DCTERMS_NS}identifier")
_DCTERMS_CREATOR = URIRef(f"{_DCTERMS_NS}creator")
_FOAF_NAME = URIRef(f"{_FOAF_NS}name")
_DCAT_DATASET = URIRef(f"{_DCAT_NS}Dataset")


def _build_element_map() -> dict:
    """Return {sample_uri_str: {"Fe": 0.5, "C": 0.5, ...}} for all samples."""
    result: dict[str, dict[str, float]] = {}
    for sample, _, species in g.triples((None, _CMSO_HAS_SPECIES, None)):
        er: dict[str, float] = {}
        for _, _, element in g.triples((species, _CMSO_HAS_ELEMENT, None)):
            symbol = g.value(element, _CMSO_HAS_CHEM_SYM)
            ratio = g.value(element, _CMSO_HAS_ELEM_RATIO)
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

    # Build sample → dataset URI map
    dataset_uri_map: dict[str, str] = {}
    for s, _, d in g.triples((None, _DCTERMS_IS_PART_OF, None)):
        dataset_uri_map[str(s)] = str(d)

    ids = kg.sample_ids  # list[URIRef]
    names = kg.sample_names  # list[str | None]
    out = []
    for sid, sname in zip(ids, names):
        er = el_map.get(str(sid), {})
        out.append(
            {
                "id": str(sid),
                "name": sname or "",
                "elements": sorted(er.keys()),
                "element_ratio": er,
                "formula": _make_formula(er),
                "dataset_uri": dataset_uri_map.get(str(sid), ""),
            }
        )
    return out


# ══════════════════════════════════════════════════════════════════════════════
# WORKFLOWS  (mirrors app/routes/workflows.py logic)
# ══════════════════════════════════════════════════════════════════════════════
_ENERGY_CALC = URIRef(f"{_ASMO_NS}EnergyCalculation")
_SIMULATION = URIRef(f"{_ASMO_NS}Simulation")
_PROV_ASSOC = URIRef(f"{_PROV_NS}wasAssociatedWith")
_PROV_GEN_BY = URIRef(f"{_PROV_NS}wasGeneratedBy")
_PROV_DERIVED = URIRef(f"{_PROV_NS}wasDerivedFrom")
_ASMO_POT = URIRef(f"{_ASMO_NS}hasInteratomicPotential")
_ASMO_METHOD = URIRef(f"{_ASMO_NS}hasComputationalMethod")
_CMSO_PATH = URIRef(f"{_CMSO_NS}hasPath")
_CMSO_REF = URIRef(f"{_CMSO_NS}hasReference")
_CMSO_SAMPLE = URIRef(f"{_CMSO_NS}AtomicScaleSample")


def _local(uri: str) -> str:
    return uri.rstrip("/").split("/")[-1].split("#")[-1]


def _exists_as_sample(node) -> bool:
    if node is None:
        return False
    return any(True for _ in g.triples((URIRef(str(node)), RDF.type, _CMSO_SAMPLE)))


def _build_workflow_record(wf_uri, type_name, type_uri):
    software_uris = [
        str(o)
        for _, _, o in g.triples((wf_uri, _PROV_ASSOC, None))
        if str(o).startswith("http")
    ]
    software = software_uris[0] if software_uris else ""

    pot_node = g.value(wf_uri, _ASMO_POT)
    potential = ""
    potential_uri = ""
    if pot_node:
        pot_label = g.value(pot_node, RDFS.label)
        pot_ref = g.value(pot_node, _CMSO_REF)
        pot_type = g.value(pot_node, RDF.type)
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

    output_samples = [
        str(s)
        for s, _, _ in g.triples((None, _PROV_GEN_BY, wf_uri))
        if s is not None and _exists_as_sample(s)
    ]
    input_set = set()
    for out_s in output_samples:
        for _, _, in_s in g.triples((URIRef(out_s), _PROV_DERIVED, None)):
            if in_s is not None and _exists_as_sample(in_s):
                input_set.add(str(in_s))

    path_lit = g.value(wf_uri, _CMSO_PATH)
    path = str(path_lit) if path_lit else ""

    return {
        "id": str(wf_uri),
        "type": type_name,
        "type_uri": type_uri,
        "method": method,
        "software": software,
        "potential": potential,
        "potential_uri": potential_uri,
        "path": path,
        "input_samples": sorted(input_set),
        "output_samples": output_samples,
        "samples": output_samples,
    }


def build_workflows():
    records = []
    for type_ref, type_label in (
        (_ENERGY_CALC, "Energy Calculation"),
        (_SIMULATION, "Simulation"),
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
# Instead of a hardcoded list, we dynamically discover calculated properties
# by finding all triples with asmo:wasCalculatedBy (= actual calculated output).
# Types that are NOT properties (workflows, potentials, methods) are skipped.
_SKIP_TYPES = {
    f"{_ASMO_NS}EnergyCalculation",
    f"{_ASMO_NS}Simulation",
    f"{_ASMO_NS}InteratomicPotential",
    f"{_ASMO_NS}ModifiedEmbeddedAtomModel",
    f"{_ASMO_NS}EmbeddedAtomModel",
    f"{_ASMO_NS}PairPotential",
    f"{_ASMO_NS}MolecularStatics",
    f"{_ASMO_NS}MolecularDynamics",
    f"{_ASMO_NS}DensityFunctionalTheory",
}

_ASMO_HAS_VALUE = URIRef(f"{_ASMO_NS}hasValue")
_ASMO_HAS_UNIT = URIRef(f"{_ASMO_NS}hasUnit")
_ASMO_CALC_BY = URIRef(f"{_ASMO_NS}wasCalculatedBy")
_CMSO_HAS_PATH = _CMSO_PATH
_CMSO_HAS_ID = URIRef(f"{_CMSO_NS}hasIdentifier")


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

    # Dynamically find all property nodes that have asmo:wasCalculatedBy
    for prop_node, _, wf_node in g.triples((None, _ASMO_CALC_BY, None)):
        prop_type_uri = g.value(prop_node, RDF.type)
        if prop_type_uri is None:
            continue
        type_str = str(prop_type_uri)
        if type_str in _SKIP_TYPES or not type_str.startswith(_ASMO_NS):
            continue

        pt = type_str[len(_ASMO_NS):]

        label_lit = g.value(prop_node, RDFS.label)
        label = str(label_lit) if label_lit else pt

        value_lit = g.value(prop_node, _ASMO_HAS_VALUE)
        has_path = g.value(prop_node, _CMSO_HAS_PATH) is not None

        if value_lit is not None:
            try:
                import math
                v = float(value_lit.toPython())
                value = None if (math.isnan(v) or math.isinf(v)) else v
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
        unit = _qudt_label(str(unit_node)) if unit_node else ""
        unit_uri = str(unit_node) if unit_node else ""

        wf_id = str(wf_node) if wf_node else ""
        samples = sim_to_samples.get(wf_id, [])

        records.append(
            {
                "id": str(prop_node),
                "type": pt,
                "label": label,
                "value": value,
                "value_is_array": value_is_array,
                "unit": unit,
                "unit_uri": unit_uri,
                "workflow_id": wf_id,
                "sample_ids": samples,
            }
        )

    records.sort(key=lambda r: (r["type"], r["label"], r["id"]))
    log.info("  Properties: %d records", len(records))
    return records


# ══════════════════════════════════════════════════════════════════════════════
# DATASETS
# ══════════════════════════════════════════════════════════════════════════════


def build_datasets():
    # Count samples per dataset
    sample_counts: dict[str, int] = {}
    for s, _, d in g.triples((None, _DCTERMS_IS_PART_OF, None)):
        k = str(d)
        sample_counts[k] = sample_counts.get(k, 0) + 1

    records = []
    for d, _, _ in g.triples((None, RDF.type, _DCAT_DATASET)):
        d_uri = str(d)
        title = str(g.value(d, _DCTERMS_TITLE) or "")
        identifier = str(g.value(d, _DCTERMS_IDENTIFIER) or d_uri)

        # Publication referenced by this dataset
        pub_node = g.value(d, _DCTERMS_IS_REF_BY)
        pub_title = ""
        pub_doi = ""
        if pub_node:
            pub_title = str(g.value(pub_node, _DCTERMS_TITLE) or "")
            pub_doi = str(g.value(pub_node, _DCTERMS_IDENTIFIER) or "")

        # Authors (foaf:name of dcterms:creator persons)
        authors = []
        for _, _, person in g.triples((d, _DCTERMS_CREATOR, None)):
            name = g.value(person, _FOAF_NAME)
            if name:
                authors.append(str(name))

        records.append(
            {
                "uri": d_uri,
                "title": title,
                "identifier": identifier,
                "publication_title": pub_title,
                "publication_doi": pub_doi,
                "authors": sorted(authors),
                "sample_count": sample_counts.get(d_uri, 0),
            }
        )

    # Sort by sample count descending
    records.sort(key=lambda x: -x["sample_count"])
    log.info("  Datasets: %d records", len(records))
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

log.info("Building datasets cache …")
datasets_data = build_datasets()

for fname, data in [
    ("samples.json", samples_data),
    ("workflows.json", wf_data),
    ("properties.json", prop_data),
    ("datasets.json", datasets_data),
]:
    out = os.path.join(CACHE_DIR, fname)
    with open(out, "w") as f:
        json.dump(data, f)
    sz = os.path.getsize(out) // 1024
    log.info("Wrote %s (%d KB)", out, sz)

log.info("Cache generation complete.")
