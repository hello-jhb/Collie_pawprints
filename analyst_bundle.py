"""
analyst_bundle.py — reviewable "analyst run package" assembled after ingestion.

This is a THIN audit/display layer, not a new extraction engine. It packages
what the SSOT already stores (bounded metrics, sheet inventory, raw insights,
catalog suggestions) into a single reviewable bundle that answers, at a glance:

  - Workbook Map      : did Collie look at the right tabs?
  - Verified Facts    : what does Collie believe (and from which cell)?
  - Issues / QC Flags : what did Collie refuse to trust, and why?
  - Status Summary    : quick QC health check
  - Business Plan Read: GPT interpretation, kept separate from facts
  - Catalog Suggestions: aliases to add to improve future extraction

It's the bridge between "engine output" and "human analyst trust" — and a fast
way to localize a remaining problem (classification vs section read vs units vs
validation vs memo).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import json
from pathlib import Path

import ssot
from knowledge_store import knowledge_diagnostics, load_active_patterns
from metric_catalog import load_metric_catalog

BUNDLE_VERSION = "2026-06-10.1"
BUNDLE_DIR = Path("assets/analyst_bundles")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _metric_row(name: str, rec: dict[str, Any]) -> dict[str, Any]:
    return {
        "metric": name,
        "value": rec.get("display_value") or rec.get("normalized_value") or rec.get("raw_value"),
        "raw_value": rec.get("raw_value"),
        "normalized_value": rec.get("normalized_value"),
        "unit": rec.get("unit"),
        "scale": rec.get("scale"),
        "period": rec.get("period"),
        "observed_period": rec.get("observed_period"),
        "status": rec.get("status"),
        "source_sheet": rec.get("source_sheet"),
        "source_cell": rec.get("source_cell"),
        "method": rec.get("extractor_confidence") or rec.get("method"),
        "notes": rec.get("validation_notes") or [],
        "candidate_count": len(rec.get("candidates") or []),
        "fallback_used": any(
            "Found via GPT fallback" in str(note)
            for note in (rec.get("validation_notes") or [])
        ),
    }


# Statuses considered "trusted enough to display as a fact" vs "needs review".
_VERIFIED_STATUSES = {"verified", "derived", "inferred", "not_applicable"}
_ISSUE_STATUSES = {"missing", "suspicious", "conflict", "candidate_pool"}


def _catalog_by_name() -> dict[str, dict[str, Any]]:
    try:
        return {m["metric_name"]: m for m in load_metric_catalog()}
    except Exception:
        return {}


def _build_extraction_plan(
    bounded_metrics: dict[str, Any],
    catalog_by_name: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    plan = []
    for name, rec in bounded_metrics.items():
        if not isinstance(rec, dict):
            continue
        cat = catalog_by_name.get(name, {})
        plan.append({
            "metric": name,
            "section": cat.get("section"),
            "expected_source_roles": cat.get("source_primary") or [],
            "forbidden_source_roles": cat.get("source_forbidden") or [],
            "actual_source_used": {
                "sheet": rec.get("source_sheet"),
                "cell": rec.get("source_cell"),
                "method": rec.get("extractor_confidence") or rec.get("method"),
                "status": rec.get("status"),
            },
        })
    return plan


def _build_source_audit(bounded_metrics: dict[str, Any]) -> list[dict[str, Any]]:
    audit_rows = []
    for name, rec in bounded_metrics.items():
        if not isinstance(rec, dict):
            continue
        audit = rec.get("audit") or {}
        candidates = rec.get("candidates") or []
        audit_rows.append({
            "metric": name,
            "accepted_source": audit.get("accepted") or {
                "value": rec.get("normalized_value"),
                "raw": rec.get("raw_value"),
                "sheet": rec.get("source_sheet"),
                "cell": rec.get("source_cell"),
                "method": rec.get("extractor_confidence") or rec.get("method"),
            },
            "rejected_candidates": audit.get("rejected", []),
            "conflicts": audit.get("conflicts", []),
            "alternate_candidates": candidates[:5],
        })
    return audit_rows


def _identity_checks(bounded_metrics: dict[str, Any]) -> list[dict[str, Any]]:
    supporting = {
        "Going-in Cap Rate": ["Net Operating Income (NOI)", "Purchase Price", "Going-in Cap Rate"],
        "Original LTV": ["Debt Amount", "Purchase Price", "Total Project Cost", "Original LTV"],
        "Total Project Cost": ["Debt Amount", "Equity Invested", "Total Project Cost"],
    }
    try:
        from metric_resolver_gpt import run_identity_checks
        flags = run_identity_checks(bounded_metrics)
    except Exception as e:
        return [{
            "check": "identity_checks",
            "status": "error",
            "supporting_metrics": [],
            "notes": [str(e)],
        }]

    rows = []
    for check_name, metrics in supporting.items():
        rows.append({
            "check": check_name,
            "status": "fail" if check_name in flags else "pass",
            "supporting_metrics": metrics,
            "notes": flags.get(check_name, []),
        })
    for check_name, notes in flags.items():
        if check_name not in supporting:
            rows.append({
                "check": check_name,
                "status": "fail",
                "supporting_metrics": [],
                "notes": notes,
            })
    return rows


def _fired_patterns(bounded_metrics: dict[str, Any]) -> list[dict[str, Any]]:
    active_by_id = {
        p.get("rule_id") or p.get("pattern_id"): p
        for p in load_active_patterns()
    }
    fired: list[dict[str, Any]] = []
    seen = set()
    for metric_name, rec in bounded_metrics.items():
        if not isinstance(rec, dict):
            continue
        for note in rec.get("validation_notes") or []:
            text = str(note)
            marker = "Pattern fired:"
            if marker not in text:
                continue
            rule_id = text.split(marker, 1)[1].split(".", 1)[0].strip()
            if not rule_id or (metric_name, rule_id) in seen:
                continue
            seen.add((metric_name, rule_id))
            fired.append({
                "metric": metric_name,
                "rule_id": rule_id,
                "scope": (active_by_id.get(rule_id) or {}).get("scope"),
                "note": text,
            })
    return fired


def _knowledge_usage(bounded_metrics: dict[str, Any]) -> dict[str, Any]:
    diagnostics = knowledge_diagnostics()
    return {
        **diagnostics,
        "fired_patterns": _fired_patterns(bounded_metrics),
    }


def build_analyst_bundle(layer: str = "underwriting") -> dict[str, Any]:
    asset = ssot.load_ssot()
    layer_data = asset.get("layers", {}).get(layer)
    if not layer_data:
        return {
            "error": f"No SSOT layer found for {layer}",
            "bundle_version": BUNDLE_VERSION,
            "created_at": _now_iso(),
        }

    bounded_metrics = layer_data.get("bounded_metrics") or {}
    sheet_inventory = layer_data.get("sheet_inventory") or {}
    raw_insights = layer_data.get("raw_insights") or {}
    extraction_metadata = layer_data.get("extraction_metadata") or {}
    catalog_by_name = _catalog_by_name()

    metric_rows = [
        _metric_row(name, rec)
        for name, rec in bounded_metrics.items()
        if isinstance(rec, dict)
    ]

    issues = [
        r for r in metric_rows
        if r.get("status") in _ISSUE_STATUSES or r.get("fallback_used")
    ]
    verified = [r for r in metric_rows if r.get("status") in _VERIFIED_STATUSES]

    # status summary — count each distinct status
    statuses = {r.get("status") for r in metric_rows}
    status_summary = {
        s: sum(1 for r in metric_rows if r.get("status") == s)
        for s in sorted(x for x in statuses if x)
    }

    return {
        "bundle_version": BUNDLE_VERSION,
        "created_at": _now_iso(),
        "asset_id": asset.get("asset_id"),
        "layer": layer,
        "run_metadata": {
            "source_file": layer_data.get("source_file"),
            "file_hash": extraction_metadata.get("file_hash"),
            "ingested_at": layer_data.get("ingested_at"),
            "bundle_created_at": _now_iso(),
            "versions": {
                "bundle_version": BUNDLE_VERSION,
                **{
                    k: v for k, v in extraction_metadata.items()
                    if k.endswith("_version")
                },
            },
            "bounded_cache": extraction_metadata.get("bounded_cache"),
            "bounded_cache_key": extraction_metadata.get("bounded_cache_key"),
        },
        "source_file": layer_data.get("source_file"),
        "ingested_at": layer_data.get("ingested_at"),
        "workbook_map": {
            "all_sheets": sheet_inventory.get("all_sheets", []),
            "by_tier": sheet_inventory.get("by_tier", {}),
            "content_roles": sheet_inventory.get("content_roles", {}),
            "authoritative_tabs": sheet_inventory.get("authoritative_tabs", {}),
            "skipped_sheets": sheet_inventory.get("skipped_sheets", []),
            "low_priority_sheets": sheet_inventory.get("low_priority_sheets", []),
        },
        "extraction_plan": _build_extraction_plan(bounded_metrics, catalog_by_name),
        "verified_facts": verified,
        "issues": issues,
        "identity_checks": _identity_checks(bounded_metrics),
        "knowledge_usage": _knowledge_usage(bounded_metrics),
        "status_summary": status_summary,
        "business_plan_read": {
            "property_type": _safe_found(raw_insights, "property_type"),
            "deal_type": _safe_found(raw_insights, "deal_type"),
            "strategy": _safe_found(raw_insights, "strategy"),
            "key_risks": _safe_found(raw_insights, "key_risks"),
            "model_summary": raw_insights.get("model_summary") if isinstance(raw_insights, dict) else None,
        },
        "source_audit": _build_source_audit(bounded_metrics),
        "catalog_suggestions": layer_data.get("catalog_suggestions", []),
    }


def _safe_found(raw_insights: dict[str, Any], key: str) -> Any:
    found = raw_insights.get("found", {}) if isinstance(raw_insights, dict) else {}
    item = found.get(key)
    if isinstance(item, dict):
        return item.get("value")
    return item


def save_analyst_bundle(layer: str = "underwriting") -> dict[str, Any]:
    bundle = build_analyst_bundle(layer)
    if "error" in bundle:
        return bundle
    BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = bundle["created_at"].replace(":", "-").replace(".", "-")
    path = BUNDLE_DIR / f"{layer}_{stamp}.json"
    with open(path, "w") as f:
        json.dump(bundle, f, indent=2, default=str)
    bundle["bundle_path"] = str(path)
    return bundle
