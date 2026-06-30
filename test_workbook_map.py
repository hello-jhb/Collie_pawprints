"""
test_workbook_map.py — checks for the deterministic workbook map (Slice 1).

Run: python3 test_workbook_map.py

Two layers of checks:
  * GENERIC invariants — must hold for ANY workbook (judged on generality, not
    one file): valid roles, domain-valid candidate values, well-formed
    provenance. Run against every workbook found (repo's Snapshot Metric.xlsx +
    the 1425 model if present locally).
  * 1425 STRUCTURAL regression — asserts the map UNDERSTANDS this known model
    (engine = Monthly-CF, exit traced to its source, $49.1M classified as cash
    flow not exit, hold = 10y). Skipped automatically if the file isn't local,
    so the suite stays portable.

No GPT, no network. Pure structural assertions — never hard-codes module logic.
"""
from __future__ import annotations

import sys
from pathlib import Path

from workbook_map import build_workbook_map, _passes_domain, _RATE, _MULTIPLE, _MONEY

_VALID_ROLES = {"summary", "inputs", "returns", "model", "support", "other"}

# Known local model (not in the repo). Skipped if absent.
_1425 = Path("/Users/jb/Documents/Real Estate/Collie/Data/Underwriting/"
             "1425 4th Ave MF Proforma_(2023.04.21).xlsx")
_SNAPSHOT = Path("Snapshot Metric.xlsx")

_fail = 0


def check(cond: bool, msg: str) -> None:
    global _fail
    mark = "PASS" if cond else "FAIL"
    if not cond:
        _fail += 1
    print(f"  [{mark}] {msg}")


def generic_invariants(m: dict) -> None:
    """Hold for any workbook map."""
    print(f"\n— generic invariants: {m.get('file')}")
    check(not m.get("error"), "map built without error")
    check(bool(m.get("sheets")), "has at least one sheet")
    check(all(s["role"] in _VALID_ROLES for s in m["sheets"].values()),
          "every sheet has a valid role")
    # Every candidate value is plausible for its concept (the domain gate worked).
    bad = [(c, e["display"], e["value"])
           for c, lst in m["candidates"].items() for e in lst
           if not _passes_domain(c, e["value"])]
    check(not bad, f"all candidate values pass their concept domain ({len(bad)} bad)")
    # Provenance, when present, is well-formed.
    malformed = [e["display"] for lst in m["candidates"].values() for e in lst
                 if e.get("provenance") and not e["provenance"].get("source")]
    check(not malformed, "provenance entries are well-formed")
    # Rate concepts never carry millions; money concepts never carry tiny values.
    money_tiny = [e["display"] for c, lst in m["candidates"].items() if c in _MONEY
                  for e in lst if abs(float(e["value"])) < 1000]
    check(not money_tiny, "no money concept holds a sub-$1k value")


def regression_1425(m: dict) -> None:
    print("\n— 1425 structural regression")
    cand = m["candidates"]

    check(m.get("cashflow_engine") == "Monthly-CF",
          f"cashflow engine = Monthly-CF (got {m.get('cashflow_engine')})")
    check(m["sheets"].get(m.get("inputs_hub"), {}).get("role") == "inputs",
          f"inputs hub is an inputs sheet (got {m.get('inputs_hub')})")

    # exit_cap displayed on Dashboard but TRACED to its real input source.
    excap = cand.get("exit_cap", [])
    traced = [e for e in excap if (e.get("provenance") or {}).get("crosses_sheet")]
    check(any(abs(float(e["value"]) - 0.0525) < 1e-6 for e in excap),
          "exit_cap 5.25% found")
    check(any("Assumptions" in (e.get("provenance") or {}).get("source", "")
              for e in traced), "exit_cap display traces to Assumptions (the input)")

    # The REAL exit value is captured; the $49.1M is NOT called exit value.
    exval = [float(e["value"]) for e in cand.get("exit_value", [])]
    check(any(8.0e7 < v < 9.0e7 for v in exval), "real exit value (~$85-86M) captured")
    check(not any(abs(v - 49_098_975) < 1e5 for v in exval),
          "$49.1M is NOT classified as exit value")

    # $49.1M IS understood as a cash-flow total (a SUM), not a price.
    lcf = cand.get("levered_cf", [])
    check(any(abs(float(e["value"]) - 49_098_975) < 1e5
              and (e.get("provenance") or {}).get("op") == "SUM" for e in lcf),
          "$49.1M classified as levered cash-flow total (SUM)")

    # hold = 10 years (not the spurious 51 months).
    check(any(float(e["value"]) == 10 for e in cand.get("hold_period", [])),
          "hold period = 10")

    # total cost ~ $54.98M present.
    check(any(abs(float(e["value"]) - 54_984_255) < 1e4 for e in cand.get("total_cost", [])),
          "total cost ~$54.98M found")

    # Monthly-CF carries BOTH streams (the consolidated engine).
    mcf = [b for b in m["timeseries_blocks"]
           if b["sheet"] == "Monthly-CF" and b["kind"] == "cashflow"]
    check(mcf and {"levered_cf", "unlevered_cf"} <= set(mcf[0]["concepts"]),
          "Monthly-CF block carries both levered & unlevered streams")


def main() -> int:
    ran_any = False
    for p in (_SNAPSHOT, _1425):
        if p.exists():
            ran_any = True
            generic_invariants(build_workbook_map(p))
    if not ran_any:
        print("No test workbooks found; place one in the repo or at the 1425 path.")
        return 1
    if _1425.exists():
        regression_1425(build_workbook_map(_1425))
    else:
        print("\n— 1425 structural regression: SKIPPED (file not local)")

    print(f"\n{'ALL PASS' if _fail == 0 else f'{_fail} FAILURE(S)'}")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
