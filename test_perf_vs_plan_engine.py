"""
test_perf_vs_plan_engine.py — checks for the perf-vs-plan definition match (Stage 3).

Run: python3 test_perf_vs_plan_engine.py

  * match_definitions LOGIC (synthetic basis dicts): agreement confirms; a plan-only
    deduction conflicts; a statement-only deduction the model can't confirm is
    'unconfirmed' (proceed-with-caveat); capex-in-opex on the statement conflicts.
  * STATEMENT BASIS on the real samples: the three statements report a sane NOI basis
    (mgmt fee detected where present; capex below NOI, not in opex).

No GPT, no network.
"""
from __future__ import annotations

import sys
from pathlib import Path

import tempfile

from perf_vs_plan_engine import match_definitions, plan_basis_from_rollup, build_perf_vs_plan
from actuals_statement import extract_actuals

_T12 = Path("/Users/jb/Documents/Real Estate/Collie/Data/T-12 Sample 1.xlsx")
_ALT = Path("/Users/jb/Documents/Real Estate/Collie/Data/OMs/Alt Flats/"
            "Alt Flats/Financials/Alt T12.xlsx")
_PROTO = Path("/Users/jb/Documents/Real Estate/Collie/Prototype 3")
_MODEL = _PROTO / "Retail-Acquisition Underwriting.xlsx"
_STMT = _PROTO / "Financial Statement 2021.xlsx"

_fail = 0


def check(cond: bool, msg: str) -> None:
    global _fail
    if not cond:
        _fail += 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")


def match_logic() -> None:
    print("\n— match_definitions logic (synthetic basis)")
    both = {"includes_mgmt_fee": True, "includes_reserves": False, "capex_in_opex": False}
    plan_y = {"includes_mgmt_fee": True, "includes_reserves": True}
    m = match_definitions(plan_y | {"includes_reserves": None}, both)
    check(m["verdict"] == "confirmed",
          "plan & statement agree on mgmt fee, neither has reserves → confirmed")

    # Plan deducts a reserve the statement does not → conflict, never a clean variance.
    m = match_definitions({"includes_mgmt_fee": True, "includes_reserves": True}, both)
    check(m["verdict"] == "conflict" and any("reserve" in c for c in m["caveats"]),
          "plan-only reserve → conflict with a caveat")

    # Statement deducts a mgmt fee the model can't confirm → unconfirmed (proceed w/ caveat).
    act_fee = {"includes_mgmt_fee": True, "includes_reserves": False, "capex_in_opex": False}
    m = match_definitions({"includes_mgmt_fee": None, "includes_reserves": None}, act_fee)
    check(m["verdict"] == "unconfirmed" and not m["confirmed"],
          "statement mgmt fee, model unknown → unconfirmed (not silently compared)")

    # Statement strikes NOI after capex → conflict.
    m = match_definitions({"includes_mgmt_fee": None, "includes_reserves": None},
                          {"includes_mgmt_fee": False, "includes_reserves": False,
                           "capex_in_opex": True})
    check(m["verdict"] == "conflict" and any("capex" in c.lower() for c in m["caveats"]),
          "capex above the NOI line on the statement → conflict")


def statement_basis() -> None:
    print("\n— statement NOI basis on real samples")
    for path, name, want_fee in ((_T12, "retail", True), (_ALT, "multifamily", True)):
        if not path.exists():
            print(f"  (skip {name}: sample not local)")
            continue
        b = extract_actuals(path).get("basis", {})
        check(b.get("includes_mgmt_fee") is want_fee,
              f"{name}: management fee {'detected' if want_fee else 'absent'} above NOI")
        check(b.get("capex_in_opex") is False,
              f"{name}: NOI struck before capex (capex not in opex)")


