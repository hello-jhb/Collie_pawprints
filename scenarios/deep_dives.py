"""
deep_dives.py — focused deep-dive sections for the Deal Review workspace.

Each function generates a single thematic section the user can request
on-demand via UI buttons:

    - Capital Structure
    - Cash Flow / NOI Trajectory
    - Return Profile
    - CapEx Plan
    - Key Risks

Each deep dive reads from the same verified SSOT (bounded_metrics, raw_insights,
time series) the main memo uses, but with a tighter prompt scoped to its topic.
This keeps the main memo short (snapshot + thesis + appendix) while letting
the user drill into any aspect with one click.
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any

import ssot
from scenarios._llm import complete, llm_available, log
from flexible_extractor import extract_time_series_rows


UPLOAD_DIR = Path("uploads")


# ---------------------------------------------------------------------------
# Shared context builders
# ---------------------------------------------------------------------------

_NAME_TO_ID: dict[str, str] | None = None


def _name_to_id() -> dict[str, str]:
    """Lazy normalized {name-or-alias: metric_id} index over the catalog."""
    global _NAME_TO_ID
    if _NAME_TO_ID is None:
        from metric_catalog import load_metric_catalog
        from flexible_extractor import normalize_text
        idx: dict[str, str] = {}
        for m in load_metric_catalog():
            idx[normalize_text(m["metric_name"])] = m["metric_id"]
            for a in m.get("aliases", []) or []:
                idx.setdefault(normalize_text(a), m["metric_id"])
        _NAME_TO_ID = idx
    return _NAME_TO_ID


def _resolve_bounded(bounded: dict, name: str) -> dict | None:
    """
    Look up a bounded record by canonical name, falling back to an alias/id
    match so a key mismatch can never silently drop a metric (e.g. Levered IRR
    stored under a non-canonical key).
    """
    rec = bounded.get(name)
    if rec:
        return rec
    from flexible_extractor import normalize_text
    mid = _name_to_id().get(normalize_text(name))
    if mid:
        for r in bounded.values():
            if r.get("metric_id") == mid:
                return r
    return None


def _ts_block(file_path, keywords: tuple[str, ...], max_rows: int = 16,
              header: str = "Time series (relevant rows):") -> str:
    """
    Parser-backed time-series block filtered to rows whose label matches any
    keyword. Authoritative periodicity + annualization come from the model
    parser; falls back to the heuristic extractor when no tables are found.
    """
    try:
        from financial_model_parser import build_time_series
        ts = build_time_series(file_path) or extract_time_series_rows(file_path)
    except Exception:
        return ""
    rel = [r for r in ts if any(k in r["label"].lower() for k in keywords)][:max_rows]
    if not rel:
        return ""
    lines = ["", header]
    for s in rel:
        values = s.get("annual_values") or s.get("values") or []
        headers = s.get("annual_headers") or s.get("headers") or []
        if s.get("annualized"):
            meta = f" [annualized from {s.get('periodicity')}; {s.get('aggregation_method')}]"
        elif s.get("periodicity"):
            meta = f" [{s.get('periodicity')}]"
        else:
            meta = ""
        vals = " | ".join(
            f"{v:,.0f}" if isinstance(v, (int, float)) and v else "—"
            for v in values[:8]
        )
        header_str = " | ".join(str(h) for h in headers[:8])
        lines.append(f"  [{s['sheet']}] {s['label']}{meta}: {header_str} => {vals}")
    return "\n".join(lines)


def _bounded_pretty(bounded: dict, metric_names: list[str]) -> str:
    """Pretty-print a subset of bounded metrics for a deep-dive prompt."""
    if not bounded:
        return "(no bounded metrics extracted)"
    lines = []
    for name in metric_names:
        rec = _resolve_bounded(bounded, name)
        if not rec:
            lines.append(f"  - {name}: MISSING")
            continue
        status = rec.get("status")
        val = rec.get("display_value", "—")
        sheet = rec.get("source_sheet")
        cell = rec.get("source_cell")
        cell_ref = f"{sheet}!{cell}" if sheet and cell else "—"
        if status in ("verified", "candidate_pool"):
            lines.append(f"  - **{name}**: {val} ({cell_ref})")
        elif status == "suspicious":
            notes = "; ".join((rec.get("validation_notes") or []))[:100]
            lines.append(f"  - **{name}**: SUSPICIOUS — {notes}")
        else:
            lines.append(f"  - **{name}**: —")
    return "\n".join(lines)


def _load_uw_layer() -> dict[str, Any] | None:
    s = ssot.load_ssot()
    return s["layers"].get("underwriting")


# ---------------------------------------------------------------------------
# Capital Structure
# ---------------------------------------------------------------------------
_CAPITAL_STRUCTURE_SYSTEM = """\
You are writing the Capital Structure section of a real estate IC memo.
Use ONLY the provided metrics with cell references. Cite cell references.
For floating-rate debt (when Interest Rate Spread + Cap are both present),
explain the floating structure: spread, cap strike, max effective rate.

