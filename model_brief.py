"""
model_brief.py — comprehension-first deal read (the product).

This is the new primary deliverable: feed a workbook, get the Outcome-style
read an analyst (or generic Claude) produces — Overview, Key Deal Stats, Debt,
Returns, Model Structure — by READING the authoritative tabs whole and
synthesizing, instead of cell-matching a 22-field checklist.

Pipeline (step 1 of the comprehension-first rebuild):
    orient (cached)  ->  read key tabs whole  ->  ONE strong-model read that
    returns BOTH the narrative brief AND structured, cell-cited facts.

This module is deliberately comprehension-ONLY: no trust engine yet. Facts are
returned with their source cells so the trust engine (grounding / authority /
reconciliation / corroboration / challenge — step 2) can verify them later, but
nothing is verified or gated here. The point of step 1 is to see raw
comprehension quality against the reference Outcome before building trust.

Output dict:
    {
      "version": BRIEF_VERSION,
      "identity": {asset, fund, location, strategy, property_type, size, ...},
      "facts":    [{field, value, display, sheet, cell, composed_from}],
      "brief":    {overview, key_stats, debt, returns, model_structure},  # markdown
      "brief_md": "<assembled markdown>",
      "sheets_read": [...],
      "model": "gpt-4o",
    }
or {"error": "..."} when the workbook can't be read or no API key is present.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

from scenarios._llm import client, MODEL, llm_available
from workbook_orientation import analyst_reading_stack

log = logging.getLogger("fb.model_brief")
if not log.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("[fb.brief] %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

BRIEF_VERSION = "2026-06-12.1"

# Bound the comprehension read. The reading stack is already capped; this is the
# upper bound on what we hand the model (whole sheets, with A1 refs).
_MAX_READ_SHEETS = 6
_MAX_TOTAL_CHARS = 30_000

_BRIEF_CACHE_DIR = Path("cache/briefs")


_SYSTEM_PROMPT = """\
You are a senior real estate investment professional reading an underwriting
workbook to brief your investment committee. You are given the FULL CONTENT of
the model's authoritative tabs (one-pager / summary / inputs / returns), each
rendered row by row with every cell's A1 reference (e.g. C8=224100). Read them
the way an analyst reads a deal — headers, table structure, and context matter.

Produce a tight deal brief AND the structured facts behind it.

HARD RULES:
- Report ONLY what the tabs actually say. If you cannot find something, omit it.
  Never invent or estimate a number that isn't supported by a cell.
- EVERY number in `facts` must carry the exact sheet and cell it came from. If a
  figure is COMPOSED (e.g. basis per key = total basis / keys, or a sum of a
  build-up), set `composed_from` to the short arithmetic and cite the cells used.
- Mind units headers: a tab marked "$ in 000s" means a cell of 224100 is
  $224.1M. Report the real magnitude in `display`, but put the literal cell
  value in `value` and name the cell — your facts are checked against the cells.
- PORTFOLIO / MULTI-ASSET models bundle several assets. Per-asset columns or
  allocation blocks are SLICES; always report the COMBINED deal-level figure,
  and note the asset breakdown in prose, not as the headline number.
- The brief is prose for humans: tight, executive, specific. Bullets, never
  markdown tables. Cite (Sheet!Cell) inline for the headline figures.

