"""
Deal Review scenario — institutional acquisition memo (Path B architecture).

Architecture:
  - Catalog provides verified facts with cell-level provenance.
  - GPT acts as the analyst: reads catalog facts + raw file content +
    multi-year time series, then writes a deal memo.
  - Output adapts to deal type (ground-up dev, value-add, core, etc.)
    rather than forcing a rigid 30-field template.

Why this design:
  - Real analysts don't fill forms when they read closing files — they
    write a thesis. The output should match what an institutional asset
    manager actually produces.
  - Templates force every field to be populated, even when irrelevant
    (e.g. "Going-in NOI" on a ground-up dev = always $0). Adaptive
    sections handle that without "—" noise.
  - GPT is good at synthesis. Don't limit it to fill-in-the-blanks.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import ssot
from scenarios._llm import complete, llm_available, log
from scenarios.profiles import filter_layer_metrics
from flexible_extractor import extract_time_series_rows
from knowledge_store import build_runtime_knowledge_block


UPLOAD_DIR = Path("uploads")


SYSTEM_PROMPT = """\
You are a senior real estate investment professional. The user is an
institutional asset manager who has ALREADY reviewed and verified the deal's
core facts in an audit appendix (a human-in-the-loop verification gate). Your
job now is to write the deal's SNAPSHOT — a tight elevator pitch — and nothing
else.

You have these inputs:
  1. BOUNDED METRICS (PRIMARY): the analyst checklist. Many carry
     status=verified with `human-verified` — these were confirmed by the user
     and are the most trustworthy facts you have; cite them plainly. Statuses:
       VERIFIED        — safe to cite as fact (human-verified ones especially).
       CANDIDATE_POOL  — top-ranked of several; cite with cell ref, no certainty.
       SUSPICIOUS      — DO NOT cite the value. Omit or note "data quality issue".
       MISSING         — no value. Omit the field. Never invent.
  2. FORMULA-TRACED METRICS: non-checklist facts reached by following formulas
     out from the verified cells (e.g. revenue/expense detail, equity split).
     These are model-derived; cite with their cell reference when useful.
  3. LEGACY CATALOG FACTS + PASS 2 INFERRED FIELDS: secondary. Note inferred
     fields as "(inferred)".
  4. TIME SERIES: multi-year NOI / revenue / cash-flow trajectory.
  5. AUTHORITATIVE SHEET CONTENT: the model's summary / one-pager / inputs
     sheets rendered IN FULL (each cell with its A1 ref). This is how you see
     the deal the way the analyst does — the stated strategy, basis build-up,
     portfolio composition, threshold notes, and anything the checklist can't
     capture. Use it for the STORY and qualitative framing.

CITATION RULES (NON-NEGOTIABLE):
- VERIFIED / CANDIDATE_POOL / traced metrics: cite with cell ref, e.g.
  "$192M (General Information!C11)".
- SUSPICIOUS: never cite the value. MISSING: omit. Inferred: mark "(inferred)".
- AUTHORITATIVE SHEET CONTENT is supporting context: you may cite a cell from
  it (Sheet!Cell) for qualitative facts and color, but when a number there
  disagrees with a bounded metric, THE BOUNDED METRIC GOVERNS — the human
  verified it. Never use sheet content to overrule or re-derive checklist
  values. Mind units headers like "$ in 000s" when reading raw cells.
- Never invent a number. If you don't have it, leave it out.

OUTPUT — write EXACTLY one section, nothing before or after it:

## Snapshot
First, ONE concise paragraph (3-5 sentences): asset name + property type (with
number of properties if a portfolio), location, size (units / SF / keys),
acquisition date, purchase price, target hold, and the headline return
(Levered IRR). This is the elevator pitch — no filler.

Then, under a bold `**Business Plan**` line, EXACTLY 2 bullets:
- Bullet 1 — opportunity framing: what asset, what market, what makes it
  attractive, in one sentence. e.g. "Well-located Class-A multifamily in the
  Glendale, CA submarket with strong rent fundamentals."
