"""
Tests for learning_store — the feed-forward memory. Uses a temp COLLIE_LEARNING_DIR so
nothing touches the real store, and verifies the promotion signal (recurring across
DISTINCT files) vs one-off noise.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture()
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLIE_LEARNING_DIR", str(tmp_path))
    monkeypatch.setenv("COLLIE_LEARNING", "1")
    import learning_store
    importlib.reload(learning_store)
    return learning_store


def test_record_and_read_roundtrip(store):
    store.record_resolution(layer="fact_review", concept="total_cost", decision=store.CORRECTED,
                            file="a.xlsx", file_hash="sha1:aaa", label="Total Uses",
                            prior_value=1.0, chosen_value=59e6, chosen_cell="debt+equity")
    ev = store.read_events()
    assert len(ev) == 1
    assert ev[0]["concept"] == "total_cost" and ev[0]["decision"] == "corrected"
    assert ev[0]["ts"] and ev[0]["file_hash"] == "sha1:aaa"


def test_disabled_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLIE_LEARNING_DIR", str(tmp_path))
    monkeypatch.setenv("COLLIE_LEARNING", "0")
    import learning_store
    importlib.reload(learning_store)
    learning_store.record_resolution(layer="x", concept="y", decision="corrected")
    assert learning_store.read_events() == []


def test_promotion_needs_multiple_distinct_files(store):
    # Same correction, but on the SAME file re-uploaded 3x → one-off, not a pattern.
    for _ in range(3):
        store.record_resolution(layer="concept_fallback", concept="ltc", decision=store.FILLED,
                                file="same.xlsx", file_hash="sha1:same", label="Loan to Cost")
    assert store.promotion_candidates(min_files=2) == []

    # The SAME label→concept on two DIFFERENT files → a promotable pattern.
    store.record_resolution(layer="concept_fallback", concept="debt_yield", decision=store.FILLED,
                            file="f1.xlsx", file_hash="sha1:f1", label="Debt Yield")
    store.record_resolution(layer="concept_fallback", concept="debt_yield", decision=store.FILLED,
                            file="f2.xlsx", file_hash="sha1:f2", label="Debt Yield")
    cands = store.promotion_candidates(min_files=2)
    assert len(cands) == 1
    assert cands[0]["concept"] == "debt_yield" and cands[0]["distinct_files"] == 2


def test_summarize_counts(store):
    store.record_resolution(layer="fact_review", concept="debt", decision=store.AGREED,
                            file="a.xlsx", file_hash="sha1:a")
    store.record_resolution(layer="fact_review", concept="total_cost", decision=store.REJECTED,
                            file="a.xlsx", file_hash="sha1:a", label="K19")
    s = store.summarize()
    assert s["total"] == 2 and s["distinct_files"] == 1
    assert s["by_decision"]["agreed"] == 1 and s["by_decision"]["rejected"] == 1


def test_fact_review_records_decisions(store, monkeypatch):
    """The review layer feeds the store: a rejected (invariant-failing) correction is
    captured — this is the loop closing on the cost<debt bug."""
    import json
    import fact_review

    class _C:
        def __init__(self, payload): self._p = json.dumps(payload); self.chat = self; self.completions = self
        def create(self, *a, **k):
            class M: content = self._p
            class Ch: message = M()
            class R: choices = [Ch()]
            return R()

    monkeypatch.setattr(fact_review, "get_client",
                        lambda: _C({"total_cost": {"ok": False, "value": 29_745_817,
                                                   "cell": "K19", "confidence": "high"}}))
    monkeypatch.setattr(fact_review, "_summary_text", lambda *_: "=== S ===\n  K19: 29745817")
    dt = {"canonical": {"total_cost": {"value": 58_900_000, "method": "derived"},
                        "debt": {"value": 32_469_957}}}
    fixture = Path(__file__).parent / "tests" / "fixtures" / "Westview Austin - Model_02.19.2022.xlsx"
    fact_review.review_headline_facts(fixture if fixture.exists() else __file__, dt)
    ev = store.read_events()
    rejected = [e for e in ev if e["decision"] == "rejected" and e["concept"] == "total_cost"]
    assert rejected, "the rejected cost<debt suggestion must be recorded for the loop"
    assert rejected[0]["chosen_value"] == 29_745_817
