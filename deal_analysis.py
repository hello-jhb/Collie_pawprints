"""
deal_analysis.py — ONE integrated, grounded analysis (replaces the four
separate GPT deep-dives).

Capital Structure · Return Profile · Cash Flow / NOI · CapEx — all read from the
validated spine (deal_truth) and the deterministic full-read roll-up
(cashflow_rollup). No GPT extraction, so it ALWAYS loads, and every number is
grounded in the cash-flow model rather than guessed off a truncated sheet.

Units: figures are normalized to full dollars — the spine and roll-up read each
sheet's DECLARED units ("$ in 000s" etc.), and the operating statement is
reconciled to the deal (stabilized NOI / exit_cap ≈ sale) for the few sheets
that don't declare units.

Public:
    build_analysis(file_path) -> {"ok", "md", "sections", "dt"}
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# Split floating-rate evidence into the index it floats over vs. its rate cap(s),
# so the structure (index + spread + cap) is shown, not a bare misleading "rate".
_RE_INDEX = re.compile(r"sofr|libor|euribor|bsby|forward curve", re.I)
_RE_CAP = re.compile(r"\bcap\b|cap strike", re.I)


def _money(v) -> str:
    if not isinstance(v, (int, float)):
        return "—"
    a = abs(v)
    if a >= 1e9:
        return f"${v/1e9:.2f}B"
    if a >= 1e6:
        return f"${v/1e6:.1f}M"
    if a >= 1e3:
        return f"${v/1e3:.0f}K"
    return f"${v:,.0f}"


def _pct(v) -> str:
    """Format a canonical rate that may be stored as a fraction (0.045) OR already
    as a percent number (4.5). The >1.5 split disambiguates those two forms."""
    if not isinstance(v, (int, float)):
        return "—"
    return f"{v*100:.2f}%" if abs(v) <= 1.5 else f"{v:.2f}%"


def _pctf(v) -> str:
    """Format a value that is ALWAYS a fraction — a computed ratio (growth, margin)
    that can legitimately exceed 1.5 (a 235% growth, a 370% margin). Always ×100,
    so it never gets mistaken for an already-percent number the way _pct would."""
    if not isinstance(v, (int, float)):
        return "—"
    return f"{v*100:.1f}%"


def _x(v) -> str:
    return f"{v:.2f}x" if isinstance(v, (int, float)) else "—"


def _val(can: dict, c: str):
    return float(can[c]["value"]) if c in can else None


def _nearest_pow10(x: float) -> float:
    import math
    return float(10 ** round(math.log10(x))) if x > 0 else 1.0


def _declared_units(t: dict) -> bool:
    """True if the concept's source sheet declared its own units (sheet_scale != 1),
    so it is already in full dollars and must NOT receive the deal anchor scale."""
    return abs(float(t.get("sheet_scale", 1.0)) - 1.0) > 1e-9


def _reconcile_operating_units(traj: dict, can: dict) -> dict:
    """Most sheets declare their units (handled upstream); a few don't. Anchor the
    operating statement to the (full-$) deal: scale so stabilized NOI / exit_cap ≈
    sale price. Reliable now that sale/cap are themselves in full dollars.

    The anchor corrects sheets that DON'T declare units. A concept whose sheet DID
    declare units is already full-$ — re-applying the anchor double-scales it (BAC:
    capex on `Model`, declared $000s, was pushed to billions by the NOI ×1000
    anchor). So gate on declared-units, NOT on which sheet: St Regis revenue/opex
    live on a different *undeclared* sheet than NOI and still need the anchor."""
    noi = traj.get("noi")
    if not (noi and isinstance(noi.get("stabilized"), (int, float))):
        return traj
    ec, sp = _val(can, "exit_cap"), _val(can, "sale_price")
    if not (ec and sp):
        return traj
    cf = ec / 100.0 if ec > 1.5 else ec
    raw = abs(noi["stabilized"])
    if cf <= 0 or raw <= 0:
        return traj
    scale = _nearest_pow10(sp * cf / raw)
    if scale in (1.0,) or scale not in (1e-3, 1e3, 1e6):
        return traj
    out = dict(traj)
    for c in ("noi", "revenue", "opex", "capex", "debt_service"):
        if c in out:
            t = out[c]
            if _declared_units(t):
                continue   # sheet declared its units: already full-$
            t = dict(t)
            for k in ("going_in", "stabilized", "exit"):
                if isinstance(t.get(k), (int, float)):
                    t[k] = t[k] * scale
            t["by_year"] = {y: v * scale for y, v in t["by_year"].items()}
            # by_period must scale too — it feeds the exit-NOI window and any
            # monthly read; leaving it raw makes it inconsistent with the headline.
            t["by_period"] = [(d, v * scale) for d, v in t.get("by_period", [])]
            out[c] = t
    return out


def _src(can: dict, c: str) -> str:
    return f"`{can[c]['source']}`" if c in can and can[c].get("source") else ""


def _line(label: str, value: str, src: str = "", flag: str = "") -> str:
    return f"- **{label}:** {value} {src}{flag}".rstrip()


def _forward_year_noi(by_period: list, months_offset) -> float | None:
    """Sum one forward year of a dated monthly series, starting `months_offset`
    months after the first period — date-windowed, so it is periodicity-agnostic.
    Used for EXIT NOI: the 12 months forward of the SALE month, what the exit price
    is struck on — NOT the last proforma year. Returns None when the forward year
    isn't fully covered by the data (so the caller keeps its default)."""
    from datetime import date
    if not by_period or not isinstance(months_offset, (int, float)):
        return None
    try:
        items = [(date.fromisoformat(str(d)[:10]), v) for d, v in by_period]
    except Exception:
        return None
    start, last = items[0][0], items[-1][0]
    off = int(months_offset)
    y = start.year + (start.month - 1 + off) // 12
    mo = (start.month - 1 + off) % 12 + 1
    w0, w1 = date(y, mo, 1), date(y + 1, mo, 1)
    window = [v for d, v in items if w0 <= d < w1]
    nonzero = [v for v in window if abs(v) > 1]
    # Need a real forward operating year: full coverage, mostly non-zero, positive
    # total — otherwise the window fell in a zero/empty region (the caller keeps
    # its default rather than reporting a spurious ~0 exit NOI).
    if last < w1 or len(nonzero) < 6 or sum(window) <= 0:
        return None
    return sum(window)


