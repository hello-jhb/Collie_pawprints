"""Shared OpenAI client for scenario narrative generation."""

from __future__ import annotations

import json
import logging
import os
import sys
import streamlit as st
from openai import OpenAI
from typing import Any

# Logger that writes to stdout so messages appear in Streamlit Cloud logs.
log = logging.getLogger("fb.llm")
if not log.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("[fb.llm] %(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)


def _get_api_key() -> str | None:
    try:
        key = st.secrets.get("OPENAI_API_KEY", None)
    except Exception:
        key = None
    return key or os.getenv("OPENAI_API_KEY")


_client: OpenAI | None = None
_client_api_key: str | None = None


def get_client() -> OpenAI | None:
    """
    Return a live OpenAI client for the current Streamlit run.

    Streamlit re-executes app modules on rerun, while helper modules imported by
    other modules can stay cached. A client created once at import time can
    therefore get stuck as None if secrets/env were temporarily unavailable, or
    keep using an old key after a reconnect. Resolve it lazily instead.
    """
    global _client, _client_api_key

    api_key = _get_api_key()
    if not api_key:
        _client = None
        _client_api_key = None
        return None

    if _client is None or api_key != _client_api_key:
        _client = OpenAI(api_key=api_key)
        _client_api_key = api_key
    return _client


class _ClientProxy:
    """Compatibility shim for modules that imported `client` directly."""

    def __bool__(self) -> bool:
        return get_client() is not None

    def __getattr__(self, name: str) -> Any:
        live_client = get_client()
        if live_client is None:
            raise RuntimeError("OPENAI_API_KEY not set in environment or Streamlit secrets")
        return getattr(live_client, name)


client = _ClientProxy()

MODEL       = "gpt-4o"
MODEL_FAST  = "gpt-4o-mini"   # used for ingest-time insight pass (cost-sensitive)


def llm_available() -> bool:
    return get_client() is not None


def complete(system: str, user: str, temperature: float = 0.2) -> str:
    """Single chat completion. Returns the assistant text."""
    live_client = get_client()
    if live_client is None:
        return "[LLM unavailable — set OPENAI_API_KEY environment variable]"
    response = live_client.chat.completions.create(
        model=MODEL,
        temperature=temperature,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return response.choices[0].message.content


# ---------------------------------------------------------------------------
# Pass 2: targeted gap-fill + surface insights
# ---------------------------------------------------------------------------

_INSIGHT_SYSTEM = """\
You are a real estate analyst reading a workbook to support a downstream report.
A deterministic pipeline has already extracted known metrics from this file using
an alias catalog. The remaining fields — listed under "FIELDS TO FIND" in the
user message — were either missed by the catalog or are inferred characteristics.

Your job: for EACH field in FIELDS TO FIND, look in the raw workbook content
and either return a value or omit the field. Plus surface a few observations
the structured pipeline can't see.

HARD RULES:
- For every field in FIELDS TO FIND, scan the raw content. If you find a clear
  value, include it. If not, omit the field (do not invent).
- For "Total Debt" type fields: SUM all loans visible (acquisition + construction
  + mezz + senior). Show the math in label_in_file (e.g. "$15.84M + $35.46M").
- For "characterization" fields (property_type, deal_type, strategy, position):
  infer from structure even when no cell is explicitly labeled that way.
- Numeric fields → return number. Text fields (property_type, etc.) → return string.
- Do NOT re-report any field marked as ALREADY FOUND.
- Be decisive on inference. Use null only when truly unknowable.
- Return ONLY valid JSON. No prose, no markdown fences, no commentary.

JSON schema:
{
  "found": {
    "<field_name_from_FIELDS_TO_FIND>": {
      "value": <number | string | null>,
      "label_in_file": "<what cell/section the value came from, OR derivation>",
      "sheet": "<sheet name if applicable>",
      "confidence": "high" | "medium" | "low"
    }
  },
  "observations": [
    "<one sentence with specific value — analytically significant items
     not captured in any field above>"
  ],
  "model_summary": "<one sentence: what kind of model is this in plain English?>"
}
"""


def run_raw_insight_pass(
    labeled_pairs: list[dict],
    layer: str,
    source_file: str,
    found_metric_names: list[str] | None = None,
    fields_to_find: list[dict] | None = None,
) -> dict:
    """
    Focused Pass 2: given what the metric catalog already found (found_metric_names)
    and what it expected but missed (missing_metric_names), ask GPT to:
      1. Find the missing metrics in the raw file content
      2. Surface 3-5 observations not captured by any catalog metric

    Only sends high-quality labeled pairs (label_ratio >= 0.5) to reduce noise
    and token cost. Uses gpt-4o-mini (~$0.01 per file).

    Returns {} if LLM unavailable or call fails.
    """
    if not client:
        log.warning(
            "Pass 2 SKIPPED for %s — OpenAI client is None "
            "(OPENAI_API_KEY not set in env or Streamlit secrets)",
            source_file,
        )
        return {}
    if not labeled_pairs:
        log.warning("Pass 2 SKIPPED for %s — no labeled pairs to send", source_file)
        return {}

    log.info(
        "Pass 2 START for %s (layer=%s) — %d pairs, %d found, %d fields to find",
        source_file, layer, len(labeled_pairs),
        len(found_metric_names or []), len(fields_to_find or []),
    )

    # Filter to high-quality pairs only:
    #   - direction right/below: label directly precedes its value (high signal)
    #   - label_len >= 5: eliminates index headers, single-letter columns, etc.
    quality_pairs = [
        p for p in labeled_pairs
        if p.get("direction") in ("right", "below")
        and p.get("label_len", 0) >= 5
    ]
    # Fall back to all pairs if filtering leaves too few
    if len(quality_pairs) < 30:
        quality_pairs = labeled_pairs

    # Format as compact sheet-grouped text
    lines = []
    current_sheet = None
    for p in quality_pairs:
        if p["sheet"] != current_sheet:
            current_sheet = p["sheet"]
            lines.append(f"\n=== {current_sheet} ===")
        lines.append(f"  {p['label']:<45} {p['value']}")

    raw_text = "\n".join(lines)

    # Build the user message
    found_block = (
        "ALREADY FOUND BY PIPELINE (do not re-report):\n"
        + "\n".join(f"  - {n}" for n in (found_metric_names or []))
        + "\n"
    ) if found_metric_names else ""

    # Format the fields to find. Each entry: {name, type, hint}
    if fields_to_find:
        fields_block_lines = ["\nFIELDS TO FIND (scan raw content for each):"]
        for f in fields_to_find:
            line = f"  - {f['name']}"
            if f.get("type"):
                line += f"  [{f['type']}]"
            if f.get("hint"):
                line += f"  — {f['hint']}"
            fields_block_lines.append(line)
        fields_block = "\n".join(fields_block_lines) + "\n"
    else:
        fields_block = "\nFIELDS TO FIND: (none specified — return characterization only)\n"

    user_msg = (
        f"File: {source_file}  |  Layer: {layer}\n\n"
        f"{found_block}"
        f"{fields_block}"
        f"\nRAW FILE CONTENT:\n{raw_text}"
    )

    try:
        response = client.chat.completions.create(
            model=MODEL_FAST,
            temperature=0.1,
            messages=[
                {"role": "system", "content": _INSIGHT_SYSTEM},
                {"role": "user",   "content": user_msg},
            ],
        )
        raw = response.choices[0].message.content.strip()
        log.info(
            "Pass 2 RESPONSE for %s — %d chars, finish_reason=%s",
            source_file, len(raw), response.choices[0].finish_reason,
        )
        # Strip markdown fences if model adds them despite instructions
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        parsed = json.loads(raw)
        # New schema: {"found": {...}, "observations": [...], "model_summary": "..."}
        found = parsed.get("found", {}) or {}
        obs   = parsed.get("observations", []) or []
        summ  = parsed.get("model_summary", "") or ""
        non_null_count = sum(
            1 for v in found.values()
            if isinstance(v, dict) and v.get("value") is not None
        )
        log.info(
            "Pass 2 PARSED for %s — %d fields with values, %d observations, model_summary=%r",
            source_file, non_null_count, len(obs), summ[:80],
        )
        return parsed
    except json.JSONDecodeError as e:
        log.error(
            "Pass 2 JSON_PARSE_FAILED for %s — %s\nRaw response (first 500 chars): %s",
            source_file, e, raw[:500] if 'raw' in locals() else "<no response>",
        )
        return {}
    except Exception as e:
        log.error(
            "Pass 2 API_CALL_FAILED for %s — %s: %s",
            source_file, type(e).__name__, str(e),
        )
        return {}
