"""
investment_intel.py — Layer 3, Investment Intelligence (the reasoning layer).

Layers 1 and 2 already exist:
    Layer 1  workbook_orientation.py   — read like an IC member (analyst stack)
    Layer 2  model_brief + trust_engine — grounded facts + per-fact confidence

This module is Layer 3 from the Deal Analysis Intel Guideline: it answers
"what do the facts MEAN?". It runs AFTER the trust engine, reasons ONLY over
verified facts, and produces the deal's "Initial View".

The anti-filler design — same trick the rest of the engine uses (code grounds,
GPT narrates):

    compute_analytics(facts)  -> deterministic deal analytics (NOI bridge,
                                 value-creation decomposition, return-source
                                 attribution, leverage accretion, fragility
                                 flags). Pure arithmetic over verified facts.
    interpret(analytics, ...)  -> ONE gpt-4o call that EXPLAINS the computed
                                 deltas. It may not invent or recompute numbers;
                                 every adjective must trace to an analytic.

So "strong returns" can never appear on its own — the model is handed
"71% of the gain is operations-driven, exit cap held flat" and told to explain
that. When no API key is present a deterministic template renders the same
analytics as prose, so Layer 3 always produces something and is fully testable
headless.

Output of build_investment_view():
    {
      "version": INTEL_VERSION,
      "analytics": {...},          # the computed deltas + flags
      "view_md": "<markdown>",     # the Initial View section
      "llm": bool,                 # whether GPT wrote it or the fallback did
    }
"""
from __future__ import annotations

import logging
import sys
from typing import Any

from metric_resolver import parse_numeric_value
from scenarios._llm import client, MODEL, llm_available

