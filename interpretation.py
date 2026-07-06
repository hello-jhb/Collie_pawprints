"""
interpretation.py — the Investment Read (interpretation layer).

GPT narrates investment judgment from a structured FACT SHEET it cannot contradict;
it never computes numbers or reads raw cells. Three trust tiers feed it:
  T1 validated   — spine / deal_truth / deal_analysis / perf-vs-plan (bulletproof)
  T2 components  — the roll-up's foot-validated revenue/opex leaves (high)
  T3 labeled     — raw-cell reads, flagged low-confidence (off the roll-up)

This file (Phase 1+2) is the DETERMINISTIC assembler: it builds the fact sheet,
classifies the deal archetype, and detects the read mode. The GPT call (Phase 5)
consumes `assemble_fact_sheet(...)` — it is not wired here yet.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger("fb.interpretation")
if not log.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("[fb.interpretation] %(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)

FACT_SHEET_VERSION = "2026-06-28.1"


# ---------------------------------------------------------------------------
# Archetype — deterministic signals -> provisional label + behaviour lens.
# (GPT later confirms at the fuzzy boundary; the lens anchors the judgment.)
# ---------------------------------------------------------------------------
_ARCHETYPE_LENS = {
    "opportunistic / development": (
        "Returns are deeply back-loaded; early NOI is expected near zero (build + "
        "lease-up). An early NOI miss is meaningless unless lease-up pace or delivery "
        "timeline slips — watch absorption, not in-place NOI."),
    "value-add": (
        "The value engine is the rent jump from repositioning. An NOI miss matters if "
        "rents/occupancy aren't responding to capex (thesis failing); it's tolerable if "
        "it's timing. Revenue ahead = thesis landing; a cost overrun is execution risk."),
    "core-plus": (
        "Mostly stabilized with a thin upside lever (inflation-plus rent growth). A miss "
        "eats the modest premium quickly — less cushion than value-add."),
    "core": (
        "Flat NOI, no ramp, no cushion — in-place yield is the thesis. Any meaningful NOI "
        "miss is direct and concerning; stability itself is what's being underwritten."),
}


def _noi_appearance_months(noi_t: dict) -> int | None:
    """Months from the start of the NOI series (≈ the close) to the first month NOI
    becomes material (≥10% of stabilized monthly). High for ground-up development
    (NOI only after delivery); ~0 for an operating acquisition."""
    bp = noi_t.get("by_period") or []
    stab = noi_t.get("stabilized")
    if not bp or not stab:
        return None
    thr = 0.10 * (stab / 12.0)
    for i, (_, v) in enumerate(bp):
        if isinstance(v, (int, float)) and v >= thr:
            return i
    return len(bp)


def _classify_archetype(dt: dict, traj: dict) -> dict[str, Any]:
    """Archetype from the BUSINESS PLAN, not an NOI ratio alone: going-in occupancy,
    capex intensity, and when NOI first appears decide it; the going-in/stabilized
    NOI ratio is a supporting signal (and the fallback when occupancy is absent).
      - development : NOI first appears ≥12 mo after the close (build + lease-up).
      - value-add   : going-in occupancy ~60–85% WITH capex, or a heavy reposition
                      (big NOI ramp + capex) even at higher occupancy.
      - core        : high occupancy, minimal capex, flat NOI — yield is the thesis.
      - core-plus   : mostly stabilized with a thin growth lever in between."""
    can = dt.get("canonical", {})
    noi = traj.get("noi") or {}
    occ = traj.get("occupancy") or {}
    gi, stab = noi.get("going_in"), noi.get("stabilized")
    pct = (gi / stab) if (isinstance(gi, (int, float)) and stab) else None
    growth = (stab / gi - 1) if (gi and stab) else None
    cost = (can.get("total_cost") or {}).get("value")
    capex_by_year = (traj.get("capex") or {}).get("by_year") or {}
    capex_total = sum(abs(v) for v in capex_by_year.values()) or None
    capex_int = (capex_total / cost) if (capex_total and cost) else None
    capex_present = bool(capex_int and capex_int >= 0.03)      # ≥3% of cost = real capital
    gi_occ = occ.get("going_in")
    noi_start = _noi_appearance_months(noi)
    g = growth or 0
    rate = (dt.get("rate_type") or {}).get("type")

    label = conf = None
    if noi_start is not None and noi_start >= 12:
        label, conf = "opportunistic / development", "high"
    elif isinstance(gi_occ, (int, float)):
        if gi_occ < 0.85 and capex_present:
            label, conf = "value-add", "high"
        elif g >= 0.20 and capex_present:
            label, conf = "value-add", "high"             # high-occupancy reposition
        elif gi_occ >= 0.90 and not capex_present and g < 0.10:
            label, conf = "core", "high"
        elif gi_occ >= 0.88 and g < 0.15:
            label, conf = "core-plus", "medium"
    if label is None:                                      # no occupancy → NOI ramp + capex
        if g >= 0.20 and capex_present:
            label, conf = "value-add", "high"
        elif g >= 0.20:
            label, conf = "value-add", "medium"
        elif pct is not None and pct >= 0.92 and not capex_present:
            label, conf = "core", "medium"
        elif pct is not None and pct >= 0.80:
            label, conf = "core-plus", "medium"
        elif pct is not None:
            label, conf = "value-add", "medium"
        else:
            label, conf = "unknown", "low"

    deal_type = (dt.get("deal_type") or "").lower()
    strategy_conflict = (deal_type == "development" and label in ("core", "core-plus"))
    if strategy_conflict:
        conf = "medium"          # behaves flat, but underwritten as development — flag it

    def _r(x):
        return round(x, 3) if isinstance(x, (int, float)) else None
    signals = {
        "going_in_occupancy": _r(gi_occ), "stabilized_occupancy": _r(occ.get("stabilized")),
        "noi_appearance_months": noi_start,
        "going_in_noi_pct_of_stabilized": _r(pct), "noi_growth": _r(growth),
        "capex_intensity": _r(capex_int), "financing": rate, "deal_type": deal_type or None,
    }
    return {"label": label, "confidence": conf, "signals": signals,
            "strategy_conflict": strategy_conflict, "lens": _ARCHETYPE_LENS.get(label, "")}


# ---------------------------------------------------------------------------
# Claims — the load-bearing conclusions, computed deterministically. GPT narrates
# these; it never derives them. Each: {id, headline, what_changed, why,
# why_matters, implication, direction, confidence, sources, guardrail}.
# ---------------------------------------------------------------------------
_NOI_VARIANCE_GATE = 0.03   # below this, NOI is "tracking" — don't dissect drivers


def _k(v):
    return f"${abs(v)/1e3:,.0f}K" if abs(v) < 1e6 else f"${abs(v)/1e6:.1f}M"


def _period_phrase(months: list[str] | None) -> str:
    """" in March 2021" / " in May and June 2021" from ["2021-03"] / ["2021-05","2021-06"].
    Empty string if there's no crisp month concentration to cite."""
    if not months:
        return ""
    import calendar
    names, years = [], set()
    for ym in months:
        y, m = ym.split("-")
        years.add(y)
        names.append(calendar.month_name[int(m)])
    joined = (names[0] if len(names) == 1
              else f"{names[0]} and {names[1]}" if len(names) == 2
              else ", ".join(names[:-1]) + f", and {names[-1]}")
    year = f" {sorted(years)[0]}" if len(years) == 1 else ""
    return f" in {joined}{year}"


