"""
aam.py — Audit Appendix Metrics (AAM).

The AAM is the small, curated set of facts the engine must get right in the
FIRST pass, before any business-plan narrative or deep-dive analysis. It is the
human-verification target: the analyst reviews and signs off on these metrics
(the "audit appendix") before the engine traces formulas outward (Stage 2) to
gather everything else.

Design intent (2026-06-10 redesign):
  - Stage 1 extraction is scoped to the AAM ONLY, so a single focused pass can
    nail them instead of a broad 5-section sweep over all 109 catalog metrics.
  - Every AAM id is an existing `in_bounded_list` catalog metric — AAM is a
    SCOPE over the catalog, NOT a new metric definition.

Interest Rate is represented as floating-rate components (base/index rate +
spread + cap) per the design decision, rather than a single all-in rate.
"""
from __future__ import annotations

# Ordered AAM concept groups. Order drives the audit-appendix display order.
AAM_GROUPS: dict[str, list[str]] = {
    "Identity": ["asset_name", "property_type", "location", "total_units", "total_sf"],
    "Basis":    ["purchase_price", "total_project_cost", "going_in_cap_rate"],
    # Going-in NOI (net_operating_income_noi) and Exit NOI are kept as SEPARATE
    # fields: a single NOI row let the engine surface the exit/stabilized column
    # (the higher number) as "the" NOI. Separating them makes the human verify
    # each, and the distinct aliases (Exit/Sale/Terminal NOI) steer the resolver
    # to the right cell for each.
    "Income":   ["net_operating_income_noi", "exit_noi"],
    "Debt":     ["debt_amount", "original_ltv", "interest_rate",
                 "interest_rate_spread", "interest_rate_cap"],
    "Timing":   ["purchase_date", "hold_period", "exit_date"],
    # Exit Value is the exit-side pricing anchor: Exit NOI is DERIVED from it
    # (Exit NOI = Exit Value × Exit Cap Rate), mirroring going-in NOI = Purchase
    # Price × Going-in Cap Rate. See _derive_noi_from_pricing in aam_extractor.
    "Return":   ["exit_value_terminal_value", "exit_cap_rate",
                 "levered_irr", "equity_multiple"],
}

# Flat ordered list of AAM metric_ids (canonical extraction / display order).
AAM_METRIC_IDS: list[str] = [mid for ids in AAM_GROUPS.values() for mid in ids]

_AAM_SET = set(AAM_METRIC_IDS)


# AAM fields that are HARD-CODED INPUTS when a model is built (the modeler types
# them in; everything else derives from them). When candidates tie on sheet
# rank, a hard-coded (non-formula) cell beats a formula cell for these fields —
# read the model the way it was made. Deliberately EXCLUDES purchase_price /
# total_project_cost / debt_amount: in portfolio models those are aggregates
# (SUM formulas over per-asset hardcodes — e.g. BAC's $192M = 88+63+41), so the
# correct deal-level cell is often a formula and a hardcode preference would
# grab a single asset's slice instead.
AAM_HARDCODED_INPUTS: set[str] = {
    "asset_name", "property_type", "location", "total_units", "total_sf",
    "going_in_cap_rate", "exit_cap_rate",
    "original_ltv", "interest_rate", "interest_rate_spread", "interest_rate_cap",
    "purchase_date", "hold_period", "exit_date",
}


def is_aam(metric_id: str) -> bool:
    """True if metric_id belongs to the Audit Appendix Metric set."""
    return metric_id in _AAM_SET


def group_of(metric_id: str) -> str | None:
    """Return the AAM display group for a metric_id, or None if not AAM."""
    for group, ids in AAM_GROUPS.items():
        if metric_id in ids:
            return group
    return None


def aam_metrics(catalog: list[dict]) -> list[dict]:
    """
    Filter a loaded metric catalog to the AAM entries, in canonical AAM order.

    Returns catalog entry dicts (same shape the resolver/extractor consume).
    """
    by_id = {m["metric_id"]: m for m in catalog}
    return [by_id[mid] for mid in AAM_METRIC_IDS if mid in by_id]


def validate_aam(catalog: list[dict]) -> list[str]:
    """
    Return AAM metric_ids missing from the catalog (empty list = all present).

    Used by tests / startup checks so the AAM set never silently drifts away
    from the catalog definitions it depends on.
    """
    have = {m["metric_id"] for m in catalog}
    return [mid for mid in AAM_METRIC_IDS if mid not in have]
