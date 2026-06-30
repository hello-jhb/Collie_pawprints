"""
metric_resolver.py — Phase 1 candidate ranking + schema validation.

Given a list of candidates from scan_workbook_for_candidates() and the metric's
schema (unit, scale, period, valid range, preferred sheets), this module:

  1. Filters out candidates that fail schema constraints
  2. Auto-corrects scale issues (e.g., value in $000s → multiply by 1,000)
  3. Ranks the survivors by preferred-sheet priority + signal quality
  4. Returns a verified metric record OR a candidate-pool record

The output shape is the new SSOT metric format:
  {
    raw_value, normalized_value, display_value,
    unit, scale, period,
    source_sheet, source_cell,
    confidence (extractor confidence: exact/high/medium/partial),
    status (verified | candidate_pool | suspicious | missing),
    validation_notes (list of strings),
    candidates  (full list, retained for resolver / audit)
  }

In Phase 2 a GPT resolver will be inserted between step 1 and step 3 to
disambiguate when multiple candidates survive validation.
"""
from __future__ import annotations
from functools import lru_cache
from typing import Any


RESOLVER_VERSION = "phase3.v10"  # identity checks run only after reconciliation


@lru_cache(maxsize=1)
def _active_knowledge_rule_ids() -> set[str]:
    try:
        from knowledge_store import load_active_patterns
        return {
            str(p.get("rule_id") or p.get("pattern_id"))
            for p in load_active_patterns()
            if p.get("scope") in ("metric_resolution", "validation")
        }
    except Exception:
        return set()


# ---------------------------------------------------------------------------
# Schema-driven validation
# ---------------------------------------------------------------------------

def _validate_against_range(value, schema: dict) -> tuple[bool, str | None]:
    """
    Return (passes, scale_correction_factor or None).

    If value is in range → passes=True.
    If value × 1000 is in range → passes=True, returns 1000 (auto-scale).
    If value × 1_000_000 is in range → passes=True, returns 1_000_000.
    Otherwise → passes=False.
    """
    # Non-numeric units (text, date) don't have numeric ranges — accept as-is
    unit = schema.get("unit")
    if unit in ("text", "date"):
        return True, None

    rmin = schema.get("range_min")
    rmax = schema.get("range_max")
    if rmin is None and rmax is None:
        return True, None  # no range constraint

    def _in_range(v):
        if rmin is not None and v < rmin:
            return False
        if rmax is not None and v > rmax:
            return False
        return True

    try:
        v = float(value)
    except (TypeError, ValueError):
        return False, None

    # Hold periods are often stored as month counts while the canonical catalog
    # unit is years. Let these through so reconciliation can normalize 60 -> 5y.
    if (
        schema.get("metric_name") == "Hold Period"
        and unit == "years"
        and 24 < v <= 360
    ):
        # Active JSON pattern hold_period_gt_24_means_months documents this
        # behavior. The code path remains hard-coded domain validation so the
        # system does not depend on GPT or observations to normalize it.
        _active_knowledge_rule_ids()
        return True, None

    if _in_range(v):
        return True, None
    if _in_range(v * 1_000):
        return True, "1000"
    if _in_range(v * 1_000_000):
        return True, "1000000"
    return False, None


def _preferred_sheet_score(sheet_name: str, preferred_sheets: list[str]) -> int:
    """
    Lower score = better match. Returns:
      0..N-1: index in preferred_sheets (first preferred → 0)
      999:    not in preferred list
    """
    if not preferred_sheets:
        return 100
    sn = sheet_name.lower()
    for i, pref_keyword in enumerate(preferred_sheets):
        if pref_keyword.lower() in sn:
            return i
    return 999


