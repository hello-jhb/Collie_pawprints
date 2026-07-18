"""
property_registry — canonical property identity for the portfolio spine (M1).

Sits ON TOP of property_id.property_identity() (the Tier-3 workbook identity
reader). That module answers "what does THIS workbook say it is"; this one
answers "which property on the spine is that" — turning the many names a
property goes by across files into ONE canonical property_id so N workbooks
can hang off the same spine.

Design rules (M1 design doc, agreed 2026-07-18):
  * Pure, generic rules — NO property names, filenames, or workbook-specific
    patterns may ever be hardcoded here. Development samples stay out of code.
  * Canonical name comes from the workbook's identity extraction; the filename
    is a fallback and is flagged as such (identity_source="filename").
  * The alias map is runtime data. It ships EMPTY and is populated as files
    are ingested. Unmatched names create a NEW property and are flagged
    NEW_PROPERTY — never fuzzy-merged silently (flag, never substitute).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ASSETS_DIR = Path("assets")
ALIAS_FILE = ASSETS_DIR / "property_aliases.json"

# Tokens that describe the FILE/MODEL, not the property. Stripped before
# slugging. Generic modelling vocabulary only — never property-specific.
NOISE_TOKENS = {
    "model", "models", "proforma", "pro", "forma", "closing", "vclosing",
    "underwriting", "uw", "final", "draft", "version", "ver", "rev",
    "revised", "update", "updated", "copy", "adept", "xlsx", "xls",
    "workbook", "file", "mf", "v1", "v2", "v3", "v4", "v5",
}

# Date-ish / version-ish fragments: 2022, 03.08.2022, 2023-04-21, 02.19.2022
_DATE_RE = re.compile(
    r"""
    \b\d{4}[-._ ]\d{1,2}[-._ ]\d{1,2}\b       # 2023-04-21 / 2023.04.21
    | \b\d{1,2}[-._ ]\d{1,2}[-._ ]\d{2,4}\b   # 03.08.2022 / 2.19.22
    | \b(19|20)\d{2}\b                        # bare year
    """,
    re.VERBOSE,
)


def canonicalize_name(raw: str) -> str:
    """
    Clean a raw name (from workbook identity OR filename) down to the
    property's display name. Generic: strips extension, dates, model-noise
    tokens, and separator clutter. Keeps street numbers — they identify.
    """
    name = str(raw or "").strip()
    name = re.sub(r"\.(xlsx|xlsm|xls|csv)$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"[_()\[\]]+", " ", name)   # separators first, so \b works
    name = _DATE_RE.sub(" ", name)
    # Hyphens used as section separators become spaces; in-word hyphens stay.
    name = re.sub(r"\s+-\s+", " ", name)
    kept = [
        tok for tok in name.split()
        if tok.lower().strip(".,") not in NOISE_TOKENS
    ]
    return " ".join(kept).strip()


def slugify(name: str) -> str:
    """Lowercase, alphanumeric + hyphens. '1425 4th Ave' -> '1425-4th-ave'."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(name).lower())
    return slug.strip("-")


# -----------------------------------------------------------------------------
# Alias map — runtime data, ships empty
# -----------------------------------------------------------------------------

def load_aliases() -> dict[str, str]:
    """observed variant (lowercased) -> canonical property_id."""
    if not ALIAS_FILE.exists():
        return {}
    with open(ALIAS_FILE, "r") as f:
        return json.load(f)


def save_aliases(aliases: dict[str, str]) -> None:
    ASSETS_DIR.mkdir(exist_ok=True)
    with open(ALIAS_FILE, "w") as f:
        json.dump(aliases, f, indent=2, sort_keys=True)


def register_alias(variant: str, property_id: str) -> None:
    """Record that an observed name variant refers to a known property."""
    aliases = load_aliases()
    aliases[str(variant).strip().lower()] = property_id
    save_aliases(aliases)


# -----------------------------------------------------------------------------
# Resolution
# -----------------------------------------------------------------------------

