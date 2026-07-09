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


def _forward_year_avg(by_period: list, months_offset, anchor_bp: list | None = None) -> float | None:
    """Mean of one forward year of a dated RATE series (occupancy, ADR, RevPAR) —
    averaged, not summed. `anchor_bp` (defaults to `by_period` itself) supplies the
    calendar anchor for the window: pass the deal's NOI series so a level series
    that carries extra history the operating stream doesn't (e.g. a hotel's ADR
    trend starting years before the NOI stream begins) still reads its going-in
    window relative to when NOI itself starts, not its own earliest data point.
    Handles both MONTHLY-grain series (average the ~12 points landing in the
    window) and ANNUAL-grain series (one point per calendar year — nothing to
    average, so the single nearest point is taken directly). None if the window
    isn't covered."""
    from datetime import date
    if not by_period or not isinstance(months_offset, (int, float)):
        return None
    try:
        items = sorted((date.fromisoformat(str(d)[:10]), v) for d, v in by_period)
    except Exception:
        return None
    try:
        anchor_items = (sorted((date.fromisoformat(str(d)[:10]), v) for d, v in anchor_bp)
                        if anchor_bp else items)
    except Exception:
        anchor_items = items
    if not anchor_items:
        return None
    start, last = anchor_items[0][0], items[-1][0]
    off = int(months_offset)
    y = start.year + (start.month - 1 + off) // 12
    mo = (start.month - 1 + off) % 12 + 1
    w0, w1 = date(y, mo, 1), date(y + 1, mo, 1)
    window = [v for d, v in items if w0 <= d < w1]
    n = len(items)
    gap_days = (items[-1][0] - items[0][0]).days / (n - 1) if n > 1 else 0
    if gap_days >= 200:                      # annual-or-coarser grain
        near = min(items, key=lambda iv: abs((iv[0] - w0).days))
        return near[1] if abs((near[0] - w0).days) <= 400 else None
    if last < w1 or len(window) < 6:
        return None
    return sum(window) / len(window)


def _select_level_series(cands: list[dict], noi_t: dict | None,
                         kinds: tuple[str, ...] | None = None) -> dict | None:
    """The deal's own LEVEL series (occupancy, ADR, RevPAR) among all candidates:
    prefer a row on the SAME sheet as NOI (tied to the operating cash flow), else
    the one whose timeline overlaps NOI's most — which also screens out a
    market/comp-set benchmark or a stress-test scenario sharing the same
    vocabulary (a 'Compset RevPAR' or a recession-case ADR rarely covers the
    deal's own hold window as well as the subject's own series does). Tie-breaks:
    economic basis, then most periods."""
    from datetime import date
    if not cands:
        return None
    lvl = ([c for c in cands if c["kind"] in kinds] if kinds else cands) or cands
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

    return max(lvl, key=lambda c: (c["sheet"] == noi_sheet, overlap(c),
                                   c.get("basis") == "economic", c["n_periods"]))


def _select_occupancy(cands: list[dict], noi_t: dict | None) -> dict | None:
    return _select_level_series(cands, noi_t, kinds=("occupancy",))


def _select_rate_series(cands: list[dict], noi_t: dict | None) -> dict | None:
    return _select_level_series(cands, noi_t)


def _operating_year_range(noi_t: dict | None) -> tuple[int, int] | None:
    """The calendar years NOI is actually non-trivial (abs > $1) — the real
    operating window, as opposed to a row's raw by_period span which can include
    leading/trailing zero years the sheet still carries cells for."""
    from datetime import date
    bp = (noi_t or {}).get("by_period") or []
    active = [d for d, v in bp if isinstance(v, (int, float)) and abs(v) > 1]
    if not active:
        return None
    try:
        years = [date.fromisoformat(str(d)[:10]).year for d in active]
    except Exception:
        return None
    return min(years), max(years)