Output format (markdown). Use BULLET POINTS for all figures — NEVER markdown
tables (they are hard to read in this app):

## Capital Structure

- **Purchase Price:** $X (Sheet!Cell)
- **Total Project Cost:** $X (Sheet!Cell)
- **Acquisition Loan:** $X (Sheet!Cell)
- **Construction Loan:** $X — only if present (conversion/dev)
- **Equity Required:** $X
- **LTV or LTC:** X% — LTC for cost-financed dev/value-add
- **Interest Rate:** (see floating rule above)
- **Loan Maturity:** X months
- **I/O Period:** X months
- **DSCR:** X.Xx
- **Debt Yield:** X.X%

Omit bullets that are missing/N/A rather than showing "—" clutter. If both an
acquisition loan and a construction loan are present, note that the
construction loan funds the project and typically repays the acquisition
bridge. Then 1-2 sentences on the capital stack's risk/return profile.
Max 200 words total. No filler.
"""


def deep_dive_capital_structure() -> dict[str, Any]:
    uw = _load_uw_layer()
    if not uw:
        return {"error": "No underwriting layer in SSOT."}
    if not llm_available():
        return {"error": "OPENAI_API_KEY is not set."}

    bounded = uw.get("bounded_metrics", {}) or {}
    relevant = [
        "Purchase Price", "Total Project Cost", "Debt Amount", "Construction Loan",
        "Equity Invested",
        "Original LTV", "Loan-to-Cost (LTC)", "Interest Rate", "Interest Rate Spread", "Interest Rate Cap",
        "Loan Maturity", "Interest-Only Period Remaining",
        "DSCR / Debt Coverage Ratio", "Debt Yield",
    ]
    # Structured debt schedule (amort / interest / balance over time), parsed.
    debt_block = ""
    source_file = uw.get("source_file")
    if source_file:
        fp = UPLOAD_DIR / source_file
        if fp.exists():
            debt_block = _ts_block(
                fp,
                ("debt service", "interest expense", "interest", "amortization",
                 "principal", "loan balance", "debt balance", "maturity", "debt addition"),
                header="Debt schedule (parsed from debt / cash-flow tables — periodicity-aware):",
            )

    user_prompt = (
        "Bounded metrics for Capital Structure:\n\n"
        + _bounded_pretty(bounded, relevant)
        + debt_block
        + "\n\nWrite the Capital Structure section."
    )
    text = complete(_CAPITAL_STRUCTURE_SYSTEM, user_prompt, temperature=0.1)
    return {"section": "capital_structure", "narrative": text}


# ---------------------------------------------------------------------------
# Cash Flow / NOI Trajectory
# ---------------------------------------------------------------------------
_CASH_FLOW_SYSTEM = """\
You are writing the Cash Flow / NOI Trajectory section of a real estate IC memo.
Use the bounded metrics AND the time-series data provided. Cite cell references.

Walk through how NOI evolves:
  - Year 1 (going-in) NOI level
  - Stabilization year and stabilized NOI
  - Exit NOI
  - Identify the trajectory shape (flat/growth/dev ramp-up/value-add lift)

Output format (markdown). Use BULLET POINTS for all figures — NEVER markdown
tables (they are hard to read in this app):

## Cash Flow / NOI Trajectory

- **Year 1 (going-in) NOI:** $X (Sheet!Cell)
- **Stabilized NOI:** $X — stabilization year
- **Exit NOI:** $X