def _performance_claims(fs: dict, perf: dict) -> list[dict]:
    var = perf.get("variance") or {}
    items = perf.get("items") or {}
    noi_pct, noi_delta = var.get("pct"), var.get("delta")
    months = var.get("n")
    conf = (f"{months}-mo: early signal" if (months and months < 6)
            else f"{months}-mo trend" if months else "—")
    lens = fs["deal"]["archetype"].get("lens", "")
    claims: list[dict] = []

    # gap_driver — GATED on the NOI variance (user rule: >3% -> read rev & exp).
    if isinstance(noi_pct, (int, float)) and abs(noi_pct) <= _NOI_VARIANCE_GATE:
        claims.append({
            "id": "gap_driver", "direction": "on_plan", "confidence": conf,
            "headline": "NOI is tracking to plan",
            "what_changed": f"NOI is within {int(_NOI_VARIANCE_GATE*100)}% of plan "
                            f"({noi_pct*100:+.1f}%).",
            "why": "No material variance to dissect.", "why_matters": "",
            "implication": "", "sources": ["variance"],
            "guardrail": f"NOI is on plan ({noi_pct*100:+.1f}%); do not over-dramatize a "
                         "small variance."})
    elif isinstance(noi_pct, (int, float)):
        opex_delta = items.get("opex_delta") or 0                     # +ve = opex OVER plan
        rev_delta = items.get("revenue_delta")
        rev_dom = (rev_delta is not None and abs(rev_delta) > abs(opex_delta))
        direction = "revenue" if rev_dom else "expense"
        rev_ahead = (rev_delta or 0) >= 0
        movers = (items.get("movers") or [])[:3]
        mv = "; ".join(f"{it['label']} {'+' if it['delta'] > 0 else '−'}{_k(it['delta'])}"
                       for it in movers)
        below = noi_pct < 0
        claims.append({
            "id": "gap_driver", "direction": direction, "confidence": conf,
            "headline": f"NOI is {abs(noi_pct)*100:.1f}% {'below' if below else 'above'} "
                        f"plan — {direction}-driven",
            "what_changed": f"NOI is {abs(noi_pct)*100:.1f}% {'below' if below else 'above'} plan.",
            "why": (f"Revenue is {'+' if rev_ahead else '−'}{_k(rev_delta or 0)} vs plan; "
                    f"opex is {'+' if opex_delta >= 0 else '−'}{_k(opex_delta)} vs plan — "
                    f"the gap is {direction}-driven."),
            "why_matters": lens,
            "implication": f"Movers: {mv}." if mv else "",
            "sources": ["variance", "items"],
            "guardrail": (f"The NOI gap is {direction.upper()}-driven; revenue is "
                          f"{'AHEAD of' if rev_ahead else 'BEHIND'} plan. Do not attribute the "
                          f"gap to {'expenses' if direction == 'revenue' else 'revenue'}.")})

    # operational_flags — plan-vs-actual line items that signal RECURRING operating
    # risk (user rules): bad debt trend, one-time/unbudgeted R&M, insurance proceeds
    # (which foreshadow a higher renewal premium). Orphans (no line on one side) are
    # folded in even when their dollar size falls outside the top-6 movers — an
    # unbudgeted category is worth flagging regardless of how it ranks by size.
    flags: list[str] = []
    flagged: dict[str, dict] = {r["concept"]: r for r in (items.get("movers") or [])}
    for r in (items.get("orphans") or []):
        flagged.setdefault(r["concept"], r)
    for it in flagged.values():
        lab = (it.get("label") or "").lower()
        d = it.get("delta") or 0
        when = _period_phrase(it.get("actual_months"))
        unbudgeted = it.get("status") == "actual_only"
        tag = " — not found in the underwriting's OPEX budget" if unbudgeted else ""
        if ("bad debt" in lab or "credit loss" in lab) and d > 0:
            flags.append(f"Bad debt is +{_k(d)} over plan{when}{tag} — read as a "
                         "collections/credit trend, not a one-off; watch tenant health.")
        elif (("r&m" in lab or "repair" in lab or "maintenance" in lab) and d > 0):
            flags.append(f"R&M is +{_k(d)} over plan{when}{tag} — check whether it's a "
                         "one-time unbudgeted repair or a rising run-rate.")
        elif "insurance" in lab and d < 0:
            flags.append("Insurance proceeds booked — flag a likely higher insurance premium at renewal.")
        elif "insurance" in lab and d > 0:
            flags.append(f"Insurance is +{_k(d)} over plan{when}{tag} — premium pressure likely to persist.")
        elif ("marketing" in lab or "leasing" in lab) and d > 0:
            flags.append(f"Marketing & leasing spend is +{_k(d)} over plan{when}{tag}.")
        elif unbudgeted and abs(d) > 1:
            flags.append(f"{it.get('label')} of {_k(it.get('actual') or 0)}{when} was not "
                         "found in the underwriting's OPEX budget, but appears in the actuals.")
    if flags:
        claims.append({
            "id": "operational_flags", "direction": "expense", "confidence": conf,
            "headline": "Operating red flags in the actuals", "what_changed": "",
            "why": " ".join(flags), "why_matters": "", "implication": "",
            "sources": ["items"],
            "guardrail": "These are operating-line flags from the actuals; surface them as "
                         "recurring risk where indicated, not as one-offs."})

    # returns_resilient
    lev = next((r for r in (perf.get("returns") or []) if r["leg"] == "levered"
                and r.get("blended_irr") is not None), None)
    if lev:
        bps = round((lev["blended_irr"] - lev["projected_irr"]) * 10000)
        claims.append({
            "id": "returns_resilient", "direction": "down" if bps < 0 else "up",
            "confidence": conf,
            "headline": f"Levered IRR tracking {lev['blended_irr']*100:.2f}% "
                        f"(underwritten {lev['projected_irr']*100:.2f}%)",
            "what_changed": f"Levered IRR {lev['projected_irr']*100:.2f}% → "
                            f"{lev['blended_irr']*100:.2f}% ({bps:+d} bps).",
            "why": f"Only the {months or 0} elapsed months reflect actuals; the rest of the "
                   "plan is held unchanged (capex & financing at plan).",
            "why_matters": "Realized impact is modest; the cushion erodes if the variance "
                           "persists into stabilization.",
            "implication": "", "sources": ["returns"],
            "guardrail": f"Levered IRR is tracking {'DOWN' if bps < 0 else 'UP'} "
                         f"({lev['projected_irr']*100:.2f}% → {lev['blended_irr']*100:.2f}%); "
                         f"do not say returns {'improved' if bps < 0 else 'declined'}."})
    return claims


