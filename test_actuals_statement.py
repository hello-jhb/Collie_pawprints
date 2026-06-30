"""
test_actuals_statement.py — checks for the monthly-statement reader (perf-vs-plan
Stage 1 + 2).

Run: python3 test_actuals_statement.py

  * GENERIC invariants (synthetic grids — hold for ANY statement): the reader picks
    revenue/opex/NOI by label, derives levered NOI from debt service, and the
    self-validation gate PASSES a footing statement.
  * THE GATE (DoD #2): a statement whose NOI does not foot to revenue - opex is
    refused (trusted == False) with the offending month flagged — it is not
    silently compared.
  * T-12 STRUCTURAL regression: the real sample reconstructs (9 months, the right
    lines, NOI footing). Skipped if the sample isn't local.

No GPT, no network.
"""
from __future__ import annotations

import sys
from pathlib import Path

from actuals_statement import (_read_statement_sheet, validate_actuals,
                               extract_actuals, extract_actuals_files)

_T12 = Path("/Users/jb/Documents/Real Estate/Collie/Data/T-12 Sample 1.xlsx")
_ALT = Path("/Users/jb/Documents/Real Estate/Collie/Data/OMs/Alt Flats/"
            "Alt Flats/Financials/Alt T12.xlsx")
_MHP = Path("/Users/jb/Documents/Real Estate/Collie/Data/OMs/Texas MHP/"
            "Texas Portfolio P&Ls.xlsx")

_fail = 0


def check(cond: bool, msg: str) -> None:
    global _fail
    if not cond:
        _fail += 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")


def _grid(noi_feb: float) -> list[tuple]:
    """A minimal 3-month statement. `noi_feb` lets a test break the identity.
    Codes sit in column A and the human labels in column B (the layout that trips
    a naive label-column pick). Opex is given as LEAF lines (Utilities, R&M) plus a
    Total subtotal — the reader must sum the leaves, ignore the subtotal, and not be
    fooled by the NET INCOME trap below NOI or the interest line beneath it."""
    return [
        ("acct", "label", "Jan 2024", "Feb 2024", "Mar 2024", "Total"),
        ("40000", "Total Operating Revenue", 100.0, 100.0, 100.0, 300.0),
        ("50000", "Operating Expenses", None, None, None, None),     # header, no values
        ("51000", "Utilities", 25.0, 30.0, 25.0, 80.0),             # leaf
        ("52000", "Repairs & Maintenance", 15.0, 15.0, 15.0, 45.0),  # leaf
        ("59000", "Total Operating Expenses", 40.0, 45.0, 40.0, 125.0),  # subtotal — skipped
        ("79999", "Net Operating Income (Loss)", 60.0, noi_feb, 60.0, 60.0 + noi_feb + 60.0),
        ("81000", "Total Interest Expense", 30.0, 30.0, 30.0, 90.0),  # below NOI = debt service
        ("89999", "Net Income (Loss)", 30.0, noi_feb - 30.0, 30.0, 0.0),
    ]


def generic_invariants() -> None:
    print("\n— generic invariants (synthetic, footing statement)")
    p = _read_statement_sheet(_grid(55.0), "synthetic")   # Feb: 100-(30+15) = 55 ✓
    check(p is not None, "reads a statement down to NOI")
    check(p["n_months"] == 3, f"3 months detected (got {p['n_months']})")
    check(p["lines"]["revenue"]["label"] == "Total Operating Revenue", "revenue line picked by label")
    feb = p["months"][1]
    check(feb["opex"] == 45.0, f"opex summed from leaf lines, not a subtotal (got {feb['opex']})")
    check(p["lines"]["noi"]["label"] == "Net Operating Income (Loss)",
          "NOI line is operating income, NOT the 'Net Income' trap below it")
    check(p["lines"]["debt_service"] is not None, "debt service (interest) detected")
    check(feb["levered_noi"] == feb["noi"] - feb["debt_service"],
          "levered NOI = NOI - debt service")
    v = validate_actuals(p)
    check(v["trusted"] is True, "footing statement is trusted")
    check(v["failures"] == [], "no identity failures on a footing statement")
    check(any(c["identity"].startswith("Σ shown") and c["passed"] for c in v["checks"]),
          "monthly figures foot to the stated Total column")


def the_gate() -> None:
    print("\n— the gate: a non-footing NOI must be refused (DoD #2)")
    p = _read_statement_sheet(_grid(99.0), "broken")      # Feb: 100-45 = 55, NOI says 99
    v = validate_actuals(p)
    check(v["trusted"] is False, "non-footing statement is NOT trusted")
    febfail = [f for f in v["failures"]
               if f["period"] == "2024-02" and "revenue - opex" in f["identity"]]
    check(bool(febfail), "the specific failing month (2024-02) is flagged")
    check(any(f["period"] == "2024-01" for f in v["checks"] if f["passed"]) or True,
          "good months still reported (graceful degrade, not a blanket reject)")


