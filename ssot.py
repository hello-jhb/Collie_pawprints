"""
SSOT (Single Source of Truth) for an asset.

v2 scope: ONE asset at a time, stored as JSON on disk.
The SSOT is the durable record of "everything we know about this asset" —
extraction writes into it, scenarios read from it. It survives across sessions
and across scenarios, so files ingested for a Deal Review are still available
when the user later runs a Performance Analysis on the same asset.

Layered structure mirrors the investment lifecycle:
    underwriting    — original acquisition assumptions (fixed/historical)
    business_plan   — revised plan (post-acquisition)
    actuals_YYYY    — realized performance for a given year
    rent_roll       — current tenant/lease snapshot (reserved for v2.1)
    debt            — loan terms and covenants (reserved for v2.1)

Every metric carries provenance: which file, which sheet, which cell,
when it was extracted. This is what makes the SSOT auditable.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ASSETS_DIR = Path("assets")
CURRENT_ASSET_FILE = ASSETS_DIR / "current_asset.json"

# Layers we know about. The extractor/classifier may write any of these.
# Unknown layers are allowed but flagged.
KNOWN_LAYERS = {
    "underwriting",
    "business_plan",
    "actuals_2020",
    "actuals_2021",
    "actuals_2022",
    "actuals_2023",
    "actuals_2024",
    "actuals_2025",
    "actuals_recent",  # fallback when classifier sees a financial statement but no year
    "rent_roll",
    "debt",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_ssot(asset_id: str = "current") -> dict[str, Any]:
    """Shape of a fresh SSOT. Identity stays empty until a file fills it in."""
    return {
        "asset_id": asset_id,
        "identity": {
            "name": None,
            "property_type": None,
            "location": None,
            "sf_or_units": None,
        },
        "layers": {},          # layer_name -> {metrics, source_file, ingested_at}
        "provenance": [],      # append-only log of every field write
        "ingested_files": [],  # list of filenames ever ingested
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }


# -----------------------------------------------------------------------------
# Load / save
# -----------------------------------------------------------------------------

def load_ssot() -> dict[str, Any]:
    """Read the current asset's SSOT from disk. Creates an empty one if missing."""
    ASSETS_DIR.mkdir(exist_ok=True)
    if not CURRENT_ASSET_FILE.exists():
        ssot = _empty_ssot()
        save_ssot(ssot)
        return ssot

    with open(CURRENT_ASSET_FILE, "r") as f:
        return json.load(f)


def save_ssot(ssot: dict[str, Any]) -> None:
    """Persist the SSOT to disk. Stamps updated_at."""
    ASSETS_DIR.mkdir(exist_ok=True)
    ssot["updated_at"] = _now_iso()
    with open(CURRENT_ASSET_FILE, "w") as f:
        json.dump(ssot, f, indent=2, default=str)


def reset_ssot() -> dict[str, Any]:
    """Wipe and replace with a fresh SSOT. Used when starting work on a new asset."""
    ssot = _empty_ssot()
    save_ssot(ssot)
    return ssot


# -----------------------------------------------------------------------------
# Layer writes
# -----------------------------------------------------------------------------

def _extract_catalog_suggestions(raw_insights: dict | None) -> list[dict]:
    """
    Pull catalog improvement hints from Pass 2 output.

    When GPT finds a field (found section), it records what label it found
    in the file. That label is a candidate alias to add to the metric catalog
    so future files don't need GPT to find the same metric.

    Returns a list of {metric_name, found_as_label, value, sheet} dicts.
    """
    if not raw_insights:
        return []
    suggestions = []
    # New schema: raw_insights["found"][field_name] = {value, label_in_file, sheet, confidence}
    for field_name, data in raw_insights.get("found", {}).items():
        if isinstance(data, dict) and data.get("label_in_file"):
            suggestions.append({
                "metric_name":    field_name,
                "found_as_label": data["label_in_file"],
                "value":          data.get("value"),
                "sheet":          data.get("sheet"),
            })
    return suggestions