def _acquisition_claims(fs: dict) -> list[dict]:
    a = fs["deal"]["archetype"]
    t = fs["deal"]["targets"]
    nb = t["noi_bridge"]
    claims: list[dict] = []

    def pct(v):
        return f"{v*100:.1f}%" if isinstance(v, (int, float)) else "—"

    def m(v):
        return _k(v) if isinstance(v, (int, float)) else "—"

    # thesis — NOI ramp tied to occupancy (the operating story behind the value)
    sig = a["signals"]
    growth = sig.get("noi_growth")
    gi_occ, stab_occ = sig.get("going_in_occupancy"), sig.get("stabilized_occupancy")
    occ_str = (f"; occupancy {gi_occ*100:.0f}% → {stab_occ*100:.0f}%"
               if isinstance(gi_occ, (int, float)) and isinstance(stab_occ, (int, float)) else "")
    claims.append({
        "id": "thesis", "direction": a["label"], "confidence": a["confidence"],
        "headline": f"{a['label'].title()} — NOI {m(nb['going_in'])} → {m(nb['exit'])}"
                    + (f" ({growth*100:+.0f}%)" if isinstance(growth, (int, float)) else "") + occ_str,
        "what_changed": "", "why": a["lens"],
        "why_matters": (f"Going-in NOI is {sig.get('going_in_noi_pct_of_stabilized', '?')} of "
                        "stabilized — the value to be created is the ramp"
                        + (", and occupancy is the lever driving it." if occ_str else ".")),
        "implication": (f"Underwritten exit {m(t['sale_price'])} at a {pct(t['exit_cap'])} cap."),
        "sources": ["archetype", "noi_bridge", "occupancy"],
        "guardrail": (f"Read this as a {a['label']} deal (lens above)."
                      + (" Note: underwritten as development but hold-period NOI is flat — "
                         "flag the strategy/behaviour mismatch." if a.get("strategy_conflict") else ""))})
    # return_profile
    claims.append({
        "id": "return_profile", "direction": "", "confidence": "T1",
        "headline": f"Levered IRR {pct(t['levered_irr'])} · EM {t.get('levered_em')}",
        "what_changed": "", "why": f"Unlevered {pct(t['unlevered_irr'])}; LTC {pct(t['ltc'])}.",
        "why_matters": "", "implication": "", "sources": ["targets"], "guardrail": ""})
    # structural_risk — COVERAGE-FIRST. Floating rate alone isn't the risk (it's
    # usually capped); the real question is whether NOI sustains ≥1.2× DSCR. Lead
    # with the measured coverage, fold financing in as a modifier.
    fin = fs["deal"]["strategy"]["financing"]
    rate = fs["deal"]["strategy"]["rate"]
    dh = t.get("dscr_health")
    capped, floating = rate.get("capped"), (fin == "floating")
    fin_note = (" Debt is floating but rate-capped, so the rate tail is bounded." if (floating and capped)
                else " Debt is floating and uncapped — a higher-rate path compresses coverage." if floating
                else "")
    if dh and dh.get("available") and dh.get("healthy"):
        claims.append({
            "id": "structural_risk", "direction": "coverage", "confidence": "T1",
            "headline": f"Debt coverage holds — DSCR ≥1.2× through stabilization"
                        + (f" (min post-stab {dh['min_dscr_post_stab']}×)" if dh.get("min_dscr_post_stab") else ""),
            "what_changed": "", "why": f"Stabilized NOI covers debt service with headroom; the low "
                            f"point {dh['min_dscr_overall']}× was during lease-up, not stabilized operations.",
            "why_matters": "Coverage is the real gate." + fin_note,
            "implication": "", "sources": ["dscr_health", "rate_type"],
            "guardrail": "DSCR is healthy post-stabilization; do NOT frame floating rate as the "
                         "headline risk — coverage holds."})
    elif dh and dh.get("available") and not dh.get("healthy"):
        claims.append({
            "id": "structural_risk", "direction": "coverage", "confidence": "T1",
            "headline": f"DSCR breach — coverage <1.2× for {dh['breach_run_months']} mo post-stabilization",
            "what_changed": "", "why": f"NOI doesn't sustain 1.2× coverage from {dh['breach_start']}.",
            "why_matters": "Sustained sub-1.2× coverage stresses distributions and any cash-management "
                           "trigger." + fin_note,
            "implication": "", "sources": ["dscr_health"],
            "guardrail": f"DSCR BREACHES 1.2× post-stabilization ({dh['breach_run_months']} mo); "
                         "this is the headline risk, not the rate type alone."})
    elif floating:
        claims.append({
            "id": "structural_risk", "direction": "coverage", "confidence": "T1",
            "headline": "Coverage not measurable from the model" + (" (floating, capped)" if capped else " (floating)"),
            "what_changed": "", "why": "No debt-service flow to compute DSCR." + fin_note,
            "why_matters": "Verify DSCR holds ≥1.2× once actuals land; "
                           + ("the cap bounds the rate tail." if capped else "uncapped floating adds rate risk."),
            "implication": "", "sources": ["rate_type"],
            "guardrail": "Financing is FLOATING; do not assume fixed-rate. Frame risk as coverage, "
                         "not the rate type alone."})

    # where_to_look — point the reader at the source tab/rows behind the thesis
    noi_src = (fs["operating"].get("noi") or {}).get("source")
    occ_src = (fs["operating"].get("occupancy") or {}).get("source")
    if noi_src:
        ptr = f"NOI trend at `{noi_src}`" + (f"; occupancy at `{occ_src}`" if occ_src else "")
        claims.append({
            "id": "where_to_look", "direction": "", "confidence": "T1",
            "headline": "Where to look", "what_changed": "",
            "why": f"To verify the ramp, see {ptr}.", "why_matters": "",
            "implication": "", "sources": ["operating"], "guardrail": ""})
    return claims


