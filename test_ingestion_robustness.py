"""
Regression tests for WORKORDER_ingestion_robustness.md.

A readable workbook must NEVER produce an opaque failure. These pin the loader
guarantees and the /api/analyze graceful-degradation contract against a real,
pathological fixture (Westview: 8,642 defined names, 5,516 of them dead #REF!).

Run:  python -m pytest test_ingestion_robustness.py -q
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

_FIXDIR = Path(__file__).parent / "tests" / "fixtures"
FIXTURE = _FIXDIR / "Westview Austin - Model_02.19.2022.xlsx"
COLORADO = _FIXDIR / "425 Colorado Proforma_03.08.2022_Adept.xlsx"


def _run_bounded(fn, timeout: float):
    """Run fn() on a thread; raise AssertionError if it doesn't finish in time.
    So a regression that reintroduces a hang FAILS loudly instead of wedging CI."""
    import threading
    box: dict = {}
    t = threading.Thread(target=lambda: box.__setitem__("r", fn()), daemon=True)
    t.start()
    t.join(timeout)
    assert not t.is_alive(), f"call did not complete within {timeout}s (hang regression)"
    return box.get("r")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Force the LLM offline so the analyze path is deterministic, fast, and free —
    it must still return a useful result with no API key (enrichment degrades)."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    yield


def test_fixture_present():
    assert FIXTURE.exists(), f"missing regression fixture: {FIXTURE}"


def test_safe_load_read_only_is_fast():
    """AC #1: read_only open of the bloated fixture in well under a second."""
    from wb_io import safe_load_workbook
    t = time.time()
    wb = safe_load_workbook(FIXTURE)
    dt = time.time() - t
    try:
        assert len(wb.sheetnames) > 0
    finally:
        wb.close()
    assert dt < 1.0, f"read_only load took {dt:.2f}s (expected < 1s)"


def test_full_load_survives_ref_bloat_within_timeout():
    """AC #3: a workbook with thousands of #REF! names still full-loads promptly —
    the sanitize pre-pass drops the dead names so the object graph stops paying."""
    from wb_io import safe_load_workbook
    t = time.time()
    wb = safe_load_workbook(FIXTURE, data_only=True, read_only=False)
    dt = time.time() - t
    try:
        # Dead #REF! names are gone; the real ones survive.
        assert len(wb.defined_names) < 8642
    finally:
        wb.close()
    assert dt < 10.0, f"sanitized full load took {dt:.2f}s"


def test_missing_file_raises_typed_error():
    from wb_io import safe_load_workbook, WorkbookLoadError
    with pytest.raises(WorkbookLoadError):
        safe_load_workbook("does-not-exist.xlsx")


def test_timeout_raises_typed_error():
    from wb_io import safe_load_workbook, WorkbookLoadError
    with pytest.raises(WorkbookLoadError):
        safe_load_workbook(FIXTURE, read_only=False, timeout=0.001)


def test_request_cache_loads_once_per_mode():
    """AC (§6): a file is loaded at most once per mode per request, and a caller
    closing a cached workbook does not break the next reader."""
    from wb_io import safe_load_workbook, workbook_cache
    with workbook_cache():
        a = safe_load_workbook(FIXTURE)
        b = safe_load_workbook(FIXTURE)
        assert a is b, "same (path, mode) should reuse the cached workbook"
        a.close()  # neutered inside a cache — must not close the shared archive
        assert b.sheetnames, "workbook still usable after a caller called close()"


def test_limited_read_reports_identity_and_inventory():
    """AC #4: limited mode carries property identity + sheet inventory."""
    import server
    r = server._limited_read(FIXTURE, FIXTURE.name, "sid123", "note here")
    assert r["mode"] == "limited"
    assert r["fact_sheet"]["ok"] is False
    assert isinstance(r["fact_sheet"]["sheets"], list) and r["fact_sheet"]["sheets"]
    assert "note here" in r["read_md"]


def test_dscr_health_does_not_hang_on_negative_noi():
    """A development deal has negative-NOI construction months, so the trailing-12
    median DSCR can be ≤0. The unit-rescale search must NOT spin on that (the bug
    that hung 425 Colorado forever → severed request → HTTP 429 on the retry)."""
    from deal_analysis import _dscr_health
    months = [f"2022-{m:02d}" for m in range(1, 13)] + [f"2023-{m:02d}" for m in range(1, 13)]
    noi = [(m, -50_000.0) for m in months[:12]] + [(m, 200_000.0) for m in months[12:]]
    ds = [(m, 100_000.0) for m in months]
    noi_t = {"by_period": noi, "stabilized": 200_000.0}
    ds_t = {"by_period": ds, "source": "test"}
    res = _run_bounded(lambda: _dscr_health(noi_t, ds_t), timeout=10.0)
    assert res is None or res.get("available") is True


@pytest.mark.skipif(not COLORADO.exists(), reason="425 Colorado fixture not present")
def test_colorado_analyze_completes_and_never_hangs():
    """Full regression for the reported file: /api/analyze on 425 Colorado returns
    200 (full or limited) with headroom — never a hang/severed request/429."""
    from fastapi.testclient import TestClient
    import server
    client = TestClient(server.app)
    resp = _run_bounded(lambda: _analyze(client, COLORADO), timeout=90.0)
    assert resp is not None and resp.status_code == 200, \
        f"got {getattr(resp, 'status_code', 'HANG')}"
    assert resp.json().get("mode") in ("acquisition", "performance", "limited")


@pytest.mark.skipif(not COLORADO.exists(), reason="425 Colorado fixture not present")
def test_colorado_summary_facts_read_correctly():
    """Extraction accuracy on the 425 Colorado summary sheet: the total unit count,
    a total project cost that respects cost≥debt, and an exit that is the deal's own
    (~$89M) rather than a $300M sales COMP."""
    from wb_io import workbook_cache
    from deal_truth import build_deal_truth
    with workbook_cache():
        dt = build_deal_truth(COLORADO)
    can = dt["canonical"]

    def val(k):
        return float(can[k]["value"]) if k in can else None

    from property_id import property_identity
    size = property_identity(COLORADO)["size"]["value"]
    assert size and size["amount"] == 144, f"units should be the 144 total, got {size}"

    cost, debt = val("total_cost"), val("debt")
    assert cost and debt and cost >= debt, f"total cost {cost} must be ≥ debt {debt}"
    assert cost > 50e6, f"total cost should reflect the full project (~$59M), got {cost}"

    sale = val("sale_price")
    assert sale is None or sale < 150e6, f"exit must be the deal's own, not a $300M comp: {sale}"


def _analyze(client, path: Path):
    with path.open("rb") as fh:
        return client.post(
            "/api/analyze",
            files={"model": (path.name, fh,
                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )


def test_analyze_never_severs_or_422s():
    """AC #2 + #6: /api/analyze on the fixture returns 200 (full OR limited) — never
    a 422 and never a severed request."""
    from fastapi.testclient import TestClient
    import server
    client = TestClient(server.app)
    t = time.time()
    resp = _analyze(client, FIXTURE)
    dt = time.time() - t
    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert body.get("session_id")
    assert body.get("mode") in ("acquisition", "performance", "limited")
    # Comfortable headroom under any reasonable request timeout.
    assert dt < 60.0, f"analyze wall-clock {dt:.1f}s"