Then 2-3 sentences explaining the trajectory and the drivers.
Max 250 words total.
"""


def deep_dive_cash_flow() -> dict[str, Any]:
    uw = _load_uw_layer()
    if not uw:
        return {"error": "No underwriting layer in SSOT."}
    if not llm_available():
        return {"error": "OPENAI_API_KEY is not set."}

    bounded = uw.get("bounded_metrics", {}) or {}
    relevant = [
        "Net Operating Income (NOI)", "Exit NOI",
        "Going-in Cap Rate", "Exit Cap Rate",
        "Exit Value / Terminal Value", "Hold Period",
        "Total Units", "Total SF",
    ]

    # Time series from the source workbook (parser-backed, periodicity-aware).
    source_file = uw.get("source_file")
    ts_block = ""
    if source_file:
        fp = UPLOAD_DIR / source_file
        if fp.exists():
            ts_block = _ts_block(fp, (
                "noi", "net operating income", "revenue",
                "egi", "gross income", "operating expense", "cash flow",
            ), max_rows=20)

    user_prompt = (
        "Bounded metrics for Cash Flow / NOI:\n\n"
        + _bounded_pretty(bounded, relevant)
        + ts_block
        + "\n\nWrite the Cash Flow / NOI Trajectory section."
    )
    text = complete(_CASH_FLOW_SYSTEM, user_prompt, temperature=0.1)
    return {"section": "cash_flow", "narrative": text}


# ---------------------------------------------------------------------------
# Return Profile
# ---------------------------------------------------------------------------
_RETURN_PROFILE_SYSTEM = """\
You are writing the Return Profile section of a real estate IC memo.
Use ONLY bounded metrics with cell references.

Output format (markdown). Use BULLET POINTS for all figures — NEVER markdown
tables (they are hard to read in this app):

## Return Profile

- **Levered IRR:** X% (Sheet!Cell)
- **Unlevered IRR:** X%
- **Equity Multiple:** X.Xx
- **Going-In Cap Rate:** X.X%
- **Exit Cap Rate:** X.X%
- **Exit Value:** $X
- **Hold Period:** X years

Then 2-3 sentences explaining where the return is coming from
(yield/cap compression/operational uplift/development premium) — based on
the cap rate spread, NOI trajectory, and hold period.
Max 200 words total.
"""


def deep_dive_return_profile() -> dict[str, Any]:
    uw = _load_uw_layer()
    if not uw:
        return {"error": "No underwriting layer in SSOT."}
    if not llm_available():
        return {"error": "OPENAI_API_KEY is not set."}

    bounded = uw.get("bounded_metrics", {}) or {}
    relevant = [
        "Levered IRR", "Unlevered IRR", "Equity Multiple",
        "Going-in Cap Rate", "Exit Cap Rate", "Exit Value / Terminal Value",
        "Hold Period", "Net Operating Income (NOI)", "Exit NOI",
    ]
    user_prompt = (
        "Bounded metrics for Return Profile:\n\n"
        + _bounded_pretty(bounded, relevant)
        + "\n\nWrite the Return Profile section."
    )
    text = complete(_RETURN_PROFILE_SYSTEM, user_prompt, temperature=0.1)
    return {"section": "return_profile", "narrative": text}


# ---------------------------------------------------------------------------
# CapEx Plan
# ---------------------------------------------------------------------------
_CAPEX_PLAN_SYSTEM = """\
You are writing the CapEx Plan section of a real estate IC memo.
Use the bounded metrics AND any time-series data provided. Cite cell references.

Output format (markdown). Use BULLET POINTS for all figures — NEVER markdown
tables (they are hard to read in this app):

## CapEx Plan

- **Total CapEx Budget:** $X (Sheet!Cell)
- **Total Project Cost:** $X
- **Hold Period:** X years

If a multi-year draw schedule is in the time series, list it as bullets:

- **Year 1:** $X
- **Year 2:** $X