def build_claims(fs: dict, perf: dict | None = None) -> list[dict]:
    if fs.get("mode") == "performance" and perf:
        return _performance_claims(fs, perf)
    return _acquisition_claims(fs)


# ---------------------------------------------------------------------------
# Fact-sheet assembly (deterministic — no GPT).
# ---------------------------------------------------------------------------
def _v(can: dict, c: str):
    return (can.get(c) or {}).get("value")


def _traj_pts(t: dict | None) -> dict | None:
    if not t:
        return None
    return {"going_in": t.get("going_in"),
            "exit": t.get("exit"), "by_year": t.get("by_year"), "source": t.get("source")}


def _cashflow_metrics(file_path, dt: dict) -> dict[str, Any]:
    """Metrics derived from the validated cash-flow streams (the summary grid asks
    for these): debt & equity AT EXIT, annual debt service, levered CF, leveraged
    cash-on-cash. All from find_spine's matched streams — no GPT, no cell reads.
    Occupancy is NOT here: it's an operating assumption, not in the cash flow."""
    from collections import defaultdict
    from cashflow_spine import find_spine
    can = dt.get("canonical", {})
    out: dict[str, Any] = {"equity_at_exit": None, "debt_at_exit": None,
                           "levered_cf_by_year": None, "levered_cf_stabilized": None,
                           "debt_service_stabilized": None, "leveraged_coc": None,
                           "occupancy": None}
    try:
        sp = find_spine(Path(file_path))
    except Exception:
        return out
    lev = (sp.matched.get("levered") or {}).get("flows")
    unl = (sp.matched.get("unlevered") or {}).get("flows")
    if not lev:
        return out

    acq = min(lev, key=lambda x: x[1])                 # equity outflow at close
    lev_sale = max(lev, key=lambda x: x[1])            # equity proceeds at exit
    equity = _v(can, "equity") or abs(acq[1]) or None
    if lev_sale[1] > 0:
        out["equity_at_exit"] = lev_sale[1]
        if unl:
            unl_sale = max(unl, key=lambda x: x[1])    # gross sale to the project
            if unl_sale[1] > out["equity_at_exit"]:
                out["debt_at_exit"] = unl_sale[1] - out["equity_at_exit"]

    # Operating levered CF per year — distributions to equity, excluding the
    # acquisition outflow and the sale proceeds.
    ann: dict[int, float] = defaultdict(float)
    for d, v in lev:
        if (d, v) == acq or (d, v) == lev_sale:
            continue
        ann[d.year] += v
    by_year = {y: round(v) for y, v in sorted(ann.items()) if abs(v) > 1}
    out["levered_cf_by_year"] = by_year or None
    pos = {y: v for y, v in by_year.items() if v > 0}
    if pos:
        stab_year = max(pos, key=pos.get)              # most-stabilized operating year
        out["levered_cf_stabilized"] = pos[stab_year]
        if equity:
            out["leveraged_coc"] = pos[stab_year] / equity
        # Debt service = the unlevered-minus-levered wedge in that operating year
        # (clean in non-capex/non-draw months). Sanity-gated against the loan size.
        if unl:
            ld = {d.isoformat()[:7]: v for d, v in lev}
            ud = {d.isoformat()[:7]: v for d, v in unl}
            wedge = sum(ud[k] - ld.get(k, 0) for k in ud if k.startswith(str(stab_year)) and k in ld)
            debt = _v(can, "debt")
            if wedge > 0 and (not debt or wedge < 0.20 * abs(debt)):
                out["debt_service_stabilized"] = wedge
    return out


