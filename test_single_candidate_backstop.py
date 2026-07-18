"""
Tests for the M0 single-candidate backstop, LLM mocked so they run offline/free.

The gap being closed: resolve_metric() marks a record "verified" when exactly
one candidate passes schema validation, and the Phase 2 GPT resolver only fires
on multi-candidate pools — so a single in-range candidate from the wrong row
shipped as verified with no reconciliation. The backstop must:

  1. leave pool-resolved / multi-candidate records alone
  2. corroborate deterministically (cross-sheet value agreement) without a model call
  3. otherwise challenge with one adversarial GPT read — reject downgrades to
     suspicious, accept stays verified, and the record says which happened
  4. NEVER substitute a value (flag, don't fix)
  5. fail open but VISIBLY when the LLM is unavailable or the call errors
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import metric_resolver_gpt as mrg
from metric_resolver import (
    is_lone_verified,
    passing_candidates,
    corroborate_lone_candidate,
)

FAKE_PATH = Path("/nonexistent/model.xlsx")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeMsg:
    def __init__(self, content): self.content = content; self.tool_calls = None


class _FakeChoice:
    def __init__(self, content): self.message = _FakeMsg(content); self.finish_reason = "stop"


class _FakeResp:
    def __init__(self, content): self.choices = [_FakeChoice(content)]


class _FakeClient:
    """Returns a fixed JSON string; counts calls so tests can assert no-call paths."""
    def __init__(self, payload: dict | None = None, raise_exc: bool = False):
        self._payload = json.dumps(payload or {})
        self._raise = raise_exc
        self.calls = 0
        self.chat = self
        self.completions = self

    def create(self, *a, **k):
        self.calls += 1
        if self._raise:
            raise RuntimeError("simulated API failure")
        return _FakeResp(self._payload)


class _FakeWorkbook:
    sheetnames: list = []
    def close(self): pass


def _cand(value, sheet, cell, passes=True, confidence="high"):
    return {
        "value": value, "sheet": sheet, "value_cell": cell,
        "label_cell": cell, "passes_validation": passes,
        "confidence": confidence, "sheet_tier": 1,
    }


def _record(status="verified", candidates=None, raw_value=1_000_000,
            sheet="Summary", cell="C5"):
    return {
        "metric_id": "m1", "metric_name": "Purchase Price",
        "raw_value": raw_value, "normalized_value": raw_value,
        "display_value": "$1.00M", "status": status,
        "source_sheet": sheet, "source_cell": cell,
        "validation_notes": [], "candidates": candidates or [],
        "audit": {"accepted": None, "rejected": [], "conflicts": []},
    }


METRIC = {"metric_id": "m1", "metric_name": "Purchase Price",
          "unit": "USD", "period": "at_close", "definition": "Contract purchase price",
          "preferred_sheets": ["summary"]}


def _patch(monkeypatch, client=None, llm=True):
    monkeypatch.setattr(mrg, "client", client or _FakeClient())
    monkeypatch.setattr(mrg, "llm_available", lambda: llm)
    monkeypatch.setattr(mrg, "safe_load_workbook", lambda *a, **k: _FakeWorkbook())


# ---------------------------------------------------------------------------
# Guards — the backstop must not touch records outside its risk profile
# ---------------------------------------------------------------------------

def test_multi_candidate_record_untouched(monkeypatch):
    """>=2 passing candidates = pool territory (GPT-adjudicated); no challenge."""
    fake = _FakeClient({"verdict": "reject"})
    _patch(monkeypatch, client=fake)
    rec = _record(candidates=[_cand(1_000_000, "Summary", "C5"),
                              _cand(2_000_000, "Returns", "D9")])
    out = mrg.challenge_single_candidate(rec, METRIC, FAKE_PATH)
    assert out["status"] == "verified"
    assert "challenge" not in out["audit"]
    assert fake.calls == 0


def test_non_verified_record_untouched(monkeypatch):
    _patch(monkeypatch)
    rec = _record(status="suspicious", candidates=[_cand(1_000_000, "Summary", "C5")])
    out = mrg.challenge_single_candidate(rec, METRIC, FAKE_PATH)
    assert out["status"] == "suspicious"
    assert "challenge" not in out["audit"]


def test_is_lone_verified_profile():
    assert is_lone_verified(_record(candidates=[_cand(1, "S", "A1")]))
    # Section-reader records carry no candidates — never lone-verified
    assert not is_lone_verified(_record(candidates=[]))
    assert not is_lone_verified(_record(candidates=[_cand(1, "S", "A1"),
                                                    _cand(2, "T", "B2")]))


# ---------------------------------------------------------------------------
# Step 1 — deterministic corroboration (no model call)
# ---------------------------------------------------------------------------

def test_cross_sheet_agreement_corroborates_without_gpt(monkeypatch):
    """A failed-validation candidate on ANOTHER sheet with the same value is
    independent evidence; the record stays verified and GPT is never called."""
    fake = _FakeClient({"verdict": "reject"})  # would reject if consulted
    _patch(monkeypatch, client=fake)
    rec = _record(candidates=[
        _cand(1_000_000, "Summary", "C5", passes=True),
        _cand(1_002_000, "Sources & Uses", "B12", passes=False),  # within 1%
    ])
    out = mrg.challenge_single_candidate(rec, METRIC, FAKE_PATH)
    assert out["status"] == "verified"
    assert out["audit"]["challenge"]["method"] == "cross_sheet_corroboration"
    assert fake.calls == 0
    assert any("corroborated" in n for n in out["validation_notes"])


def test_same_sheet_agreement_does_not_corroborate():
    """Two cells on the SAME sheet agreeing is not independent evidence."""
    rec = _record(candidates=[
        _cand(1_000_000, "Summary", "C5", passes=True),
        _cand(1_000_000, "Summary", "C50", passes=False),
    ])
    assert corroborate_lone_candidate(rec) is None


def test_disagreeing_candidate_does_not_corroborate():
    rec = _record(candidates=[
        _cand(1_000_000, "Summary", "C5", passes=True),
        _cand(5_400_000, "Returns", "B12", passes=False),
    ])
    assert corroborate_lone_candidate(rec) is None


# ---------------------------------------------------------------------------
# Step 2 — adversarial challenge
# ---------------------------------------------------------------------------

def test_challenge_reject_downgrades_to_suspicious(monkeypatch):
    _patch(monkeypatch, client=_FakeClient({
        "verdict": "reject",
        "reasoning": "column header says Exit NOI; going-in was requested",
        "confidence": "high"}))
    rec = _record(candidates=[_cand(1_000_000, "Summary", "C5")])
    out = mrg.challenge_single_candidate(rec, METRIC, FAKE_PATH)
    assert out["status"] == "suspicious"
    assert out["audit"]["challenge"]["verdict"] == "reject"
    assert any("REJECTED" in n for n in out["validation_notes"])
    # Flag, never fix: the value must be untouched for the human to see
    assert out["raw_value"] == 1_000_000
    assert out["source_cell"] == "C5"


def test_challenge_accept_stays_verified_with_note(monkeypatch):
    _patch(monkeypatch, client=_FakeClient({
        "verdict": "accept",
        "reasoning": "row label 'Purchase Price' on the summary tab",
        "confidence": "high"}))
    rec = _record(candidates=[_cand(1_000_000, "Summary", "C5")])
    out = mrg.challenge_single_candidate(rec, METRIC, FAKE_PATH)
    assert out["status"] == "verified"
    assert out["audit"]["challenge"]["verdict"] == "accept"
    assert any("challenged and accepted" in n for n in out["validation_notes"])


# ---------------------------------------------------------------------------
# Fail open, visibly
# ---------------------------------------------------------------------------

def test_llm_unavailable_leaves_verified_but_annotated(monkeypatch):
    _patch(monkeypatch, llm=False)
    rec = _record(candidates=[_cand(1_000_000, "Summary", "C5")])
    out = mrg.challenge_single_candidate(rec, METRIC, FAKE_PATH)
    assert out["status"] == "verified"
    assert out["audit"]["challenge"]["verdict"] == "unchallenged"
    assert any("UNCHALLENGED" in n for n in out["validation_notes"])


def test_api_failure_fails_open_with_note(monkeypatch):
    _patch(monkeypatch, client=_FakeClient(raise_exc=True))
    rec = _record(candidates=[_cand(1_000_000, "Summary", "C5")])
    out = mrg.challenge_single_candidate(rec, METRIC, FAKE_PATH)
    assert out["status"] == "verified"
    assert out["audit"]["challenge"]["verdict"] == "unchallenged"
    assert any("UNCHALLENGED" in n for n in out["validation_notes"])
