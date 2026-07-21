"""
portfolio — M1 roll-up engine: one fund-level read across N property SSOTs.

Core principle (M1 design doc, agreed 2026-07-18): ratios are RE-DERIVED from
summed components, never averaged. A weighted average is the fallback only
when components are missing, and gets flagged WEIGHTED_FALLBACK.

Other rules enforced here:
  * Per-cell provenance — every roll-up cell carries the full list of
    per-property inputs (property_id, value, layer, source file/sheet/cell).
  * A property missing a metric never silently drops out: it lands in
    `missing` and the cell is flagged PARTIAL_ROLLUP.
  * Same-layer only: a roll-up combines values from ONE layer. Properties
    that don't have that layer at all are listed under `missing_layer` and
    the cell is flagged LAYER_MISMATCH. Never fudged.
  * Cross-source disagreement inside a property (structured extraction vs
    bounded record) is RECORDED verbatim in portfolio["disagreements"] and
    not adjudicated — that's M2.
  * IRRs do not aggregate: reported as a min–max range. Portfolio IRR needs
    a merged cash-flow timeline — out of scope for M1.

Nothing here knows any property by name. The spec below is keyed by metric
catalog ids only.
"""

from __future__ import annotations

from typing import Any

from ssot import (
    list_property_ids,
    load_property_ssot,
    load_portfolio,
    save_portfolio,
    _now_iso,
)

ROLLUP_VERSION = "m1.v1"

# -----------------------------------------------------------------------------
# Aggregation spec — keyed by metric catalog metric_id.
#   sum      : Σ across properties.
#   derived  : numerator_sum / denominator_sum (component metric_ids given).
#              Falls back to a weighted average of per-property values
#              (weight_by) only when components are unavailable → flagged.
#   weighted : weighted average by weight_by (no meaningful component form).
#   range    : min–max across properties, no single number.
#   none     : not aggregated in M1 — reported per property only.
# -----------------------------------------------------------------------------

AGGREGATION_SPEC: dict[str, dict[str, Any]] = {
    # --- sums (extensive quantities) ---
    "purchase_price":                      {"method": "sum"},
    "closing_costs":                       {"method": "sum"},
    "total_acquisition_cost_all_in_basis": {"method": "sum"},
    "capex_budget":                        {"method": "sum"},
    "capital_expenditures":                {"method": "sum"},
    "total_project_cost":                  {"method": "sum"},
    "debt_amount":                         {"method": "sum"},
    "construction_loan":                   {"method": "sum"},
    "equity_invested":                     {"method": "sum"},
    "lp_equity":                           {"method": "sum"},
    "gp_equity_sponsor_equity":            {"method": "sum"},
    "effective_gross_revenue_egi":         {"method": "sum"},
    "operating_expenses":                  {"method": "sum"},
    "net_operating_income_noi":            {"method": "sum"},
    "debt_service":                        {"method": "sum"},
    "hard_costs":                          {"method": "sum"},
    "soft_costs":                          {"method": "sum"},
    "market_value":                        {"method": "sum"},
    "exit_value_terminal_value":           {"method": "sum"},
    "exit_noi":                            {"method": "sum"},
    "total_units":                         {"method": "sum"},
    "total_sf":                            {"method": "sum"},

    # --- derived ratios (recomputed from summed components, never averaged) ---
    "going_in_cap_rate": {
        "method": "derived",
        "numerator": "net_operating_income_noi",
        "denominator": "purchase_price",
        "weight_by": "purchase_price",     # fallback weighting only
    },
    "current_ltv": {
        "method": "derived",
        "numerator": "debt_amount",
        "denominator": "market_value",
        "weight_by": "market_value",
    },
    "original_ltv": {
        "method": "derived",
        "numerator": "debt_amount",
        "denominator": "purchase_price",
        "weight_by": "purchase_price",
    },
    "yield_on_cost": {
        "method": "derived",
        "numerator": "net_operating_income_noi",
        "denominator": "total_project_cost",
        "weight_by": "total_project_cost",
    },
    "exit_cap_rate": {
        "method": "derived",
        "numerator": "exit_noi",
        "denominator": "exit_value_terminal_value",
        "weight_by": "exit_value_terminal_value",
    },

    # --- weighted averages (no component form) ---
    "interest_rate":      {"method": "weighted", "weight_by": "debt_amount"},
    "physical_occupancy": {"method": "weighted", "weight_by": "total_units",
                           "weight_fallback": "total_sf"},

    # --- ranges (do not aggregate to one number) ---
    "levered_irr":   {"method": "range"},
    "unlevered_irr": {"method": "range"},

    # --- reported per property only in M1 ---
    "equity_multiple": {"method": "none"},
    "hold_period":     {"method": "none"},
}