def _format_display(value, unit: str | None, scale: str | None) -> str:
    """Format a value for narrative use given its unit/scale."""
    if value is None:
        return "—"
    if unit == "text":
        return str(value)
    if unit == "date":
        import datetime as _dt
        if isinstance(value, _dt.datetime):
            return value.strftime("%Y-%m-%d")
        if isinstance(value, _dt.date):
            return value.isoformat()
        return str(value)
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)

    if unit == "USD":
        if abs(v) >= 1_000_000:
            return f"${v/1_000_000:,.2f}M"
        if abs(v) >= 1_000:
            return f"${v/1_000:,.0f}K"
        return f"${v:,.2f}"
    if unit == "ratio":
        if abs(v) < 1.5:           # treat as fraction → percentage
            return f"{v*100:.2f}%"
        return f"{v:,.2f}x"
    if unit == "percent":
        return f"{v:.2f}%"
    if unit == "months":
        if v >= 12:
            return f"{v:.0f} months ({v/12:.1f} yr)"
        return f"{v:.0f} months"
    if unit == "years":
        return f"{v:.1f} years"
    if unit == "count":
        return f"{int(v):,}"
    if unit == "sf":
        return f"{int(v):,} SF"
    return f"{v:,.2f}"


# ---------------------------------------------------------------------------
# Main resolver
# ---------------------------------------------------------------------------

def resolve_metric(metric: dict, candidates: list[dict]) -> dict:
    """
    Apply schema validation + preferred-sheet ranking to pick the best candidate.

    Returns a SSOT-shaped metric record.
    """
    notes: list[str] = []
    preferred_sheets = metric.get("preferred_sheets", []) or []
    unit  = metric.get("unit")
    scale = metric.get("scale")
    period = metric.get("period")

    if not candidates:
        return _make_record(
            metric=metric, candidate=None, normalized_value=None,
            status="missing", notes=["No candidates found in any scanned sheet."],
            candidates=[],
        )

    # Score every candidate
    scored = []
    for cand in candidates:
        c_notes: list[str] = []

        # Range / scale validation
        passes_range, scale_correction = _validate_against_range(cand["value"], metric)
        if not passes_range:
            c_notes.append(
                f"value {cand['value']} outside range "
                f"[{metric.get('range_min')}, {metric.get('range_max')}]"
            )
        elif scale_correction:
            c_notes.append(f"auto-scaled by {scale_correction} (raw was likely in 000s/M)")

        # Preferred sheet ranking
        pref_score = _preferred_sheet_score(cand["sheet"], preferred_sheets)

        # Annotate the candidate dict itself (so Phase 2 resolver can read it)
        cand["passes_validation"] = passes_range
        cand["scale_correction"]  = scale_correction
        cand["pref_score"]        = pref_score

        scored.append({
            "candidate":         cand,
            "passes_validation": passes_range,
            "scale_correction":  scale_correction,
            "pref_score":        pref_score,
            "notes":             c_notes,
        })

    # Filter to validation-passing candidates first
    passing = [s for s in scored if s["passes_validation"]]
    pool = passing or scored  # fallback to all candidates if none pass

    # Sort by:
    #   1. preferred_sheet_score (lower is better)
    #   2. sheet_tier (effective, lower is better)
    #   3. name_tier (one-pager beats secondary summaries on equal effective tier)
    #   4. hardcode rank — for modeler-input metrics (metric carries
    #      prefer_hardcoded, set by the AAM extractor), a hard-coded cell beats
    #      a formula cell at the same sheet rank. Candidates without the
    #      is_hardcoded annotation (legacy paths) are never penalized.
    #   5. extractor confidence
    _CONF_TIER = {"exact": 0, "high": 1, "medium": 2, "partial": 3}

    def _hard_rank(s) -> int:
        if not metric.get("prefer_hardcoded"):
            return 0
        return 1 if s["candidate"].get("is_hardcoded") is False else 0

    pool.sort(key=lambda s: (
        s["pref_score"],
        s["candidate"].get("sheet_tier", 99),
        s["candidate"].get("name_tier", 99),
        _hard_rank(s),
        _CONF_TIER.get(s["candidate"]["confidence"], 9),
        -s["candidate"].get("label_ratio", 0),
    ))

    top = pool[0]
    cand = top["candidate"]
    raw_value = cand["value"]

    # Apply scale correction if needed
    if top["scale_correction"] == "1000":
        normalized_value = float(raw_value) * 1000
    elif top["scale_correction"] == "1000000":
        normalized_value = float(raw_value) * 1_000_000
    else:
        normalized_value = raw_value

    # Preferred-sheet enforcement: if this metric declares preferred_sheets but
    # the chosen candidate is NOT on any of them (pref_score 999 = no match),
    # we don't trust the value — the authoritative source wasn't found, so the
    # value likely came from an unrelated sheet (e.g. Total Project Cost landing
    # on an F&B-sales cell in an expense-analysis tab). Mark off_preferred so the
    # downstream GPT fallback gets a chance to read the preferred sheets directly.
    has_preferred = bool(preferred_sheets)
    chosen_off_preferred = has_preferred and top["pref_score"] >= 999

    # Determine status
    if top["passes_validation"] and not chosen_off_preferred:
        if len(passing) > 1:
            # Multiple candidates passed schema — needs Phase 2 resolver
            status = "candidate_pool"
            notes.append(
                f"{len(passing)} candidates passed schema validation; "
                "top one taken by preferred-sheet rank. "
                "Phase 2 resolver will disambiguate."
            )
        else:
            status = "verified"
    elif top["passes_validation"] and chosen_off_preferred:
        # Value passed range check but came from a non-authoritative sheet.
        # Treat as missing so the GPT fallback re-reads the preferred sheets;
        # if fallback also fails, it will surface honestly rather than show
        # a confident wrong number from the wrong tab.
        status = "missing"
        notes.append(
            f"Best candidate is on '{cand.get('sheet')}' which is NOT a preferred "
            f"source for this metric ({', '.join(preferred_sheets)}). "
            "Routing to GPT fallback to read the authoritative sheet."
        )
    else:
        status = "suspicious"
        notes.append("No candidate passed schema validation. Top candidate by rank used; may be wrong.")

    notes.extend(top["notes"])

    return _make_record(
        metric=metric,
        candidate=cand,
        normalized_value=normalized_value,
        status=status,
        notes=notes,
        candidates=[s["candidate"] for s in scored],
    )