def _level_bookends(series: dict, noi_t: dict | None = None) -> dict:
    """Going-in (12-mo forward of the deal's operating start — NOI's own
    by_period start when given, else the series' own start) + stabilized (the
    plateau: average of the level from the first year it reaches ≥95% of its
    peak). Shared by occupancy, ADR, and RevPAR — a LEVEL series is never summed.
    The plateau is computed only over years NOI is actually active: an assumption
    row (ADR, occupancy) often keeps projecting years past the deal's own exit,
    and those trailing years would otherwise inflate "stabilized" with values
    the hold never actually reaches."""
    anchor_bp = (noi_t or {}).get("by_period")
    gi = _forward_year_avg(series.get("by_period"), 0, anchor_bp=anchor_bp)
    out = {"going_in": round(gi, 4) if gi is not None else None,
           "stabilized": None, "by_year": series.get("by_year"),
           "source": series.get("source"), "kind": series.get("kind"),
           "basis": series.get("basis"), "label": series.get("label")}
    by = series.get("by_year") or {}
    rng = _operating_year_range(noi_t)
    if rng:
        by = {y: v for y, v in by.items() if rng[0] <= y <= rng[1]}
    if by:
        mx = max(by.values())
        plateau = [y for y in sorted(by) if by[y] >= 0.95 * mx]
        if plateau:
            vals = [by[y] for y in sorted(by) if y >= plateau[0]]
            out["stabilized"] = round(sum(vals) / len(vals), 4)
    return out


def _occupancy_bookends(series: dict, noi_t: dict | None = None) -> dict:
    return _level_bookends(series, noi_t)


def _revpar_bridge(adr: dict | None, occ: dict | None) -> dict | None:
    """Split the RevPAR change from going-in to stabilized into the portion driven
    by ADR vs. the portion driven by occupancy (RevPAR = ADR x Occupancy), using
    the exact average-price/average-volume attribution (the cross term is split
    evenly, so the two effects sum to the full RevPAR change with no residual):
        rate_effect = delta(ADR) * avg(Occupancy)   occ_effect = delta(Occupancy) * avg(ADR)
    Names the dominant lever so the thesis claim can credit the actual driver
    instead of defaulting to occupancy."""
    if not (adr and occ):
        return None
    a0, a1 = adr.get("going_in"), adr.get("stabilized")
    o0, o1 = occ.get("going_in"), occ.get("stabilized")
    if not all(isinstance(x, (int, float)) for x in (a0, a1, o0, o1)):
        return None
    rate_effect = (a1 - a0) * (o0 + o1) / 2.0
    occ_effect = (o1 - o0) * (a0 + a1) / 2.0
    total = rate_effect + occ_effect
    if abs(total) < 1.0:                      # RevPAR essentially flat — no lever to name
        return {"rate_share": None, "occ_share": None, "lever": "flat",
                "rate_effect": round(rate_effect, 2), "occ_effect": round(occ_effect, 2)}
    rate_share, occ_share = rate_effect / total, occ_effect / total
    if abs(rate_share) >= 0.65:
        lever = "rate"
    elif abs(occ_share) >= 0.65:
        lever = "occupancy"
    else:
        lever = "rate + occupancy"
    return {"rate_share": round(rate_share, 3), "occ_share": round(occ_share, 3),
            "lever": lever, "rate_effect": round(rate_effect, 2),
            "occ_effect": round(occ_effect, 2)}


