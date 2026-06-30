"""
actuals_statement.py — read a monthly operating STATEMENT (a financial statement,
not a DCF model) down to NOI + debt service, and let it grade its own parse.

Why this is its own reader (a statement is shaped unlike the model):
  * No DCF / no stated IRR — it self-validates by an accounting identity, not XIRR.
  * Labels frequently sit in a second column (the account NAME) beside a GL-code
    column, so the *densest* text column is the codes, not the labels.
  * The period axis is often TEXT ("Jan 2022") with a trailing Total / YTD column
    that must be excluded from the monthly series.
  * Coverage is partial (T1 / T3 / T6 …) — as few as ONE month — so the engine's
    six-period stream minimum (cashflow_spine._MIN_PERIODS) does NOT apply.
  * Product types disagree on vocabulary and structure: a retail T-12 reads
    "TOTAL OPERATING REVENUE / TOTAL OPERATING EXPENSES / NET OPERATING INCOME";
    a multifamily one reads "Gross Operating Income", splits opex into
    "Discretionary + Fixed" with NO single opex total, and may carry no debt line.

The taxonomy-independent invariant that earns trust — mirroring the model side's
stated-IRR self-consistency — is the identity ``revenue - opex = NOI`` per month,
where opex is the SUM of the leaf expense line items between the revenue total and
the NOI line (every Total/Net/Gross subtotal skipped, so the expense hierarchy
cannot double-count). If it does not foot, we FLAG and refuse rather than emit a
confident-but-wrong NOI. That refusal is the product working.

Discipline (same hard rule as the engine — no single-file tuning): the revenue and
NOI lines are found by UNIVERSAL accounting vocabulary, never by GL account codes
(which vary by Yardi / MRI / RealPage) or row numbers; opex comes from the leaf sum,
not a vocabulary-matched cell; and the arithmetic identity grades the result.

Scope (V1): NOI + debt service only, plus the expense category subtotals as drivers.
Not a general statement parser, and no returns recalc here — that lives in the
perf-vs-plan engine and stays gated until the actuals provide a definition-
compatible cash-flow replacement.
"""
from __future__ import annotations

import datetime as _dt
import logging
import re as _re
import sys
from pathlib import Path
from typing import Any

from cashflow_spine import _load_grids, _coerce_date, _num, sheet_scale