def assemble_fact_sheet(file_path: str | Path, dt: dict | None = None,
                        analysis: dict | None = None,
                        perf: dict | None = None,
                        analysis_id: str | None = None) -> dict[str, Any]:
    """Aggregate Deal Truth + Deal Analysis + (optional) perf-vs-plan into the one
    structured object the GPT layer consumes. Deterministic; T1 facts + T2 footed
    components + archetype + guardrails + confidence + mode."""
    file_path = Path(file_path)
    if dt is None:
        from deal_truth import build_deal_truth
        dt = build_deal_truth(file_path)
    if not dt.get("engine_found", True):
        log.warning("[%s] engine_not_found reason=%s", analysis_id, dt.get("reason"))
        return {"ok": False, "reason": dt.get("reason", "cash-flow engine not found"),
                "version": FACT_SHEET_VERSION}
    if analysis is None:
        from deal_analysis import build_analysis
        analysis = build_analysis(file_path, dt=dt)

    can = dt.get("canonical", {})
    traj = analysis.get("traj") or {}
    components = analysis.get("components") or {}
    rate = dt.get("rate_type") or {}
    struct = rate.get("structure") or {}
    hold = dt.get("hold") or {}

    archetype = _classify_archetype(dt, traj)

    deal = {
        "archetype": archetype,
        "strategy": {
            "deal_type": dt.get("deal_type"),
            "hold": {"months": hold.get("months"), "years": hold.get("years")},
            "financing": rate.get("type"),
            "rate": {"spread": (struct.get("spread") or {}).get("value"),
                     "floor": (struct.get("floor") or {}).get("value"),
                     "capped": any("cap" in str(e).lower()
                                   for e in (rate.get("evidence") or []))},
        },
        "targets": {
            "purchase_price": _v(can, "purchase_price"),
            "capex": (traj.get("capex") or {}).get("stabilized"),
            "levered_irr": _v(can, "levered_irr"), "unlevered_irr": _v(can, "unlevered_irr"),
            "levered_em": _v(can, "equity_multiple"),
            "noi_bridge": {"going_in": (traj.get("noi") or {}).get("going_in"),
                           "exit": (traj.get("noi") or {}).get("exit")},
            "going_in_cap": _v(can, "going_in_cap"), "exit_cap": _v(can, "exit_cap"),
            "sale_price": _v(can, "sale_price"), "yield_on_cost": _v(can, "yield_on_cost"),
            "total_cost": _v(can, "total_cost"), "debt": _v(can, "debt"),
            "equity": _v(can, "equity"), "ltv": _v(can, "ltv"), "ltc": _v(can, "ltc"),
        },
    }
    deal["targets"].update(_cashflow_metrics(file_path, dt))
    deal["targets"]["dscr_health"] = analysis.get("dscr_health")
    # Tier-3 property identity for the Summary header (best-effort, labels misses).
    try:
        from property_id import property_identity
        deal["property"] = property_identity(file_path)
    except Exception:
        deal["property"] = None

    operating = {c: _traj_pts(traj.get(c)) for c in ("noi", "revenue", "opex", "capex")}
    # Occupancy is a LEVEL series (a %), not a flow — carried as going-in/stabilized
    # bookends + by_year trend, or None when the model has no date-axis occupancy row
    # (then the render says "not found" rather than blanking it). [[investment-read-v2]]
    operating["occupancy"] = traj.get("occupancy")
    operating["components"] = {
        c: {"total": d["total"], "footed": d["footed"],
            "components": [{"label": x["label"], "stabilized": x["stabilized"],
                            "going_in": x["going_in"], "share": x["share"],
                            "source": x["source"]} for x in d["components"]]}
        for c, d in components.items()
    }

    mode = "performance" if (perf and perf.get("ok")) else "acquisition"
    performance = None
    if mode == "performance":
        var = perf.get("variance") or {}
        rets = {r["leg"]: r for r in (perf.get("returns") or [])}
        performance = {
            "as_of_months": var.get("n"),
            "noi_variance_pct": var.get("pct"),
            "blended_returns": {leg: {"projected_irr": r.get("projected_irr"),
                                      "blended_irr": r.get("blended_irr"),
                                      "projected_em": r.get("projected_em"),
                                      "blended_em": r.get("blended_em")}
                                for leg, r in rets.items()},
            "definition_match": (perf.get("definition_match") or {}).get("verdict"),
            "line_items": perf.get("items"),
        }

    # Guardrails — but DROP the source-conflict rails for concepts the validated
    # trajectory / rate-structure supersedes: those rails name the broken point-fact
    # as the "winner" (e.g. noi=$65,793, interest_rate=0) and would re-inject the very
    # values the trajectory replaced. Keep all other guardrails (debt, exit, floating).
    import re as _re
    _superseded = _re.compile(
        r"disagree on (noi|revenue|opex|operating expense|capex|interest)", _re.I)
    guardrails = [g["message"] for g in (dt.get("guardrails") or [])
                  if g.get("message") and not _superseded.search(g["message"])]

    confidence = {
        "data_coverage": {"noi": "T1", "opex_components":
                          ("T2-footed" if (components.get("opex") or {}).get("footed") else "T2-unfooted"),
                          "revenue_components":
                          ("T2-footed" if (components.get("revenue") or {}).get("footed") else "T2-unfooted")},
        "definition_match": (perf.get("definition_match") or {}).get("verdict") if perf else None,
        "months_of_actuals": (perf.get("variance") or {}).get("n") if perf else None,
    }

    fs = {"ok": True, "version": FACT_SHEET_VERSION, "mode": mode,
          "deal": deal, "operating": operating, "performance": performance,
          "guardrails": guardrails, "confidence": confidence}
    # Claims (computed) + their derived guardrails — the binding layer GPT obeys.
    fs["claims"] = build_claims(fs, perf)
    fs["guardrails"] = guardrails + [c["guardrail"] for c in fs["claims"] if c.get("guardrail")]

    log.info("[%s] fact sheet mode=%s archetype=%s(%s) claims=%d guardrails=%d "
             "data_coverage=%s", analysis_id, mode, archetype.get("label"),
             archetype.get("confidence"), len(fs["claims"]), len(fs["guardrails"]),
             confidence["data_coverage"])
    for msg in fs["guardrails"]:
        log.info("[%s] guardrail: %s", analysis_id, msg)
    return fs


