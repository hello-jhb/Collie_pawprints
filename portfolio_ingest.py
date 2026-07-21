"""
portfolio_ingest — M1 wiring: route each analyzed workbook onto the
portfolio spine.

Called by the server AFTER a successful single-asset analyze. Best-effort and
strictly additive: any failure here is logged and swallowed — it must never
break the live deal-analyzer response. The single-asset session flow is
untouched; this only ADDS a per-property record + refreshed roll-up.

Generality rule: nothing here knows any property by name.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger("collie.portfolio")

# Deal-truth canonical concept -> metric catalog metric_id.
# Only concepts with a clean catalog counterpart; the rest stay session-only.
CANONICAL_TO_METRIC_ID = {
    "purchase_price":  "purchase_price",
    "total_cost":      "total_project_cost",
    "debt":            "debt_amount",
    "equity":          "equity_invested",
    "noi":             "net_operating_income_noi",
    "going_in_cap":    "going_in_cap_rate",
    "exit_cap":        "exit_cap_rate",
    "exit_value":      "exit_value_terminal_value",
    "yield_on_cost":   "yield_on_cost",
    "ltv":             "original_ltv",   # UW-layer LTV is against basis at close
    "levered_irr":     "levered_irr",
    "unlevered_irr":   "unlevered_irr",
    "equity_multiple": "equity_multiple",
}

_SOURCE_RE = re.compile(r"^(?P<sheet>[^!]+)!(?P<cell>[A-Z]{1,3}\d+)")


def _split_source(source: str | None) -> tuple[str | None, str | None]:
    """'Annual-CF!D9 (XIRR)' -> ('Annual-CF', 'D9'); unparseable -> (raw, None)."""
    if not source:
        return None, None
    m = _SOURCE_RE.match(str(source).strip())
    if m:
        return m.group("sheet"), m.group("cell")
    return str(source), None


def record_analysis(
    model_path: str | Path,
    *,
    canonical: dict[str, Any],
    identity_name: str | None = None,
    layer: str = "underwriting",
) -> dict[str, Any]:
    """
    Persist one analyzed workbook's canonical facts into its per-property
    SSOT and refresh the portfolio roll-up.

    `canonical`     — deal_truth's canonical dict ({concept: {value, source,...}}).
    `identity_name` — property name the pipeline already extracted (avoids a
                      second identity read); falls back to reading the workbook.

    Returns {property_id, flags, metrics_written} or {skipped: reason}.
    """
    try:
        # A record must be anchored to a real file — never fabricate a
        # property from a stringified path or a file we can't read.
        if not model_path or not Path(str(model_path)).is_file():
            return {"skipped": "NO_SOURCE"}

        from metric_catalog import load_metric_catalog
        from property_registry import (
            resolve_property_id, resolve_for_workbook, register_alias,
            _plausible_workbook_name, canonicalize_name,
        )
        from ssot import (
            load_property_ssot, save_property_ssot, _now_iso,
        )
        from portfolio import compute_rollup

        # --- identity ---
        if identity_name and _plausible_workbook_name(
                canonicalize_name(identity_name)):
            res = resolve_property_id(identity_name,
                                      filename=str(model_path))
        else:
            res = resolve_for_workbook(model_path)
        pid = res.get("property_id")
        if not pid:
            log.info("portfolio: no identity for %s — skipped (%s)",
                     model_path, res.get("flags"))
            return {"skipped": "NO_IDENTITY", "flags": res.get("flags", [])}

        # remember every observed variant so future files resolve to this id
        if res.get("display_name"):
            register_alias(res["display_name"], pid)

        # --- per-property SSOT write ---
        names = {m["metric_id"]: m.get("metric_name")
                 for m in load_metric_catalog()}
        ssot = load_property_ssot(pid)
        now = _now_iso()
        fname = Path(str(model_path)).name

        lyr = ssot["layers"].setdefault(layer, {"metrics": {}})
        lyr["source_file"] = fname
        lyr["ingested_at"] = now
        lyr.setdefault("metrics", {})
        lyr.setdefault("bounded_metrics", {})

        written = 0
        for concept, rec in (canonical or {}).items():
            mid = CANONICAL_TO_METRIC_ID.get(concept)
            if not mid or not isinstance(rec, dict) or rec.get("value") is None:
                continue
            sheet, cell = _split_source(rec.get("source"))
            lyr["metrics"][names.get(mid, mid)] = {
                "value": rec["value"],
                "sheet": sheet,
                "cell": cell,
                "confidence": "high" if rec.get("validated") else "medium",
            }
            ssot["provenance"].append({
                "layer": layer, "field": names.get(mid, mid),
                "value": rec["value"], "source_file": fname,
                "sheet": sheet, "cell": cell, "extracted_at": now,
            })
            written += 1

        if res.get("display_name"):
            ssot["identity"]["name"] = res["display_name"]
        if fname not in ssot["ingested_files"]:
            ssot["ingested_files"].append(fname)
        save_property_ssot(pid, ssot)

        # --- refresh the fund roll-up ---
        compute_rollup(layer=layer, save=True)

        log.info("portfolio: %s -> %s (%d metrics, flags=%s)",
                 fname, pid, written, res.get("flags"))
        return {"property_id": pid, "flags": res.get("flags", []),
                "metrics_written": written}

    except Exception as e:  # noqa: BLE001 — never break the live analyze path
        log.warning("portfolio: record_analysis failed for %s: %s",
                    model_path, e)
        return {"skipped": "ERROR", "error": str(e)}