def _forward_year_avg(by_period: list, months_offset) -> float | None:
    """Mean of one forward year of a dated RATE series (occupancy) — averaged, not
    summed. None if the window isn't fully covered. Mirrors _forward_year_noi's
    date-windowing so occupancy bookends anchor to the close the same way NOI does."""
    from datetime import date
    if not by_period or not isinstance(months_offset, (int, float)):
        return None
    try:
        items = [(date.fromisoformat(str(d)[:10]), v) for d, v in by_period]
    except Exception:
        return None
    start, last = items[0][0], items[-1][0]
    off = int(months_offset)
    y = start.year + (start.month - 1 + off) // 12
    mo = (start.month - 1 + off) % 12 + 1
    w0, w1 = date(y, mo, 1), date(y + 1, mo, 1)
    window = [v for d, v in items if w0 <= d < w1]
    if last < w1 or len(window) < 6:
        return None
    return sum(window) / len(window)


def _select_occupancy(cands: list[dict], noi_t: dict | None) -> dict | None:
    """The deal's occupancy series among all candidates: prefer an occupancy row on
    the SAME sheet as NOI (tied to the operating cash flow), else the one whose
    timeline overlaps NOI's most. Tie-breaks: economic basis, then most periods."""
    from datetime import date
    if not cands:
        return None
    occ = [c for c in cands if c["kind"] == "occupancy"] or cands
    noi_sheet = ((noi_t or {}).get("source") or "").split("!")[0]
    nd = []
    if noi_t and noi_t.get("by_period"):
        try:
            nd = [date.fromisoformat(str(d)[:10]) for d, _ in noi_t["by_period"]]
        except Exception:
            nd = []
    nlo, nhi = (min(nd), max(nd)) if nd else (None, None)

    def overlap(c):
        if nlo is None:
            return c["n_periods"]
        try:
            cd = [date.fromisoformat(str(d)[:10]) for d, _ in c["by_period"]]
        except Exception:
            return 0
        return sum(1 for d in cd if nlo <= d <= nhi)

    return max(occ, key=lambda c: (c["sheet"] == noi_sheet, overlap(c),
                                   c["basis"] == "economic", c["n_periods"]))


