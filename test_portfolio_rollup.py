"""
M1 tests — portfolio spine: canonical identity, per-property SSOTs, roll-up.

Synthetic fixtures only (generality rule, 2026-07-18): no real property
names, filenames, or workbook-specific patterns anywhere in this file.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_assets(tmp_path, monkeypatch):
    """Point every module's assets dir at a temp dir."""
    import ssot
    import property_registry
    assets = tmp_path / "assets"
    monkeypatch.setattr(ssot, "ASSETS_DIR", assets)
    monkeypatch.setattr(ssot, "CURRENT_ASSET_FILE", assets / "current_asset.json")
    monkeypatch.setattr(ssot, "PORTFOLIO_FILE", assets / "portfolio.json")
    monkeypatch.setattr(property_registry, "ASSETS_DIR", assets)
    monkeypatch.setattr(property_registry, "ALIAS_FILE", assets / "property_aliases.json")
    yield assets


# -----------------------------------------------------------------------------
# property_registry — canonical identity
# -----------------------------------------------------------------------------

def test_canonicalize_strips_model_noise_and_dates():
    from property_registry import canonicalize_name
    assert canonicalize_name("Alpha Tower - Model_2023-04-21_Closing Model.xlsx") \
        == "Alpha Tower"
    assert canonicalize_name("100 Main St Proforma_03.08.2022_Adept.xlsx") \
        == "100 Main St"


def test_slug_keeps_street_numbers():
    from property_registry import canonicalize_name, slugify
    assert slugify(canonicalize_name("100 Main St Proforma.xlsx")) == "100-main-st"


def test_new_property_is_flagged_never_merged():
    from property_registry import resolve_property_id
    res = resolve_property_id("Alpha Tower")
    assert res["property_id"] == "alpha-tower"
    assert "NEW_PROPERTY" in res["flags"]
    assert res["identity_source"] == "workbook"


def test_alias_map_resolves_variants_and_ships_empty():
    from property_registry import load_aliases, register_alias, resolve_property_id
    assert load_aliases() == {}  # ships empty — populated at runtime only
    register_alias("Alpha Twr LP", "alpha-tower")
    res = resolve_property_id("Alpha Twr LP")
    assert res["property_id"] == "alpha-tower"
    assert "NEW_PROPERTY" not in res["flags"]
    assert res["matched_alias"] == "alpha twr lp"


def test_filename_fallback_is_flagged():
    from property_registry import resolve_property_id
    res = resolve_property_id(None, filename="/tmp/Beta Court Model_v2.xlsx")
    assert res["property_id"] == "beta-court"
    assert "FILENAME_IDENTITY" in res["flags"]
    assert res["identity_source"] == "filename"


def test_lone_generic_word_is_not_a_property_name():
    from property_registry import _plausible_workbook_name
    assert not _plausible_workbook_name("Rate")
    assert not _plausible_workbook_name("Total")
    assert _plausible_workbook_name("Rate Street Lofts")  # multi-word is fine
    assert _plausible_workbook_name("Alpha")              # non-generic lone word ok


def test_no_identity_returns_flag_not_guess():
    from property_registry import resolve_property_id
    res = resolve_property_id(None, filename=None)
    assert res["property_id"] is None
    assert "NO_IDENTITY" in res["flags"]


# -----------------------------------------------------------------------------
# ssot — per-property storage
# -----------------------------------------------------------------------------

def _seed(pid, layer, metrics):
    """Write a property SSOT with structured metrics for one layer."""
    from ssot import load_property_ssot, save_property_ssot
    s = load_property_ssot(pid)
    s["layers"][layer] = {
        "source_file": f"{pid}-fixture.xlsx",
        "metrics": {
            name: {"value": v, "sheet": "S1", "cell": "B2", "confidence": "high"}
            for name, v in metrics.items()
        },
        "bounded_metrics": {},
    }
    save_property_ssot(pid, s)


def test_property_ssots_are_separate_files(_isolated_assets):
    from ssot import list_property_ids, load_property_ssot
    _seed("alpha-tower", "underwriting", {"Purchase Price": 100})
    _seed("beta-court", "underwriting", {"Purchase Price": 200})
    assert list_property_ids() == ["alpha-tower", "beta-court"]
    assert load_property_ssot("alpha-tower")["asset_id"] == "alpha-tower"


def test_reserved_and_malformed_ids_rejected():
    from ssot import property_ssot_path
    for bad in ("portfolio", "current_asset", "property_aliases", "Alpha Tower", ""):
        with pytest.raises(ValueError):
            property_ssot_path(bad)


def test_set_active_property_feeds_single_asset_api(_isolated_assets):
    from ssot import set_active_property, load_ssot
    _seed("alpha-tower", "underwriting", {"Purchase Price": 100})
    set_active_property("alpha-tower")
    assert load_ssot()["asset_id"] == "alpha-tower"