# ---------------------------------------------------------------------------
# Human-readable dump (for review — not the GPT prompt).
# ---------------------------------------------------------------------------
def render_fact_sheet(fs: dict) -> str:
    if not fs.get("ok"):
        return f"(fact sheet unavailable: {fs.get('reason')})"

    def M(v):
        return f"${v/1e6:.1f}M" if isinstance(v, (int, float)) and abs(v) >= 1e6 else (
            f"${v/1e3:.0f}K" if isinstance(v, (int, float)) and abs(v) >= 1e3 else (
                f"${v:,.0f}" if isinstance(v, (int, float)) else "—"))

    def P(v):
        return f"{v*100:.1f}%" if isinstance(v, (int, float)) else "—"

    L = [f"FACT SHEET  ·  mode={fs['mode']}  ·  v{fs['version']}", ""]
    prop = fs["deal"].get("property")
    if prop:
        from property_id import identity_line
        L.append(f"PROPERTY: {identity_line(prop)}")
    a = fs["deal"]["archetype"]
    L.append(f"ARCHETYPE: {a['label']} ({a['confidence']} confidence)")
    L.append(f"  signals: {a['signals']}")
    s, t = fs["deal"]["strategy"], fs["deal"]["targets"]
    L.append(f"STRATEGY: {s['deal_type']} · hold {s['hold']['months']} mo · {s['financing']}"
             f" (spread {P(s['rate']['spread'])}, floor {P(s['rate']['floor'])})")
    L.append(f"TARGETS:  levered IRR {P(t['levered_irr'])} · EM {t['levered_em']} · "
             f"exit {M(t['sale_price'])} @ {P(t['exit_cap'])} cap")
    nb = t["noi_bridge"]
    L.append(f"  NOI bridge: {M(nb['going_in'])} -> {M(nb['exit'])} exit")
    occ = fs["operating"].get("occupancy")
    if occ:
        L.append(f"  occupancy: {P(occ['going_in'])} going-in -> {P(occ['stabilized'])} "
                 f"stabilized  ({occ['kind']}, {occ['source']})")
    else:
        L.append("  occupancy: not found (no date-axis occupancy row in the model)")
    dh = t.get("dscr_health")
    if dh and dh.get("available"):
        if dh["healthy"]:
            tail = (f"healthy (min post-stab {dh['min_dscr_post_stab']}×)"
                    if dh.get("min_dscr_post_stab") is not None else "healthy")
        else:
            tail = (f"BREACH — <1.2× for {dh['breach_run_months']} mo from "
                    f"{dh['breach_start']}")
        L.append(f"  DSCR health: {tail}; min {dh['min_dscr_overall']}× @ {dh['min_dscr_month']} "
                 f"(stab {dh['stabilization_month']})")
    else:
        L.append("  DSCR health: not available from model (no debt-service flow)")
    L.append(f"  cost {M(t['total_cost'])} · debt {M(t['debt'])} · equity {M(t['equity'])} · LTC {P(t['ltc'])}")
    dc = fs.get("confidence", {}).get("data_coverage") or {}
    if dc:
        L.append("DATA CONFIDENCE: " + " · ".join(f"{k}: {v}" for k, v in dc.items())
                 + "  (T1=validated cash-flow/spine; T2-footed=components sum to the "
                   "total; T2-unfooted=a single line, not a footed total — hedge it)")
    comps = fs["operating"]["components"]
    for c in ("opex", "revenue"):
        cc = comps.get(c)
        if not cc:
            continue
        flag = "FOOTED" if cc["footed"] else "unfooted (not asserted)"
        L.append(f"{c.upper()} COMPONENTS [{flag}] total {M(cc['total'])}:")
        for x in cc["components"][:6]:
            L.append(f"    {x['label'][:32]:<32} {M(x['stabilized'])} ({P(x['share'])})")
    if fs["performance"]:
        pf = fs["performance"]
        L.append(f"PERFORMANCE: {pf['as_of_months']} mo · NOI {P(pf['noi_variance_pct'])} vs plan"
                 f" · def-match {pf['definition_match']}")
    L.append("")
    L.append(f"CLAIMS ({len(fs.get('claims', []))}):")
    for cl in fs.get("claims", []):
        L.append(f"  [{cl['id']}] {cl['headline']}  ({cl.get('confidence', '')})")
        if cl.get("why"):
            L.append(f"      why: {cl['why']}")
        if cl.get("implication"):
            L.append(f"      → {cl['implication']}")
    if fs["guardrails"]:
        L.append("GUARDRAILS:")
        for g in fs["guardrails"][:6]:
            L.append(f"    - {g[:100]}")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# The Investment Read — GPT narrates the fact sheet (Phase 5). GPT writes prose