def _occupancy_bookends(series: dict) -> dict:
    """Going-in (12-mo forward of the series start ≈ the close) + stabilized (the
    plateau: average occupancy from the first year it reaches ≥95% of its peak)."""
    gi = _forward_year_avg(series.get("by_period"), 0)
    out = {"going_in": round(gi, 4) if gi is not None else None,
           "stabilized": None, "by_year": series.get("by_year"),
           "source": series.get("source"), "kind": series.get("kind"),
           "basis": series.get("basis"), "label": series.get("label")}
    by = series.get("by_year") or {}
    if by:
        mx = max(by.values())
        plateau = [y for y in sorted(by) if by[y] >= 0.95 * mx]
        if plateau:
            vals = [by[y] for y in sorted(by) if y >= plateau[0]]
            out["stabilized"] = round(sum(vals) / len(vals), 4)
    return out


def _dscr_health(noi_t: dict | None, ds_t: dict | None) -> dict | None:
    """Trailing-12-month DSCR (NOI ÷ debt service) + a post-stabilization coverage
    flag. None when the model carries no debt-service flow (UI says "not available
    from model" — recompute when actuals land). TTM smooths seasonal/hotel NOI and
    extends cleanly to monthly actuals. Stabilization = first month TTM NOI reaches
    ≥95% of stabilized NOI (Phase-0 rule); a breach is DSCR < 1.2 for >3 consecutive
    months AFTER stabilization — a pre-stabilization dip is lease-up, expected."""
    import statistics
    if not noi_t or not ds_t:
        return None
    noi_m = {str(d)[:7]: v for d, v in (noi_t.get("by_period") or [])}
    ds_m = {str(d)[:7]: abs(v) for d, v in (ds_t.get("by_period") or [])}
    months = sorted(set(noi_m) & set(ds_m))
    stab = noi_t.get("stabilized")
    pos_ds = [ds_m[m] for m in months if ds_m[m] > 0]
    if len(months) < 12 or not stab or not pos_ds:
        return None
    med_ds = statistics.median(pos_ds)

    series: list[tuple[str, float, float]] = []      # (month, dscr_ttm, ttm_noi)
    for i in range(11, len(months)):
        win = months[i - 11:i + 1]
        if any(ds_m[w] < 0.2 * med_ds for w in win):  # window spans a debt payoff — skip
            continue
        tn, td = sum(noi_m[w] for w in win), sum(ds_m[w] for w in win)
        if td > 0:
            series.append((months[i], tn / td, tn))
    if not series:
        return None
    # Units sanity: NOI is full-$, debt service is native — a constant off-by-1000
    # shows as an off-scale DSCR; rescale the whole series into range (no per-file tuning).
    med = statistics.median([d for _, d, _ in series])
    k = 1.0
    while med * k < 0.3:
        k *= 1000.0
    while med * k > 30:
        k /= 1000.0
    series = [(m, d * k, tn) for m, d, tn in series]

    stab_m = next((m for m, _, tn in series if tn >= 0.95 * stab), None)
    post = [(m, d) for m, d, _ in series if stab_m and m >= stab_m]
    longest = run = 0
    start = run_start = None
    for m, d in post:
        if d < 1.2:
            run += 1
            run_start = run_start or m
            if run > longest:
                longest, start = run, run_start
        else:
            run, run_start = 0, None
    lo = min(series, key=lambda x: x[1])
    breach = longest > 3
    return {
        "available": True,
        "source": f"computed: trailing-12 NOI ÷ {ds_t.get('source')}",
        "stabilization_month": stab_m,
        "min_dscr_overall": round(lo[1], 2), "min_dscr_month": lo[0],
        "min_dscr_post_stab": round(min(d for _, d in post), 2) if post else None,
        "n_post_stab_months": len(post),
        "breach": breach, "breach_run_months": longest if breach else 0,
        "breach_start": start if breach else None,
        "healthy": not breach,
    }