log = logging.getLogger("fb.intel")
if not log.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("[fb.intel] %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

INTEL_VERSION = "2026-06-13.1"

# ---------------------------------------------------------------------------
# Concept mapping — turn a free-form fact field into the analytic role it plays.
# First match wins; EXIT variants and UNLEVERED are listed before their
# going-in / levered counterparts so substrings don't mis-bind
# ("unlevered irr" contains "levered irr"; "exit cap" contains "cap").
# ---------------------------------------------------------------------------
_CONCEPTS: list[tuple[str, tuple[str, ...]]] = [
    ("exit_noi",       ("exit noi", "terminal noi", "forward noi", "reversion noi",
                        "stabilized noi", "stabilised noi")),
    ("going_in_noi",   ("net operating income", "going-in noi", "going in noi",
                        "in-place noi", "year 1 noi", "year-1 noi", "noi")),
    ("exit_cap",       ("exit cap", "terminal cap", "disposition cap", "reversion cap")),
    ("going_in_cap",   ("going-in cap", "going in cap", "entry cap", "in-place cap",
                        "acquisition cap", "cap rate")),
    ("exit_value",     ("exit value", "terminal value", "gross sale", "sale price",
                        "reversion value", "disposition value", "gross exit")),
    ("purchase_price", ("purchase price", "acquisition price", "acquisition cost")),
    ("total_basis",    ("total project cost", "total basis", "total capitalization",
                        "all-in basis", "total uses", "total cost")),
    ("yield_on_cost",  ("yield on cost", "yield-on-cost", "development yield",
                        "untrended yield", "stabilized yield")),
    ("unlevered_irr",  ("unlevered irr", "unleveraged irr", "unlevered return")),
    ("levered_irr",    ("levered irr", "leveraged irr", "project irr", "irr")),
    ("equity_multiple",("equity multiple", "moic", "multiple on invested", "em")),
    ("cash_on_cash",   ("cash-on-cash", "cash on cash", "cash yield", "average cash")),
    ("debt",           ("loan amount", "debt amount", "total debt", "mortgage",
                        "senior loan", "debt proceeds")),
    ("equity",         ("equity required", "total equity", "equity invested",
                        "peak equity", "equity proceeds")),
    ("ltc",            ("loan-to-cost", "loan to cost", "ltc")),
    ("ltv",            ("loan-to-value", "loan to value", "ltv")),
    ("dscr",           ("dscr", "debt service coverage", "debt coverage")),
    ("debt_yield",     ("debt yield",)),
    ("interest_spread",("interest rate spread", "loan spread", "credit spread", "spread")),
    ("rate_cap",       ("interest rate cap", "rate cap", "cap strike")),
    ("interest_rate",  ("interest rate", "all-in rate", "coupon", "fixed rate")),
    ("hold_period",    ("hold period", "hold years", "investment period", "term (years)")),
    ("size",           ("total keys", "keys", "total units", "units", "total sf",
                        "square feet", "rentable sf", "net rentable")),
]

# Concepts expressed as a rate/fraction (a value > 1.5 means it was given in
# percent points, e.g. 6.0 -> 0.06). Ratios/multiples (DSCR, EM) are NOT here.
_RATE_CONCEPTS = {
    "going_in_cap", "exit_cap", "yield_on_cost", "levered_irr", "unlevered_irr",
    "cash_on_cash", "ltv", "ltc", "debt_yield", "interest_spread", "rate_cap",
    "interest_rate",
}

# Institutional fragility thresholds — flags to CHECK, not verdicts. Documented
# so the panel can show why a flag fired.
_HIGH_LTV = 0.70
_HIGH_LTC = 0.75
_LOW_DSCR = 1.25
_LOW_DEBT_YIELD = 0.08
_THIN_COC = 0.05


def _concept_of(field: str) -> str | None:
    f = (field or "").lower()
    for concept, kws in _CONCEPTS:
        if any(kw in f for kw in kws):
            return concept
    return None


def _magnitude(fact: dict) -> float | None:
    """Real-world magnitude of a fact: parse the human display first ('$224.1M'
    -> 224100000, '6.0%' -> 0.06), falling back to the literal value."""
    for key in ("display", "value"):
        raw = fact.get(key)
        if raw in (None, "", "-", "—"):
            continue
        num, ok = parse_numeric_value(raw)
        if ok and isinstance(num, (int, float)) and not isinstance(num, bool):
            return float(num)
    return None


def _rate(v: float | None) -> float | None:
    """Normalize a rate to a fraction: 6.0 -> 0.06, 0.06 -> 0.06."""
    if v is None:
        return None
    return v / 100.0 if abs(v) > 1.5 else v


def _collect(facts: list[dict]) -> dict[str, dict]:
    """First verified fact per concept -> {value, display, sheet, cell}. Rate
    concepts are normalized to fractions; everything else keeps its magnitude."""
    out: dict[str, dict] = {}
    for f in facts:
        c = _concept_of(f.get("field", ""))
        if not c or c in out:
            continue
        mag = _magnitude(f)
        if mag is None:
            continue
        val = _rate(mag) if c in _RATE_CONCEPTS else mag
        out[c] = {
            "value": val,
            "display": f.get("display", f.get("value")),
            "sheet": f.get("sheet"),
            "cell": f.get("cell"),
            "field": f.get("field"),
        }
    return out


# ---------------------------------------------------------------------------
# Deterministic analytics
# ---------------------------------------------------------------------------

def _src(c: dict, concept: str) -> str | None:
    info = c.get(concept)
    if not info:
        return None
    s, cell = info.get("sheet"), info.get("cell")
    return f"{s}!{cell}" if s and cell else None


def compute_analytics(facts: list[dict]) -> dict[str, Any]:
    """
    Decompose the deal from its verified facts. Pure arithmetic — no LLM. Each
    block is guarded so a missing input never kills the rest. Returns a dict of
    computed metrics, a list of human-readable `lines`, the `flags`, and the
    `inputs` (concept -> value) it had to work with.
    """
    c = {k: v["value"] for k, v in _collect(facts).items()}
    coll = _collect(facts)
    out: dict[str, Any] = {"inputs": c, "lines": [], "flags": [], "missing": []}
    lines: list[str] = out["lines"]
    flags: list[dict] = out["flags"]

    P = c.get("purchase_price")
    basis = c.get("total_basis")
    c0 = c.get("going_in_cap")
    c1 = c.get("exit_cap")
    N0 = c.get("going_in_noi")
    N1 = c.get("exit_noi")
    V1 = c.get("exit_value")

    # Derive the missing pricing leg from the cap identity where possible.
    derived: set[str] = set()
    if N0 is None and P and c0:
        N0 = P * c0
        derived.add("going_in_noi")
    if N1 is None and V1 and c1:
        N1 = V1 * c1
        derived.add("exit_noi")
    if V1 is None and N1 and c1:
        V1 = N1 / c1
        derived.add("exit_value")
    entry_val = (N0 / c0) if (N0 and c0) else P

    # --- Cap rate spread (entry vs exit) ---------------------------------
    if c0 and c1:
        spread_bps = (c1 - c0) * 10000
        out["cap_spread_bps"] = round(spread_bps, 0)
        stance = ("assumes EXPANSION (conservative)" if spread_bps > 10 else
                  "assumes COMPRESSION (aggressive)" if spread_bps < -10 else
                  "holds the cap ~flat")
        lines.append(f"Exit cap {_pct(c1)} vs going-in {_pct(c0)} → {_bps(spread_bps)}; {stance}.")
        if spread_bps < -10:
            flags.append({"code": "relies_on_compression",
                          "label": "Return relies on cap compression",
                          "detail": f"exit cap {_pct(c1)} is below going-in {_pct(c0)} ({_bps(spread_bps)})"})

    # --- NOI bridge -------------------------------------------------------
    if N0 and N1:
        growth = N1 / N0 - 1
        out["noi_growth_pct"] = round(growth, 4)
        hold = c.get("hold_period")
        cagr_s = ""
        if hold and hold > 0 and N1 > 0 and N0 > 0:
            cagr = (N1 / N0) ** (1 / hold) - 1
            out["noi_cagr_pct"] = round(cagr, 4)
            cagr_s = f" ({_pct(cagr)}/yr over {hold:g}y)"
        lines.append(f"NOI bridge: {_money(N0)} → {_money(N1)}, {_pct(growth)} growth{cagr_s}.")

    # --- Value-creation decomposition (operations vs revaluation) --------
    if N0 and N1 and c0 and c1:
        ops = (N1 - N0) / c0                 # NOI growth at entry cap
        reval = N1 * (1 / c1 - 1 / c0)       # cap movement at exit NOI
        total = ops + reval
        out["value_bridge"] = {
            "operations": round(ops, 0), "revaluation": round(reval, 0),
            "total": round(total, 0),
        }
        if abs(total) > 1:
            ops_sh, reval_sh = ops / total, reval / total
            out["value_bridge"]["operations_share"] = round(ops_sh, 3)
            out["value_bridge"]["revaluation_share"] = round(reval_sh, 3)
            lines.append(
                f"Value bridge: {_money(total)} gain ≈ {_money(ops)} operations "
                f"({_pct(ops_sh)}) + {_money(reval)} revaluation ({_pct(reval_sh)})."
            )
            if reval_sh > 0.5:
                flags.append({"code": "exit_dependent",
                              "label": "Gain is exit/revaluation-driven",
                              "detail": f"{_pct(reval_sh)} of the value gain comes from the exit, not operations"})

    # --- Development / value-add spread (create vs buy) ------------------
    yoc = c.get("yield_on_cost")
    if yoc and c1:
        dev_bps = (yoc - c1) * 10000
        out["dev_spread_bps"] = round(dev_bps, 0)
        lines.append(f"Development spread: yield-on-cost {_pct(yoc)} vs exit cap {_pct(c1)} → {_bps(dev_bps)}.")

    # --- Leverage accretion ----------------------------------------------
    lev, unlev = c.get("levered_irr"), c.get("unlevered_irr")
    if lev is not None and unlev is not None:
        acc = (lev - unlev) * 100
        out["leverage_accretion_pts"] = round(acc, 1)
        verdict = "accretive" if acc > 0.2 else "dilutive" if acc < -0.2 else "neutral"
        lines.append(f"Leverage: levered IRR {_pct(lev)} vs unlevered {_pct(unlev)} → {acc:+.1f} pts ({verdict}).")
        if acc < -0.2:
            flags.append({"code": "leverage_dilutive",
                          "label": "Leverage is dilutive",
                          "detail": f"levered IRR is below unlevered by {abs(acc):.1f} pts"})

    # --- Basis per unit ---------------------------------------------------
    size = c.get("size")
    base_for_unit = basis or P
    if base_for_unit and size and size > 0:
        per = base_for_unit / size
        out["basis_per_unit"] = round(per, 0)
        lines.append(f"Basis per unit: {_money(per)} ({_money(base_for_unit)} / {size:,.0f}).")

    # --- Debt fragility flags --------------------------------------------
    rate_type = next((str(f.get("value")) for f in facts
                      if f.get("concept") == "rate_type"
                      or str(f.get("field", "")).strip().lower() in ("rate type", "interest rate type")),
                     None)
    floating = (c.get("interest_spread") is not None or c.get("rate_cap") is not None
                or rate_type == "floating")
    if floating:
        if c.get("interest_spread") is not None or c.get("rate_cap") is not None:
            cap_note = f", cap {_pct(c['rate_cap'])}" if c.get("rate_cap") else " (no cap found)"
            detail = (f"spread {_pct(c['interest_spread'])}{cap_note}" if c.get("interest_spread")
                      else f"rate cap present{cap_note}")
        else:
            detail = "index-linked (SOFR/LIBOR + spread); levered return rides the rate path"
        flags.append({"code": "floating_rate", "label": "Floating-rate debt", "detail": detail})
    ltv, ltc, dscr, dy = c.get("ltv"), c.get("ltc"), c.get("dscr"), c.get("debt_yield")
    if ltv and ltv > _HIGH_LTV:
        flags.append({"code": "high_ltv", "label": "High leverage",
                      "detail": f"LTV {_pct(ltv)} > {_pct(_HIGH_LTV)}"})
    if ltc and ltc > _HIGH_LTC:
        flags.append({"code": "high_ltc", "label": "High leverage (cost)",
                      "detail": f"LTC {_pct(ltc)} > {_pct(_HIGH_LTC)}"})
    if dscr and dscr < _LOW_DSCR:
        flags.append({"code": "low_dscr", "label": "Thin debt-service coverage",
                      "detail": f"DSCR {dscr:.2f}x < {_LOW_DSCR:.2f}x"})
    if dy and dy < _LOW_DEBT_YIELD:
        flags.append({"code": "low_debt_yield", "label": "Low debt yield",
                      "detail": f"debt yield {_pct(dy)} < {_pct(_LOW_DEBT_YIELD)}"})
    coc = c.get("cash_on_cash")
    if coc is not None and coc < _THIN_COC:
        flags.append({"code": "thin_cash_yield", "label": "Thin interim cash yield",
                      "detail": f"cash-on-cash {_pct(coc)} < {_pct(_THIN_COC)}"})

    # Returns passthrough (for the prose).
    if lev is not None:
        out["levered_irr"] = round(lev, 4)
    if c.get("equity_multiple") is not None:
        out["equity_multiple"] = round(c["equity_multiple"], 2)
    if c.get("hold_period") is not None:
        out["hold_period"] = c["hold_period"]

    # Note what was missing for the headline decompositions (transparency). A
    # value derived from the cap identity (e.g. going-in NOI = price × cap)
    # counts as available, not missing.
    for need, label in (("going_in_cap", "going-in cap"), ("exit_cap", "exit cap"),
                        ("going_in_noi", "going-in NOI"), ("exit_noi", "exit NOI"),
                        ("levered_irr", "levered IRR")):
        if c.get(need) is None and need not in derived:
            out["missing"].append(label)

    out["sources"] = {k: _src(coll, k) for k in coll}
    return out


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _pct(frac: float | None) -> str:
    return "—" if frac is None else f"{frac * 100:.1f}%"


def _bps(b: float) -> str:
    return f"{b:+.0f} bps"


def _money(v: float | None) -> str:
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1e9:
        return f"${v / 1e9:.2f}B"
    if a >= 1e6:
        return f"${v / 1e6:.1f}M"
    if a >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:,.0f}"