# -----------------------------------------------------------------------------
# Per-property metric reads (bounded record preferred, structured fallback)
# -----------------------------------------------------------------------------

_USABLE_STATUSES = ("verified", "candidate_pool", "derived", "human_verified")


def _catalog_names() -> dict[str, str]:
    """metric_id -> canonical metric_name (from the metric catalog)."""
    from metric_catalog import load_metric_catalog
    return {m["metric_id"]: m.get("metric_name") for m in load_metric_catalog()}


def _property_metric(
    ssot: dict[str, Any],
    layer: str,
    metric_id: str,
    metric_name: str | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """
    Read one metric from one property's SSOT layer.

    Returns (input_record, disagreement). input_record is None when the
    property has no usable value. disagreement is recorded (not resolved)
    when the bounded record and the structured extraction disagree.
    """
    lyr = (ssot.get("layers") or {}).get(layer)
    if not lyr:
        return None, None

    pid = ssot.get("asset_id")
    src_file = lyr.get("source_file")

    bounded = None
    for rec in (lyr.get("bounded_metrics") or {}).values():
        if rec.get("metric_id") == metric_id or (
            metric_name and rec.get("metric_name") == metric_name
        ):
            bounded = rec
            break

    structured = None
    if metric_name:
        structured = (lyr.get("metrics") or {}).get(metric_name)

    b_val = bounded.get("normalized_value") if bounded else None
    b_ok = bounded is not None and b_val is not None and \
        bounded.get("status") in _USABLE_STATUSES
    s_val = structured.get("value") if structured else None

    disagreement = None
    if b_ok and s_val is not None and _num(b_val) is not None \
            and _num(s_val) is not None and _num(b_val) != _num(s_val):
        disagreement = {
            "property_id": pid, "layer": layer, "metric_id": metric_id,
            "bounded_value": b_val, "structured_value": s_val,
            "source_file": src_file,
            "note": "recorded, not adjudicated (M2)",
        }

    if b_ok:
        val = _num(b_val)
        if val is None:
            return None, disagreement
        return {
            "property_id": pid, "value": val, "layer": layer,
            "source_file": src_file,
            "sheet": bounded.get("source_sheet"),
            "cell": bounded.get("source_cell"),
            "status": bounded.get("status"),
            "basis": "bounded",
        }, disagreement

    val = _num(s_val)
    if val is None:
        return None, disagreement
    return {
        "property_id": pid, "value": val, "layer": layer,
        "source_file": src_file,
        "sheet": structured.get("sheet"),
        "cell": structured.get("cell"),
        "status": structured.get("confidence"),
        "basis": "structured",
    }, disagreement


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.replace(",", "").replace("$", "").strip())
        except ValueError:
            return None
    return None


# -----------------------------------------------------------------------------
# Roll-up
# -----------------------------------------------------------------------------

def _cell(method: str) -> dict[str, Any]:
    return {"value": None, "method": method, "inputs": [], "missing": [],
            "missing_layer": [], "flags": []}