def resolve_property_id(
    raw_name: str | None,
    *,
    filename: str | None = None,
    known_ids: set[str] | None = None,
) -> dict[str, Any]:
    """
    Resolve a canonical property_id for an ingested workbook.

    `raw_name`  — property name from the workbook's own identity extraction
                  (property_id.property_identity()["name"]). Preferred source.
    `filename`  — fallback when the workbook yields no usable name. Flagged.
    `known_ids` — property ids that already exist (defaults to alias-map values).

    Returns {property_id, display_name, identity_source, flags, matched_alias}.
    Flags: NEW_PROPERTY (id not seen before), FILENAME_IDENTITY (fallback
    used), NO_IDENTITY (nothing to go on — caller must ask the user).
    """
    flags: list[str] = []
    aliases = load_aliases()
    known = set(known_ids) if known_ids is not None else set(aliases.values())

    if raw_name and str(raw_name).strip():
        source_text, identity_source = str(raw_name), "workbook"
    elif filename and str(filename).strip():
        source_text = Path(str(filename)).name
        identity_source = "filename"
        flags.append("FILENAME_IDENTITY")
    else:
        return {
            "property_id": None, "display_name": None,
            "identity_source": None, "flags": ["NO_IDENTITY"],
            "matched_alias": None,
        }

    # 1) exact alias hit on the raw text (pre-canonicalization)
    alias_hit = aliases.get(source_text.strip().lower())
    display = canonicalize_name(source_text)
    # 2) alias hit on the canonicalized name
    if alias_hit is None and display:
        alias_hit = aliases.get(display.lower())

    if alias_hit:
        return {
            "property_id": alias_hit, "display_name": display or source_text,
            "identity_source": identity_source, "flags": flags,
            "matched_alias": source_text.strip().lower(),
        }

    if not display:
        return {
            "property_id": None, "display_name": None,
            "identity_source": identity_source,
            "flags": flags + ["NO_IDENTITY"], "matched_alias": None,
        }

    pid = slugify(display)
    if pid not in known:
        flags.append("NEW_PROPERTY")
    return {
        "property_id": pid, "display_name": display,
        "identity_source": identity_source, "flags": flags,
        "matched_alias": None,
    }


# Generic financial/spreadsheet vocabulary that can never BE a property name
# on its own (a lone "Rate" / "Total" cell picked up by the identity reader).
# Multi-word names containing these words are fine ("Rate Street Lofts").
_IMPLAUSIBLE_LONE_NAMES = {
    "rate", "rates", "total", "totals", "value", "values", "income",
    "revenue", "expense", "expenses", "summary", "cost", "costs", "price",
    "amount", "yield", "return", "returns", "cap", "noi", "irr", "debt",
    "equity", "cash", "flow", "annual", "monthly", "budget", "actual",
    "actuals", "forecast", "plan", "exit", "hold", "period", "date",
}


def _plausible_workbook_name(display: str) -> bool:
    """A one-token, purely generic financial word is a misread, not a name."""
    toks = display.split()
    if len(toks) >= 2:
        return True
    return bool(toks) and toks[0].lower() not in _IMPLAUSIBLE_LONE_NAMES


def resolve_for_workbook(file_path: str | Path,
                         known_ids: set[str] | None = None) -> dict[str, Any]:
    """
    Convenience: run the Tier-3 identity reader on a workbook, then resolve
    the canonical property_id (workbook name preferred, filename fallback).
    A workbook-supplied name that fails the plausibility guard is rejected
    (flagged WORKBOOK_NAME_REJECTED) and the filename is used instead —
    flag, never guess.
    """
    raw_name = None
    try:
        from property_id import property_identity
        idf = property_identity(file_path)
        name_field = (idf or {}).get("name") or {}
        if name_field.get("status") in ("found", "conflict"):
            raw_name = name_field.get("value")
    except Exception:
        raw_name = None  # unreadable workbook → filename fallback below

    rejected = False
    if raw_name and not _plausible_workbook_name(canonicalize_name(raw_name)):
        raw_name, rejected = None, True

    res = resolve_property_id(
        raw_name, filename=str(file_path), known_ids=known_ids
    )
    if rejected:
        res["flags"].append("WORKBOOK_NAME_REJECTED")
    return res