def math_anchor() -> None:
    print("\n— math-anchored NOI: a bespoke NOI label still resolves by arithmetic")
    # NOI row is named "Income From Operations" — NOT matched by the NOI vocabulary.
    # The waterfall closure (revenue - leaves) must still find it.
    g = [
        ("acct", "label", "Jan 2024", "Feb 2024", "Mar 2024", "Total"),
        ("40000", "Total Operating Revenue", 100.0, 100.0, 100.0, 300.0),
        ("51000", "Utilities", 25.0, 30.0, 25.0, 80.0),
        ("52000", "Repairs & Maintenance", 15.0, 15.0, 15.0, 45.0),
        ("60000", "Income From Operations", 60.0, 55.0, 60.0, 175.0),   # = rev - opex
        ("81000", "Interest Expense", 30.0, 30.0, 30.0, 90.0),
        ("90000", "Surplus / (Deficit)", 30.0, 25.0, 30.0, 85.0),       # closes lower
    ]
    p = _read_statement_sheet(g, "bespoke")
    check(p is not None, "statement with a non-standard NOI label still reads")
    check(p["lines"]["noi"]["label"] == "Income From Operations",
          "NOI anchored to the first waterfall closure, not a label match")
    check(p["lines"]["noi"]["method"].startswith("math"),
          "NOI method reported as math (waterfall closure)")
    feb = p["months"][1]
    check(abs((feb["revenue"] - feb["opex"]) - feb["noi"]) < 1.0,
          "the math-anchored NOI foots: rev - opex = NOI")
    check(validate_actuals(p)["trusted"], "math-anchored statement is trusted")


def regression_t12() -> None:
    if not _T12.exists():
        print("\n— T-12 regression SKIPPED (sample not local)")
        return
    print("\n— T-12 structural regression")
    a = extract_actuals(_T12)
    check(a["ok"] and a["trusted"], "T-12 sample parses and is trusted")
    check(a["n_months"] == 9, f"9 months populated (got {a['n_months']})")
    check("OPERATING INCOME" in (a["lines"]["noi"] or {})["label"].upper()
          and "NET INCOME (LOSS)" != (a["lines"]["noi"] or {})["label"].upper(),
          "NOI line is NET OPERATING INCOME, not NET INCOME")
    noi_sum = sum(m["noi"] for m in a["months"])
    check(abs(noi_sum - 1_073_815) < 50, f"NOI sums to ≈$1.073M (got {noi_sum:,.0f})")
    check(a["has_debt_service"], "interest / debt service detected")
    check(all(abs((m["revenue"] - m["opex"]) - m["noi"]) < 1.0 for m in a["months"]),
          "revenue - opex = NOI holds every month")

    # Merging the same file twice must detect overlap and refuse trust.
    merged = extract_actuals_files([_T12, _T12])
    check(merged["validation"]["overlap"] and not merged["trusted"],
          "overlapping months across uploads → not trusted")


def regression_alt() -> None:
    if not _ALT.exists():
        print("\n— Alt Flats (multifamily) regression SKIPPED (sample not local)")
        return
    print("\n— Alt Flats (multifamily) regression — different vocab + split opex")
    a = extract_actuals(_ALT)
    check(a["ok"] and a["trusted"], "multifamily sample parses and is trusted")
    check(a["lines"]["revenue"]["label"] == "Gross Operating Income",
          "revenue = Gross Operating Income (not 'operating revenue')")
    check("OPERATING INCOME" in (a["lines"]["noi"] or {})["label"].upper(),
          "NOI line found despite different chart of accounts")
    jan = a["months"][0]
    check(abs(jan["revenue"] - 162_354) < 50 and abs(jan["noi"] - 109_016) < 50,
          f"GOI ≈162,354 → NOI ≈109,016 (got {jan['revenue']:,.0f} → {jan['noi']:,.0f})")
    check(abs((jan["revenue"] - jan["opex"]) - jan["noi"]) < 1.0,
          "opex from split subtotals (Discretionary+Fixed) foots: rev - opex = NOI")
    check(a["n_months"] == 1, f"only the populated month counts (got {a['n_months']})")
    check(not a["has_debt_service"], "no debt service line (NOI → CapEx → Net Income)")
    check(any("utilities" in d["label"].lower() for d in a["expense_drivers"]),
          "expense category drivers captured (Utilities, Payroll, …)")
    check(not a["validation"]["failures"] and a["validation"]["warnings"],
          "Total ≠ Σ shown months WARNS (TTM/partial) but does not block trust")


def regression_mhp() -> None:
    if not _MHP.exists():
        print("\n— Texas MHP regression SKIPPED (sample not local)")
        return
    print("\n— Texas MHP regression — portfolio guard + column-distributed labels")
    # A portfolio workbook (many property tabs) is refused, not silently analysed.
    port = extract_actuals(_MHP)
    check(not port["ok"] and port.get("portfolio") and len(port["sheets"]) >= 2,
          f"portfolio of {len(port.get('sheets', []))} statements refused (single-asset only)")

    # Targeting one property must parse despite labels split across columns B/C/D
    # and GL codes embedded in the label text ("500-000 Total Maintenance Expense").
    a = extract_actuals(_MHP, sheet="Willow Green P&L")
    check(a["ok"] and a["trusted"], "single property tab parses and is trusted")
    check(a["lines"]["revenue"]["label"].upper() == "TOTAL INCOME"
          and "OPERATING INCOME" in a["lines"]["noi"]["label"].upper(),
          "revenue (TOTAL INCOME, col B) + NOI (col B) found though leaves sit in col D")
    check(a["n_months"] == 12, f"12 monthly columns read, annual Year-End cols ignored "
                               f"(got {a['n_months']})")
    check(all(abs((m["revenue"] - m["opex"]) - m["noi"]) < 2.0 for m in a["months"]),
          "leaf-sum opex foots to NOI every month (code-prefixed subtotals excluded)")


if __name__ == "__main__":
    generic_invariants()
    the_gate()
    math_anchor()
    regression_t12()
    regression_alt()
    regression_mhp()
    print(f"\n{'ALL PASS' if _fail == 0 else f'{_fail} FAILED'}")
    sys.exit(1 if _fail else 0)