def _detect_reposition(traj: dict) -> dict[str, Any] | None:
    """Detect a mid-hold renovation/reposition: NOI dips well below its prior
    in-place level then recovers to a NEW, HIGHER plateau, with capex concentrated
    in the trough years and non-trivial going-in occupancy (the asset was already
    operating — unlike a ground-up development, which ramps NOI from ~zero with no
    prior in-place level to dip below). None when the pattern doesn't fire."""
    from datetime import date
    noi = traj.get("noi") or {}
    by_year = noi.get("by_year") or {}
    years_all = sorted(by_year)
    # Drop a trailing run of structural zero years — the row often keeps
    # projecting years past the deal's own exit, and those would otherwise look
    # like the deepest "trough" in the series.
    last_real = len(years_all)
    while last_real > 0 and abs(by_year[years_all[last_real - 1]]) <= 1:
        last_real -= 1
    years_all = years_all[:last_real]
    # Only consider FULL (near-full, >=11mo) years for the trough search — a
    # partial close-stub year's low ANNUAL SUM reflects a short year, not a real
    # dip, and would otherwise masquerade as the deepest trough.
    months_per_year: dict[int, int] = {}
    try:
        for d, v in noi.get("by_period") or []:
            if isinstance(v, (int, float)):
                months_per_year[date.fromisoformat(str(d)[:10]).year] = \
                    months_per_year.get(date.fromisoformat(str(d)[:10]).year, 0) + 1
    except Exception:
        pass
    years = [y for y in years_all if months_per_year.get(y, 12) >= 11]
    if len(years) < 4:
        return None
    vals = [by_year[y] for y in years]
    trough_i = min(range(len(vals)), key=lambda i: vals[i])
    if trough_i == 0 or trough_i == len(vals) - 1:
        return None                            # trough at an edge isn't mid-hold
    pre, post = vals[:trough_i], vals[trough_i + 1:]
    if not pre or not post:
        return None
    pre_level, trough, post_peak = max(pre), vals[trough_i], max(post)
    occ = traj.get("occupancy") or {}
    gi_occ = occ.get("going_in")
    if not (isinstance(gi_occ, (int, float)) and gi_occ >= 0.40):
        return None                            # too low — reads as lease-up from ~zero
    # V-shape: a real dip that recovers to a NEW HIGHER plateau, not just back to
    # where it was (that's noise, not a reposition).
    if not (pre_level > 0 and trough <= 0.6 * pre_level and post_peak > pre_level * 1.05):
        return None
    capex_by_year = (traj.get("capex") or {}).get("by_year") or {}
    trough_years = years[max(0, trough_i - 1):trough_i + 2]     # trough year +/- 1
    capex_total = sum(abs(v) for v in capex_by_year.values())
    capex_trough = sum(abs(capex_by_year.get(y, 0)) for y in trough_years)
    if not (capex_total > 0 and capex_trough / capex_total >= 0.5):
        return None
    return {"trough_year": years[trough_i], "trough_noi": round(trough),
            "pre_level": round(pre_level), "post_peak": round(post_peak),
            "capex_share_in_trough": round(capex_trough / capex_total, 3)}


def _clean_going_in_noi(noi_t: dict, reposition: dict) -> float | None:
    """Going-in NOI = the last undisrupted in-place operating year at/around the
    close — the first CALENDAR year with near-full coverage (>=11 months of data
    in the model's own series) that isn't the detected renovation-disruption
    trough. Guards against (a) a partial close-stub year understating the
    in-place base and (b) landing on the reposition trough instead of the true
    in-place run-rate. Only called once a reposition has fired."""
    from datetime import date
    by_year = noi_t.get("by_year") or {}
    years = sorted(by_year)
    if not years:
        return None
    months_per_year: dict[int, int] = {}
    try:
        for d, v in noi_t.get("by_period") or []:
            if not isinstance(v, (int, float)):
                continue
            y = date.fromisoformat(str(d)[:10]).year
            months_per_year[y] = months_per_year.get(y, 0) + 1
    except Exception:
        pass
    trough_year = reposition.get("trough_year")
    for y in years:
        if months_per_year.get(y, 12) < 11:
            continue                              # partial stub year — skip
        if trough_year is not None and y == trough_year:
            continue                              # renovation-disrupted year — skip
        if by_year[y] > 0:
            return by_year[y]
    return None


def _ym(d) -> str:
    return str(d)[:7]


