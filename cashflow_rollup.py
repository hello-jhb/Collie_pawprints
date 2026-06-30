"""
cashflow_rollup.py — deterministic full read + annual roll-up of the cash-flow model.

The problem this solves: asking GPT to read NOI off a monthly cash-flow tab fails,
because the tab is rendered TRUNCATED (first ~60 columns, char-capped) and GPT
can't roll a wide time series up in its head — so it fabricates cell refs and the
trust engine omits everything.

The fix (the user's instruction): read the financial-model sheet IN ITS ENTIRETY
— every column — detect the period axis, and roll every line item up to ANNUAL
totals deterministically. The result is a compact annual table that (a) is
grounded in the real cells, (b) loses nothing to truncation, and (c) is small
enough to hand to GPT in full for narration. Crucial info lives in the first few
columns (row labels), so those are always captured.

No GPT, no per-file logic. Reuses the spine's axis detection (dates / years /
text headers, the same code that already validated across the corpus).

Public:
    rollup_sheet(grid, name)         -> annual roll-up + per-period series of one sheet
    rollup_model(file_path, sheets)  -> roll up the cash-flow model sheet(s)
    concept_trajectories(rollup)     -> {concept: {by_year, by_period, going_in, ...}}

Each line item also carries `by_period` — the raw monthly (or whatever-periodicity)
series — so the plan's month-by-month projection can be lined up against actuals
(perf-vs-plan). The annual roll-up is unchanged; the per-period series is additive.
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Any

from cashflow_spine import _load_grids, _date_run, _num, periodicity_of, sheet_scale
from workbook_map import _concept_of

log = logging.getLogger("fb.rollup")
if not log.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("[fb.rollup] %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

ROLLUP_VERSION = "2026-06-15.1"

# Line-item concepts that are FLOWS (roll up by SUM). Everything else (rates,
# ratios, balances, occupancy, ADR) is not summable and is ignored for roll-up.
_FLOW_CONCEPTS = {"noi", "revenue", "opex", "capex", "debt_service",
                  "levered_cf", "unlevered_cf"}

# Words that mark a running balance / waterfall / equity line — NOT an operating
# flow. Such a row can out-mass a real revenue/NOI line and hijack the pick.
_NON_OPERATING_WORDS = ("balance", "cumulative", "running", "waterfall",
                        "distribution", "promote", "contribution", "beginning",
                        "ending", "trap", "reserve account", "irr", "multiple")


def _is_operating_row(label: str) -> bool:
    l = (label or "").lower()
    return not any(w in l for w in _NON_OPERATING_WORDS)


# A true NET operating income line vs. a GROSS one (GOI/GOP sits above opex). When
# both exist, the net line is the real NOI; gross only wins on magnitude otherwise.
_RE_TRUE_NOI = re.compile(r"net operating income|\bnoi\b", re.I)
_RE_GROSS_OI = re.compile(r"gross operating (income|profit)|\bgoi\b|\bgop\b|gross income", re.I)
# Rows that mention "debt service" but are a CASH-FLOW line or an allocation, not the
# debt-service payment itself — must not be picked as debt service for DSCR.
_RE_NOT_DS = re.compile(r"cash flow|paid from|used as|\bbefore\b|\bafter\b|capitalized|"
                        r"coverage|net of|available", re.I)


# --- Occupancy / vacancy: a LEVEL series (a %), never summed -----------------
def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _as_rate_series(item: dict) -> list[tuple[str, float]] | None:
    """A line item's by_period coerced to fractions, or None if it isn't a rate.
    occupancy/vacancy rows arrive $-SCALED like every other row (rollup multiplies
    by sheet_scale); a % must NOT be scaled, so divide it back out, then map
    percent form (92) → fraction (0.92). Rejects $-loss and room-count rows that
    share the 'vacancy'/'occupancy' vocabulary by their out-of-range magnitude."""
    sc = item.get("sheet_scale") or 1.0
    raw = [(d, v / sc) for d, v in item.get("by_period", []) if isinstance(v, (int, float))]
    if len(raw) < 3:
        return None
    med = _median([abs(v) for _, v in raw])
    if med <= 1.5:
        k = 1.0                       # already a fraction
    elif med <= 100.0:
        k = 0.01                      # percent form
    else:
        return None                   # room count / dollars — not a rate
    scaled = [(d, v * k) for d, v in raw]
    # A real rate stays in band every period. A $-loss / SF / count row that
    # shares the vocabulary (e.g. a lease-audit "VACANT" row carrying square feet)
    # has out-of-band spikes — reject if more than 5% of periods fall outside it.
    if sum(1 for _, v in scaled if -0.05 <= v <= 1.2) < 0.95 * len(scaled):
        return None
    return scaled


def occupancy_candidates(rollup: dict) -> list[dict]:
    """Every occupancy/vacancy LEVEL series in the workbook, normalized to an
    OCCUPANCY fraction (vacancy → 1 − vacancy) and averaged per year (a level, so
    mean, never sum). Choosing WHICH series is the deal's occupancy — the one
    aligned to the operating timeline — is the caller's job (it has the hold
    window); this just yields clean, plausibility-gated candidates."""
    out: list[dict] = []
    for it in rollup.get("line_items", []):
        if it.get("concept") not in ("occupancy", "vacancy"):
            continue
        series = _as_rate_series(it)
        if not series:
            continue
        if it["concept"] == "vacancy":
            # A vacancy that reads 0 every period is an empty/status row, not a
            # real vacancy rate — deriving "100% occupied" from it is spurious.
            if _median([v for _, v in series]) < 0.002:
                continue
            series = [(d, 1.0 - v) for d, v in series]
        occ = [v for _, v in series]
        # Plausibility: a real occupancy averages 20–100% and stays within [0,1.1].
        if not (0.20 <= _median(occ) <= 1.05) or sum(1 for v in occ if 0 <= v <= 1.1) < 0.9 * len(occ):
            continue
        by_year_vals: dict[int, list[float]] = {}
        for d, v in series:
            by_year_vals.setdefault(int(str(d)[:4]), []).append(v)
        by_year = {y: round(sum(vs) / len(vs), 4) for y, vs in sorted(by_year_vals.items())}
        lab = it["label"].lower()
        out.append({
            "label": it["label"], "source": f"{it['sheet']}!row{it['row']}",
            "sheet": it["sheet"], "kind": it["concept"],
            "basis": "economic" if "economic" in lab else "physical" if "physical" in lab else "stated",
            "by_period": [(d, round(v, 4)) for d, v in series],
            "by_year": by_year, "n_periods": len(series),
        })
    return out


def _label_col(grid: list[tuple], first_period_col: int) -> int:
    """The column before the period axis with the most text — the line-item
    labels. (A stray label in column A must not hijack a sheet whose labels are
    in B; pick the densest column.)"""
    best_c, best_n = 0, -1
    for c in range(0, max(1, first_period_col)):
        n = sum(1 for row in grid
                if c < len(row) and isinstance(row[c], str) and row[c].strip())
        if n > best_n:
            best_c, best_n = c, n
    return best_c


def rollup_sheet(grid: list[tuple], name: str) -> dict | None:
    """Read a sheet IN FULL, find its period axis, and roll every labelled line
    item up to annual totals. Horizontal axis (periods across columns) — the
    dominant cash-flow layout. Returns None if no period axis is found."""
    # Best horizontal axis = the row with the longest run of dates.
    best = None
    for r, row in enumerate(grid):
        run = _date_run(list(row))
        if run and (best is None or len(run) > len(best[1])):
            best = (r, run)
    if not best:
        return None
    arow, run = best
    cols = [i for i, _ in run]
    dates = [d for _, d in run]
    periodicity = periodicity_of([(d, 0.0) for d in dates])
    lc = _label_col(grid, cols[0])
    sc = sheet_scale(grid)               # the sheet's declared units → full $

    items: list[dict] = []
    for r in range(len(grid)):
        if r == arow:
            continue
        label = grid[r][lc] if lc < len(grid[r]) else None
        if not (isinstance(label, str) and label.strip()):
            continue
        pairs = [(dates[i], _num(grid[r][c]))
                 for i, c in enumerate(cols) if c < len(grid[r])]
        nums = [(d, v * sc) for d, v in pairs if v is not None]
        if len(nums) < 3:
            continue
        by_year: dict[int, float] = {}
        months: dict[int, int] = {}
        for d, v in nums:
            by_year[d.year] = by_year.get(d.year, 0.0) + v
            months[d.year] = months.get(d.year, 0) + 1
        items.append({
            "label": label.strip()[:48], "row": r + 1,
            "concept": _concept_of(label),
            "sheet_scale": sc,   # >1 means the sheet declared units (already full-$)
            "by_year": dict(sorted(by_year.items())),
            "by_period": [(d.isoformat(), v) for d, v in nums],   # scaled per-period series
            "periodicity": periodicity,
            "months_per_year": months,
            "total": sum(v for _, v in nums), "n_periods": len(nums),
        })
    years = sorted({y for it in items for y in it["by_year"]})
    return {"sheet": name, "axis_row": arow + 1, "periodicity": periodicity,
            "years": years, "line_items": items}


def rollup_model(file_path: str | Path, sheets: list[str] | None = None) -> dict:
    """Roll up the cash-flow model sheet(s). `sheets=None` rolls up EVERY sheet
    with a period axis (the operating proforma and the CF tab can be different
    sheets — NOI often lives on the proforma, the streams on the CF tab). Returns
    the per-sheet roll-ups plus a merged line-item list."""
    grids = _load_grids(Path(file_path))
    names = sheets if sheets is not None else list(grids.keys())
    rollups = []
    for s in names:
        if s in grids:
            ru = rollup_sheet(grids[s], s)
            if ru and ru["line_items"]:
                rollups.append(ru)

    # NB: line items are reported in each sheet's NATIVE units. A sheet can be in
    # $ and another in $000s; resolving absolute scale requires the deal anchor
    # (NOI/exit_cap ≈ sale, debt+equity=cost), which the caller (deal_truth) has —
    # cross-sheet peak-ratio guessing mis-scales unlike sheets and is avoided here.
    line_items = [it | {"sheet": ru["sheet"]} for ru in rollups for it in ru["line_items"]]
    return {"version": ROLLUP_VERSION, "sheets": [r["sheet"] for r in rollups],
            "rollups": rollups, "line_items": line_items}


def _full_year_value(item: dict, pick: str) -> float | None:
    """A headline value from a line item's annual roll-up, using only FULL years
    (a partial first/last calendar year understates a flow)."""
    by_year = item["by_year"]
    mpy = item.get("months_per_year", {})
    mx = max(mpy.values(), default=1)
    full = [(y, v) for y, v in by_year.items() if mpy.get(y, 1) >= max(1, mx - 2)]
    pos = [(y, v) for y, v in full if abs(v) > 1] or [(y, v) for y, v in by_year.items() if abs(v) > 1]
    if not pos:
        return None
    if pick == "going_in":
        # Year-1 NOI = the first COMPLETE calendar year. A partial first year (a
        # mid-year acquisition's stub — e.g. a Feb close gives 11 months) under-
        # states the going-in run-rate, so skip it and use the first full year.
        complete = [(y, v) for y, v in pos if mpy.get(y, mx) >= mx]
        cand = complete or pos
        return next((v for _, v in cand if v > 0), cand[0][1])
    if pick == "exit":
        return pos[-1][1]
    if pick == "stabilized":
        return max((v for _, v in pos), key=abs)
    return None


def _traj(it: dict) -> dict:
    return {
        "label": it["label"], "source": f"{it['sheet']}!row{it['row']}",
        "sheet_scale": it.get("sheet_scale", 1.0),
        "by_year": it["by_year"],
        "by_period": it.get("by_period", []),
        "periodicity": it.get("periodicity"),
        "going_in": _full_year_value(it, "going_in"),
        "stabilized": _full_year_value(it, "stabilized"),
        "exit": _full_year_value(it, "exit"),
    }


def concept_trajectories(rollup: dict) -> dict[str, dict]:
    """Per FLOW concept, the single best consolidated line item (most periods,
    then largest magnitude — a total dominates its components, avoiding the
    double-count of summing component + total rows). The operating statement
    (revenue / opex / NOI / capex / debt service) is anchored to the SHEET where
    NOI was found, so the lines are mutually consistent (same context + units);
    the cash-flow streams are taken wherever they live."""
    def pick(concept, sheet=None):
        cands = [it for it in rollup["line_items"]
                 if it.get("concept") == concept and _is_operating_row(it["label"])
                 and (sheet is None or it["sheet"] == sheet)]
        if concept == "debt_service":
            # "Cash Flow Before/After Debt Service", "...Paid from NOI", "Capitalized
            # Interest" all carry the words "debt service" but are NOT the payment —
            # they'd give a nonsense DSCR. Keep only the actual service line.
            cands = [it for it in cands if not _RE_NOT_DS.search(it["label"])]
        if concept == "noi":
            # A "gross operating income/profit" row out-masses the real NOI (it sits
            # above opex), so largest-magnitude can grab GROSS instead of NET. Prefer
            # a TRUE NOI-labelled row — but only when one exists: some models expose
            # only GOP as their bottom line (1425), and those must keep it.
            true_noi = [it for it in cands if _RE_TRUE_NOI.search(it["label"])
                        and not _RE_GROSS_OI.search(it["label"])]
            if true_noi:
                cands = true_noi
        return max(cands, key=lambda it: (it["n_periods"], abs(it["total"]))) if cands else None

    out: dict[str, dict] = {}
    noi = pick("noi")
    anchor = noi["sheet"] if noi else None
    if noi:
        out["noi"] = _traj(noi)
    for concept in ("revenue", "opex", "capex", "debt_service"):
        it = pick(concept, anchor) or pick(concept)   # prefer NOI's sheet
        if it:
            out[concept] = _traj(it)
    for concept in ("unlevered_cf", "levered_cf"):
        it = pick(concept)
        if it:
            out[concept] = _traj(it)
    return out


# A subtotal / structural row — NOT a leaf component (skip to avoid double-count).
_RE_SUBTOTAL = re.compile(r"\btotal\b|net operating|\bnoi\b|effective gross|"
                          r"potential gross|scheduled|gross revenue|gross income", re.I)


def _row_of(traj_entry: dict) -> tuple[str | None, int | None]:
    src = str((traj_entry or {}).get("source", ""))
    if "!row" not in src:
        return None, None
    sheet, _, r = src.partition("!row")
    try:
        return sheet, int(r)
    except ValueError:
        return sheet, None


def concept_components(rollup: dict, traj: dict) -> dict[str, dict]:
    """Tier-2 breakdown: the COMPONENT leaf lines that make up revenue / opex, read
    POSITIONALLY off the anchor sheet (only the totals are concept-tagged; the leaves
    are not). Opex leaves sit between the revenue total and the opex total; revenue
    leaves sit above the revenue total. Subtotal rows are skipped to avoid double-
    counting, and a deterministic FOOT-CHECK (Sigma leaves ~ validated total) marks
    whether the breakdown is trustworthy — GPT narrates only what foots.

    Scaled to the reconciled trajectory's full-$ units (via the picked line's raw
    stabilized). `traj` is the reconciled (full-$) trajectory."""
    items = rollup.get("line_items", [])
    by_key = {(it["sheet"], it["row"]): it for it in items}
    out: dict[str, dict] = {}
    rev_sheet, rev_row = _row_of(traj.get("revenue"))
    opex_sheet, opex_row = _row_of(traj.get("opex"))

    def build(concept, sheet, total_row, lo, hi):
        picked = by_key.get((sheet, total_row))
        if not picked:
            return None
        raw_total = _full_year_value(picked, "stabilized")
        scaled_total = traj[concept]["stabilized"]
        if not raw_total or not scaled_total:
            return None
        scale = scaled_total / raw_total
        comps = []
        for it in items:
            if it["sheet"] != sheet or not (lo < it["row"] < hi):
                continue
            if not _is_operating_row(it["label"]) or _RE_SUBTOTAL.search(it["label"]):
                continue
            stab = _full_year_value(it, "stabilized")
            if stab is None or abs(stab) < 1:
                continue
            gi = _full_year_value(it, "going_in")
            comps.append({
                "label": it["label"], "source": f"{sheet}!row{it['row']}",
                "stabilized": stab * scale,
                "going_in": (gi * scale) if gi is not None else None,
                "by_year": {y: v * scale for y, v in it["by_year"].items()},
                "share": stab / raw_total,
            })
        comps.sort(key=lambda c: -abs(c["stabilized"]))
        foot = sum(c["stabilized"] for c in comps)
        footed = abs(foot - scaled_total) / abs(scaled_total) <= 0.03
        return {"total": scaled_total, "components": comps,
                "footed": footed, "foot_sum": foot, "n": len(comps)}

    # OPEX: leaves between the revenue total and the opex total (clean, flat).
    if opex_sheet and opex_row:
        lo = rev_row if (rev_sheet == opex_sheet and rev_row and rev_row < opex_row) else 0
        r = build("opex", opex_sheet, opex_row, lo, opex_row)
        if r:
            out["opex"] = r
    # REVENUE: leaves above the revenue total (hierarchical — may not foot; the
    # foot-check flags it and the caller simply won't assert an unfooted breakdown).
    if rev_sheet and rev_row:
        r = build("revenue", rev_sheet, rev_row, 0, rev_row)
        if r:
            out["revenue"] = r
    return out


# ---------------------------------------------------------------------------
# Corpus harness — roll up each model's engine and show the NOI/rev/opex trend
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from cashflow_spine import find_spine
    args = sys.argv[1:]
    targets: list[Path] = []
    for a in args:
        p = Path(a)
        if p.is_dir():
            targets += sorted(p.glob("*.xlsx"))
        elif p.exists():
            targets.append(p)
    targets = [t for t in targets if not t.name.startswith("~$")]
    if not targets:
        print("usage: python cashflow_rollup.py <dir-or-files>")
        raise SystemExit(1)

    def fmt(v):
        if v is None:
            return "—"
        a = abs(v)
        return f"{v/1e6:.1f}M" if a >= 1e6 else f"{v/1e3:.0f}K" if a >= 1e3 else f"{v:.0f}"

    for t in targets:
        sp = find_spine(t)
        anchors = sp.diagnostics.get("anchor_sheets") or []
        print("\n" + "=" * 90)
        print(f"{t.name}   engine anchors: {anchors or '—'}")
        ru = rollup_model(t)                      # roll up EVERY period-axis sheet
        traj = concept_trajectories(ru)
        if not traj:
            print(f"  rolled up {len(ru['line_items'])} line items; no flow concept matched")
        for concept in ("revenue", "opex", "noi", "capex", "debt_service",
                        "unlevered_cf", "levered_cf"):
            tr = traj.get(concept)
            if not tr:
                continue
            print(f"  {concept:<13} {fmt(tr['going_in']):>8} → {fmt(tr['exit']):>8}  "
                  f"({fmt(tr['stabilized'])} stab)  '{tr['label'][:26]}'  {tr['source']}")