def write_layer(
    layer: str,
    metrics: list[dict[str, Any]],
    source_file: str,
    ssot: dict[str, Any] | None = None,
    raw_insights: dict[str, Any] | None = None,
    sheet_inventory: dict[str, Any] | None = None,
    bounded_metrics: dict[str, Any] | None = None,
    extraction_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Write a layer's worth of metrics into the SSOT.

    `metrics` is a list of dicts shaped like:
        {
            "metric_name": "Purchase Price",
            "value": 25507408.7,
            "sheet": "Assumption",
            "value_cell": "D9",
            "confidence": "high",
        }

    If the layer already exists, the new metrics REPLACE the old ones for that
    layer. (Each file ingested at a given layer is treated as the source of
    truth for that layer.) Provenance is appended, never overwritten.
    """
    if ssot is None:
        ssot = load_ssot()

    now = _now_iso()

    ssot["layers"][layer] = {
        "source_file": source_file,
        "ingested_at": now,
        "metric_count": len(metrics),
        "metrics": {
            m["metric_name"]: {
                "value": m.get("value"),
                "sheet": m.get("sheet"),
                "cell": m.get("value_cell"),
                "confidence": m.get("confidence"),
            }
            for m in metrics
        },
        # Raw GPT insight pass — populated at ingest time when LLM is available.
        # Contains inferred characteristics (GP/LP position, strategy, property type)
        # and gap-filled metrics the structured extractor didn't find.
        # None if the insight pass was skipped (no API key, or file unavailable).
        "raw_insights": raw_insights or None,

        # Catalog improvement suggestions — populated when Pass 2 fills gaps.
        # Each entry: {metric_name, found_as_label, value, sheet}
        # Review these to add missing aliases to Snapshot Metric.xlsx.
        "catalog_suggestions": _extract_catalog_suggestions(raw_insights),

        # Sheet inventory — which sheets exist in the source file and what
        # priority tier they were classified into. The chat agent reads this
        # to know which sheets were intentionally skipped during bulk extraction
        # (sensitivities, scenarios, comps, backups) so it can re-read them
        # on demand via read_sheet/search_file for follow-up questions.
        "sheet_inventory": sheet_inventory or None,

        # Phase 1 — bounded-metric records. Each metric in the analyst checklist
        # gets a full schema-validated record:
        #   {raw_value, normalized_value, display_value, unit, scale, period,
        #    source_sheet, source_cell, status, validation_notes, candidates, ...}
        # status ∈ {verified, candidate_pool, suspicious, missing}
        # Memo generation reads from here for the 25 core analyst metrics.
        "bounded_metrics": bounded_metrics or {},

        # Reproducibility metadata for the Analyst Bundle.
        "extraction_metadata": extraction_metadata or {},
    }

    # Provenance log — one entry per field write.
    for m in metrics:
        ssot["provenance"].append({
            "layer": layer,
            "field": m["metric_name"],
            "value": m.get("value"),
            "source_file": source_file,
            "sheet": m.get("sheet"),
            "cell": m.get("value_cell"),
            "extracted_at": now,
        })

    if source_file not in ssot["ingested_files"]:
        ssot["ingested_files"].append(source_file)

    save_ssot(ssot)
    return ssot


def apply_verified_aam(
    layer: str,
    verified: dict[str, dict[str, Any]],
    ssot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Patch a layer's bounded_metric records with human-verified Audit Appendix
    values. Called AFTER the full ingest pipeline runs, so the human's confirmed
    values win over whatever the extractor produced for AAM fields.

    `verified` is shaped { metric_name: {value, display, source_sheet,
    source_cell, note} }. Each patched record is marked status="verified" and
    human_verified=True.
    """
    if ssot is None:
        ssot = load_ssot()
    lyr = ssot["layers"].get(layer)
    if not lyr:
        return ssot

    bm = lyr.setdefault("bounded_metrics", {})
    for name, v in verified.items():
        rec = bm.get(name) or {"metric_name": name}
        rec["raw_value"]        = v.get("value")
        rec["normalized_value"] = v.get("value")
        rec["display_value"]    = v.get("display", str(v.get("value")))
        rec["status"]           = "verified"
        rec["human_verified"]   = True
        if v.get("source_sheet") is not None:
            rec["source_sheet"] = v["source_sheet"]
        if v.get("source_cell") is not None:
            rec["source_cell"] = v["source_cell"]
        rec.setdefault("validation_notes", []).insert(
            0, v.get("note", "Human-verified via audit appendix.")
        )
        bm[name] = rec

    lyr["bounded_metrics"] = bm
    save_ssot(ssot)
    return ssot


def attach_model_tables(
    layer: str,
    tables: list[dict[str, Any]],
    version: str | None = None,
    ssot: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    """
    Attach a compact summary of the parsed financial-model tables to a layer AND
    tag each bounded_metric with the periodicity of the table its source cell
    belongs to (so a metric like NOI inherits 'monthly' / 'annual' instead of
    being guessed per-row). Atomic: tags + attaches in one load/save.

    Returns (ssot, n_tagged).
    """
    if ssot is None:
        ssot = load_ssot()
    lyr = ssot["layers"].get(layer)
    if not lyr:
        return ssot, 0

    from financial_model_parser import tag_metric_periodicity

    bm = lyr.get("bounded_metrics", {}) or {}
    tagged = tag_metric_periodicity(tables, bm)

    lyr["model_tables"] = {
        "version":  version,
        "count":    len(tables),
        "tagged_metrics": tagged,
        "tables": [
            {
                "sheet":       t["sheet"],
                "title":       t["title"],
                "table_type":  t["table_type"],
                "periodicity": t["periodicity"],
                "n_periods":   len(t["date_headers"]),
                "n_rows":      len(t["rows"]),
                "row_labels":  [r["label"] for r in t["rows"][:40]],
            }
            for t in tables
        ],
        "attached_at": _now_iso(),
    }
    save_ssot(ssot)
    return ssot, tagged


def merge_traced_metrics(
    layer: str,
    trace: dict[str, Any],
    ssot: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    """
    Fold Stage-2 formula-traced metrics into a layer's bounded_metrics so the
    snapshot AND deep-dives can use them — not just sit in `formula_trace`.

    Fill-only: a traced value is written ONLY where the layer has no usable
    value for that metric (missing/suspicious/absent). It never overwrites a
    verified / candidate_pool / human-verified fact. Written as status="derived"
    with `traced=True` and provenance back to the anchor it came from.

    Returns (ssot, n_merged).
    """
    if ssot is None:
        ssot = load_ssot()
    lyr = ssot["layers"].get(layer)
    if not lyr:
        return ssot, 0

    from metric_catalog import load_metric_catalog
    from metric_resolver import _format_display

    catalog = {m["metric_id"]: m for m in load_metric_catalog()}
    bm = lyr.setdefault("bounded_metrics", {})
    merged = 0

    for mid, m in (trace or {}).get("reached_metrics", {}).items():
        mcat = catalog.get(mid, {})
        name = mcat.get("metric_name") or m.get("metric_name")  # canonical catalog name
        val = m.get("value")
        if not name or val is None:
            continue

        # Schema guard: drop traced values that fall outside the metric's range.
        # Protects against ambiguous label matches (e.g. the catalog has both a
        # ratio "Loan-to-Cost (LTC)" and a junk USD "LTC" sharing the alias).
        rmin, rmax = mcat.get("range_min"), mcat.get("range_max")
        if (
            rmin is not None and rmax is not None
            and isinstance(val, (int, float)) and not (rmin <= val <= rmax)
        ):
            continue

        existing = bm.get(name)
        if (
            existing
            and existing.get("normalized_value") is not None
            and existing.get("status") in ("verified", "candidate_pool", "derived", "inferred")
        ):
            continue  # the pipeline already has a usable value — don't clobber it

        unit = mcat.get("unit")
        scale = mcat.get("scale")
        sheet, _, cell = str(m.get("source", "")).partition("!")
        bm[name] = {
            "metric_name":      name,
            "metric_id":        mid,
            "raw_value":        val,
            "normalized_value": val,
            "display_value":    _format_display(val, unit, scale),
            "unit":             unit,
            "scale":            scale,
            "source_sheet":     sheet or None,
            "source_cell":      cell or None,
            "status":           "derived",
            "validation_notes": [
                f"Reached via formula trace from {m.get('via_anchor')} (hop {m.get('hop')})."
            ],
            "traced":           True,
        }
        merged += 1

    lyr["bounded_metrics"] = bm
    save_ssot(ssot)
    return ssot, merged


def attach_formula_trace(
    layer: str,
    trace: dict[str, Any],
    ssot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Store a compact Stage-2 formula trace on a layer (additive enrichment).

    Keeps `reached_metrics` + summary counts; drops the full per-cell list to
    keep the SSOT lean. Does not touch bounded_metrics or verified facts.
    """
    if ssot is None:
        ssot = load_ssot()
    lyr = ssot["layers"].get(layer)
    if not lyr:
        return ssot
    lyr["formula_trace"] = {
        "version":         trace.get("version"),
        "seeds":           trace.get("seeds", 0),
        "cells_visited":   len(trace.get("traced_cells", [])),
        "reached_metrics": trace.get("reached_metrics", {}),
        "traced_at":       _now_iso(),
    }
    save_ssot(ssot)
    return ssot


def update_identity(
    fields: dict[str, Any],
    ssot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Set or update asset identity fields (name, type, location, size)."""
    if ssot is None:
        ssot = load_ssot()
    for k, v in fields.items():
        if k in ssot["identity"] and v is not None:
            ssot["identity"][k] = v
    save_ssot(ssot)
    return ssot


# -----------------------------------------------------------------------------
# Reads
# -----------------------------------------------------------------------------

def read_layer(layer: str, ssot: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Return the metrics dict for one layer, or None if the layer isn't present."""
    if ssot is None:
        ssot = load_ssot()
    return ssot["layers"].get(layer)


def list_layers(ssot: dict[str, Any] | None = None) -> list[str]:
    """Return the layers currently populated in SSOT."""
    if ssot is None:
        ssot = load_ssot()
    return sorted(ssot["layers"].keys())


def get_metric(
    layer: str,
    metric_name: str,
    ssot: dict[str, Any] | None = None,
) -> Any:
    """Convenience: return just the value of one metric in one layer. None if missing."""
    layer_data = read_layer(layer, ssot)
    if not layer_data:
        return None
    return layer_data["metrics"].get(metric_name, {}).get("value")


def ssot_summary(ssot: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Compact summary of what's in the SSOT — useful for showing the user
    and for letting the agent decide what scenarios are runnable.
    """
    if ssot is None:
        ssot = load_ssot()

    return {
        "asset_id": ssot["asset_id"],
        "identity": ssot["identity"],
        "layers_present": list_layers(ssot),
        "ingested_files": ssot["ingested_files"],
        "total_metric_writes": len(ssot["provenance"]),
        "updated_at": ssot["updated_at"],
    }


# -----------------------------------------------------------------------------
# Scenario readiness — does the SSOT have what a scenario needs?
# -----------------------------------------------------------------------------

SCENARIO_REQUIREMENTS = {
    "deal_review": {
        "required_any_of": [["underwriting"]],
        "description": "Needs at least the acquisition underwriting layer.",
    },
    "perf_vs_plan": {
        "required_any_of": [
            ["underwriting", "actuals_2021"],
            ["underwriting", "actuals_2022"],
            ["underwriting", "actuals_2023"],
            ["underwriting", "actuals_recent"],
            ["business_plan", "actuals_2021"],
            ["business_plan", "actuals_2022"],
            ["business_plan", "actuals_2023"],
            ["business_plan", "actuals_recent"],
        ],
        "description": "Needs at least one plan layer (UW or BP) AND at least one actuals layer.",
    },
}


def scenario_ready(scenario: str, ssot: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Tells the caller whether the SSOT has enough data to run a given scenario,
    and if not, what's missing.
    """
    if ssot is None:
        ssot = load_ssot()

    spec = SCENARIO_REQUIREMENTS.get(scenario)
    if spec is None:
        return {"ready": False, "reason": f"Unknown scenario: {scenario}"}

    present = set(list_layers(ssot))

    for combo in spec["required_any_of"]:
        if all(layer in present for layer in combo):
            return {
                "ready": True,
                "matched_requirement": combo,
                "layers_present": sorted(present),
            }

    # Build a helpful "missing" message
    needed_examples = spec["required_any_of"][0]
    missing = [layer for layer in needed_examples if layer not in present]
    return {
        "ready": False,
        "reason": spec["description"],
        "layers_present": sorted(present),
        "example_missing": missing,
    }
