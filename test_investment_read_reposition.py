"""
test_investment_read_reposition.py — checks for the rate-lever / reposition
signals added to the Investment Read (deal_analysis.py + interpretation.py).

Run: python3 test_investment_read_reposition.py

Two layers of checks:
  * UNIT — _revpar_bridge, _detect_reposition, and _deal_phasing against
    hand-built trajectory dicts: rate-led vs. occupancy-led vs. flat RevPAR
    bridges; a reposition firing on a genuine mid-hold renovation V-shape but
    NOT on a monotonic lease-up; and a development phase timeline (build ->
    delivery -> lease-up -> stabilize) vs. a flat in-place deal (kind "none").
  * ST REGIS STRUCTURAL regression — the real hotel-repositioning model reads
    rate-led, classifies as a repositioning, labels the DSCR low point
    "renovation disruption" instead of "lease-up", and states a renovation
    phase timeline (reno window + capex share, reopen, stabilization, post-stab
    hold). Skipped if the file isn't local, so the suite stays portable.

No GPT, no network.
"""
from __future__ import annotations

import sys
from pathlib import Path

from deal_analysis import (_revpar_bridge, _detect_reposition, _deal_phasing,
                           _ttm_stabilization_month)
from interpretation import assemble_fact_sheet, _phasing_summary

_ST_REGIS = Path("/Users/jb/Documents/Real Estate/Collie/Data/Underwriting/St Regis Model.xlsx")
_1425 = Path("/Users/jb/Documents/Real Estate/Collie/Data/Underwriting/"
             "1425 4th Ave MF Proforma_(2023.04.21).xlsx")

_fail = 0


def check(cond: bool, msg: str) -> None:
    global _fail
    if not cond:
        _fail += 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")


def _noi_traj(by_year: dict[int, float]) -> dict:
    """A synthetic NOI trajectory with real MONTHLY by_period coverage (12
    points/year) — _detect_reposition's stub-year guard requires >=11 months of
    data per year, so a single annual point per year would look like every year
    is a stub and the whole series would get filtered out."""
    by_period = [(f"{y}-{m:02d}-28", v / 12.0)
                 for y, v in sorted(by_year.items()) for m in range(1, 13)]
    return {"by_year": by_year, "by_period": by_period, "source": "Test!row1"}


# ---------------------------------------------------------------------------
# _revpar_bridge — rate-vs-occupancy attribution
# ---------------------------------------------------------------------------

def revpar_bridge_unit() -> None:
    print("\n— _revpar_bridge: rate vs. occupancy attribution")

    # Rate-led: ADR steps up hard, occupancy is flat-to-down (St Regis shape).
    adr = {"going_in": 525.0, "stabilized": 874.0}
    occ = {"going_in": 0.79, "stabilized": 0.74}
    b = _revpar_bridge(adr, occ)
    check(b is not None and b["lever"] == "rate", "ADR up + occupancy down -> lever = rate")
    check(b["rate_share"] > 0.6, f"rate_share dominates (got {b['rate_share']})")

    # Occupancy-led: occupancy climbs, ADR flat (classic lease-up value-add).
    adr2 = {"going_in": 200.0, "stabilized": 205.0}
    occ2 = {"going_in": 0.60, "stabilized": 0.90}
    b2 = _revpar_bridge(adr2, occ2)
    check(b2 is not None and b2["lever"] == "occupancy", "occupancy up + ADR flat -> lever = occupancy")

    # Both moving comparably -> neither dominates.
    adr3 = {"going_in": 200.0, "stabilized": 240.0}
    occ3 = {"going_in": 0.65, "stabilized": 0.80}
    b3 = _revpar_bridge(adr3, occ3)
    check(b3 is not None and b3["lever"] == "rate + occupancy",
          f"comparable moves -> lever = rate + occupancy (got {b3 and b3['lever']})")

    # Missing data -> None, no crash.
    check(_revpar_bridge(None, occ) is None, "missing ADR -> None")
    check(_revpar_bridge(adr, None) is None, "missing occupancy -> None")