Return ONLY JSON, no prose outside it, no code fences:
{
  "identity": {
    "asset": "<name>", "fund": "<sponsor/fund or null>",
    "location": "<city, state>", "property_type": "<hotel/multifamily/...>",
    "strategy": "<core / value-add / development / rebrand / ...>",
    "size": "<keys / units / SF>"
  },
  "facts": [
    {"field": "Purchase Price", "value": 224100, "display": "$224.1M",
     "sheet": "General Information", "cell": "C8", "composed_from": null},
    {"field": "Basis per Key", "value": 1606532, "display": "$1.6M/key",
     "sheet": "Executive Summary", "cell": "C42",
     "composed_from": "Total Basis 403239 / 251 keys"}
  ],
  "brief": {
    "overview": "<2-3 sentence what-is-this-deal paragraph>",
    "key_stats": "<bulleted key deal stats: price, basis, size, fund, strategy>",
    "debt": "<bulleted loan / rate / hedge / LTV>",
    "returns": "<bulleted IRR / equity multiple / yield-on-cost / exit>",
    "model_structure": "<1-2 sentences on how the workbook is organized>"
  }
}
"""


def _brief_cache_path(file_path: Path) -> Path | None:
    try:
        from extraction_cache import file_sha256
        import hashlib
        key = hashlib.sha256(
            f"{file_sha256(file_path)}|{BRIEF_VERSION}|{MODEL}".encode()
        ).hexdigest()
        return _BRIEF_CACHE_DIR / f"{key}.json"
    except Exception:
        return None


def _assemble_md(identity: dict, brief: dict) -> str:
    """Compose the display markdown from the structured brief sections."""
    name = identity.get("asset") or "Deal"
    parts: list[str] = [f"### {name}"]
    ov = brief.get("overview")
    if ov:
        parts.append(ov)
    section_order = [
        ("key_stats", "Key Deal Stats"),
        ("debt", "Debt"),
        ("returns", "Returns"),
        ("model_structure", "Model Structure"),
    ]
    for key, title in section_order:
        body = brief.get(key)
        if body and str(body).strip():
            parts.append(f"**{title}**\n\n{body}")
    return "\n\n".join(parts)


def _parse_json(raw: str) -> dict | None:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def build_model_brief(file_path: str | Path, use_cache: bool = True) -> dict[str, Any]:
    """
    Comprehension-first read of a workbook. Orient -> read key tabs whole ->
    ONE strong-model call returning the narrative brief + cell-cited facts.

    Cached by file hash + version + model, so re-opens and reruns are free.
    Returns {"error": ...} when no API key is available or the read fails.
    """
    file_path = Path(file_path)

    cache_p = _brief_cache_path(file_path) if use_cache else None
    if cache_p is not None and cache_p.exists():
        try:
            with open(cache_p) as fh:
                cached = json.load(fh)
            if cached.get("version") == BRIEF_VERSION and cached.get("brief_md"):
                return cached
        except Exception:
            pass

    if not llm_available():
        return {"error": "OPENAI_API_KEY is not set — the model brief needs the LLM."}

    sheets_read, cells_block = analyst_reading_stack(
        file_path, max_sheets=_MAX_READ_SHEETS, max_total_chars=_MAX_TOTAL_CHARS
    )
    if not cells_block:
        return {"error": "Could not render any authoritative tabs to read."}

    user_msg = (
        "Read this underwriting model and produce the deal brief + facts.\n\n"
        f"AUTHORITATIVE TABS ({', '.join(sheets_read)}):\n\n{cells_block}"
    )

    try:
        resp = client.chat.completions.create(
            model=MODEL,
            temperature=0.1,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
        )
        parsed = _parse_json(resp.choices[0].message.content or "")
    except Exception as e:
        log.error("Model brief read failed for %s: %s", file_path.name, e)
        return {"error": f"Model brief read failed: {type(e).__name__}: {e}"}

    if not parsed:
        return {"error": "Model brief returned unparseable JSON."}

    identity = parsed.get("identity", {}) or {}
    brief = parsed.get("brief", {}) or {}
    facts = parsed.get("facts", []) or []
    result = {
        "version": BRIEF_VERSION,
        "identity": identity,
        "facts": facts,
        "brief": brief,
        "brief_md": _assemble_md(identity, brief),
        "sheets_read": sheets_read,
        "model": MODEL,
    }

    log.info(
        "Model brief for %s — %d facts, sheets: %s",
        file_path.name, len(facts), ", ".join(sheets_read),
    )
    if cache_p is not None:
        try:
            _BRIEF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with open(cache_p, "w") as fh:
                json.dump(result, fh)
        except Exception:
            pass
    return result


_FINALIZE_SYSTEM = """\
You are tightening a real estate deal brief so it asserts ONLY facts that were
independently verified. You are given the draft brief plus three fact lists:
VERIFIED (trustworthy), FLAGGED (a cross-check disagreed — surface but mark
unverified), and OMITTED (could not be confirmed — must NOT appear).