log = logging.getLogger("fb.actuals")
if not log.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("[fb.actuals] %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

ACTUALS_VERSION = "2026-06-16.2"

# Universal revenue-total vocabulary (retail "operating revenue", multifamily
# "gross operating income" / "effective gross income", "gross potential", "total
# income"). NOT account codes — those are property-manager-specific. The arithmetic
# identity then grades the choice.
_RE_REVENUE = _re.compile(
    r"operating revenue|gross operating income|effective gross income|gross potential"
    r"|\begi\b|\bgoi\b|total revenue|total operating income|total income|net rental income")
_RE_NOI = _re.compile(r"net operating income|\bnoi\b")
_RE_NETINC = _re.compile(r"\bnet income\b")            # the trap line that sits below NOI
_RE_INTEREST = _re.compile(r"interest expense|debt service|total interest|mortgage interest")
# NOI-basis markers — what is deducted ABOVE the NOI line decides whether two NOIs
# mean the same thing (the Stage-3 definition match). Detected on the leaf labels.
_RE_MGMT = _re.compile(r"management fee|mgmt fee|asset management fee")
_RE_RESERVE = _re.compile(r"replacement reserve|capital reserve|reserve for replacement|\breserves?\b")
_RE_CAPEX = _re.compile(r"capital expenditure|\bcapex\b|capital expense|cap[\s-]?ex\b")
_RE_TOTALCOL = _re.compile(r"\b(total|ytd|year[\s-]?to[\s-]?date|ttm|t-?12)\b", _re.I)
_RE_WORD = _re.compile(r"[A-Za-z]{3,}")               # a real label word (not a code)
# A GL code PREFIX embedded in the label text ("500-000 Total Maintenance Expense").
# Some exporters put the code in its own column; others prefix it onto the name — we
# strip it before matching so "500-000 Total …" reads as a subtotal, not a leaf.
_RE_CODEPREFIX = _re.compile(r"^\d[\dA-Za-z]*(?:-[\dA-Za-z]+)+\s+")
# A row that is a SUBTOTAL (a sum of other rows), excluded from the leaf opex sum so
# the expense hierarchy never double-counts. Universal across charts of accounts.
_RE_SUBTOTAL = _re.compile(r"^(total|net|gross|subtotal|sub[\s-]total)\b")

# Canonical operating-expense categories — for line-item variance ("what moved").
# Names won't match across statement vs model, so we map BOTH to these concepts and
# compare like-for-like. Order matters (first match wins): the specific categories
# come before the catch-alls (e.g. "Property Tax" → tax before "Audit & Tax Fees" →
# professional). A leaf that matches nothing falls into "other" — so the per-concept
# sums always foot back to total opex (the variance bridge can self-check).
_OPEX_CATS = [
    ("real_estate_tax",     r"property tax|real estate tax|\br/?e tax\b"),
    ("insurance",           r"insurance"),
    ("utilities",           r"utilit|electric|\bwater\b|sewer|\btrash\b|\bgas\b|\bpower\b|telephon"),
    ("repairs_maintenance", r"repair|mainten|\br&m\b|r & m|hvac|plumb|elevator|pest|janitor"
                            r"|landscap|\bgrounds\b|turnover|\bturn\b|cleaning|sweeping|amenity"),
    ("management",          r"management fee|mgmt fee|property management|prop mgmt|asset management|management exp"),
    ("payroll",             r"payroll|salar|\bwages\b|\blabor\b|bonus|employee"),
    ("marketing_leasing",   r"market|advertis|leasing|promotion|\blease\b|\bcrm\b|retention|website"),
    ("professional",        r"professional|legal|\baudit\b|account|consult|bookkeep|tax fee"),
    ("bad_debt",            r"bad debt"),
    ("security",            r"\bsecurity\b"),
    ("administrative",      r"administ|\badmin\b|general & admin|\bg&a\b|\boffice\b|postage|bank fee"
                            r"|software|licens|business tax|invoice|intercom|\bdues\b|subscription"),
]
_OPEX_CAT_RE = [(c, _re.compile(p)) for c, p in _OPEX_CATS]
_RE_OPEX_TOTAL = _re.compile(r"^total operating expense|^total cost of (re|real estate)"
                             r"|^operating expense|^total expense", _re.I)


def opex_concept(label: str) -> str | None:
    """Map an expense line to a canonical category concept. None = the opex GRAND
    total (skip — not a category); 'other' = a real line that matched no category."""
    n = _clean_label(label).strip().lower()
    if _RE_OPEX_TOTAL.match(n):
        return None
    for concept, rx in _OPEX_CAT_RE:
        if rx.search(n):
            return concept
    return "other"


# ---------------------------------------------------------------------------
# Layout detection (statement-appropriate; no six-period floor)
# ---------------------------------------------------------------------------

def _clean_label(label: str) -> str:
    return _RE_CODEPREFIX.sub("", str(label).strip()).strip()[:60]


def _norm(label: str) -> str:
    return _re.sub(r"\s+", " ", _clean_label(label).lower())


def _increasing(dated: list[tuple[int, _dt.date]]) -> list[tuple[int, _dt.date]]:
    """Keep only the strictly-increasing-by-date prefix run — drops a trailing
    duplicate or a stray non-monotonic cell (e.g. a Total column read as a date)."""
    run: list[tuple[int, _dt.date]] = []
    prev: _dt.date | None = None
    for c, d in dated:
        if prev is None or d > prev:
            run.append((c, d))
            prev = d
    return run


def _find_total_col(axis_row: tuple, run: list[tuple[int, _dt.date]]) -> int | None:
    """A Total / YTD / TTM column header in the axis row (so monthly figures can be
    checked against the stated total). Searched to the right of the last date."""
    last = run[-1][0] if run else -1
    for c in range(last + 1, len(axis_row)):
        v = axis_row[c]
        if isinstance(v, str) and _RE_TOTALCOL.search(v):
            return c
    return None


def _find_period_axis(grid: list[tuple]) -> dict | None:
    """The row with the most month/period dates is the period axis. Unlike the
    engine's stream finder, ONE date is enough (T1 statements are valid)."""
    best: dict | None = None
    for r, row in enumerate(grid):
        dated = [(c, d) for c, v in enumerate(row)
                 if (d := _coerce_date(v)) is not None]
        run = _increasing(dated)
        if run and (best is None or len(run) > len(best["dates"])):
            best = {"row": r, "dates": run, "total_col": _find_total_col(row, run)}
    return best


def _is_label(v: Any) -> bool:
    return isinstance(v, str) and bool(_RE_WORD.search(v))


def _row_label(row: tuple, first_period_col: int) -> tuple[str | None, int | None]:
    """This row's label = the RIGHTMOST text cell left of the period axis. Some
    statements keep every label in one column (codes beside them in column A);
    others encode the hierarchy by COLUMN — sections in B, subtotals in C, leaves
    in D — so the most-specific label present is the rightmost one. Numeric columns
    (e.g. interspersed Year-End totals) are skipped because they aren't words."""
    for c in range(min(first_period_col, len(row)) - 1, -1, -1):
        if _is_label(row[c]):
            return row[c], c
    return None, None


# ---------------------------------------------------------------------------
# Line-item selection
# ---------------------------------------------------------------------------

def _magnitude(series: dict[_dt.date, float]) -> float:
    return sum(abs(v) for v in series.values())


def _approx(a: float, b: float, *, rel: float = 0.01, floor: float = 1.0) -> bool:
    return abs(a - b) <= max(floor, rel * max(abs(a), abs(b)))


def _pick(rows: list[dict], pat: _re.Pattern, *,
          exclude: _re.Pattern | None = None) -> dict | None:
    """Best row whose label matches `pat`: the largest-magnitude match (the grand
    total dwarfs its components), with a subtotal label as a weak tiebreak. Magnitude
    must lead — a small "Total Other Income" must not outrank the larger top revenue
    line just because it says "total"."""
    cands = [row for row in rows
             if pat.search(row["norm"]) and not (exclude and exclude.search(row["norm"]))]
    if not cands:
        return None
    return max(cands, key=lambda row: (_magnitude(row["series"]),
                                       1 if "total" in row["norm"] else 0))


def _is_subtotal(norm: str) -> bool:
    return bool(_RE_SUBTOTAL.match(norm))


def _closes(rev: dict, leaf_sum: dict, cand: dict, date_cols: list) -> bool:
    """Does `cand` close the waterfall — value ≈ revenue − Σ(leaf expenses so far),
    across every populated month? This is the math definition of a subtotal like NOI
    or Net Income, independent of what it is named."""
    pops = 0
    for _, d in date_cols:
        rv, cv = rev["series"].get(d), cand["series"].get(d)
        if rv is None or cv is None or (rv == 0 and cv == 0):
            continue
        pops += 1
        if not _approx(rv - leaf_sum[d], cv):
            return False
    return pops >= 1


def _anchor_noi(rows: list[dict], rev: dict, date_cols: list) -> dict | None:
    """Find NOI by MATH, not name: walk down from the revenue total accumulating
    leaf expenses; NOI is the FIRST row that closes revenue − Σleaves = value. Later
    closures are below-the-line subtotals (Net Income, etc.). A label that says NOI
    only breaks ties among closures — so a bespoke or missing NOI label still works."""
    leaf_sum = {d: 0.0 for _, d in date_cols}
    closures: list[dict] = []
    for x in sorted((r for r in rows if r["idx"] > rev["idx"]), key=lambda r: r["idx"]):
        if _closes(rev, leaf_sum, x, date_cols):
            closures.append(x)
        if not _is_subtotal(x["norm"]):
            for d in leaf_sum:
                leaf_sum[d] += x["series"].get(d, 0.0)
    if not closures:
        return None
    labeled = [c for c in closures
               if _RE_NOI.search(c["norm"]) and not _RE_NETINC.search(c["norm"])]
    return labeled[0] if labeled else closures[0]


def _read_statement_sheet(grid: list[tuple], name: str) -> dict | None:
    axis = _find_period_axis(grid)
    if not axis:
        return None
    date_cols = axis["dates"]
    first_col = date_cols[0][0]
    scale = sheet_scale(grid)

    rows: list[dict] = []
    for r, row in enumerate(grid):
        if r == axis["row"]:
            continue
        label, _ = _row_label(row, first_col)
        if label is None:
            continue
        series = {d: _num(row[c]) * scale
                  for c, d in date_cols
                  if c < len(row) and _num(row[c]) is not None}
        if not series:
            continue
        total = (_num(row[axis["total_col"]]) * scale
                 if axis["total_col"] is not None and axis["total_col"] < len(row)
                 and _num(row[axis["total_col"]]) is not None else None)
        m = _RE_CODEPREFIX.match(str(label).strip())
        rows.append({"idx": r, "label": _clean_label(label), "norm": _norm(label),
                     "row": r + 1, "code": m.group(0).strip() if m else None,
                     "series": series, "total": total})

    rev = _pick(rows, _RE_REVENUE)
    ds = _pick(rows, _RE_INTEREST)
    if rev is None:
        return None        # need the top income total to start the waterfall

    # NOI: prefer a labelled line (so a labelled-but-non-footing statement still
    # gets flagged per month), else anchor it by math — the waterfall closure. The
    # math closure also confirms a labelled NOI (it is the identity).
    noi = _pick(rows, _RE_NOI, exclude=_RE_NETINC)
    noi_method = "label"
    if noi is None or noi["idx"] <= rev["idx"]:
        noi = _anchor_noi(rows, rev, date_cols)
        noi_method = "math (waterfall closure)"
    if noi is None:
        return None        # no NOI line and nothing closes the waterfall

    # Opex = Σ leaf expense items strictly between revenue and NOI. Skipping every
    # Total/Net/Gross subtotal means the expense hierarchy can't double-count, and
    # this works whether opex is one line (retail) or split (multifamily). The
    # identity revenue - opex = NOI then grades the whole read.
    region = [x for x in rows if rev["idx"] < x["idx"] < noi["idx"]]
    leaves = [x for x in region if not _is_subtotal(x["norm"])]
    subtotals = [x for x in region if _is_subtotal(x["norm"])]
    opex_series = {d: sum(x["series"].get(d, 0.0) for x in leaves) for _, d in date_cols}

    months = []
    for _, d in date_cols:
        rv, nv, ov = rev["series"].get(d), noi["series"].get(d), opex_series.get(d)
        dv = ds["series"].get(d) if ds else None
        months.append({
            "period": d.isoformat()[:7], "date": d.isoformat(),
            "revenue": rv, "opex": ov, "noi": nv, "debt_service": dv,
            "levered_noi": (nv - dv) if (nv is not None and dv is not None) else None,
        })
    # A month counts only if it carries real activity — an all-zero column is "no
    # data this month" (partial-year actuals), not zero performance.
    populated = [m for m in months
                 if (m["revenue"] not in (None, 0) or m["noi"] not in (None, 0))]
    months = populated or months

    # NOI basis — what is deducted ABOVE the NOI line (mgmt fee / reserves) and
    # whether capex sits below it. This is what the Stage-3 definition match compares.
    above = leaves
    below = [x for x in rows if x["idx"] > noi["idx"]]
    basis = {
        "includes_mgmt_fee": any(_RE_MGMT.search(x["norm"]) for x in above),
        "includes_reserves": any(_RE_RESERVE.search(x["norm"]) for x in above),
        "capex_in_opex": any(_RE_CAPEX.search(x["norm"]) for x in above),
        "capex_below_noi": any(_RE_CAPEX.search(x["norm"]) for x in below),
    }

    # Expense category subtotals = the key drivers for the "what moved" read.
    drivers = sorted(
        ({"label": x["label"], "row": x["row"], "magnitude": _magnitude(x["series"]),
          "series": {d.isoformat()[:7]: v for d, v in x["series"].items()}}
         for x in subtotals if _magnitude(x["series"]) > 0),
        key=lambda d: -d["magnitude"])

    def meta(row: dict | None, **extra) -> dict | None:
        return None if row is None else (
            {"label": row["label"], "row": row["row"], "code": row["code"],
             "total": row["total"]} | extra)

    return {
        "version": ACTUALS_VERSION, "sheet": name, "scale": scale,
        "n_months": len(months), "months": months,
        "lines": {"revenue": meta(rev), "noi": meta(noi, method=noi_method),
                  "debt_service": meta(ds),
                  "opex": {"label": "Σ operating expense line items", "derived": True,
                           "n_leaves": len(leaves)}},
        "expense_drivers": drivers,
        "expense_leaves": [
            {"label": x["label"], "concept": opex_concept(x["label"]),
             "series": {d.isoformat()[:7]: v for d, v in x["series"].items()}}
            for x in leaves],
        "basis": basis,
        "has_debt_service": ds is not None,
    }


# ---------------------------------------------------------------------------
# Stage 2 — self-validation (the statement grades its own parse)
# ---------------------------------------------------------------------------

def validate_actuals(parsed: dict) -> dict:
    """Gate the actuals before any comparison. Identity 1 (revenue - opex = NOI,
    opex independently summed from leaf expenses) is load-bearing and gates. Foot-
    to-total is INFORMATIONAL — a Total column may be TTM/annualized, so a mismatch
    warns but does not block. A real identity failure FLAGS the specific period."""
    checks: list[dict] = []
    failures: list[dict] = []
    warnings: list[dict] = []

    # Identity 1 — revenue - opex = NOI, per (populated) month.
    for m in parsed["months"]:
        r, o, n = m["revenue"], m["opex"], m["noi"]
        if None in (r, o, n):
            checks.append({"identity": "revenue - opex = NOI", "period": m["period"],
                           "passed": None, "detail": "missing a component this month"})
            continue
        ok = _approx(r - o, n)
        checks.append({"identity": "revenue - opex = NOI", "period": m["period"],
                       "passed": ok,
                       "detail": f"{r:,.0f} - {o:,.0f} = {r - o:,.0f} vs NOI {n:,.0f}"})
        if not ok:
            failures.append({"identity": "revenue - opex = NOI", "period": m["period"],
                             "detail": f"revenue - opex ({r - o:,.0f}) does not reconcile "
                                       f"to stated NOI ({n:,.0f})"})

    # Identity 3 — monthly figures foot to a stated Total/YTD column, WHEN it is a
    # sum of the shown months (informational; a TTM/annualized total legitimately
    # differs and must not block trust).
    for key in ("revenue", "noi"):
        line = parsed["lines"].get(key)
        if not line or line.get("total") is None:
            continue
        s = sum(m[key] for m in parsed["months"] if m[key] is not None)
        ok = _approx(s, line["total"], rel=0.01, floor=2.0)
        checks.append({"identity": f"Σ shown {key} = stated total", "period": "—",
                       "passed": ok,
                       "detail": f"Σ {s:,.0f} vs stated {line['total']:,.0f}"})
        if not ok:
            warnings.append({"identity": f"Σ shown {key} = stated total",
                             "detail": f"shown months sum to {s:,.0f} but the stated total "
                                       f"is {line['total']:,.0f} — likely TTM/annualized or "
                                       f"partial; using the shown months"})

    passed = [c for c in checks if c["passed"] is True]
    trusted = not failures and parsed["lines"].get("noi") is not None
    return {"trusted": trusted, "n_checks": len(checks), "n_passed": len(passed),
            "checks": checks, "failures": failures, "warnings": warnings}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_actuals(file_path: str | Path, sheet: str | None = None) -> dict[str, Any]:
    """Read one monthly operating statement → clean monthly series to NOI (+ debt
    service where present), self-validated. Returns:
        {ok, trusted, n_months, months:[{period,date,revenue,opex,noi,
         debt_service?,levered_noi?}], lines:{...}, expense_drivers:[...],
         validation:{...}, ...}
    `ok` = a statement was parsed; `trusted` = its identities foot. Downstream
    comparison must gate on `trusted`, not `ok`.

    A workbook holding SEVERAL property statements (a portfolio) is refused with the
    sheet list — V1 analyses a single asset (per the phase brief). Pass `sheet=` to
    target one property's tab."""
    file_path = Path(file_path)
    try:
        grids = _load_grids(file_path)
    except Exception as e:                       # pragma: no cover - defensive
        return {"ok": False, "trusted": False, "version": ACTUALS_VERSION,
                "reason": f"could not open statement: {e}", "file": file_path.name}
    if sheet is not None:
        grids = {sheet: grids[sheet]} if sheet in grids else {}

    parsed_sheets = [(name, p) for name, grid in grids.items()
                     if (p := _read_statement_sheet(grid, name))]
    if not parsed_sheets:
        return {"ok": False, "trusted": False, "version": ACTUALS_VERSION,
                "file": file_path.name,
                "reason": "no monthly operating statement with revenue + NOI lines found"}
    if len(parsed_sheets) >= 2:
        names = [n for n, _ in parsed_sheets]
        return {"ok": False, "trusted": False, "version": ACTUALS_VERSION,
                "file": file_path.name, "portfolio": True, "sheets": names,
                "reason": (f"this workbook holds {len(names)} separate property "
                           f"statements ({', '.join(names[:6])}"
                           f"{'…' if len(names) > 6 else ''}). V1 analyses a single "
                           f"asset — upload one property's statement, or tell me which "
                           f"sheet to use.")}

    best = parsed_sheets[0][1]
    best["file"] = file_path.name
    best["validation"] = validate_actuals(best)
    best["ok"] = True
    best["trusted"] = best["validation"]["trusted"]
    log.info("ACTUALS %s — sheet=%s, %d month(s), NOI=%s, DS=%s, trusted=%s (%d/%d checks)",
             file_path.name, best["sheet"], best["n_months"],
             (best["lines"]["noi"] or {}).get("label"),
             "yes" if best["has_debt_service"] else "none", best["trusted"],
             best["validation"]["n_passed"], best["validation"]["n_checks"])
    return best


def extract_actuals_files(paths: list[str | Path]) -> dict[str, Any]:
    """Merge several monthly statements (uploaded at once) into one continuous
    series 1..N, ordered by month. Each file self-validates; the merged result is
    trusted only if every part is and the months are contiguous with no overlap."""
    parts = [extract_actuals(p) for p in paths]
    good = [p for p in parts if p.get("ok")]
    if not good:
        return {"ok": False, "trusted": False, "version": ACTUALS_VERSION,
                "reason": "no readable statement among the uploads", "parts": parts}

    by_month: dict[str, dict] = {}
    overlap = False
    for p in good:
        for m in p["months"]:
            if m["period"] in by_month:
                overlap = True
            by_month[m["period"]] = m
    months = [by_month[k] for k in sorted(by_month)]
    trusted = all(p.get("trusted") for p in good) and not overlap
    return {"ok": True, "trusted": trusted, "version": ACTUALS_VERSION,
            "n_months": len(months), "months": months, "lines": good[0]["lines"],
            "expense_drivers": good[0].get("expense_drivers", []),
            "expense_leaves": good[0].get("expense_leaves", []),
            "basis": good[0].get("basis", {}),
            "has_debt_service": any(p.get("has_debt_service") for p in good),
            "validation": {"parts": [p["validation"] for p in good], "overlap": overlap},
            "files": [p.get("file") for p in good]}


def render_actuals_text(a: dict) -> str:
    if not a.get("ok"):
        return f"ACTUALS — not readable: {a.get('reason', 'unknown')}"
    noi_l = (a["lines"]["noi"] or {}).get("label")
    rev_l = (a["lines"]["revenue"] or {}).get("label")
    L = [f"ACTUALS — {a.get('file', a.get('files'))}  ({a['n_months']} month(s), "
         f"sheet '{a.get('sheet', '—')}', scale ×{a.get('scale', 1):g})",
         "trusted: " + ("✓ yes" if a["trusted"] else "✗ NO — see failures"),
         f"revenue line: {rev_l}   NOI line: {noi_l}   "
         f"debt service: {'yes' if a['has_debt_service'] else 'none'}", ""]
    for m in a["months"]:
        L.append(f"  {m['period']}: rev {m['revenue']:,.0f}  opex {m['opex']:,.0f}  "
                 f"NOI {m['noi']:,.0f}"
                 + (f"  DS {m['debt_service']:,.0f}  levNOI {m['levered_noi']:,.0f}"
                    if m['debt_service'] is not None else ""))
    if a.get("expense_drivers"):
        L.append("\nkey expense drivers (largest first):")
        for d in a["expense_drivers"][:8]:
            L.append(f"  {d['label']}: {d['magnitude']:,.0f}")
    v = a["validation"]
    L.append(f"\nself-check: {v['n_passed']}/{v['n_checks']} passed")
    for f in v.get("failures", []):
        L.append(f"  ✗ {f['identity']} [{f['period']}]: {f['detail']}")
    for w in v.get("warnings", []):
        L.append(f"  ⚠ {w['identity']}: {w['detail']}")
    return "\n".join(L)


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        print(render_actuals_text(extract_actuals(arg)))
        print("=" * 78)