# ---------------------------------------------------------------------------
# Interpretation (the grounded GPT pass)
# ---------------------------------------------------------------------------

_INTERPRET_SYSTEM = """\
You are a senior real estate investment professional writing the INITIAL VIEW of
an acquisition for an investment committee. You are NOT extracting data — Layer 1
(navigation) and Layer 2 (grounded facts) already ran. Your job is judgment:
explain what the numbers MEAN.

You are given (1) PRE-COMPUTED ANALYTICS — deterministic decompositions of this
deal (NOI bridge, value-creation split, leverage accretion, fragility flags) —
and (2) the VERIFIED FACTS behind them, each with its Sheet!Cell.

HARD RULES:
- Explain the analytics; do NOT recompute or invent numbers. Every figure you
  cite must already appear in the analytics or the verified facts.
- PREFER FACTS OVER ADJECTIVES. Never write "strong returns" or "attractive
  deal" on their own — name the SOURCE of the return and the evidence.
    Good: "The ~18% IRR is roughly 70% revaluation-driven; the exit leans on NOI
           growth of 34% with the cap held flat, so the return depends on
           achieving stabilization, with thin interim cash yield."
    Bad:  "This is an attractive deal with strong returns."
- Separate where the return comes from: operations vs. cap-rate movement vs.
  leverage vs. exit timing. If the analytics say revaluation-driven, say so.
- Name what could BREAK the deal, grounded in the flags and sensitivities
  (exit-cap expansion, floating-rate exposure, stabilization risk, CapEx, etc.).
- If a headline decomposition is missing inputs, say briefly what's missing
  rather than guessing.

OUTPUT — markdown, tight, executive, bullets (never tables). Sections, each 1-3
bullets:

**Return composition** — operations vs revaluation vs leverage, with the split.
**Value creation** — how going-in NOI becomes exit value; is it earned or repriced.
**Leverage & debt risk** — accretive or fragile; floating/coverage/refi exposure.
**What breaks it** — the 2-3 specific things that most threaten the return.
**Initial view** — ONE sentence: is the deal paid enough for its execution and
financing risk, and on what does that hinge.

Max 220 words. No preamble, no fences.
"""