# ---------------------------------------------------------------------------
# _detect_reposition — V-shape trough + capex-in-trough + real occupancy
# ---------------------------------------------------------------------------

def detect_reposition_unit() -> None:
    print("\n— _detect_reposition: fires on a renovation V-shape, not on lease-up")

    # A real mid-hold reposition: healthy in-place NOI -> deep trough (reno) ->
    # a NEW higher plateau, with capex concentrated in the trough year.
    traj_reposition = {
        "noi": _noi_traj({2018: 8_000_000, 2019: 8_100_000, 2020: 2_000_000,
                          2021: 9_500_000, 2022: 22_000_000, 2023: 27_000_000}),
        "occupancy": {"going_in": 0.80},
        "capex": {"by_year": {2019: 2_000_000, 2020: 95_000_000, 2021: 3_000_000}},
    }
    r = _detect_reposition(traj_reposition)
    check(r is not None and r["trough_year"] == 2020, f"reposition fires, trough=2020 (got {r})")

    # A monotonic lease-up from near-zero: no prior in-place level, no trough to
    # dip below — must NOT be read as a reposition.
    traj_leaseup = {
        "noi": _noi_traj({2018: 100_000, 2019: 2_000_000, 2020: 4_500_000,
                          2021: 6_800_000, 2022: 7_700_000}),
        "occupancy": {"going_in": 0.10},
        "capex": {"by_year": {2018: 20_000_000, 2019: 5_000_000}},
    }
    check(_detect_reposition(traj_leaseup) is None,
          "monotonic lease-up from near-zero does NOT fire reposition")

    # A dip that just recovers to the SAME level (noise, not a reposition: no
    # new higher plateau) must not fire either.
    traj_noise = {
        "noi": _noi_traj({2018: 8_000_000, 2019: 6_500_000, 2020: 8_100_000, 2021: 8_050_000}),
        "occupancy": {"going_in": 0.80},
        "capex": {"by_year": {2019: 8_000_000}},
    }
    check(_detect_reposition(traj_noise) is None,
          "a dip recovering to the SAME level (no new plateau) does NOT fire reposition")

    # Same V-shape but capex is NOT concentrated in the trough (spread evenly
    # across many more years than just the trough window) -> not a
    # renovation-funded trough, must not fire.
    traj_no_capex = {
        "noi": _noi_traj({2018: 8_000_000, 2019: 8_100_000, 2020: 2_000_000,
                          2021: 9_500_000, 2022: 22_000_000, 2023: 27_000_000}),
        "occupancy": {"going_in": 0.80},
        "capex": {"by_year": {y: 1_000_000 for y in range(2014, 2024)}},
    }
    check(_detect_reposition(traj_no_capex) is None,
          "V-shape without capex concentrated in the trough does NOT fire reposition")


# ---------------------------------------------------------------------------
# St Regis structural regression (real hotel-repositioning model)
# ---------------------------------------------------------------------------

def st_regis_regression() -> None:
    print("\n— St Regis structural regression (rate-led repositioning)")
    fs = assemble_fact_sheet(_ST_REGIS)
    check(fs.get("ok"), "fact sheet built without error")
    if not fs.get("ok"):
        return

    a = fs["deal"]["archetype"]
    check(a["label"] == "value-add / repositioning",
          f"archetype = value-add / repositioning (got {a['label']})")
    check(a["signals"].get("revpar_lever") == "rate",
          f"RevPAR bridge lever = rate (got {a['signals'].get('revpar_lever')})")
    check(a["signals"].get("reposition") is not None, "reposition signal is populated")

    claims = {c["id"]: c for c in fs["claims"]}
    thesis = claims.get("thesis")
    check(thesis is not None and "ADR" in thesis["headline"], "thesis headline cites ADR")
    check(thesis is not None and "occupancy" not in thesis["why_matters"].split(".")[0].lower()
          or "rate" in (thesis or {}).get("why_matters", "").lower(),
          "thesis why_matters credits rate, not occupancy, as the lever")

    risk = claims.get("structural_risk")
    check(risk is not None and "renovation disruption" in risk["why"],
          "DSCR low point labeled 'renovation disruption', not 'lease-up'")
    check(risk is None or "lease-up" not in risk["why"],
          "DSCR narrative does not say 'lease-up' for a repositioning")

    # Going-in NOI: the old bug landed on a partial close-stub (~$7.5M); the
    # fix steps to the nearest clean, undisrupted full year (~$8.05M).
    nb = fs["deal"]["targets"]["noi_bridge"]
    check(isinstance(nb.get("going_in"), (int, float)) and nb["going_in"] > 7_900_000,
          f"going-in NOI past the old stub-year bug (got {nb.get('going_in')})")