def _months_between(a: str | None, b: str | None) -> int | None:
    """Whole months from 'YYYY-MM' a to b (b − a). None if either is missing or
    unparseable — so a caller can render the boundary as 'not determinable'."""
    if not a or not b:
        return None
    try:
        return (int(b[:4]) - int(a[:4])) * 12 + (int(b[5:7]) - int(a[5:7]))
    except (ValueError, IndexError):
        return None


def _ttm_stabilization_month(noi_t: dict | None) -> str | None:
    """First month TTM NOI reaches ≥95% of stabilized NOI — the SAME rule
    _dscr_health uses for `stabilization_month`, applied to the NOI series ALONE
    so it still resolves when the model carries no debt-service flow (dscr_health
    is None then). Not a second definition: phasing prefers dscr_health's value
    when it exists and falls back to this. Returns 'YYYY-MM' or None."""
    bp = (noi_t or {}).get("by_period") or []
    stab = (noi_t or {}).get("stabilized")
    if not bp or not stab:
        return None
    vals: dict[str, float] = {}
    for d, v in bp:
        if isinstance(v, (int, float)):
            vals[_ym(d)] = v
    mk = sorted(vals)
    if len(mk) < 12:
        return None
    for i in range(11, len(mk)):
        if sum(vals[m] for m in mk[i - 11:i + 1]) >= 0.95 * stab:
            return mk[i]
    return None


