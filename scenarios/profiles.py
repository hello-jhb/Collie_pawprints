"""
scenarios/profiles.py — which catalog metrics each scenario cares about.

Filtering happens at SSOT read time (not at extraction time). Rationale:
  - Extraction stays scenario-agnostic — the SSOT is a complete asset record
    that future scenarios (Lease Review, Debt Analysis) can reuse without
    re-ingesting.
  - Each scenario tool reads only its allowed metrics before building the
    GPT prompt. This keeps narratives focused and prevents the model from
    drifting into metrics that aren't its concern.

Profile rules per category:
  - "all"                        → include every metric in the category
  - {"include_names": [...]}     → include only these specific metric names

Metric names below MUST match the `metric_name` column in Snapshot Metric.xlsx
exactly (we validate this at import time with _validate_profile_names).
"""

from __future__ import annotations

from typing import Any

from metric_catalog import load_metric_catalog


# -----------------------------------------------------------------------------
# Profile definitions
# -----------------------------------------------------------------------------

SCENARIO_PROFILES: dict[str, dict[str, Any]] = {
    "deal_review": {
        "description": (
            "Summarize the acquisition thesis from the underwriting layer. "
            "Going-in basis, the full projected cash flow waterfall, planned "
            "returns, exit assumption, and going-in debt structure."
        ),
        "categories": {
            "Investment Basis": "all",
            "Cash Flow": "all",           # projected waterfall from UW proforma
            "Valuation & Returns": "all",
            # Going-in debt only — Original LTV, term/rate, etc.
            # Going-in debt terms — includes projected DSCR and Debt Yield
            # because these are UW requirements modeled at acquisition.
            "Debt & Leverage": {
                "include_names": [
                    "Original LTV",
                    "LTC",
                    "Interest Rate",
                    "Loan Maturity",
                    "Hedging Cost / Swap Cost",
                    "Loan Balance",
                    "Debt Service Constant",
                    "Interest-Only Period Remaining",
                    "DSCR / Debt Coverage Ratio",
                    "Debt Yield",
                    "Refinance DSCR",
                    "Break-even Occupancy (Monthly)",
                ],
            },
        },
    },
}
# NOTE: the perf-vs-plan profile lived here for the old LLM scenario
# (scenarios/perf_vs_plan.py). That scenario was replaced by the deterministic
# perf_vs_plan_engine (which reads the workbook directly, not SSOT metric subsets),
# so the profile is gone with it.


# -----------------------------------------------------------------------------
# Filter function used by scenario modules
# -----------------------------------------------------------------------------

def filter_layer_metrics(
    layer_data: dict[str, Any],
    scenario: str,
    catalog_index: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Return a copy of layer_data with metrics filtered down to those allowed
    by the given scenario's profile.

    layer_data shape (matches ssot.write_layer output):
        {
          "source_file": "...",
          "ingested_at": "...",
          "metric_count": N,
          "metrics": {
            "Purchase Price": {"value": ..., "sheet": ..., "cell": ..., "confidence": ...},
            ...
          }
        }

    catalog_index is an optional pre-computed {metric_name: category} dict;
    if not provided, it's built fresh (a small but non-zero cost per call).
    """
    profile = SCENARIO_PROFILES.get(scenario)
    if profile is None:
        # Unknown scenario — be permissive (return data unchanged) so we don't
        # accidentally drop everything.
        return layer_data

    if catalog_index is None:
        catalog_index = _build_catalog_index()

    allowed_rules = profile["categories"]
    filtered: dict[str, Any] = {}

    for metric_name, metric_data in layer_data.get("metrics", {}).items():
        category = catalog_index.get(metric_name)
        if category is None:
            continue  # metric not in our catalog (shouldn't happen, but skip)

        rule = allowed_rules.get(category)
        if rule is None:
            continue  # category not in this scenario's profile

        if rule == "all":
            filtered[metric_name] = metric_data
        elif isinstance(rule, dict) and "include_names" in rule:
            if metric_name in rule["include_names"]:
                filtered[metric_name] = metric_data

    # Preserve the layer-level metadata, just swap the metrics dict.
    return {
        **layer_data,
        "metrics": filtered,
        "metric_count": len(filtered),
        "filtered_for_scenario": scenario,
    }


# -----------------------------------------------------------------------------
# Internal helpers + validation
# -----------------------------------------------------------------------------

def _build_catalog_index() -> dict[str, str]:
    """{metric_name: category} for every metric in the catalog."""
    return {m["metric_name"]: m.get("category", "") for m in load_metric_catalog()}


def _validate_profile_names() -> list[str]:
    """
    Sanity check: every metric name in any include_names list must exist in
    the live catalog. Returns a list of unknown names (empty list = all good).
    Called at import time so typos surface immediately.
    """
    catalog_names = {m["metric_name"] for m in load_metric_catalog()}
    unknown: list[str] = []
    for scenario, profile in SCENARIO_PROFILES.items():
        for category, rule in profile["categories"].items():
            if isinstance(rule, dict) and "include_names" in rule:
                for name in rule["include_names"]:
                    if name not in catalog_names:
                        unknown.append(f"{scenario}/{category}: {name!r}")
    return unknown


# Run validation on import. We just print a warning rather than raising so
# the app still starts (the filter will silently skip unknown names).
_unknown = _validate_profile_names()
if _unknown:
    import warnings
    warnings.warn(
        "Some metric names in scenarios/profiles.py don't match the catalog:\n  "
        + "\n  ".join(_unknown),
        stacklevel=2,
    )
