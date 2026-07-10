"""
tier2_read.py — GPT summary read when the cash-flow oracle can't validate.

Tier 1: the deterministic engine reproduces the stated IRR from a cash-flow stream —
fully validated (the acquisition/performance read).
Tier 2 (this module): the oracle can't find/validate an engine, BUT the summary tab
still carries the economics (people rush the summary and skip a clean model). GPT
reads the declaration sheets into the SAME fact-sheet shape the UI renders, flagged
`mode="gpt_read"` / `validated=False` so it shows the full grid under a clear
"not IRR-validated" banner.
Tier 3: not even the summary is legible → caller falls back to `limited` mode.

Everything here is best-effort and clearly labelled — nothing produced is presented as
validated. Returns None when the LLM is unavailable or nothing usable comes back, so
the caller cleanly degrades to limited mode (today's behaviour, unchanged).
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

from scenarios._llm import get_client, MODEL, REASONING_EFFORT

log = logging.getLogger("fb.tier2")
if not log.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("[fb.tier2] %(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

TIER2_VERSION = "tier2_read.v1"

_SYSTEM = """\
You read a real-estate underwriting SUMMARY into structured facts. The deterministic
engine could not validate a cash-flow model, so you are the fallback — read the
summary/assumptions text (every value is cell-referenced) and report the deal's
headline economics. Report ONLY what the summary actually shows; omit anything you
can't find (do not guess). Full dollars unless the sheet declares thousands/millions;
ratios (caps, LTV, LTC) as decimals (5.0% -> 0.05).

Return ONLY valid JSON (no prose, no fences) with this schema; use null for anything
absent:
{
 "property_type": "<Multifamily|Office|Retail|Hotel|Industrial|Mixed-Use|null>",
 "unit_count": <number|null>,
 "deal_type": "<acquisition|development|value-add|redevelopment|null>",
 "hold_months": <number|null>,
 "financing": "<fixed|floating|null>",
 "purchase_price": <number|null>,
 "total_cost": <number|null>,
 "debt": <number|null>, "equity": <number|null>,
 "ltv": <number|null>, "ltc": <number|null>,
 "noi_going_in": <number|null>, "noi_stabilized": <number|null>,
 "exit_value": <number|null>, "exit_cap": <number|null>, "going_in_cap": <number|null>,
 "levered_irr": <number|null>, "unlevered_irr": <number|null>, "equity_multiple": <number|null>,
 "notes": "<one sentence on what this model is / what's missing>"
}
"""


def _f(d: dict, k: str):
    v = d.get(k)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def build_gpt_read(file_path: str | Path, model_filename: str, sid: str, dt: dict) -> dict | None:
    """GPT-read the summary into a fact-sheet payload, or None to fall back to limited."""
    file_path = Path(file_path)
    client = get_client()
    if client is None:
        return None

    from fact_review import _summary_text
    summary = _summary_text(file_path)
    if not summary:
        return None

    try:
        resp = client.chat.completions.create(
            model=MODEL, reasoning_effort=REASONING_EFFORT,
            messages=[{"role": "system", "content": _SYSTEM},
                      {"role": "user", "content": f"SUMMARY SHEET TEXT:\n{summary}"}],
        )
        raw = (resp.choices[0].message.content or "").strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            raw = raw[4:] if raw.startswith("json") else raw
        g = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        log.warning("[%s] tier-2 read failed (%s: %s)", sid, type(e).__name__, e)
        return None
    if not isinstance(g, dict):
        return None

    # Property identity: prefer the deterministic reader (it just got the units fix);
    # fall back to GPT's type/units only where the reader found nothing.
    prop = None
    try:
        from property_id import property_identity
        prop = property_identity(file_path)
    except Exception:
        prop = None
    if prop and g.get("property_type") and (prop.get("type") or {}).get("status") == "not_found":
        prop["type"] = {"value": g["property_type"], "source": "gpt-read", "status": "found"}

    hold_m = _f(g, "hold_months")
    targets = {
        "purchase_price": _f(g, "purchase_price"), "total_cost": _f(g, "total_cost"),
        "debt": _f(g, "debt"), "equity": _f(g, "equity"),
        "ltv": _f(g, "ltv"), "ltc": _f(g, "ltc"),
        "sale_price": _f(g, "exit_value"), "exit_cap": _f(g, "exit_cap"),
        "going_in_cap": _f(g, "going_in_cap"),
        "levered_irr": _f(g, "levered_irr"), "unlevered_irr": _f(g, "unlevered_irr"),
        "levered_em": _f(g, "equity_multiple"),
        "dscr_health": None,
    }
    fs = {
        "ok": True, "mode": "gpt_read", "validated": False, "version": TIER2_VERSION,
        "banner": "GPT-read from the summary tab — NOT IRR-validated (no cash-flow "
                  "engine could be reproduced). Treat figures as the model's stated "
                  "numbers, not independently verified.",
        "reason": dt.get("reason"),
        "deal": {
            "property": prop,
            "archetype": {"label": g.get("deal_type") or "unclassified",
                          "confidence": "gpt-read", "signals": g.get("notes") or ""},
            "strategy": {"deal_type": g.get("deal_type"),
                         "hold": {"months": hold_m, "years": round(hold_m / 12, 1) if hold_m else None},
                         "financing": g.get("financing"),
                         "rate": {"spread": None, "floor": None, "capped": False}},
            "targets": targets,
        },
        "operating": {"noi": {"going_in": _f(g, "noi_going_in"), "exit": _f(g, "noi_stabilized")},
                      "occupancy": None},
    }

    def _M(v):
        return f"${v/1e6:.1f}M" if isinstance(v, (int, float)) else "—"

    read_md = "\n".join([
        f"## {Path(model_filename).name.split('__', 1)[-1]} — summary read",
        "",
        "> ⚠️ **Not IRR-validated.** No cash-flow engine could be reproduced, so these are "
        "the summary tab's stated figures, read by GPT — not independently verified.",
        "",
        (g.get("notes") or ""),
        "",
        f"- **Acquisition price:** {_M(targets['purchase_price'])}",
        f"- **Total cost:** {_M(targets['total_cost'])}",
        f"- **Debt:** {_M(targets['debt'])}",
        f"- **Exit:** {_M(targets['sale_price'])}"
        + (f" @ {targets['exit_cap']*100:.1f}% cap" if targets['exit_cap'] else ""),
        f"- **Levered IRR:** {targets['levered_irr']*100:.1f}%" if targets['levered_irr'] else "- **Levered IRR:** —",
        "",
        "Ask about any sheet and I'll read it directly.",
    ])
    log.info("[%s] tier-2 GPT read built (type=%s cost=%s exit=%s)", sid,
             g.get("property_type"), targets["total_cost"], targets["sale_price"])
    return {"session_id": sid, "mode": "gpt_read", "read_md": read_md,
            "detail_md": None, "fact_sheet": fs}
