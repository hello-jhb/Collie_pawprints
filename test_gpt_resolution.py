"""
Tests for the GPT resolution layer (fact_review + tier2_read), with the LLM mocked so
they run offline/free. The point is the SAFETY of the merge — "GPT proposes, the oracle
disposes" — not the model's answers.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "tests" / "fixtures" / "425 Colorado Proforma_03.08.2022_Adept.xlsx"


class _FakeMsg:
    def __init__(self, content): self.content = content; self.tool_calls = None


class _FakeChoice:
    def __init__(self, content): self.message = _FakeMsg(content); self.finish_reason = "stop"


class _FakeResp:
    def __init__(self, content): self.choices = [_FakeChoice(content)]


class _FakeClient:
    """Returns a fixed JSON string for any chat.completions.create call."""
    def __init__(self, payload: dict):
        self._payload = json.dumps(payload)
        self.chat = self
        self.completions = self

    def create(self, *a, **k):
        return _FakeResp(self._payload)


def _patch_client(monkeypatch, module, payload):
    monkeypatch.setattr(module, "get_client", lambda: _FakeClient(payload))
    # _summary_text needs a real (non-empty) render; keep it simple/offline.
    monkeypatch.setattr(module if hasattr(module, "_summary_text") else module,
                        "_summary_text", lambda *_: "=== SHEET: Summary ===\n  K19: 29745817",
                        raising=False)


def test_review_locks_oracle_validated_facts(monkeypatch):
    """A cf-validated fact is NEVER overridden, even if GPT disagrees with high conf."""
    import fact_review
    monkeypatch.setattr(fact_review, "get_client",
                        lambda: _FakeClient({"total_cost": {"ok": False, "value": 1.0,
                                                            "cell": "X1", "confidence": "high"}}))
    monkeypatch.setattr(fact_review, "_summary_text", lambda *_: "=== SHEET: S ===\n  X1: 1")
    dt = {"canonical": {"total_cost": {"value": 54_980_000, "method": "recomputed",
                                       "cf_validated": True, "source": "stream"}}}
    out = fact_review.review_headline_facts(FIX, dt)
    assert out["total_cost"]["value"] == 54_980_000, "validated cost must stay locked"


def test_review_rejects_suggestion_that_breaks_cost_ge_debt(monkeypatch):
    """GPT suggesting the phased at-close cost (below debt) must be REJECTED — this is
    the Colorado bug we fixed deterministically; GPT must not reintroduce it."""
    import fact_review
    monkeypatch.setattr(fact_review, "get_client",
                        lambda: _FakeClient({"total_cost": {"ok": False, "value": 29_745_817,
                                                            "cell": "K19", "confidence": "high"}}))
    monkeypatch.setattr(fact_review, "_summary_text", lambda *_: "=== SHEET: S ===\n  K19: 29745817")
    dt = {"canonical": {
        "total_cost": {"value": 58_900_000, "method": "derived", "source": "debt+equity"},
        "debt": {"value": 32_469_957}}}
    out = fact_review.review_headline_facts(FIX, dt)
    assert out["total_cost"]["value"] == 58_900_000, "cost<debt suggestion must be rejected"


def test_review_adopts_confident_valid_correction(monkeypatch):
    """A confident, in-range correction to a NON-validated field is adopted, with the
    prior value retained for provenance."""
    import fact_review
    monkeypatch.setattr(fact_review, "get_client",
                        lambda: _FakeClient({"purchase_price": {"ok": False, "value": 28_800_000,
                                                                "cell": "L22", "confidence": "high"}}))
    monkeypatch.setattr(fact_review, "_summary_text", lambda *_: "=== SHEET: S ===\n  L22: 28800000")
    dt = {"canonical": {"purchase_price": {"value": 288_000, "method": "vocab", "source": "guess"}}}
    out = fact_review.review_headline_facts(FIX, dt)
    assert out["purchase_price"]["value"] == 28_800_000
    assert out["purchase_price"]["prior_value"] == 288_000
    assert out["purchase_price"]["method"] == "gpt_reviewed"


def test_review_is_noop_without_client(monkeypatch):
    import fact_review
    monkeypatch.setattr(fact_review, "get_client", lambda: None)
    dt = {"canonical": {"total_cost": {"value": 1.0, "method": "vocab"}}}
    assert fact_review.review_headline_facts(FIX, dt)["total_cost"]["value"] == 1.0


def test_render_gpt_read_does_not_crash():
    """A gpt_read fact sheet renders compactly for chat without hitting the validated
    shape (noi_bridge etc.)."""
    from interpretation import render_fact_sheet
    fs = {"ok": True, "mode": "gpt_read", "version": "t2", "banner": "unvalidated",
          "deal": {"property": None, "archetype": {"label": "development"},
                   "strategy": {"deal_type": "development", "hold": {"months": 60}, "financing": "floating"},
                   "targets": {"purchase_price": 28.8e6, "total_cost": 59e6, "debt": 32e6,
                               "sale_price": 89e6, "exit_cap": 0.0475, "levered_irr": 0.2, "levered_em": 2.2}},
          "operating": {"noi": {"going_in": 3.5e6, "exit": 4e6}}}
    out = render_fact_sheet(fs)
    assert "NOT IRR-VALIDATED" in out and "$59.0M" in out


@pytest.mark.skipif(not FIX.exists(), reason="Colorado fixture not present")
def test_read_row_series_returns_dated_monthly_series():
    """The chat depth tool: a wide monthly CF row comes back as a dated series so the
    agent can reason about timing (riskiest month, coverage dip)."""
    import shutil, os, tools
    tools.UPLOAD_DIR.mkdir(exist_ok=True)
    dst = tools.UPLOAD_DIR / "captest__425.xlsx"
    shutil.copy(FIX, dst)
    try:
        r = tools.read_row_series("captest__425.xlsx", "Budget & Draw Schedule", 111)
        assert "error" not in r, r
        assert r["n_points"] > 50, "monthly model should return many periods (not 30-col capped)"
        assert r["has_period_header"] and any(p["period"] for p in r["points"])
    finally:
        os.remove(dst)


def test_chat_question_cap(monkeypatch):
    """After CHAT_QUESTION_CAP questions the session is capped (a focused analyst, not
    an open chatbot)."""
    from fastapi.testclient import TestClient
    import server, scenarios._llm as _llm
    monkeypatch.setattr(_llm, "get_client", lambda: _FakeClient({"x": 1}))
    client = TestClient(server.app)
    sid = "capsid"
    server._SESSIONS[sid] = {"model_path": "x", "model_filename": "captest__x.xlsx",
                             "fact_sheet": {"ok": False, "reason": "n/a"}, "read_md": None,
                             "read_source": "limited", "history": None, "engine_sheet": None,
                             "phasing": None, "turns": 0}
    caps = [client.post("/api/chat", data={"session_id": sid, "message": f"q{i}"}).json().get("capped", False)
            for i in range(server.CHAT_QUESTION_CAP + 1)]
    assert caps[:server.CHAT_QUESTION_CAP] == [False] * server.CHAT_QUESTION_CAP
    assert caps[server.CHAT_QUESTION_CAP] is True, "the (cap+1)th question must be blocked"


@pytest.mark.skipif(not FIX.exists(), reason="Colorado fixture not present")
def test_tier2_builds_full_grid_shape(monkeypatch):
    """tier2 build produces a fact-sheet payload the UI grid can render, flagged
    unvalidated."""
    import tier2_read
    payload = {"property_type": "Multifamily", "unit_count": 144, "deal_type": "development",
               "hold_months": 61, "financing": "floating", "purchase_price": 28_800_000,
               "total_cost": 58_900_000, "debt": 32_469_957, "equity": 26_434_917,
               "exit_value": 88_921_402, "exit_cap": 0.0475, "levered_irr": 0.2045,
               "equity_multiple": 2.26, "noi_going_in": 3_520_935, "noi_stabilized": 4_021_520,
               "notes": "office-to-resi conversion"}
    monkeypatch.setattr(tier2_read, "get_client", lambda: _FakeClient(payload))
    r = tier2_read.build_gpt_read(FIX, "sid__425.xlsx", "sid", {"reason": "no engine"})
    assert r and r["mode"] == "gpt_read"
    fs = r["fact_sheet"]
    assert fs["ok"] is True and fs["validated"] is False and fs["banner"]
    assert fs["deal"]["targets"]["total_cost"] == 58_900_000
    assert fs["deal"]["targets"]["sale_price"] == 88_921_402
    assert "IRR-validated" in r["read_md"]