def end_to_end() -> None:
    if not (_MODEL.exists() and _STMT.exists()):
        print("\n— matched-pair end-to-end SKIPPED (Prototype 3 not local)")
        return
    print("\n— matched-pair end-to-end (Retail UW model + 2021 statement)")
    r = build_perf_vs_plan(_MODEL, _STMT)
    check(r["ok"], "pipeline runs on the matched pair")
    v = r["variance"]
    check(v["basis"] == "calendar",
          "aligns by CALENDAR month, not naive elapsed index")
    check(v["n"] == 10, f"10 overlapping months — plan starts Mar 2021 (got {v['n']})")
    check(v["pct"] is not None and abs(v["pct"] * 100 - (-17.5)) < 0.6,
          f"NOI ≈17.5% below plan (got {v['pct'] * 100:+.1f}%)")
    check(r["definition_match"]["verdict"] == "unconfirmed"
          and any("management fee" in c for c in r["definition_match"]["caveats"]),
          "mgmt-fee definition caveat fires visibly (DoD #3)")
    # Output B — blended returns now computed (actuals 17.5% under plan → both legs dip).
    rets = {x["leg"]: x for x in r["returns"]}
    check(rets.get("unlevered", {}).get("available") and rets.get("levered", {}).get("available"),
          "both legs' blended returns computed (NOI-delta needs no debt service)")
    check(rets["unlevered"]["blended_irr"] < rets["unlevered"]["projected_irr"]
          and rets["levered"]["blended_irr"] < rets["levered"]["projected_irr"],
          "blended IRR tracks BELOW plan on both legs (actuals were under)")
    check(rets["levered"]["projected_irr"] - rets["levered"]["blended_irr"]
          > rets["unlevered"]["projected_irr"] - rets["unlevered"]["blended_irr"],
          "leverage amplifies the NOI miss (levered drops more bps)")
    # What moved — line-item variance, with the bridge as the self-check.
    items = r.get("items")
    check(items and items["categories"], "line-item variance decomposed by category")
    resid = v["delta"] - items["explained_noi_delta"]
    check(abs(resid) <= max(2.0, 0.03 * abs(v["delta"])),
          f"variance bridge closes: revenue Δ − opex Δ ≈ NOI Δ (residual {resid:,.0f})")
    cats = {c["concept"] for c in items["categories"]}
    check({"utilities", "repairs_maintenance"} <= cats,
          "real opex categories aligned plan↔actual (utilities, R&M, …)")
    check(any(c["status"] == "actual_only" for c in items["categories"]),
          "an orphan is surfaced (a category in the actuals not in the plan — e.g. bad debt)")


def returns_splice() -> None:
    print("\n— returns: NOI-delta splice (preserves capex; direction correct)")
    import datetime as dt
    from perf_vs_plan_engine import _blend_leg, xirr
    # acq, 4 operating months (one a capex/TI OUTFLOW), sale.
    flows = [
        (dt.date(2021, 1, 1), -1000.0),
        (dt.date(2021, 2, 1), 100.0),
        (dt.date(2021, 3, 1), -300.0),    # capex month — operating but negative
        (dt.date(2021, 4, 1), 100.0),
        (dt.date(2021, 5, 1), 100.0),
        (dt.date(2021, 12, 1), 1300.0),   # sale
    ]
    overlap = {"2021-02", "2021-03", "2021-04", "2021-05"}
    base = _blend_leg(flows, {k: 0.0 for k in overlap}, overlap)
    check(abs(base["blended_irr"] - xirr(flows)) < 1e-9,
          "zero NOI-delta leaves the stream (and IRR) unchanged")
    lower = _blend_leg(flows, {k: -20.0 for k in overlap}, overlap)
    higher = _blend_leg(flows, {k: +20.0 for k in overlap}, overlap)
    check(lower["blended_irr"] < base["blended_irr"] < higher["blended_irr"],
          "actual below plan → IRR drops; above plan → IRR rises (capex NOT overwritten)")
    check(base["n_spliced"] == 4, "all operating overlap months adjusted, acq + sale untouched")


def trust_gate_blocks() -> None:
    print("\n— trust gate: a non-footing statement blocks the comparison (DoD #2)")
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "2021"
    for row in [
        [None, None, "Jan 2021", "Feb 2021", "Mar 2021", "Total"],
        ["40000", "Total Operating Revenue", 100, 100, 100, 300],
        ["51000", "Utilities", 25, 30, 25, 80],
        ["52000", "Repairs & Maintenance", 15, 15, 15, 45],
        ["79999", "Net Operating Income", 60, 99, 60, 219],   # Feb should be 55
    ]:
        ws.append(row)
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tf:
        wb.save(tf.name)
        r = build_perf_vs_plan(_MODEL if _MODEL.exists() else "unused.xlsx", tf.name)
    check(not r["ok"] and r["blocked"] == "actuals_not_trusted",
          "non-footing statement → comparison withheld, not silently computed")


if __name__ == "__main__":
    match_logic()
    statement_basis()
    returns_splice()
    end_to_end()
    trust_gate_blocks()
    print(f"\n{'ALL PASS' if _fail == 0 else f'{_fail} FAILED'}")
    sys.exit(1 if _fail else 0)