Then 2-3 sentences explaining the CapEx allocation (deferred maintenance,
unit renovation, building systems, ground-up construction, etc.).
Max 250 words total.
"""


def deep_dive_capex_plan() -> dict[str, Any]:
    uw = _load_uw_layer()
    if not uw:
        return {"error": "No underwriting layer in SSOT."}
    if not llm_available():
        return {"error": "OPENAI_API_KEY is not set."}

    bounded = uw.get("bounded_metrics", {}) or {}
    relevant = [
        "CapEx Budget", "Total Project Cost", "Purchase Price",
        "Hold Period", "Total Units", "Total SF",
    ]

    source_file = uw.get("source_file")
    ts_block = ""
    if source_file:
        fp = UPLOAD_DIR / source_file
        if fp.exists():
            ts_block = _ts_block(fp, (
                "capex", "hard cost", "soft cost", "construction",
                "total project", "draw", "tenant improvement", "ti",
            ), max_rows=25, header="CapEx-related time series (periodicity-aware):")

    user_prompt = (
        "Bounded metrics for CapEx Plan:\n\n"
        + _bounded_pretty(bounded, relevant)
        + ts_block
        + "\n\nWrite the CapEx Plan section."
    )
    text = complete(_CAPEX_PLAN_SYSTEM, user_prompt, temperature=0.1)
    return {"section": "capex_plan", "narrative": text}


# ---------------------------------------------------------------------------
# Key Risks
# ---------------------------------------------------------------------------
_KEY_RISKS_SYSTEM = """\
You are writing the Key Risks section of a real estate IC memo.
Identify 3-5 risks that are SPECIFIC TO THIS DEAL based on the metrics and
context provided. Each risk must reference a specific number, cell, or
inferred characteristic.

FORBIDDEN: generic boilerplate risks ("market risk", "interest rate risk")
without a model-grounded basis. If you cite "interest rate risk," it must
tie to a specific assumption in the model (floating rate exposure, refinance
risk at a specific maturity, etc.).

Output format (markdown):

## Key Risks

1. **Risk Title** — One sentence with the specific data point that creates
   the risk. Cite cell reference where possible.

2. **Risk Title** — ...

3. **Risk Title** — ...