def compute_rollup(
    property_ids: list[str] | None = None,
    layer: str = "underwriting",
    save: bool = True,
) -> dict[str, Any]:
    """
    Compute the fund-level roll-up across per-property SSOTs for ONE layer.

    Returns the portfolio dict (and persists it to assets/portfolio.json
    unless save=False).
    """
    pids = property_ids if property_ids is not None else list_property_ids()
    names = _catalog_names()
    ssots = {pid: load_property_ssot(pid) for pid in pids}

    has_layer = [p for p in pids if layer in (ssots[p].get("layers") or {})]
    no_layer = [p for p in pids if p not in has_layer]

    disagreements: list[dict[str, Any]] = []
    values: dict[str, dict[str, dict[str, Any]]] = {}  # metric_id -> pid -> rec

    for mid in AGGREGATION_SPEC:
        values[mid] = {}
        for pid in has_layer:
            rec, dis = _property_metric(ssots[pid], layer, mid, names.get(mid))
            if dis:
                disagreements.append(dis)
            if rec is not None:
                values[mid][pid] = rec

    rollup: dict[str, Any] = {}

    def _finish(mid: str, cell: dict[str, Any]) -> None:
        cell["missing"] = sorted(set(has_layer) - set(values[mid])) if has_layer else []
        cell["missing_layer"] = list(no_layer)
        if cell["missing"] and cell["inputs"]:
            cell["flags"].append("PARTIAL_ROLLUP")
        if no_layer:
            cell["flags"].append("LAYER_MISMATCH")
        rollup[mid] = cell

    for mid, spec in AGGREGATION_SPEC.items():
        method = spec["method"]
        recs = values[mid]
        cell = _cell(method)
        cell["inputs"] = [recs[p] for p in sorted(recs)]

        if method == "sum":
            if recs:
                cell["value"] = sum(r["value"] for r in recs.values())

        elif method == "derived":
            num_recs = values.get(spec["numerator"], {})
            den_recs = values.get(spec["denominator"], {})
            # component path: only properties present in BOTH sums
            both = sorted(set(num_recs) & set(den_recs))
            den_sum = sum(den_recs[p]["value"] for p in both) if both else 0
            if both and den_sum:
                cell["value"] = sum(num_recs[p]["value"] for p in both) / den_sum
                cell["derived_from"] = {
                    "numerator": spec["numerator"],
                    "denominator": spec["denominator"],
                    "properties": both,
                }
                if set(both) != set(has_layer):
                    cell["flags"].append("PARTIAL_ROLLUP")
            elif recs:
                # fallback: weighted average of per-property ratio values
                wid = spec.get("weight_by")
                w_recs = values.get(wid, {}) if wid else {}
                pids_w = sorted(set(recs) & set(w_recs))
                w_sum = sum(w_recs[p]["value"] for p in pids_w) if pids_w else 0
                if pids_w and w_sum:
                    cell["value"] = sum(
                        recs[p]["value"] * w_recs[p]["value"] for p in pids_w
                    ) / w_sum
                    cell["flags"].append("WEIGHTED_FALLBACK")
                    cell["weighted_by"] = wid

        elif method == "weighted":
            wid = spec.get("weight_by")
            w_recs = values.get(wid, {}) if wid else {}
            pids_w = sorted(set(recs) & set(w_recs))
            used_wid = wid
            if not pids_w and spec.get("weight_fallback"):
                used_wid = spec["weight_fallback"]
                w_recs = values.get(used_wid, {})
                pids_w = sorted(set(recs) & set(w_recs))
            w_sum = sum(w_recs[p]["value"] for p in pids_w) if pids_w else 0
            if pids_w and w_sum:
                cell["value"] = sum(
                    recs[p]["value"] * w_recs[p]["value"] for p in pids_w
                ) / w_sum
                cell["weighted_by"] = used_wid
                if set(pids_w) != set(recs):
                    cell["flags"].append("PARTIAL_ROLLUP")

        elif method == "range":
            if recs:
                vals = [r["value"] for r in recs.values()]
                cell["value"] = {"min": min(vals), "max": max(vals)}

        elif method == "none":
            cell["flags"].append("NOT_AGGREGATED")
            # per-property values stay visible via inputs

        _finish(mid, cell)

    portfolio = load_portfolio()
    portfolio["properties"] = list(pids)
    portfolio["rollup"] = rollup
    portfolio["rollup_layer"] = layer
    portfolio["disagreements"] = disagreements
    portfolio["rollup_version"] = ROLLUP_VERSION
    portfolio["computed_at"] = _now_iso()
    if save:
        save_portfolio(portfolio)
    return portfolio
