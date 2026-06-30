"""
test_deal_truth.py — checks for the canonical deal-truth layer (Slice 2).

Run: python3 test_deal_truth.py

  * GENERIC invariants — hold for ANY workbook: well-formed canonical facts,
    identities, and guardrails; a validated return really does match its stated
    value; guardrails are evidence-bearing (never hard-coded per file).
  * 1425 STRUCTURAL regression — the deal reconstructs coherently: engine, deal
    type, oracle-validated returns, the three identities, and the right dynamic
    guardrails (cash-flow-total-not-price, non-standard multiple, development).
    Skipped if the model isn't local.

No GPT, no network.
"""
from __future__ import annotations

import sys
from pathlib import Path

from deal_truth import build_deal_truth, _IRR_TOL

_1425 = Path("/Users/jb/Documents/Real Estate/Collie/Data/Underwriting/"
             "1425 4th Ave MF Proforma_(2023.04.21).xlsx")
_SNAPSHOT = Path("Snapshot Metric.xlsx")

_fail = 0


def check(cond: bool, msg: str) -> None:
    global _fail
    if not cond:
        _fail += 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")


def _idents(d: dict) -> dict:
    return {i["name"]: i for i in d["identities"]}


def _codes(d: dict) -> set:
    return {g["code"] for g in d["guardrails"]}


def generic_invariants(d: dict) -> None:
    print(f"\n— generic invariants: {d.get('file')}")
    check(not d.get("error"), "built without error")
    check(isinstance(d.get("canonical"), dict), "canonical is a dict")
    check(all("value" in c and "source" in c for c in d["canonical"].values()),
          "every canonical fact has value + source")
    # A return flagged validated truly matches its stated IRR.
    bad = []
    for leg in ("levered", "unlevered"):
        o = d["oracle"].get(leg)
        if o and o.get("validated"):
            if abs(o["recomputed_irr"] - o["stated_irr"]) > _IRR_TOL:
                bad.append(leg)
    check(not bad, "validated returns actually match stated IRR")
    check(all(g.get("code") and g.get("message") for g in d["guardrails"]),
          "every guardrail has a code + message")
    check(all("name" in i and "checked" in i for i in d["identities"]),
          "every identity is well-formed")
    check(isinstance(d.get("brief_facts"), list)
          and all("label" in b and "found" in b for b in d["brief_facts"]),
          "brief_facts is a well-formed non-negotiable set")


def regression_1425(d: dict) -> None:
    print("\n— 1425 structural regression")
    can = d["canonical"]
    ids = _idents(d)
    codes = _codes(d)

    check(d["deal_type"] == "development", f"deal type = development (got {d['deal_type']})")
    check(d["cashflow_engine"] == "Monthly-CF", "cashflow engine = Monthly-CF")

    lev = d["oracle"].get("levered")
    check(lev and abs(lev["recomputed_irr"] - 0.1731) < 0.002 and lev["validated"],
          "levered IRR recomputed ≈17.31% and validated against the stream")
    check("equity_multiple" in can and abs(float(can["equity_multiple"]["value"]) - 3.23) < 0.05,
          "equity multiple ≈3.23x (from cash flow)")
    check("exit_cap" in can and abs(float(can["exit_cap"]["value"]) - 0.0525) < 1e-6,
          "exit cap canonical = 5.25% (the referenced input, not 6%)")
    check("total_cost" in can and abs(float(can["total_cost"]["value"]) - 54_984_255) < 0.05 * 54_984_255,
          "total cost within 5% of $54.98M (stream draws ≈ stated cost)")
    check("noi" in can and float(can["noi"]["value"]) > 1e6, "stabilized NOI captured")

    check(ids.get("debt+equity≈cost", {}).get("passed") is True, "identity: debt+equity≈cost passes")
    check(ids.get("exit_value≈NOI/exit_cap", {}).get("passed") is True,
          "identity: exit value ≈ NOI / exit cap passes")
    check(ids.get("recomputed_irr≈stated", {}).get("passed") is True,
          "identity: recomputed IRR ≈ stated passes")

    check("cashflow_total_not_price" in codes,
          "guardrail: the $49.1M cash-flow total is flagged as not-a-price")
    check("nonstandard_multiple" in codes,
          "guardrail: stated multiple flagged non-standard vs cash-flow multiple")
    check("development_language" in codes, "guardrail: development → yield-on-cost language")
    check("missing_metric" not in codes, "no false 'metric missing' (all non-negotiables found)")
    check("returns_unvalidated" not in codes, "returns are validated (no unvalidated guardrail)")

    # Slice: hold from the sale event, full non-negotiable set, stream-sourced NOI.
    hold = d.get("hold") or {}
    check(110 <= (hold.get("months") or 0) <= 121,
          f"hold detected from the sale event (~120 mo, got {hold.get('months')})")
    check("unlevered_equity_multiple" in can
          and abs(float(can["unlevered_equity_multiple"]["value"]) - 2.21) < 0.1,
          "unlevered equity multiple ≈2.21x (from the unlevered stream)")
    check("sale_price" in can and abs(float(can["sale_price"]["value"]) - 86_491_038) < 0.05 * 86_491_038,
          "gross sale price within 5% of $86.5M")
    noi_op = (d.get("operating_series") or {}).get("noi") or {}
    check(noi_op.get("provenance") in ("operating_model", "unlevered_stream"),
          f"NOI trajectory is cash-flow-sourced, not the summary (got {noi_op.get('provenance')})")
    check(isinstance(noi_op.get("terminal"), (int, float)) and noi_op["terminal"] > 3e6,
          "stream-derived terminal NOI is realistic (~$4.5M)")
    bf = {b["label"]: b["found"] for b in d["brief_facts"]}
    check(all(bf.get(lbl) for lbl in
              ("Total cost", "Hold period", "Sales price (gross)", "Exit cap rate",
               "Levered IRR", "Unlevered IRR", "Levered equity multiple",
               "Unlevered equity multiple")),
          "all required brief non-negotiables found")


def main() -> int:
    ran = False
    for p in (_SNAPSHOT, _1425):
        if p.exists():
            ran = True
            generic_invariants(build_deal_truth(p))
    if not ran:
        print("No test workbooks found.")
        return 1
    if _1425.exists():
        regression_1425(build_deal_truth(_1425))
    else:
        print("\n— 1425 structural regression: SKIPPED (file not local)")
    print(f"\n{'ALL PASS' if _fail == 0 else f'{_fail} FAILURE(S)'}")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