def _analytics_block(analytics: dict) -> str:
    lines = analytics.get("lines", [])
    flags = analytics.get("flags", [])
    parts = ["COMPUTED ANALYTICS:"]
    parts += [f"- {ln}" for ln in lines] or ["- (insufficient inputs to decompose)"]
    if flags:
        parts.append("FLAGS:")
        parts += [f"- {f['label']}: {f['detail']}" for f in flags]
    if analytics.get("missing"):
        parts.append("MISSING INPUTS: " + ", ".join(analytics["missing"]))
    return "\n".join(parts)


def _facts_block(facts: list[dict]) -> str:
    rows = []
    for f in facts:
        src = f"{f.get('sheet')}!{f.get('cell')}"
        rows.append(f"- {f.get('field')}: {f.get('display', f.get('value'))} ({src})")
    return "VERIFIED FACTS:\n" + ("\n".join(rows) or "(none)")


def _guardrail_block(guardrails: list[str] | None) -> str:
    """Bind the narrative to the deterministic validation results: things the
    facts do NOT support, which the model must not assert."""
    if not guardrails:
        return ""
    lines = "\n".join(f"- {g}" for g in guardrails)
    return ("\n\nBINDING CONSTRAINTS — these are non-negotiable; obey every one and "
            "never contradict them:\n" + lines)