def _make_record(
    metric: dict,
    candidate: dict | None,
    normalized_value,
    status: str,
    notes: list[str],
    candidates: list[dict],
) -> dict:
    unit  = metric.get("unit")
    scale = metric.get("scale")
    period = metric.get("period")
    return {
        "metric_id":         metric["metric_id"],
        "metric_name":       metric["metric_name"],
        "raw_value":         candidate["value"] if candidate else None,
        "normalized_value":  normalized_value,
        "display_value":     _format_display(normalized_value, unit, scale),
        "unit":              unit,
        "scale":             scale,
        "period":            period,
        "source_sheet":      candidate["sheet"] if candidate else None,
        "source_cell":       candidate["value_cell"] if candidate else None,
        "sheet_tier":        candidate.get("sheet_tier") if candidate else None,
        "extractor_confidence": candidate["confidence"] if candidate else None,
        "status":            status,
        "validation_notes":  notes,
        "candidates":        candidates,  # full list, for audit / Phase 2 resolver
        "audit":             {
            "accepted": (
                {"value": candidate["value"], "cell": candidate.get("value_cell"),
                 "sheet": candidate.get("sheet"), "method": "proximity_fallback"}
                if candidate else None
            ),
            "rejected": [],
            "conflicts": [],
        },
        "in_bounded_list":   metric.get("in_bounded_list", False),
    }


# ---------------------------------------------------------------------------
# Phase 3 — validation of a section-reader extraction (authority-first path)
# ---------------------------------------------------------------------------

def _role_of_sheet(sheet_name, sheet_role_map) -> str | None:
    if not sheet_name or not sheet_role_map:
        return None
    info = sheet_role_map.get(sheet_name)
    if isinstance(info, dict):
        return info.get("role")
    if isinstance(info, str):
        return info
    return None