def guard_1425_not_over_fired() -> None:
    print("\n— 1425 (non-reposition) guard: detector does not over-fire")
    fs = assemble_fact_sheet(_1425)
    check(fs.get("ok"), "fact sheet built without error")
    if not fs.get("ok"):
        return
    a = fs["deal"]["archetype"]
    check(a["signals"].get("reposition") is None,
          "reposition detector stays silent on a genuine lease-up model")
    check(a["label"] != "value-add / repositioning",
          f"archetype is not repositioning (got {a['label']})")
    # 1425's NOI is present from the first period (no build/reno gap) -> phasing
    # kind "none", and no phasing claim is emitted.
    ph = fs["deal"].get("phasing")
    check(ph is None or ph.get("kind") == "none",
          f"phasing kind = none for a non-phased deal (got {ph and ph.get('kind')})")
    check(not any(c["id"] == "phasing" for c in fs["claims"]),
          "no phasing claim emitted when kind = none")


# ---------------------------------------------------------------------------
# _deal_phasing — phase-timeline computer (development / core unit cases)
# ---------------------------------------------------------------------------

def _month_range(start_ym: str, n: int) -> list[str]:
    y, mo = int(start_ym[:4]), int(start_ym[5:7])
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{mo:02d}")
        mo += 1
        if mo > 12:
            mo, y = 1, y + 1
    return out


def _series(months: list[str], vals: list[float], source: str) -> dict:
    return {"by_period": [(f"{ym}-28", v) for ym, v in zip(months, vals)], "source": source}


def deal_phasing_unit() -> None:
    print("\n— _deal_phasing: development timeline (build -> delivery -> lease-up -> stabilize)")
    # 24 months of ~zero NOI (construction), then a lease-up ramp to a $6M
    # stabilized run-rate; capex concentrated in the build window.
    months = _month_range("2019-01", 60)
    noi_vals = ([0.0] * 24                                   # 2019-2020: under construction
                + [200_000 + 25_000 * i for i in range(12)]  # 2021: lease-up ramp
                + [500_000] * 24)                            # 2022-2023: stabilized (~$6M/yr)
    noi_t = dict(_series(months, noi_vals, "Test!rowNOI"), stabilized=6_000_000)
    capex_vals = [4_000_000] * 24 + [0.0] * 36               # all capex in the build window
    capex_t = _series(months, capex_vals, "Test!rowCapex")
    hold = {"months": 84, "sale_date": "2025-12-31"}

    ph = _deal_phasing(noi_t, capex_t, hold, None, None)
    check(ph is not None and ph["kind"] == "development", f"kind = development (got {ph and ph['kind']})")
    check(ph["build_or_reno"]["start"] == "2019-01", "build window starts at close (2019-01)")
    check(ph["delivery_or_reopen"] == "2021-01", "delivery is the first material-NOI month after the gap")
    check(ph["build_or_reno"]["months"] == 24, f"build window ~24 months (got {ph['build_or_reno']['months']})")
    check(ph["build_or_reno"]["capex_share"] is not None and ph["build_or_reno"]["capex_share"] > 0.9,
          "capex concentrated in the build window")
    check(ph["stabilization"] is not None, "stabilization month pinned")
    check(ph["leaseup"]["end"] == ph["stabilization"], "lease-up ends at stabilization")
    check(isinstance(ph["post_stab_hold_months"], int) and ph["post_stab_hold_months"] > 0,
          "post-stab hold months computed and positive")
    # A ground-up development's pre-delivery gap is NOT a value-add trough.
    check(_detect_reposition({"noi": {"by_year": {}, "by_period": noi_t["by_period"]},
                              "occupancy": {"going_in": 0.0}, "capex": capex_t}) is None,
          "pre-delivery gap does not trip the reposition detector")

    print("\n— _deal_phasing: core / stabilized -> kind = none")
    flat = _month_range("2020-01", 48)
    core_noi = dict(_series(flat, [500_000] * 48, "Test!rowNOI"), stabilized=6_000_000)
    ph_core = _deal_phasing(core_noi, None, {"months": 48, "sale_date": "2023-12-31"}, None, None)
    check(ph_core is not None and ph_core["kind"] == "none",
          f"flat in-place NOI -> kind none (got {ph_core and ph_core.get('kind')})")