def build_analysis(file_path: str | Path, dt: dict | None = None) -> dict[str, Any]:
    if dt is None:
        from deal_truth import build_deal_truth
        dt = build_deal_truth(file_path)
    if not dt.get("engine_found", True):
        md = ("### Deal Analysis\n\n> ⚠ **Cash-flow engine not found.** No stream "
              "reproduced the model's stated IRR, so the deal was not reconstructed. "
              + (dt.get("reason") or ""))
        return {"ok": False, "md": md, "sections": {}, "dt": dt}

    can = dt.get("canonical", {})
    sections: dict[str, str] = {}

    # The reconciled, full-$ operating roll-up — computed once and reused for the
    # NOI trajectory, the CapEx line, AND the going-in cap (which must divide a
    # going-in NOI that is in the SAME full-dollar units as total cost).
    try:
        from cashflow_rollup import rollup_model, concept_trajectories, concept_components
        _ru = rollup_model(file_path)
        _raw_ct = concept_trajectories(_ru)
        traj = _reconcile_operating_units(_raw_ct, can)
    except Exception:
        traj, _ru, _raw_ct = {}, None, {}

    # Both NOI bookends are date-windowed off the cash flow, not read from a
    # calendar-year column:
    #   exit NOI    = the 12 months FORWARD of the SALE month (what the exit price
    #                 is struck on), not the last proforma year — matters on short
    #                 holds that sell mid-proforma (Westview sells month 35 of 131).
    #   going-in NOI = the 12 months forward of the CLOSE (offset 0). The first full
    #                 CALENDAR year overstates going-in when the NOI row carries
    #                 pre-close trailing actuals (BAC closes 2018-03 yet the row
    #                 shows 2018-2020 history, so the "first full year" 2019 skips
    #                 the lease-up ramp and lands on an already-stabilized number).
    # _forward_year_noi self-gates: it returns None on an uncovered or non-positive
    # window (a deal that opens pre-revenue in lease-up), so we keep the first-full-
    # calendar-year default in that case rather than reporting a spurious ~0.
    noi_t = traj.get("noi")
    if noi_t:
        bp = noi_t.get("by_period")
        hold_mo = (dt.get("hold") or {}).get("months")
        patch: dict[str, float] = {}
        gi = _forward_year_noi(bp, 0)
        if isinstance(gi, (int, float)):
            patch["going_in"] = gi
        if hold_mo:
            ex = _forward_year_noi(bp, hold_mo)
            if isinstance(ex, (int, float)):
                patch["exit"] = ex
        if patch:
            traj = dict(traj)
            traj["noi"] = {**noi_t, **patch}

    # Occupancy/vacancy — a LEVEL series the flow roll-up drops. Pick the series
    # aligned to the operating timeline and anchor going-in/stabilized to the close.
    try:
        from cashflow_rollup import occupancy_candidates
        occ_sel = _select_occupancy(occupancy_candidates(_ru), traj.get("noi")) if _ru else None
    except Exception:
        occ_sel = None
    if occ_sel:
        traj = dict(traj)
        traj["occupancy"] = _occupancy_bookends(occ_sel)

    # DSCR Health — trailing-12 NOI ÷ debt service, flagged for sustained post-
    # stabilization coverage stress. Uses the RAW (un-reconciled) debt-service flow,
    # which the operating reconcile drops; None when the model has no such flow.
    dscr_health = _dscr_health(traj.get("noi"), (_raw_ct or {}).get("debt_service"))

    # Tier-2 operating components (foot-validated) for the interpretation layer.
    try:
        from cashflow_rollup import concept_components
        components = concept_components(_ru, traj) if _ru else {}
    except Exception:
        components = {}

    # --- Capital Structure ------------------------------------------------
    cs = ["#### Capital Structure"]
    for c, lab in (("total_cost", "Total cost"), ("purchase_price", "Acquisition cost"),
                   ("debt", "Debt"), ("equity", "Equity")):
        if c in can:
            cf = " ✅" if can[c].get("cf_validated") else ""
            cs.append(_line(lab, _money(_val(can, c)), _src(can, c), cf))
    rt = dt.get("rate_type") or {}
    is_floating = rt.get("type") == "floating"
    # On floating debt the stored "interest rate" is the SPREAD/margin over the
    # index — there is no single all-in rate (it = index path + spread). Label it
    # as such rather than passing the spread off as the interest rate.
    struct = rt.get("structure") or {}
    sp = struct.get("spread")
    for c, lab in (("ltv", "LTV"), ("ltc", "LTC")):
        if c in can:
            cs.append(_line(lab, _pct(_val(can, c)), _src(can, c)))
    # Rate / spread: on floating debt prefer the spread the model CARRIES (a per-
    # period spread row) over the input-cell scan, which often misfires (0.00%).
    if is_floating and sp:
        cs.append(_line("Spread (over index)", _pct(sp["value"]), f"`{sp['source']}`"))
    elif "interest_rate" in can:
        cs.append(_line("Spread (over index)" if is_floating else "Interest rate",
                        _pct(_val(can, "interest_rate")), _src(can, "interest_rate")))
    if is_floating and struct.get("floor"):     # show the floor alongside the spread
        fl = struct["floor"]
        cs.append(_line("Rate floor", _pct(fl["value"]), f"`{fl['source']}`"))
    for c, lab in (("dscr", "DSCR"), ("debt_yield", "Debt yield")):
        if c in can:
            v = _val(can, c)
            cs.append(_line(lab, _x(v) if c == "dscr" else _pct(v), _src(can, c)))
    if rt.get("type") in ("floating", "fixed"):
        ev = rt.get("evidence", [])
        if is_floating:
            idx = [e for e in ev if _RE_INDEX.search(e)]
            caps = [e for e in ev if _RE_CAP.search(e)]
            bits = []
            if idx:
                bits.append("index-linked (" + "; ".join(idx[:2]) + ")")
            if caps:
                bits.append(f"{len(caps)} rate cap{'s' if len(caps) > 1 else ''} "
                            + "(" + "; ".join(caps[:2]) + ")")
            detail = " · ".join(bits) or "; ".join(ev[:2])
            cs.append(_line("Rate type", "Floating", detail, " ⚠ exposed to rate moves"))
        else:
            cs.append(_line("Rate type", "Fixed", "; ".join(ev[:2])))
    sections["capital_structure"] = "\n".join(cs)

    # --- Return Profile ---------------------------------------------------
    rp = ["#### Return Profile"]
    for c, lab in (("levered_irr", "Levered IRR"), ("unlevered_irr", "Unlevered IRR")):
        if c in can:
            rp.append(_line(lab, _pct(_val(can, c)), _src(can, c), " ✓ validated"))
    for c, lab in (("equity_multiple", "Levered equity multiple"),
                   ("unlevered_equity_multiple", "Unlevered equity multiple")):
        if c in can:
            rp.append(_line(lab, _x(_val(can, c)), _src(can, c)))
    h = dt.get("hold")
    if h and h.get("months"):
        early = (f" — _sells at month {h['months']} of a {h['model_months']}-month model_"
                 if h.get("sells_before_model_end") else "")
        rp.append(_line("Hold period", f"{h['months']} mo ({h['years']:g} yr)",
                        f"`{h.get('source','')}`", early))
    for c, lab in (("sale_price", "Sale price"), ("exit_cap", "Exit cap"),
                   ("yield_on_cost", "Yield on cost")):
        if c in can:
            v = _val(can, c)
            disp = _pct(v) if c == "exit_cap" or c == "yield_on_cost" else _money(v)
            rp.append(_line(lab, disp, _src(can, c)))
    # Going-in cap = going-in (year-1) NOI / total cost. Derive it here from the
    # reconciled roll-up: the going-in NOI must be in the same full-$ units as the
    # cost, and it must be the GOING-IN year — not stabilized. (deal_truth derives
    # it off an unreconciled NOI whose units/year can be wrong, e.g. Westview's
    # $000s NOI ÷ full-$ cost gave 0.01%.)
    gi_noi = (traj.get("noi") or {}).get("going_in")
    cost = _val(can, "total_cost") or _val(can, "purchase_price")
    if isinstance(gi_noi, (int, float)) and cost:
        rp.append(_line("Going-in cap", _pct(gi_noi / cost),
                        "`derived: going-in NOI / total cost`"))
    elif "going_in_cap" in can:
        rp.append(_line("Going-in cap", _pct(_val(can, "going_in_cap")), _src(can, "going_in_cap")))
    sections["return_profile"] = "\n".join(rp)

    # --- Cash Flow / NOI (grounded roll-up) -------------------------------
    cf = ["#### Cash Flow / NOI Trajectory"]
    noi = traj.get("noi")
    if noi:
        gi, st, ex = noi.get("going_in"), noi.get("stabilized"), noi.get("exit")
        cf.append(_line("NOI", f"{_money(gi)} going-in → {_money(st)} stabilized → {_money(ex)} exit",
                        f"`{noi['source']}`"))
        if isinstance(gi, (int, float)) and isinstance(st, (int, float)) and gi:
            cf.append(_line("NOI growth (going-in → stabilized)", _pctf(st/gi - 1)))
        # Revenue must be >= NOI (NOI = revenue - opex, opex >= 0). When the picked
        # revenue row is below NOI it is the wrong row / wrong scope (e.g. St Regis,
        # whose consolidated hotel revenue isn't captured as one row) — show neither
        # the revenue nor a margin derived from it, rather than a >100% margin.
        rev = traj.get("revenue")
        rev_st = rev.get("stabilized") if rev else None
        rev_num = isinstance(rev_st, (int, float)) and isinstance(st, (int, float)) and st > 0
        rev_ok = rev_num and rev_st >= st
        rev_below = rev_num and rev_st < st          # impossible: revenue < NOI
        if rev_ok:
            cf.append(_line("NOI margin (stabilized)", _pctf(st / rev_st)))
        for c, lab in (("revenue", "Revenue"), ("opex", "Operating expenses")):
            if c == "revenue":
                if rev_below:
                    cf.append("- _Revenue not shown — the model's revenue rows don't "
                              "reconcile to NOI (revenue reads below NOI)._")
                if not rev_ok:               # below NOI, or unparseable — skip the line
                    continue
            tr = traj.get(c)
            if tr:
                cf.append(_line(lab, f"{_money(tr.get('going_in'))} → {_money(tr.get('stabilized'))}",
                                f"`{tr['source']}`"))
    else:
        cf.append("_No operating line items found in the model's cash-flow sheets._")
    sections["cash_flow"] = "\n".join(cf)

    # --- CapEx ------------------------------------------------------------
    cp = ["#### CapEx"]
    cx = traj.get("capex")
    if cx:
        cp.append(_line("CapEx / reserves",
                        f"{_money(cx.get('going_in'))} going-in → {_money(cx.get('stabilized'))} stabilized",
                        f"`{cx['source']}`"))
    else:
        cp.append("_No CapEx / reserve line identified._")
    sections["capex"] = "\n".join(cp)

    # --- Summary cross-check (engine vs the model's headline) -------------
    sc_rows = dt.get("summary_check", [])
    if sc_rows:
        sx = ["#### Summary Cross-Check — engine vs the model's headline"]
        for r in sc_rows:
            ev = _pct(r["engine"]) if r["kind"] == "rate" else _money(r["engine"])
            sv = _pct(r["summary"]) if r["kind"] == "rate" else _money(r["summary"])
            mark = "✓" if r["match"] else "✗ **mismatch — engine wins**"
            sx.append(f"- **{r['label']}:** engine {ev} vs summary {sv} {mark} "
                      f"`{r.get('source','')}`")
        sections["summary_check"] = "\n".join(sx)

    order = ["capital_structure", "return_profile", "cash_flow", "capex", "summary_check"]
    md = "### Deal Analysis — grounded in the cash-flow model\n\n" + \
        "\n\n".join(sections[k] for k in order if k in sections)
    return {"ok": True, "md": md, "sections": sections, "dt": dt,
            "traj": traj, "components": components, "dscr_health": dscr_health}


if __name__ == "__main__":
    import sys
    for a in sys.argv[1:]:
        r = build_analysis(a)
        print("\n" + ("=" * 80) + f"\n{Path(a).name}  ok={r['ok']}\n" + ("=" * 80))
        print(r["md"])