(3-5 items total. Max 250 words.)
"""


def deep_dive_key_risks() -> dict[str, Any]:
    uw = _load_uw_layer()
    if not uw:
        return {"error": "No underwriting layer in SSOT."}
    if not llm_available():
        return {"error": "OPENAI_API_KEY is not set."}

    bounded = uw.get("bounded_metrics", {}) or {}
    relevant = list(bounded.keys())  # all bounded metrics — risks may come from anywhere

    raw_insights = uw.get("raw_insights") or {}
    observations = raw_insights.get("observations", []) or []
    model_summary = raw_insights.get("model_summary", "") or ""

    user_prompt = (
        f"Model summary: {model_summary}\n\n"
        "All bounded metrics:\n\n"
        + _bounded_pretty(bounded, relevant)
        + "\n\nAdditional context from Pass 2 observations:\n"
        + ("\n".join(f"  - {o}" for o in observations) if observations else "  (none)")
        + "\n\nWrite the Key Risks section."
    )
    text = complete(_KEY_RISKS_SYSTEM, user_prompt, temperature=0.2)
    return {"section": "key_risks", "narrative": text}


# ---------------------------------------------------------------------------
# Dispatcher (used by app + tools)
# ---------------------------------------------------------------------------

DEEP_DIVES: dict[str, Any] = {
    "capital_structure": deep_dive_capital_structure,
    "cash_flow":         deep_dive_cash_flow,
    "return_profile":    deep_dive_return_profile,
    "capex_plan":        deep_dive_capex_plan,
    "key_risks":         deep_dive_key_risks,
}


def run_deep_dive(name: str) -> dict[str, Any]:
    fn = DEEP_DIVES.get(name)
    if not fn:
        return {"error": f"Unknown deep dive: {name}. Valid: {sorted(DEEP_DIVES.keys())}"}
    return fn()


# ---------------------------------------------------------------------------
# Focused dive — read the topic's sheets WHOLE and summarize (comprehension)
# ---------------------------------------------------------------------------
# The deal brief reads the summary/inputs tabs; a focused dive reads the tabs
# that actually hold the topic (the NOI/proforma for cash flow, the debt sheet
# for capital structure, the returns/waterfall for returns) — picks them by
# name, reads them WHOLE, and summarizes in ONE pass. Fast (1 read + 1 call) and
# grounded in the real sheet, vs the old agentic re-read or time-series scrape.

_TOPIC_READ: dict[str, dict[str, Any]] = {
    "cash_flow": {
        "title": "Cash Flow / NOI Trajectory",
        "keywords": ("noi", "net operating income", "cash flow", "cashflow",
                     "proforma", "pro forma", "p&l", "pnl", "operating statement",
                     "operating income", "rollup", "roll-up"),
        "roles": ("model",),
        "focus": (
            "Report the ANNUAL NOI trajectory. NOI is an ANNUAL figure — if a "
            "sheet shows monthly or quarterly columns, use the annual total / "
            "roll-up column, NEVER a single month (a single month would be ~1/12 "
            "of the annual and is wrong). Give, as bullets with Sheet!Cell:\n"
            "- Going-in / Year-1 ANNUAL NOI\n- Stabilized NOI and the year it stabilizes\n"
            "- Exit / terminal NOI (12-mo forward at sale)\nThen 2-3 sentences on the "
            "trajectory shape (flat / growth / dev ramp / value-add lift) and its drivers."
        ),
    },
    "return_profile": {
        "title": "Return Profile",
        "keywords": ("irr", "return", "waterfall", "promote", "equity multiple",
                     "moic", "yield", "distribution", "cash on cash", "cash-on-cash", "tracker"),
        "roles": ("returns", "summary"),
        "focus": (
            "Report the return profile as bullets with Sheet!Cell: Levered IRR, "
            "Unlevered IRR, Equity Multiple, Going-in Cap, Exit Cap, Exit Value, "
            "Hold Period. If a sensitivity/scenario grid is present, add 1-2 bullets "
            "on what swings the return most, with the deltas. Then 2-3 sentences on "
            "where the return comes from (yield / cap compression / operational "
            "uplift / development premium)."
        ),
    },
    "capital_structure": {
        "title": "Capital Structure",
        "keywords": ("debt", "loan", "financing", "mortgage", "ltv", "ltc", "dscr",
                     "interest", "libor", "sofr", "sources", "uses", "capitalization", "leverage"),
        "roles": ("inputs", "returns", "model"),
        "focus": (
            "Report the capital stack as bullets with Sheet!Cell: Purchase Price / "
            "Total Project Cost, Loan amount(s), Equity, LTV or LTC, Interest Rate "
            "(if floating, express as spread + cap strike + max effective rate), "
            "Loan Maturity, I/O period, DSCR, Debt Yield. Omit what's absent. Then "
            "1-2 sentences on the stack's risk/return profile."
        ),
    },
    "capex_plan": {
        "title": "CapEx Plan",
        "keywords": ("capex", "cap ex", "capital expenditure", "renovation", "hard cost",
                     "soft cost", "construction", "ff&e", "os&e", "draw", "tenant improvement", "ti"),
        "roles": ("model", "support"),
        "focus": (
            "Report the CapEx plan as bullets with Sheet!Cell: Total CapEx Budget, "
            "Total Project Cost, per-key or per-SF basis where the sheet allows, and "
            "a multi-year draw schedule if present. Then 2-3 sentences on what the "
            "CapEx buys (deferred maintenance, unit reno, building systems, ground-up)."
        ),
    },
    "key_risks": {
        "title": "Key Risks",
        "keywords": ("sensitivity", "scenario", "assumption", "risk", "recession",
                     "downside", "stress", "exit", "growth"),
        "roles": ("summary", "inputs", "returns"),
        "focus": (
            "Identify 3-5 risks SPECIFIC TO THIS DEAL, each tied to a specific "
            "number, cell, or assumption you read (cite Sheet!Cell). FORBIDDEN: "
            "generic boilerplate ('market risk') with no model-grounded basis. If "
            "sensitivity/scenario tables exist, ground at least one risk in them "
            "(e.g. exit-cap or rent-growth swings). Numbered list, max 250 words."
        ),
    },
}


# Tokens that mark a SECONDARY sheet — a side asset, a variant, an input/output
# feeder, or a historical/backup — which should lose to the primary sheet on a
# keyword tie (e.g. "Golf P&L" / "P&L Inputs" must not beat the main "P&L").
_SECONDARY_TOKENS = {
    "golf", "alt", "detail", "backup", "old", "bridge", "comp", "comps",
    "historical", "historicals", "input", "inputs", "output", "outputs", "bs",
}


def _kw_hit(name_lower: str, tokens: set[str], kw: str) -> bool:
    """Keyword match. Short keywords (<=3 chars, e.g. 'ti', 'noi', 'irr') must
    match a WHOLE token, not a substring — otherwise 'ti' hits 'valuaTIon' and
    'penetraTIon'. Multiword/punctuated keywords match as a substring."""
    if len(kw) <= 3 and kw.isalnum():
        return kw in tokens
    return kw in name_lower


def _select_topic_sheets(
    topic: str, file_path, orientation: dict | None, max_sheets: int = 4,
) -> list[str]:
    """Pick the sheets that actually hold this topic: name-keyword match across
    ALL sheets (skip-tier included — reading a sensitivity tab for risks is
    desired), ranked by match strength (favouring concise primary sheets,
    penalising secondary/variant ones) then orientation confidence. Falls back
    to the topic's orientation roles, then to nothing."""
    import re
    spec = _TOPIC_READ.get(topic, {})
    keywords = spec.get("keywords", ())
    sheets_info = (orientation or {}).get("sheets", {}) or {}
    names = list(sheets_info.keys())
    if not names:
        try:
            import openpyxl
            wb = openpyxl.load_workbook(file_path, read_only=True)
            names = list(wb.sheetnames)
            wb.close()
        except Exception:
            return []

    scored: list[tuple[float, float, str]] = []
    for n in names:
        nl = n.lower()
        tokens = {t for t in re.split(r"[^a-z0-9]+", nl) if t}
        hits = sum(1 for k in keywords if _kw_hit(nl, tokens, k))
        if not hits:
            continue
        clean_bonus = 1.0 if len(tokens) <= 2 else 0.0   # concise primary sheet
        penalty = len(tokens & _SECONDARY_TOKENS)         # side/variant/feeder
        conf = (sheets_info.get(n, {}) or {}).get("confidence", 0.0) or 0.0
        scored.append((hits + clean_bonus - penalty, conf, n))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    picked = [n for _, _, n in scored[:max_sheets]]

    if not picked:  # fall back to the topic's orientation roles
        rmap = (orientation or {}).get("map", {}) or {}
        for role in spec.get("roles", ()):
            picked.extend(rmap.get(role, []))
        picked = picked[:max_sheets]
    return picked


