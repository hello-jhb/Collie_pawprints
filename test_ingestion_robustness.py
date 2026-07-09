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

FIXTURE = Path(__file__).parent / "tests" / "fixtures" / "Westview Austin - Model_02.19.2022.xlsx"


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