# only; the claims/numbers are pre-computed and binding. Deterministic fallback
# when no API key, so it always renders (like deal_analysis).
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = """\
You are an experienced real-estate asset manager writing the opening of an investment-
committee memo. You are given a VALIDATED Fact Sheet (every number is already correct),
a set of computed CLAIMS, and binding GUARDRAILS. Your job is to turn them into a concise
investment READ — what this is, what changed, why, what deserves attention, what's next.

HARD RULES (a violation makes the read worthless):
1. Narrate ONLY from the Fact Sheet and Claims. NEVER invent, recompute, or alter a number.
2. Obey EVERY guardrail. Never contradict one. They override your instincts.
3. The Claims are your findings — do not flip an attribution or change a direction. Explain them.
4. Apply the archetype lens when judging whether something is concerning or expected.
5. Tie the NOI trend to OCCUPANCY when occupancy is given — occupancy is the lever behind the ramp.
6. Frame risk as COVERAGE, not rate type. Do NOT lead with "floating-rate risk": floating debt is
   usually capped, so the real question is whether NOI sustains ≥1.2× DSCR (use the DSCR Health claim).
   Only flag the rate when coverage is actually at risk or unmeasurable.
7. For acquisitions keep risk GENERIC to the archetype (market/demand vs hold for core; lease-up /
   reposition execution for value-add; delivery/absorption for development). For performance mode, be
   SPECIFIC: surface operating flags (bad debt, unbudgeted R&M, insurance) as recurring risk.
8. Write investment prose, not a metric list. Tight. An analyst's voice, not a dashboard.
9. Areas for Review are POINTERS to investigate, not prescribed actions — phrase them as
   things worth investigating further, proportional to the issue, never as directives.
10. If mode is "acquisition" there are no actuals — do NOT discuss "what changed" or performance;
    ground the Overall Assessment and Executive Summary in the strength of the thesis instead.
11. Respect DATA CONFIDENCE. A component marked T2-unfooted is a single line item, not a
    footed total — hedge it ("based on a single line, not a footed total") rather than stating
    it with the same certainty as a T1 or T2-footed figure.
12. Assume the reader already knows the deal basics (strategy, hold period, going-in cap rate)
    from the underwriting itself — do not restate them. Every section should earn its place by
    answering "why does this matter", not by repeating what's already in the model.

OUTPUT — exactly these four sections, markdown headers:
## Overall Assessment
   One line: a short bold verdict (e.g. "**On track.**", "**Needs attention.**", "**Materially
   off plan.**") followed by one sentence on why. This is the single most important line in the
   read — a reader who reads nothing else should still know where the deal stands.
## Executive Summary
   2–4 sentences on the CURRENT STATE: how the investment is performing overall, whether it is
   on plan or materially off plan, and the single biggest change since underwriting. Do not
   restate deal basics the reader already knows — focus on what has happened, not what was planned.
## Key Findings
   3–5 bullets, each an evidence-backed observation drawn from the Claims — what happened AND
   why it matters, not just a restated number. Lead with the most important.
## Areas for Review
   1–2 items that most affect future performance or risk (coverage-framed, not rate-alarmist),
   framed as pointers to investigate further — not as recommended actions.
"""


def _prompt_payload(fs: dict) -> str:
    import json
    lines = [f"MODE: {fs['mode']}", "", "FACT SHEET (validated — do not alter):",
             render_fact_sheet(fs), "", "CLAIMS (your findings — explain, do not change):"]
    for c in fs.get("claims", []):
        lines.append(json.dumps({k: c[k] for k in
                     ("id", "headline", "what_changed", "why", "why_matters", "implication",
                      "direction", "confidence") if c.get(k)}, default=str))
    lines += ["", "GUARDRAILS (binding — never contradict):"]
    lines += [f"- {g}" for g in fs.get("guardrails", [])]
    return "\n".join(lines)