_FOCUSED_SYSTEM = """\
You are a real estate analyst writing ONE section of an IC memo. You are given
the FULL CONTENT of the workbook tabs that hold this topic (each cell with its
A1 reference). Read them the way an analyst does — headers, table structure, and
units matter. Report ONLY what the cells support; never invent a number. Mind
units headers ("$ in 000s" means a cell of 26995 is $27.0M — report the real
magnitude). Bullets in the narrative, NEVER markdown tables.

Return BOTH the section narrative AND the structured facts behind it. Every
number you cite must appear in `facts` with the EXACT sheet and cell it came
from (so each can be verified against the workbook). For a composed figure
(per-key, a sum), set `composed_from` to the short arithmetic.

Return ONLY JSON, no prose outside it, no code fences:
{
  "facts": [
    {"field": "<label>", "value": <number or string>, "display": "<$27.0M>",
     "sheet": "<exact sheet>", "cell": "<A1>", "composed_from": null}
  ],
  "narrative": "<the section as markdown bullets + prose>"
}
"""


def _factcheck_md(scored: dict) -> str:
    """Compact fact-check block appended to a dive so its credibility travels
    with it in the transcript."""
    facts = scored.get("facts") or []
    if not facts:
        return ""
    s = scored.get("summary", {})
    mark = {"show": "✓", "flag": "⚠", "omit": "✕"}
    lines = [
        f"**Fact check** — {s.get('high',0)} high · {s.get('medium',0)} medium · "
        f"{s.get('flagged',0)} flagged · {s.get('omitted',0)} omitted"
    ]
    for f in facts:
        t = f.get("trust", {})
        line = (f"{mark.get(t.get('verdict'),'·')} {f.get('field')}: "
                f"{f.get('display', f.get('value'))} ({f.get('sheet')}!{f.get('cell')})")
        if t.get("notes"):
            line += f" — {'; '.join(t['notes'])[:140]}"
        lines.append(line)
    return "\n\n".join(["\n".join(lines[:1] + ["- " + l for l in lines[1:]])])