# -----------------------------------------------------------------------------
# portfolio — roll-up semantics
# -----------------------------------------------------------------------------

NOI = "Net Operating Income (NOI)"
PRICE = "Purchase Price"
DEBT = "Debt Amount"
IRR = "Levered IRR"


def _fund(layer="underwriting"):
    _seed("p-one", layer, {PRICE: 1000.0, NOI: 60.0, DEBT: 600.0, IRR: 0.12})
    _seed("p-two", layer, {PRICE: 3000.0, NOI: 120.0, DEBT: 1500.0, IRR: 0.18})


def test_sums_and_provenance():
    from portfolio import compute_rollup
    _fund()
    pf = compute_rollup(["p-one", "p-two"], layer="underwriting")
    cell = pf["rollup"]["purchase_price"]
    assert cell["value"] == 4000.0
    assert cell["method"] == "sum"
    # per-cell provenance: every input carries property, file, sheet, cell
    assert [i["property_id"] for i in cell["inputs"]] == ["p-one", "p-two"]
    assert all(i["sheet"] and i["cell"] and i["source_file"] for i in cell["inputs"])
    assert cell["flags"] == []


def test_ratio_is_derived_from_sums_not_averaged():
    from portfolio import compute_rollup
    _fund()
    pf = compute_rollup(["p-one", "p-two"], layer="underwriting")
    cap = pf["rollup"]["going_in_cap_rate"]
    # (60+120)/(1000+3000) = 4.5% — NOT the average of 6% and 4% (5%)
    assert cap["value"] == pytest.approx(0.045)
    assert "WEIGHTED_FALLBACK" not in cap["flags"]
    assert cap["derived_from"]["properties"] == ["p-one", "p-two"]


def test_irr_reported_as_range_only():
    from portfolio import compute_rollup
    _fund()
    pf = compute_rollup(["p-one", "p-two"], layer="underwriting")
    irr = pf["rollup"]["levered_irr"]
    assert irr["method"] == "range"
    assert irr["value"] == {"min": 0.12, "max": 0.18}


def test_missing_metric_is_partial_never_silent():
    from portfolio import compute_rollup
    _seed("p-one", "underwriting", {PRICE: 1000.0, NOI: 60.0})
    _seed("p-two", "underwriting", {PRICE: 3000.0})  # no NOI
    pf = compute_rollup(["p-one", "p-two"], layer="underwriting")
    noi = pf["rollup"]["net_operating_income_noi"]
    assert noi["value"] == 60.0
    assert noi["missing"] == ["p-two"]
    assert "PARTIAL_ROLLUP" in noi["flags"]


def test_layer_mismatch_flagged_not_fudged():
    from portfolio import compute_rollup
    _seed("p-one", "underwriting", {PRICE: 1000.0})
    _seed("p-two", "actuals_2025", {PRICE: 3000.0})  # different layer
    pf = compute_rollup(["p-one", "p-two"], layer="underwriting")
    cell = pf["rollup"]["purchase_price"]
    # actuals value must NOT leak into an underwriting roll-up
    assert cell["value"] == 1000.0
    assert cell["missing_layer"] == ["p-two"]
    assert "LAYER_MISMATCH" in cell["flags"]


def test_disagreement_recorded_not_adjudicated():
    from ssot import load_property_ssot, save_property_ssot
    from portfolio import compute_rollup
    _seed("p-one", "underwriting", {PRICE: 1000.0})
    s = load_property_ssot("p-one")
    s["layers"]["underwriting"]["bounded_metrics"][PRICE] = {
        "metric_name": PRICE, "metric_id": "purchase_price",
        "normalized_value": 1100.0, "status": "verified",
        "source_sheet": "S9", "source_cell": "C3",
    }
    save_property_ssot("p-one", s)
    pf = compute_rollup(["p-one"], layer="underwriting")
    # bounded (verified) value used; conflict recorded verbatim
    assert pf["rollup"]["purchase_price"]["value"] == 1100.0
    assert len(pf["disagreements"]) == 1
    d = pf["disagreements"][0]
    assert d["bounded_value"] == 1100.0 and d["structured_value"] == 1000.0
    assert "not adjudicated" in d["note"]


def test_equity_multiple_not_aggregated():
    from portfolio import compute_rollup
    _fund()
    pf = compute_rollup(["p-one", "p-two"], layer="underwriting")
    em = pf["rollup"]["equity_multiple"]
    assert em["value"] is None
    assert "NOT_AGGREGATED" in em["flags"]


def test_rollup_persists_with_version(_isolated_assets):
    from portfolio import compute_rollup, ROLLUP_VERSION
    from ssot import load_portfolio
    _fund()
    compute_rollup(["p-one", "p-two"], layer="underwriting")
    pf = load_portfolio()
    assert pf["rollup_version"] == ROLLUP_VERSION
    assert pf["properties"] == ["p-one", "p-two"]
    assert pf["rollup_layer"] == "underwriting"