def parse_numeric_value(raw, unit: str | None = None):
    """
    Robustly coerce a GPT-returned value into a number where the metric is
    numeric. Handles the formats GPT commonly emits:
        "$287,425"     → 287425.0
        "287,425"      → 287425.0
        "8.17%"        → 0.0817      (percent → fraction)
        "0.0817"       → 0.0817
        "(2,719,030)"  → -2719030.0  (accounting negatives)
        "2.0x"         → 2.0
        "$192.0M"      → 192000000.0
        "5 years"      → 5.0
        123 / 1.4      → unchanged

    Returns (parsed_value, ok). For text/date units, returns (raw, True) so the
    caller keeps the string. ok=False means it couldn't be parsed as a number.
    """
    if unit in ("text", "date"):
        return raw, True
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw), True
    if not isinstance(raw, str):
        return raw, False

    s = raw.strip()
    if not s:
        return raw, False

    # Accounting negative: (1,234) → -1,234
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()

    is_percent = s.endswith("%")
    # Magnitude suffixes
    mult = 1.0
    low = s.lower()
    if low.endswith("m") and not low.endswith("mm"):
        mult, s = 1_000_000.0, s[:-1]
    elif low.endswith("mm"):
        mult, s = 1_000_000.0, s[:-2]
    elif low.endswith("bn") or low.endswith("b"):
        mult, s = 1_000_000_000.0, s.rstrip("bBnN")
    elif low.endswith("k"):
        mult, s = 1_000.0, s[:-1]
    elif low.endswith("x"):
        s = s[:-1]  # multiple, no magnitude change

    # Strip currency, commas, %, stray words (years, months), whitespace
    import re as _re
    s = s.replace("$", "").replace(",", "").replace("%", "")
    s = _re.sub(r"[a-zA-Z]+", "", s).strip()

    try:
        num = float(s) * mult
    except (ValueError, TypeError):
        return raw, False

    if negative:
        num = -num
    if is_percent:
        num = num / 100.0
    return num, True


def _values_disagree(a, b, tol: float = 0.02) -> bool:
    """True if two values differ beyond tolerance (2% for numbers; exact for text)."""
    pa, oka = parse_numeric_value(a)
    pb, okb = parse_numeric_value(b)
    if oka and okb and isinstance(pa, (int, float)) and isinstance(pb, (int, float)):
        if pa == pb:
            return False
        denom = max(abs(pa), abs(pb), 1e-9)
        return abs(pa - pb) / denom > tol
    return str(a).strip().lower() != str(b).strip().lower()