You may also be given CASH-FLOW VALIDATED facts (reconstructed and recomputed
from the model's cash flow — the strongest source) and BINDING CONSTRAINTS.

Rewrite the brief:
- When a CASH-FLOW VALIDATED fact is present, use ITS value (it overrides the
  draft and any VERIFIED value for the same metric). State returns, exit price,
  hold period and the capital stack from these.
- State VERIFIED numbers plainly, with their (Sheet!Cell).
- For a FLAGGED number, you may mention it but append "(unverified — to confirm)"
  and, if given, note the conflicting value.
- NEVER state an OMITTED number, and never invent one to fill the gap; just
  leave that point out.
- Obey every BINDING CONSTRAINT; never assert what one forbids.
- Keep the section structure (Overview, Key Deal Stats, Debt, Returns, Model
  Structure). Tight, executive, bullets — never markdown tables.

Return ONLY the markdown brief, no preamble, no fences.
"""


def _fact_line(f: dict) -> str:
    src = f"{f.get('sheet')}!{f.get('cell')}"
    return f"- {f.get('field')}: {f.get('display', f.get('value'))} ({src})"


def finalize_brief(brief: dict, scored: dict, canonical: list[dict] | None = None,
                   guardrails: list[str] | None = None) -> dict[str, Any]:
    """
    Regenerate the brief narrative so it asserts only verified facts — the
    "no wrong data" guarantee for what the user reads. `scored` is the
    trust_engine.score_facts output. `canonical` (from deal_truth.to_intel_facts)
    are cash-flow-validated facts that OVERRIDE the brief's own extraction, and
    `guardrails` (deal_truth.guardrail_lines) bind what may be asserted — so the
    brief itself, not just Layer 3, runs off the validated spine.

    Returns {"brief_md", "finalized": bool, "counts": {...}}. Falls back to the
    original brief + a deterministic verified/flagged appendix when no LLM is
    available or the call fails.
    """
    facts = scored.get("facts", []) or []
    verified = [f for f in facts if f.get("trust", {}).get("verdict") == "show"]
    flagged = [f for f in facts if f.get("trust", {}).get("verdict") == "flag"]
    omitted = [f for f in facts if f.get("trust", {}).get("verdict") == "omit"]
    canonical = canonical or []
    counts = {"verified": len(verified), "flagged": len(flagged), "omitted": len(omitted)}

    def _deterministic() -> str:
        parts = [brief.get("brief_md", "")]
        if canonical:
            parts.append("**Cash-flow validated:**\n"
                         + "\n".join(_fact_line(f) for f in canonical))
        if flagged:
            parts.append("**⚠ Flagged — verify before relying on these:**\n"
                         + "\n".join(_fact_line(f) for f in flagged))
        if omitted:
            parts.append("_Omitted (could not be confirmed): "
                         + ", ".join(f.get("field", "?") for f in omitted) + "._")
        return "\n\n".join(p for p in parts if p)

    if not verified and not flagged and not canonical:
        return {"brief_md": brief.get("brief_md", ""), "finalized": False, "counts": counts}
    if not llm_available():
        return {"brief_md": _deterministic(), "finalized": bool(canonical), "counts": counts}

    flagged_lines = []
    for f in flagged:
        better = (f.get("trust", {}).get("notes") or [""])[0]
        flagged_lines.append(_fact_line(f) + (f"  [{better}]" if better else ""))

    guard_block = ""
    if guardrails:
        guard_block = "\n\nBINDING CONSTRAINTS (obey every one):\n" + \
            "\n".join(f"- {g}" for g in guardrails)

    user_msg = (
        "DRAFT BRIEF:\n" + brief.get("brief_md", "") + "\n\n"
        "CASH-FLOW VALIDATED (authoritative — override the draft):\n"
        + ("\n".join(_fact_line(f) for f in canonical) or "(none)") + "\n\n"
        "VERIFIED:\n" + ("\n".join(_fact_line(f) for f in verified) or "(none)") + "\n\n"
        "FLAGGED:\n" + ("\n".join(flagged_lines) or "(none)") + "\n\n"
        "OMITTED:\n" + (", ".join(f.get("field", "?") for f in omitted) or "(none)")
        + guard_block
    )
    try:
        resp = client.chat.completions.create(
            model=MODEL, temperature=0.1,
            messages=[
                {"role": "system", "content": _FINALIZE_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
        )
        md = (resp.choices[0].message.content or "").strip()
        if md.startswith("```"):
            md = md.split("```")[1]
            if md.startswith("markdown"):
                md = md[8:]
        return {"brief_md": md.strip() or _deterministic(), "finalized": True, "counts": counts}
    except Exception as e:
        log.error("Brief finalize failed: %s", e)
        return {"brief_md": _deterministic(), "finalized": False, "counts": counts}


if __name__ == "__main__":
    import sys as _sys
    path = _sys.argv[1] if len(_sys.argv) > 1 else None
    if not path:
        print("usage: python model_brief.py <workbook.xlsx>")
        raise SystemExit(1)
    out = build_model_brief(path)
    if "error" in out:
        print("ERROR:", out["error"])
    else:
        print(out["brief_md"])
        print(f"\n--- {len(out['facts'])} facts, read {out['sheets_read']} ---")