def focused_dive(topic: str, file_path, orientation: dict | None) -> dict[str, Any]:
    """Comprehension dive: read the topic's sheets whole, extract cell-cited
    facts + narrative in one pass, then score the facts through the SAME trust
    engine the brief uses (grounding / reconcile / challenge — scoped to these
    sheets) and finalize the narrative to verified facts. The dive carries the
    same credibility as the brief."""
    spec = _TOPIC_READ.get(topic)
    if not spec:
        return {"error": f"Unknown analysis: {topic}"}
    if not llm_available():
        return {"error": "OPENAI_API_KEY is not set."}

    sheets = _select_topic_sheets(topic, file_path, orientation)
    if not sheets:
        return {"error": f"No sheets matched {spec['title']} in this workbook."}

    from workbook_orientation import render_sheets_text
    from scenarios._llm import client, MODEL
    cells_block = render_sheets_text(Path(file_path), sheets, max_total_chars=26_000)
    if not cells_block:
        return {"error": f"Couldn't read the {spec['title']} sheets."}

    user_msg = (
        f"TOPIC: {spec['title']}\n\n{spec['focus']}\n\n"
        f"WORKBOOK TABS ({', '.join(sheets)}):\n\n{cells_block}"
    )
    try:
        resp = client.chat.completions.create(
            model=MODEL, temperature=0.1,
            messages=[
                {"role": "system", "content": _FOCUSED_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
        )
        raw = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        return {"error": f"{spec['title']} read failed: {type(e).__name__}: {e}"}

    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {"facts": [], "narrative": raw}  # tolerate prose-only output
    facts = parsed.get("facts") or []
    narrative = (parsed.get("narrative") or "").strip()
    if not narrative and not facts:
        return {"error": f"{spec['title']} returned nothing usable."}

    # Score the dive's facts through the trust engine — the dive's own sheets are
    # the authority for this topic, and the challenge re-reads the SAME block.
    scored = {"facts": [], "summary": {}}
    final_md = narrative
    try:
        from trust_engine import score_facts
        from model_brief import finalize_brief
        scored = score_facts(
            {"facts": facts}, file_path, orientation,
            authoritative_sheets=set(sheets), cells_block=cells_block,
        )
        fin = finalize_brief({"brief_md": narrative, "facts": facts}, scored)
        final_md = fin.get("brief_md") or narrative
    except Exception as e:
        log.error("Dive trust scoring failed for %s: %s", topic, e)

    fc = _factcheck_md(scored)
    body = f"## {spec['title']}\n\n{final_md}" + (f"\n\n---\n\n{fc}" if fc else "")
    return {"section": topic, "title": spec["title"], "narrative": body,
            "sheets_read": sheets, "scored": scored}


# ---------------------------------------------------------------------------
# Agent-driven deep dives — the dive as a task for the tool-equipped agent
# ---------------------------------------------------------------------------
# Instead of a single-shot prompt over already-extracted SSOT data, the dive
# becomes an instruction the chat agent executes WITH ITS FILE TOOLS: read the
# relevant sheets (read_sheet / search_file), then write the section. This is
# how an analyst answers "dig into returns" — they go back to the workbook.
# The static functions above remain as the no-orientation fallback.

# Per-dive reading plan: which workbook-map roles to read, what to search for,
# and the section's output spec (bullets, never tables — app-wide rule).
_AGENT_DIVE_SPECS: dict[str, dict[str, Any]] = {
    "capital_structure": {
        "title": "Capital Structure",
        "read_roles": ("inputs", "summary"),
        "search_hint": "debt, loan, interest rate, spread, LTV, DSCR, maturity",
        "output": """\
## Capital Structure
- **Purchase Price / Total Project Cost / Loan(s) / Equity Required** with cell refs
- **LTV or LTC**, **Interest Rate** (floating: spread + cap strike + max effective rate)
- **Loan Maturity / I/O Period / DSCR / Debt Yield** where present
Omit missing bullets. Close with 1-2 sentences on the capital stack's
risk/return profile. Max 200 words.""",
    },
    "cash_flow": {
        "title": "Cash Flow / NOI Trajectory",
        "read_roles": ("model", "summary"),
        "search_hint": "NOI, net operating income, revenue, operating expenses, cash flow",
        "output": """\
## Cash Flow / NOI Trajectory
- **Year 1 (going-in) NOI / Stabilized NOI (+ stabilization year) / Exit NOI** with cell refs
Annualize monthly tables before citing annual figures (say "annualized").
Close with 2-3 sentences on the trajectory shape (flat / growth / dev ramp /
value-add lift) and its drivers. Max 250 words.""",
    },
    "return_profile": {
        "title": "Return Profile",
        "read_roles": ("returns", "summary"),
        "search_hint": "IRR, equity multiple, waterfall, promote, sensitivity",
        "output": """\
## Return Profile
- **Levered IRR / Unlevered IRR / Equity Multiple / Going-In Cap / Exit Cap /
  Exit Value / Hold Period** with cell refs
If the workbook has sensitivity or scenario tables, read them and add 1-2
bullets on what swings the return most (with the specific deltas).
Close with 2-3 sentences on where the return comes from (yield / cap
compression / operational uplift / development premium). Max 250 words.""",
    },
    "capex_plan": {
        "title": "CapEx Plan",
        "read_roles": ("support", "model"),
        "search_hint": "capex, renovation, hard cost, soft cost, draw schedule, TI",
        "output": """\
## CapEx Plan
- **Total CapEx Budget / Total Project Cost** with cell refs; per-key or per-SF
  basis when the size metrics allow
- Multi-year draw schedule as bullets when present
Close with 2-3 sentences on what the CapEx buys (deferred maintenance, unit
renovation, systems, ground-up). Max 250 words.""",
    },
    "key_risks": {
        "title": "Key Risks",
        "read_roles": ("summary", "inputs"),
        "search_hint": "sensitivity, scenario, assumptions, growth, vacancy",
        "output": """\
## Key Risks
3-5 numbered risks SPECIFIC TO THIS DEAL, each tied to a specific number,
cell, or assumption you read (cite Sheet!Cell). FORBIDDEN: generic boilerplate
("market risk") without a model-grounded basis. If sensitivity tables exist,
ground at least one risk in them. Max 250 words.""",
    },
}


def agent_dive_instruction(
    name: str,
    workbook_map: dict[str, list[str]] | None,
    source_file: str | None,
) -> str | None:
    """
    Build the agent task for a deep dive: read the right sheets first, then
    write the section. `workbook_map` is the Workbook Orientation role map
    ({role: [sheet names]}); when absent the agent is told to list sheets and
    choose. Returns None for an unknown dive name.
    """
    spec = _AGENT_DIVE_SPECS.get(name)
    if not spec:
        return None

    # Within each role, an explicit NAME ("One Pager", "NOI", "Cash Flow")
    # outranks template tabs that content-classified into the same role —
    # the stable sort keeps the orientation's confidence order within ties.
    from flexible_extractor import sheet_priority_tier
    read_sheets: list[str] = []
    if workbook_map:
        for role in spec["read_roles"]:
            names = sorted(workbook_map.get(role) or [], key=sheet_priority_tier)
            read_sheets.extend(names[:3])
    if workbook_map:
        map_lines = "\n".join(
            f"  {role}: {', '.join(names)}"
            for role, names in workbook_map.items()
            if names and role != "other"
        )
        map_block = f"Workbook map (sheet roles from content analysis):\n{map_lines}\n"
    else:
        map_block = "No workbook map available — call list_sheets first and choose.\n"

    reading_plan = (
        f"Start by reading: {', '.join(dict.fromkeys(read_sheets))} (read_sheet)."
        if read_sheets else
        "Pick the most relevant sheets from the map / list_sheets."
    )

    return f"""\
Deep dive: {spec['title']} — for the deal in `{source_file or 'the uploaded workbook'}`.

{map_block}
Work like an analyst answering "dig into {spec['title'].lower()}":
1. {reading_plan} Use search_file for anything you can't locate
   (try: {spec['search_hint']}). Read MORE sheets if a number needs chasing.
2. Cross-check what you read against the verified SSOT facts
   (get_layer_details for "underwriting") — human-verified values GOVERN on
   any disagreement; never overrule them with a raw cell.
3. Then write EXACTLY this section, citing Sheet!Cell for every figure.
   Bullets only — NEVER markdown tables.

{spec['output']}"""