- Bullet 2 — strategy + lever: Core / Core-Plus / Value-Add / Opportunistic /
  Ground-up Development, plus the specific value-creation lever, in one
  sentence. e.g. "Value-Add via unit renovations and lease-up of vacant
  inventory to drive ~$2.5M NOI uplift."

STYLE: markdown, tight, executive. Target 120-200 words total. Do NOT write
Risks, Audit Appendix, Capital Structure, Cash Flow, Return Profile, or CapEx
— those are on-demand analyses the user triggers separately.

FLOATING-RATE NOTE: if Interest Rate Spread AND Interest Rate Cap are both
present the debt is floating; if you mention the rate, express it as
"<Spread>% spread + <Cap>% cap" rather than a single fixed rate.

PERIODICITY NOTE: a metric tagged `[table:monthly]` or `[table:quarterly]` is a
SINGLE-PERIOD figure from a roll-up table (e.g. one month's NOI) — NEVER present
it as an annual number. For an annual figure use the TIME SERIES (annual NOI)
instead, and say "monthly" / "annualized" explicitly when you cite it.
"""


def _format_time_series_block(series: list[dict], max_rows: int = 25) -> str:
    """Render time series as a readable text table for GPT."""
    if not series:
        return "(no time series extracted from this file)"

    # Group by sheet, take most analytically relevant rows
    # Priority: NOI, Revenue, EGI, Operating Expenses, Cash Flow, Debt Service
    priority_terms = [
        "noi", "net operating income", "egi", "effective gross",
        "operating expense", "total expense", "cash flow",
        "debt service", "rental income", "total income",
        "potential gross", "occupancy", "stabilized",
        "total project", "total uses", "total sources", "equity funded",
    ]

    def row_priority(s):
        label_lower = s["label"].lower()
        for i, kw in enumerate(priority_terms):
            if kw in label_lower:
                return i
        return 999

    series_sorted = sorted(series, key=row_priority)[:max_rows]

    lines = []
    current_sheet = None
    for s in series_sorted:
        if s["sheet"] != current_sheet:
            current_sheet = s["sheet"]
            lines.append(f"\n[{current_sheet}]")
        headers = s.get("annual_headers") or s["headers"]
        values = s.get("annual_values") or s["values"]
        meta = ""
        if s.get("annualized"):
            meta = f" [annualized from monthly; {s.get('aggregation_method')}]"
        elif s.get("periodicity"):
            meta = f" [{s.get('periodicity')}]"

        # Format values
        vals = []
        for v in values[:8]:
            if v is None:
                vals.append("—")
            elif abs(v) >= 1_000_000:
                vals.append(f"${v/1_000_000:.2f}M")
            elif abs(v) >= 1_000:
                vals.append(f"${v/1_000:.0f}K")
            elif isinstance(v, float) and abs(v) < 1:
                vals.append(f"{v:.1%}")
            else:
                vals.append(f"{v:,.0f}")
        if headers:
            lines.append("  " + " | ".join(str(h) for h in headers[:8]))
        lines.append(f"  {(s['label'] + meta)[:70]:<70} {' | '.join(vals)}")
    return "\n".join(lines)


def _format_catalog_facts(metrics: dict) -> str:
    """Render catalog metrics as a citable list with cell references."""
    lines = []
    for name, data in metrics.items():
        if data.get("value") is None:
            continue
        val = data["value"]
        cell = f"{data.get('sheet','?')}!{data.get('cell','?')}"
        # Format number nicely
        if isinstance(val, (int, float)):
            if abs(val) >= 1_000_000:
                v_str = f"${val/1_000_000:.2f}M"
            elif abs(val) >= 1_000:
                v_str = f"${val:,.0f}"
            elif abs(val) < 1 and val != 0:
                v_str = f"{val:.2%}"
            else:
                v_str = f"{val:,.2f}"
        else:
            v_str = str(val)
        lines.append(f"  - **{name}**: {v_str}  ({cell})")
    return "\n".join(lines) if lines else "  (no catalog facts extracted)"


def _format_bounded_metrics(bounded: dict) -> str:
    """
    Render Phase 1 bounded metrics grouped by status, with cell provenance and
    explicit data-quality flags. This is the PRIMARY input to the memo —
    catalog-verified numbers with audit-grade citations.
    """
    if not bounded:
        return "(No bounded-metric extraction available — Phase 1 pipeline did not run.)"

    # Group by status
    verified, inferred, conflict, pool, suspicious, missing, na = [], [], [], [], [], [], []
    for name, rec in bounded.items():
        status = rec.get("status")
        if status == "verified":
            verified.append((name, rec))
        elif status in ("inferred", "derived"):
            inferred.append((name, rec))
        elif status == "conflict":
            conflict.append((name, rec))
        elif status == "candidate_pool":
            pool.append((name, rec))
        elif status == "suspicious":
            suspicious.append((name, rec))
        elif status == "not_applicable":
            na.append((name, rec))
        elif status == "missing":
            missing.append((name, rec))

    def _fmt_record(name, rec):
        val = rec["display_value"]
        sheet = rec.get("source_sheet")
        cell = rec.get("source_cell")
        cell_ref = f"{sheet}!{cell}" if sheet and cell else "—"
        period = rec.get("period")
        period_tag = f" [{period}]" if period and period != "n/a" else ""
        tp = rec.get("table_periodicity")
        tp_tag = f" [table:{tp}]" if tp else ""
        return f"  - **{name}**{period_tag}{tp_tag}: {val}  ({cell_ref})"

    lines = []
    if verified:
        lines.append("VERIFIED (authoritative source, validated; safe to cite):")
        for name, rec in verified:
            lines.append(_fmt_record(name, rec))
        lines.append("")
    if inferred:
        lines.append("INFERRED (derived or GPT-inferred from verified context; cite, note as inferred):")
        for name, rec in inferred:
            lines.append(_fmt_record(name, rec))
        lines.append("")
    if conflict:
        lines.append("CONFLICT (authoritative sources DISAGREE — show both with ⚠, never pick one):")
        for name, rec in conflict:
            primary = _fmt_record(name, rec)
            confs = (rec.get("audit", {}) or {}).get("conflicts", [])
            alt_str = "; ".join(
                f"{c.get('sheet')}!{c.get('cell')}={c.get('value')}" for c in confs
            )
            lines.append(f"{primary}  ⚠ vs {alt_str}")
        lines.append("")
    if pool:
        lines.append("CANDIDATE POOL (multiple candidates passed schema; "
                     "top-ranked taken — cite with cell ref, no editorial certainty):")
        for name, rec in pool:
            lines.append(_fmt_record(name, rec))
        lines.append("")
    if suspicious:
        lines.append("SUSPICIOUS (failed schema validation — DO NOT cite as fact, "
                     "represent as data quality issue):")
        for name, rec in suspicious:
            notes = "; ".join(rec.get("validation_notes", []))[:160]
            lines.append(f"  - **{name}**: {rec['display_value']}  — {notes}")
        lines.append("")
    if na:
        lines.append("NOT APPLICABLE (legitimately N/A for this deal type — show as 'N/A', "
                     "do NOT treat as a data gap):")
        for name, rec in na:
            notes = "; ".join(rec.get("validation_notes", []))[:140]
            lines.append(f"  - **{name}**: N/A — {notes}")
        lines.append("")
    if missing:
        lines.append("MISSING (no candidates found in scanned sheets — represent as '—'):")
        for name, _ in missing:
            lines.append(f"  - **{name}**")
        lines.append("")

    return "\n".join(lines).strip()


def _format_traced_metrics(trace: dict) -> str:
    """Render Stage-2 formula-traced metrics (reached from verified anchors)."""
    reached = (trace or {}).get("reached_metrics", {}) or {}
    if not reached:
        return "(no formula-traced metrics — trace did not run or reached nothing)"
    lines = []
    for m in reached.values():
        val = m.get("value")
        if isinstance(val, (int, float)):
            if abs(val) >= 1_000_000:
                vs = f"${val/1_000_000:.2f}M"
            elif abs(val) >= 1_000:
                vs = f"${val:,.0f}"
            elif abs(val) < 1 and val != 0:
                vs = f"{val:.2%}"
            else:
                vs = f"{val:,.2f}"
        else:
            vs = str(val)
        lines.append(
            f"  - **{m['metric_name']}**: {vs}  ({m['source']}) "
            f"— traced from {m.get('via_anchor')}"
        )
    return "\n".join(lines)


def _format_pass2_fields(raw_insights: dict) -> str:
    """Render Pass 2 found fields as a list."""
    if not raw_insights:
        return "(Pass 2 did not run — no inferred fields available)"
    found = raw_insights.get("found", {}) or {}
    if not found:
        return "(Pass 2 ran but found no additional fields)"
    lines = []
    for field_name, data in found.items():
        if not isinstance(data, dict) or data.get("value") is None:
            continue
        val = data["value"]
        label = data.get("label_in_file", "")
        sheet = data.get("sheet", "")
        loc = f" [{sheet}: {label}]" if label or sheet else ""
        lines.append(f"  - **{field_name}**: {val}{loc}")
    return "\n".join(lines) if lines else "(no fields populated)"


def generate_deal_review() -> dict[str, Any]:
    """
    Generate the institutional deal memo.
    """
    s = ssot.load_ssot()
    underwriting = s["layers"].get("underwriting")
    if not underwriting:
        return {"error": "No underwriting layer in SSOT. Upload an acquisition file first."}

    if not llm_available():
        return {"error": "OPENAI_API_KEY is not set."}

    # Apply scenario profile so we only pass relevant catalog metrics
    filtered = filter_layer_metrics(underwriting, "deal_review")
    catalog_metrics = filtered.get("metrics", {})

    # Pass 2 inferred fields
    raw_insights = underwriting.get("raw_insights") or {}

    # Time series from the source file (NOI/revenue/cash flow trajectory)
    source_file = underwriting.get("source_file")
    time_series_block = ""
    sheets_block = ""
    sheets_read: list[str] = []
    if source_file:
        file_path = UPLOAD_DIR / source_file
        if file_path.exists():
            try:
                # Parser-first: table periodicity is authoritative. Fall back to
                # the heuristic extractor only if no relevant tables were found.
                from financial_model_parser import build_time_series
                ts = build_time_series(file_path) or extract_time_series_rows(file_path)
                time_series_block = _format_time_series_block(ts)
            except Exception as e:
                time_series_block = f"(time series extraction failed: {e})"

            # Whole-sheet context: the analyst's reading stack (one-pager /
            # inputs / secondary summaries) rendered in full, so the narrative
            # can see the deal story — not just extracted metrics. Best-effort.
            try:
                from workbook_orientation import analyst_reading_stack
                sheets_read, sheets_block = analyst_reading_stack(file_path)
            except Exception as e:
                sheets_block = ""
                log.error("Snapshot sheet-context read failed for %s: %s", source_file, e)
        else:
            time_series_block = f"(source file not found in uploads: {source_file})"

    # Pass 2 observations (free-form context GPT noted at ingest)
    observations = raw_insights.get("observations", []) or []
    model_summary = raw_insights.get("model_summary", "") or ""

    # Phase 1 — bounded analyst-checklist metrics with schema validation
    bounded_metrics = underwriting.get("bounded_metrics", {}) or {}
    business_plan_patterns = build_runtime_knowledge_block(["business_plan"])

    # Stage 2 — formula-traced metrics reached from the verified anchors
    formula_trace = underwriting.get("formula_trace", {}) or {}

    # Build the user prompt
    user_prompt = f"""\
ASSET: {source_file or 'Unknown'}
INGESTED: {underwriting.get('ingested_at', 'Unknown')}

{f'PASS 2 MODEL SUMMARY: {model_summary}' if model_summary else ''}

===== ANALYST CHECKLIST — BOUNDED METRICS (PRIMARY SOURCE) =====
These 25 metrics are the analyst's deal-review checklist, each schema-validated
with explicit provenance. Status-based citation rules apply (see system prompt).

{_format_bounded_metrics(bounded_metrics)}

===== FORMULA-TRACED METRICS (Stage 2 — reached from verified anchors; cite with cell ref) =====

{_format_traced_metrics(formula_trace)}

===== LEGACY CATALOG FACTS (secondary — use only for metrics not in the bounded list above) =====

{_format_catalog_facts(catalog_metrics)}

===== PASS 2 INFERRED FIELDS (use; note as "(inferred)" if cited) =====

{_format_pass2_fields(raw_insights)}

===== PASS 2 OBSERVATIONS (use for context / risks) =====

{chr(10).join(f'  - {o}' for o in observations) if observations else '  (none)'}

===== TIME SERIES (multi-year projections — use for NOI / cash flow trajectory) =====
{time_series_block}

===== AUTHORITATIVE SHEET CONTENT (full pages: {', '.join(sheets_read) if sheets_read else 'unavailable'}) =====
Use for the deal STORY, strategy, and qualitative context. Bounded metrics
above govern on any numeric disagreement. Mind units headers ("$ in 000s").

{sheets_block or '(sheet content unavailable)'}

===== ACTIVE BUSINESS-PLAN KNOWLEDGE PATTERNS (interpretive only; do not create facts) =====
{business_plan_patterns or '(no active business-plan patterns)'}

Now write ONLY the Snapshot section (paragraph + 2 Business Plan bullets) per
your system prompt. Prefer human-verified facts. Be specific. Cite cell
references where facts are used. NEVER cite SUSPICIOUS or MISSING values as fact.
"""

    narrative = complete(SYSTEM_PROMPT, user_prompt, temperature=0.2)

    # Memorialize the acquisition (write-once)
    _memorialize_acquisition(s, narrative, filtered, underwriting)

    # Bounded-metric status breakdown for diagnostics
    bounded_status_counts: dict[str, int] = {}
    for rec in bounded_metrics.values():
        s = rec.get("status", "unknown")
        bounded_status_counts[s] = bounded_status_counts.get(s, 0) + 1

    return {
        "scenario": "deal_review",
        "narrative": narrative,
        "data_used": {
            "layers": ["underwriting"],
            "source_files": [source_file] if source_file else [],
            "bounded_metric_count": len(bounded_metrics),
            "bounded_status_counts": bounded_status_counts,
            "catalog_metric_count": len(catalog_metrics),
            "pass2_field_count": len(raw_insights.get("found", {}) if raw_insights else {}),
            "time_series_rows": len(time_series_block.splitlines()) if time_series_block else 0,
            "sheets_read_whole": sheets_read,
        },
    }


def _memorialize_acquisition(
    s: dict[str, Any],
    narrative: str,
    filtered: dict[str, Any],
    underwriting: dict[str, Any],
) -> None:
    """Save the acquisition memo as a permanent record (write-once)."""
    if s["layers"].get("acquisition_summary"):
        return
    s["layers"]["acquisition_summary"] = {
        "source_file": underwriting.get("source_file"),
        "ingested_at": underwriting.get("ingested_at"),
        "metric_count": filtered.get("metric_count", 0),
        "metrics":      filtered.get("metrics", {}),
        "narrative":    narrative,
    }
    ssot.save_ssot(s)