def interpret(analytics: dict, facts: list[dict], identity: dict | None,
              guardrails: list[str] | None = None) -> str:
    """ONE gpt-4o call that explains the computed analytics. Returns markdown,
    or '' on failure (caller falls back to the deterministic template)."""
    if not llm_available():
        return ""
    ident = identity or {}
    head = (f"DEAL: {ident.get('asset') or 'Unnamed'} — {ident.get('property_type') or '?'}"
            f", {ident.get('location') or '?'}; strategy {ident.get('strategy') or '?'}.")
    user_msg = (head + "\n\n" + _analytics_block(analytics) + "\n\n"
                + _facts_block(facts) + _guardrail_block(guardrails))
    try:
        resp = client.chat.completions.create(
            model=MODEL, temperature=0.2,
            messages=[
                {"role": "system", "content": _INTERPRET_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
        )
        md = (resp.choices[0].message.content or "").strip()
        if md.startswith("```"):
            md = md.split("```")[1]
            if md.startswith("markdown"):
                md = md[8:]
        return md.strip()
    except Exception as e:
        log.error("Initial View interpretation failed: %s", e)
        return ""


def _deterministic_view(analytics: dict) -> str:
    """No-key fallback: render the analytics as prose so Layer 3 always produces
    something and is fully testable headless."""
    parts: list[str] = []
    lines = analytics.get("lines", [])
    if lines:
        parts.append("**Deal analytics**\n\n" + "\n".join(f"- {ln}" for ln in lines))
    flags = analytics.get("flags", [])
    if flags:
        parts.append("**Flags to check**\n\n"
                     + "\n".join(f"- {f['label']} — {f['detail']}" for f in flags))
    if analytics.get("missing"):
        parts.append("_Could not fully decompose — missing: "
                     + ", ".join(analytics["missing"]) + "._")
    if not parts:
        return "_Not enough verified return/pricing facts to form an initial view._"
    parts.append("_Set OPENAI_API_KEY for the written investment view._")
    return "\n\n".join(parts)


def build_investment_view(
    scored: dict, identity: dict | None = None, use_llm: bool = True,
    guardrails: list[str] | None = None,
) -> dict[str, Any]:
    """
    Layer 3 entry point. `scored` is the trust_engine.score_facts output (or a
    canonical-fact set from deal_truth). Reasons only over verified (verdict ==
    'show') facts, computes the analytics, and writes the Initial View (GPT when
    available, deterministic template otherwise). `guardrails` (from deal_truth)
    bind the narrative to what the validated facts actually support.
    """
    all_facts = scored.get("facts") or []
    verified = [f for f in all_facts if f.get("trust", {}).get("verdict") == "show"]
    analytics = compute_analytics(verified)

    md = interpret(analytics, verified, identity, guardrails) if use_llm else ""
    llm = bool(md)
    if not md:
        md = _deterministic_view(analytics)

    view_md = "### Initial View\n\n" + md
    return {
        "version": INTEL_VERSION,
        "analytics": analytics,
        "view_md": view_md,
        "llm": llm,
        "n_verified": len(verified),
    }


if __name__ == "__main__":
    # Quick self-check on a synthetic deal (no key needed).
    demo = {"facts": [
        {"field": "Purchase Price", "display": "$224.1M", "value": 224100000,
         "sheet": "One Pager", "cell": "C8", "trust": {"verdict": "show"}},
        {"field": "Going-in Cap Rate", "display": "5.5%", "value": 0.055,
         "sheet": "One Pager", "cell": "C9", "trust": {"verdict": "show"}},
        {"field": "Exit Cap Rate", "display": "6.0%", "value": 0.06,
         "sheet": "Returns", "cell": "C20", "trust": {"verdict": "show"}},
        {"field": "Exit NOI", "display": "$16.5M", "value": 16500000,
         "sheet": "Returns", "cell": "C21", "trust": {"verdict": "show"}},
        {"field": "Levered IRR", "display": "18.0%", "value": 0.18,
         "sheet": "Returns", "cell": "C30", "trust": {"verdict": "show"}},
        {"field": "Unlevered IRR", "display": "14.0%", "value": 0.14,
         "sheet": "Returns", "cell": "C31", "trust": {"verdict": "show"}},
        {"field": "DSCR", "display": "1.15x", "value": 1.15,
         "sheet": "Debt", "cell": "C12", "trust": {"verdict": "show"}},
        {"field": "Interest Rate Spread", "display": "3.2%", "value": 0.032,
         "sheet": "Debt", "cell": "C8", "trust": {"verdict": "show"}},
        {"field": "Hold Period", "display": "5 years", "value": 5,
         "sheet": "One Pager", "cell": "C11", "trust": {"verdict": "show"}},
        {"field": "Total Keys", "display": "251", "value": 251,
         "sheet": "One Pager", "cell": "C5", "trust": {"verdict": "show"}},
    ]}
    out = build_investment_view(demo, {"asset": "Demo Hotel"}, use_llm=False)
    import json as _j
    print(_j.dumps(out["analytics"], indent=2, default=str))
    print("\n" + out["view_md"])