def phasing_st_regis_regression() -> None:
    print("\n— St Regis phasing regression (repositioning timeline)")
    fs = assemble_fact_sheet(_ST_REGIS)
    check(fs.get("ok"), "fact sheet built without error")
    if not fs.get("ok"):
        return
    ph = fs["deal"].get("phasing")
    check(ph is not None and ph["kind"] == "repositioning",
          f"phasing kind = repositioning (got {ph and ph.get('kind')})")
    br = ph["build_or_reno"]
    check(br.get("start") is not None and br.get("months") and br["months"] > 0,
          f"non-null reno window with months > 0 (got {br.get('months')})")
    check(isinstance(br.get("capex_share"), (int, float)) and br["capex_share"] > 0.5,
          f"capex_share > 0.5 in the reno window (got {br.get('capex_share')})")
    check(ph.get("stabilization") is not None, "stabilization date pinned")
    check(isinstance(ph.get("post_stab_hold_months"), int) and ph["post_stab_hold_months"] > 0,
          f"post_stab_hold_months > 0 (got {ph.get('post_stab_hold_months')})")

    claims = {c["id"]: c for c in fs["claims"]}
    check("phasing" in claims, "a phasing claim is emitted")
    # Numeric grounding: every date/duration the claim narrates is also on the
    # rendered fact sheet (the phasing summary is shared between the two).
    from interpretation import render_fact_sheet
    fs_text = render_fact_sheet(fs)
    check(_phasing_summary(ph) in fs_text, "phasing summary appears verbatim on the rendered fact sheet")


def stabilization_helper_unit() -> None:
    print("\n— _ttm_stabilization_month: reproduces dscr_health's rule on NOI alone")
    if not _ST_REGIS.exists():
        print("  (skipped — St Regis file not local)")
        return
    from deal_analysis import build_analysis
    r = build_analysis(_ST_REGIS)
    noi_t = r["traj"]["noi"]
    dh = r.get("dscr_health") or {}
    check(_ttm_stabilization_month(noi_t) == dh.get("stabilization_month"),
          f"NOI-only TTM stabilization matches dscr_health ({dh.get('stabilization_month')})")


def main() -> int:
    revpar_bridge_unit()
    detect_reposition_unit()
    deal_phasing_unit()
    stabilization_helper_unit()
    if _ST_REGIS.exists():
        st_regis_regression()
        phasing_st_regis_regression()
    else:
        print("\n— St Regis structural regression: SKIPPED (file not local)")
    if _1425.exists():
        guard_1425_not_over_fired()
    else:
        print("\n— 1425 non-reposition guard: SKIPPED (file not local)")
    print(f"\n{'ALL PASS' if _fail == 0 else f'{_fail} FAILURE(S)'}")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