def _deal_phasing(noi_t: dict | None, capex_t: dict | None, hold: dict | None,
                  reposition: dict | None, dscr_health: dict | None) -> dict | None:
    """Explicit phase timeline for a development or repositioning deal, computed
    from the dated NOI + capex series (never GPT):
      build/reno window → delivery/reopen → lease-up → stabilization → post-stab hold.
    Returns {"kind": "none"} for a core / stabilized acquisition (no build or reno
    phase to state), so the claim simply omits. Any boundary that can't be pinned
    from full data is returned as None (rendered 'not determinable') and the
    confidence labelled down rather than guessed."""
    if not noi_t:
        return None
    bp = noi_t.get("by_period") or []
    stab = noi_t.get("stabilized")
    if not bp or not stab:
        return {"kind": "none"}
    months = [_ym(d) for d, _ in bp]
    close = months[0]
    # Stabilization: reuse dscr_health's value (computed with debt-service
    # context) when present, else the same ≥95%-of-stabilized TTM rule on NOI.
    stab_m = (dscr_health or {}).get("stabilization_month") or _ttm_stabilization_month(noi_t)
    src = " / ".join(x for x in [noi_t.get("source"), (capex_t or {}).get("source")] if x) or None

    def capex_share(lo: str | None, hi: str | None):
        cbp = (capex_t or {}).get("by_period") or []
        tot = sum(abs(v) for _, v in cbp if isinstance(v, (int, float)))
        if tot <= 0 or not lo or not hi:
            return None
        win = sum(abs(v) for d, v in cbp if isinstance(v, (int, float)) and lo <= _ym(d) <= hi)
        return round(win / tot, 3)

    def post_stab():
        sale = (hold or {}).get("sale_date")
        if stab_m and sale:
            n = _months_between(stab_m, _ym(sale))
            return n if (n is not None and n >= 0) else None
        hm = (hold or {}).get("months")
        n = _months_between(close, stab_m)
        return (hm - n) if (stab_m and hm is not None and n is not None) else None

    # --- Repositioning: reno = the rooms-dark / offline window around the trough.
    if reposition:
        vals = {_ym(d): v for d, v in bp if isinstance(v, (int, float))}
        mk = sorted(vals)
        pre = reposition.get("pre_level")
        trough_year = reposition.get("trough_year")
        reno_start = reno_end = reopen = None
        # Anchor the trough to the reposition's trough YEAR, not the global min:
        # a series that opens with zero months at close (before revenue starts)
        # would otherwise tie the "min" onto the series start, not the renovation.
        in_trough_yr = [m for m in mk if trough_year and m[:4] == str(trough_year)]
        if pre and (in_trough_yr or mk):
            thr = 0.30 * (pre / 12.0)            # a month running <30% of in-place = offline
            trough_m = min(in_trough_yr or mk, key=lambda m: vals[m])
            ti = mk.index(trough_m)
            lo = hi = ti
            while lo - 1 >= 0 and vals[mk[lo - 1]] < thr:
                lo -= 1
            while hi + 1 < len(mk) and vals[mk[hi + 1]] < thr:
                hi += 1
            reno_start, reno_end = mk[lo], mk[hi]
            reopen = mk[hi + 1] if hi + 1 < len(mk) else None
        reno_months = _months_between(reno_start, reopen)
        leaseup_months = _months_between(reopen, stab_m)
        confidence = "T2" if (reno_start and reopen and stab_m) else "T3"
        return {
            "kind": "repositioning", "close": close,
            "build_or_reno": {"start": reno_start, "end": reno_end, "months": reno_months,
                              "capex_share": capex_share(reno_start, reno_end)},
            "delivery_or_reopen": reopen,
            "leaseup": {"start": reopen, "end": stab_m, "months": leaseup_months},
            "stabilization": stab_m, "post_stab_hold_months": post_stab(),
            "source": src, "confidence": confidence,
        }

    # --- Development: build = close → first material NOI month (no prior in-place).
    thr = 0.10 * (stab / 12.0)
    appear_i = next((i for i, (_, v) in enumerate(bp)
                     if isinstance(v, (int, float)) and v >= thr), None)
    if appear_i is not None and appear_i >= 12:
        delivery = months[appear_i]
        build_end = months[appear_i - 1]
        build_months = _months_between(close, delivery)
        leaseup_months = _months_between(delivery, stab_m)
        confidence = "T2" if stab_m else "T3"
        return {
            "kind": "development", "close": close,
            "build_or_reno": {"start": close, "end": build_end, "months": build_months,
                              "capex_share": capex_share(close, build_end)},
            "delivery_or_reopen": delivery,
            "leaseup": {"start": delivery, "end": stab_m, "months": leaseup_months},
            "stabilization": stab_m, "post_stab_hold_months": post_stab(),
            "source": src, "confidence": confidence,
        }

    return {"kind": "none"}


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

    # Occupancy/vacancy — a LEVEL series the flow roll-up drops. Pick the series
    # aligned to the operating timeline and anchor going-in/stabilized to the close.
    try:
        from cashflow_rollup import occupancy_candidates
        occ_sel = _select_occupancy(occupancy_candidates(_ru), traj.get("noi")) if _ru else None
    except Exception:
        occ_sel = None
    if occ_sel:
        traj = dict(traj)
        traj["occupancy"] = _occupancy_bookends(occ_sel, traj.get("noi"))

    # ADR / RevPAR — hotel rate LEVEL series, the same treatment as occupancy
    # (never summed). Only fires for hospitality deals; silently absent elsewhere.
    try:
        from cashflow_rollup import rate_level_candidates
        adr_sel = _select_rate_series(rate_level_candidates(_ru, "adr"), traj.get("noi")) if _ru else None
        revpar_sel = _select_rate_series(rate_level_candidates(_ru, "revpar"), traj.get("noi")) if _ru else None
    except Exception:
        adr_sel = revpar_sel = None
    if adr_sel:
        traj = dict(traj)
        traj["adr"] = _level_bookends(adr_sel, traj.get("noi"))
    if revpar_sel:
        traj = dict(traj)
        traj["revpar"] = _level_bookends(revpar_sel, traj.get("noi"))
    elif traj.get("adr") and traj.get("occupancy"):
        # No explicit RevPAR row — derive it (RevPAR = ADR x Occupancy) so the
        # bridge below still has something to decompose.
        a, o = traj["adr"], traj["occupancy"]
        if isinstance(a.get("going_in"), (int, float)) and isinstance(o.get("going_in"), (int, float)):
            stab = (round(a["stabilized"] * o["stabilized"], 2)
                    if isinstance(a.get("stabilized"), (int, float))
                    and isinstance(o.get("stabilized"), (int, float)) else None)
            traj = dict(traj)
            traj["revpar"] = {"going_in": round(a["going_in"] * o["going_in"], 2),
                              "stabilized": stab, "source": "derived: ADR x Occupancy",
                              "kind": "revpar"}
    bridge = _revpar_bridge(traj.get("adr"), traj.get("occupancy"))
    if bridge:
        traj = dict(traj)
        traj["revpar_bridge"] = bridge

    # Undisrupted/unaffected NOI — some models carry a parallel NOI read that
    # excludes an NOI-guarantee or disruption-credit line, so the ramp can be
    # read organic-vs-supported instead of taking the (possibly propped)
    # headline NOI as fully organic. Reconcile it through the SAME deal-anchor
    # scale as the headline NOI (NOI/exit_cap ~ sale) rather than assuming it
    # shares the headline row's sheet/units.
    try:
        from cashflow_rollup import undisrupted_noi_candidates
        und_cands = undisrupted_noi_candidates(_ru) if _ru else []
    except Exception:
        und_cands = []
    if und_cands:
        noi_sheet = ((traj.get("noi") or {}).get("source") or "").split("!")[0]
        best_und = max(und_cands, key=lambda t: (t["source"].split("!")[0] == noi_sheet,
                                                  len(t.get("by_period") or [])))
        rescaled = _reconcile_operating_units({"noi": best_und}, can)
        traj = dict(traj)
        traj["noi_undisrupted"] = rescaled["noi"]

    # Reposition/renovation detection — needs NOI's by_year, occupancy, and capex,
    # all of which are now populated. Feeds the going-in NOI anchor below (skip the
    # trough) and the archetype classifier downstream.
    reposition = _detect_reposition(traj)
    if reposition:
        traj = dict(traj)
        traj["reposition"] = reposition

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
    # When a reposition fires, the rolling window can instead land on a partial
    # close-stub or the renovation trough itself and UNDERSTATE the true in-place
    # base — re-anchor to the nearest clean, undisrupted calendar year in that case
    # (gated on the reposition flag so an ordinary lease-up deal, where the rolling
    # window was deliberately chosen over calendar years, is untouched).
    noi_t = traj.get("noi")
    if noi_t:
        bp = noi_t.get("by_period")
        hold_mo = (dt.get("hold") or {}).get("months")
        patch: dict[str, float] = {}
        gi = _forward_year_noi(bp, 0)
        if reposition:
            clean_gi = _clean_going_in_noi(noi_t, reposition)
            if isinstance(clean_gi, (int, float)):
                gi = clean_gi
        if isinstance(gi, (int, float)):
            patch["going_in"] = gi
        if hold_mo:
            ex = _forward_year_noi(bp, hold_mo)
            if isinstance(ex, (int, float)):
                patch["exit"] = ex
        if patch:
            traj = dict(traj)
            traj["noi"] = {**noi_t, **patch}

    # DSCR Health — trailing-12 NOI ÷ debt service, flagged for sustained post-
    # stabilization coverage stress. Uses the RAW (un-reconciled) debt-service flow,
    # which the operating reconcile drops; None when the model has no such flow.
    dscr_health = _dscr_health(traj.get("noi"), (_raw_ct or {}).get("debt_service"))

    # Deal phasing — the build/reno → delivery/reopen → lease-up → stabilization
    # timeline, derived from the NOI + capex series (reuses the reposition trough
    # window and dscr_health's stabilization month). "none" for a stabilized deal.
    phasing = _deal_phasing(traj.get("noi"), traj.get("capex"), dt.get("hold"),
                            traj.get("reposition"), dscr_health)
    if phasing:
        traj = dict(traj)
        traj["phasing"] = phasing

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