def build_section_record(metric: dict, extraction: dict, sheet_role_map: dict | None) -> dict | None:
    """
    Build a verified SSOT record from a Section Reader extraction, OR return None
    to signal the value was rejected (caller then tries the proximity fallback).

    extraction shape (from section_reader.read_section):
        {found, value, cell, sheet, column_header, reasoning, alt?}

    Validation gates (any failure → reject → None):
      1. found must be true with a value
      2. forbidden-source: the value's sheet role must not be in source_forbidden
      3. range/unit: value within schema bounds (auto-scale ×1000/×1M if needed)

    Conflict (status="conflict"): an 'alt' value from another authoritative tab
    that disagrees beyond tolerance.

    Every record carries an `audit` block:
        {accepted: {...}, rejected: [...], conflicts: [...]}
    """
    unit  = metric.get("unit")
    scale = metric.get("scale")
    period = metric.get("period")
    audit: dict = {"accepted": None, "rejected": [], "conflicts": []}

    if not extraction or not extraction.get("found"):
        return None

    raw_value = extraction.get("value")
    sheet = extraction.get("sheet")
    cell  = extraction.get("cell")
    role  = _role_of_sheet(sheet, sheet_role_map)
    # Normalize one_pager -> summary for source_primary / source_forbidden
    # membership: catalog hierarchy lists (authored in the xlsx) use 'summary'.
    from re_knowledge import hierarchy_role
    h_role = hierarchy_role(role)
    reasoning = extraction.get("reasoning", "")
    observed_period = extraction.get("period") or extraction.get("column_header")

    if raw_value in (None, "", "—"):
        return None

    # Gate 1.5 — ROBUST NUMERIC PARSING.
    # GPT returns "$287,425", "8.17%", "(2,719,030)", "$192M", etc. Coerce to a
    # number before range validation; text/date pass through unchanged.
    value, parse_ok = parse_numeric_value(raw_value, unit)
    if unit not in ("text", "date") and not parse_ok:
        audit["rejected"].append({
            "value": raw_value, "cell": cell, "sheet": sheet, "role": role,
            "reason": f"could not parse '{raw_value}' as a number for unit={unit}",
        })
        return None

    # Gate 2 — forbidden source
    forbidden = metric.get("source_forbidden") or []
    if h_role and h_role in forbidden:
        audit["rejected"].append({
            "value": raw_value, "cell": cell, "sheet": sheet, "role": role,
            "reason": f"forbidden source role '{role}' for this metric",
        })
        return None

    # Gate 2.5 — PRIMARY-SOURCE ENFORCEMENT.
    # If the metric declares primary roles, the accepted value must come from
    # one of them. A value from a non-primary, non-forbidden role (e.g. GPT
    # pulled NOI off a debt tab) is rejected → proximity fallback. If the
    # sheet role is unknown (classification gap), we allow it but note it.
    primary = metric.get("source_primary") or []
    if primary and h_role is not None and h_role not in primary:
        audit["rejected"].append({
            "value": raw_value, "cell": cell, "sheet": sheet, "role": role,
            "reason": f"role '{role}' is not a primary source {primary} for this metric",
        })
        return None

    # Gate 3 — range / unit (with auto-scale). Text/date short-circuit inside.
    passes, scale_correction = _validate_against_range(value, metric)
    if not passes:
        audit["rejected"].append({
            "value": value, "cell": cell, "sheet": sheet, "role": role,
            "reason": f"out of range [{metric.get('range_min')}, {metric.get('range_max')}]",
        })
        return None

    if scale_correction == "1000":
        normalized = float(value) * 1000
    elif scale_correction == "1000000":
        normalized = float(value) * 1_000_000
    else:
        normalized = value

    notes: list[str] = [f"Section reader: {reasoning}"] if reasoning else []
    if scale_correction:
        notes.append(f"auto-scaled by {scale_correction}")

    # Conflict detection via alt
    status = "verified"
    alt = extraction.get("alt")
    if isinstance(alt, dict) and alt.get("value") not in (None, "", "—"):
        if _values_disagree(value, alt["value"]):
            status = "conflict"
            audit["conflicts"].append({
                "value": alt.get("value"), "cell": alt.get("cell"), "sheet": alt.get("sheet"),
            })
            notes.append(
                f"CONFLICT: authoritative sources disagree — "
                f"{sheet}!{cell}={value} vs {alt.get('sheet')}!{alt.get('cell')}={alt.get('value')}"
            )

    audit["accepted"] = {
        "raw": raw_value, "value": value, "normalized": normalized,
        "cell": cell, "sheet": sheet, "role": role,
        "observed_period": observed_period,
        "method": "section_reader", "reason": reasoning,
    }

    return {
        "metric_id":            metric["metric_id"],
        "metric_name":          metric["metric_name"],
        "raw_value":            raw_value,
        "normalized_value":     normalized,
        "display_value":        _format_display(normalized, unit, scale),
        "unit":                 unit,
        "scale":                scale,
        "period":               period,
        "observed_period":      observed_period,
        "source_sheet":         sheet,
        "source_cell":          cell,
        "sheet_tier":           None,
        "extractor_confidence": "section_reader",
        "status":               status,
        "validation_notes":     notes,
        "candidates":           [],
        "audit":                audit,
        "in_bounded_list":      metric.get("in_bounded_list", False),
    }


# ---------------------------------------------------------------------------
# Cache key — Phase 1 versioning
# ---------------------------------------------------------------------------

def make_cache_key(
    file_hash: str,
    catalog_version: str,
    extractor_version: str | None = None,
    resolver_version: str | None = None,
) -> str:
    """
    Build a versioned cache key. Any version bump invalidates old cached results.
    """
    import hashlib
    if extractor_version is None:
        from flexible_extractor import EXTRACTOR_VERSION as ev
        extractor_version = ev
    if resolver_version is None:
        resolver_version = RESOLVER_VERSION
    # Knowledge layer + sheet classifier are part of how extraction reasons,
    # so their versions belong in the cache key — editing RE knowledge or the
    # classifier invalidates stale cached extractions automatically.
    try:
        from re_knowledge import KNOWLEDGE_VERSION
    except Exception:
        KNOWLEDGE_VERSION = "na"
    try:
        from sheet_classifier import CLASSIFIER_VERSION
    except Exception:
        CLASSIFIER_VERSION = "na"
    try:
        from section_reader import SECTION_READER_VERSION
    except Exception:
        SECTION_READER_VERSION = "na"
    composite = (
        f"{file_hash}|{catalog_version}|{extractor_version}|{resolver_version}"
        f"|{KNOWLEDGE_VERSION}|{CLASSIFIER_VERSION}|{SECTION_READER_VERSION}"
    )
    return hashlib.sha256(composite.encode("utf-8")).hexdigest()