def _overall_assessment(fs: dict) -> str:
    """A one-line verdict — the single most important thing a reader sees. Derived from
    the same NOI-variance gate the gap_driver claim uses, so it never disagrees with it."""
    a = fs["deal"]["archetype"]
    pf = fs.get("performance")
    if not pf:                                     # acquisition mode — no actuals to judge
        return f"**{a['label'].title()} acquisition.** {a.get('lens', '')}"
    pct = pf.get("noi_variance_pct")
    if pct is None:
        return "**Early read.** Not enough elapsed history yet to call a trend."
    if abs(pct) <= _NOI_VARIANCE_GATE:
        return f"**On track.** NOI is within {int(_NOI_VARIANCE_GATE*100)}% of plan ({pct*100:+.1f}%)."
    verdict = "Needs attention" if pct < 0 else "Tracking ahead of plan"
    return f"**{verdict}.** NOI is {abs(pct)*100:.1f}% {'below' if pct < 0 else 'above'} plan."


def _deterministic_read(fs: dict) -> str:
    a = fs["deal"]["archetype"]
    out = ["## Overall Assessment", _overall_assessment(fs),
           "", "## Executive Summary",
           f"{a['label'].title()} deal ({a['confidence']} confidence). {a.get('lens','')}",
           "", "## Key Findings"]
    for c in fs.get("claims", []):
        out.append(f"- **{c['headline']}** — {c.get('why','')}"
                   + (f" {c['implication']}" if c.get("implication") else ""))
    out += ["", "## Areas for Review",
            "_(Narrative read requires an API key; showing the computed findings above.)_"]
    return "\n".join(out)


def _numeric_tokens(text: str) -> list[tuple[str, float]]:
    """Pull ($-amount / percent / bare-number) tokens out of prose, normalized to a
    comparable float: $1.2M and $1,200,000 both -> ("usd", 1200000.0); 17.5% and
    17.50% both -> ("pct", 17.5). Used only to sanity-check GPT's narration against
    the fact sheet it was given — not used for any extraction or computation."""
    import re
    pattern = re.compile(r'\$?-?\d[\d,]*\.?\d*[%MmKkBb]?(?![a-zA-Z])')
    out: list[tuple[str, float]] = []
    for m in pattern.finditer(text):
        raw = m.group()
        if not any(ch.isdigit() for ch in raw):
            continue
        is_pct = raw.endswith('%')
        is_usd = raw.startswith('$')
        suffix = raw[-1].lower() if (not is_pct and raw[-1].lower() in 'mkb') else None
        core = raw[1:] if is_usd else raw
        core = core[:-1] if (is_pct or suffix) else core
        try:
            val = float(core.replace(',', ''))
        except ValueError:
            continue
        if suffix == 'm':
            val *= 1e6
        elif suffix == 'k':
            val *= 1e3
        elif suffix == 'b':
            val *= 1e9
        kind = "pct" if is_pct else ("usd" if (is_usd or suffix) else "num")
        if kind == "num" and abs(val) < 1000:
            continue                                       # list numbering / small counts, not facts
        out.append((kind, val))
    return out


def _numbers_match(a: float, b: float) -> bool:
    if a == b:
        return True
    scale = max(abs(a), abs(b), 1.0)
    return abs(a - b) <= max(0.01 * scale, 0.5)


def _check_numeric_grounding(md: str, fs_text: str, analysis_id: str | None) -> None:
    """Warning-only: flag (don't block) any number GPT's narration states that doesn't
    trace back to the fact sheet it was given, after normalizing $/%/K/M/B formatting."""
    fs_nums = _numeric_tokens(fs_text)
    unsupported = [(k, v) for k, v in _numeric_tokens(md)
                  if not any(k == fk and _numbers_match(v, fv) for fk, fv in fs_nums)]
    if unsupported:
        log.warning("[%s] investment read cites %d number(s) not traced to the fact "
                    "sheet (warning only, response not blocked): %s",
                    analysis_id, len(unsupported), unsupported[:10])


def build_investment_read(file_path, dt=None, analysis=None, perf=None,
                          analysis_id: str | None = None) -> dict[str, Any]:
    """The Investment Read artifact. Assembles the fact sheet, then GPT narrates it
    under the binding guardrails (deterministic fallback when no key)."""
    fs = assemble_fact_sheet(file_path, dt=dt, analysis=analysis, perf=perf,
                             analysis_id=analysis_id)
    if not fs.get("ok"):
        return {"ok": False, "reason": fs.get("reason"),
                "md": f"> Investment read unavailable: {fs.get('reason')}"}
    from scenarios._llm import llm_available, complete
    if not llm_available():
        log.info("[%s] investment read source=deterministic (no API key)", analysis_id)
        return {"ok": True, "source": "deterministic", "fact_sheet": fs,
                "md": _deterministic_read(fs)}
    try:
        md = complete(_SYSTEM_PROMPT, _prompt_payload(fs), temperature=0.2)
    except Exception as e:                                  # pragma: no cover - defensive
        log.warning("[%s] investment read GPT call failed (%s: %s); falling back to "
                    "deterministic", analysis_id, type(e).__name__, e)
        return {"ok": True, "source": "deterministic", "fact_sheet": fs,
                "md": _deterministic_read(fs), "note": f"{type(e).__name__}: {e}"}
    log.info("[%s] investment read source=gpt", analysis_id)
    _check_numeric_grounding(md, render_fact_sheet(fs), analysis_id)
    return {"ok": True, "source": "gpt", "fact_sheet": fs, "md": md}


if __name__ == "__main__":
    import sys
    print(render_fact_sheet(assemble_fact_sheet(sys.argv[1])))
