"""
knowledge_store.py - safe runtime bridge for Collie's JSON knowledge layer.

There are three separate concepts:

1. observations/ are deal-specific reviewed evidence. They are never loaded by
   runtime extraction or prompt construction.
2. patterns/ are distilled reusable knowledge. Only entries with
   status == "active" can influence runtime.
3. runtime knowledge block is a small prompt fragment built from active
   patterns only.

GPT may suggest observations, but GPT does not decide truth. Promotion remains:
Observation -> Hypothesis -> Rule, with human approval represented by an active
status in the pattern catalog.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any


log = logging.getLogger("fb.knowledge_store")

KNOWLEDGE_DIR = Path("knowledge")
PATTERNS_DIR = KNOWLEDGE_DIR / "patterns"
OBSERVATIONS_DIR = KNOWLEDGE_DIR / "observations"

INACTIVE_STATUSES = {"candidate", "draft", "rejected", "superseded", "archived", "inactive"}
VALID_SCOPES = {"workbook_mapping", "metric_resolution", "validation", "business_plan", "chat"}

PATTERN_FILES = {
    "model_patterns": "model_patterns.json",
    "metric_patterns": "metric_patterns.json",
    "business_plan_patterns": "business_plan_patterns.json",
}

# Auto-synthesized learning candidates live in their own file, separate from the
# curated pattern files, so the learning loop never edits hand-authored rules.
LEARNED_FILE = "learned_patterns.json"


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _rule_from_metric(metric: dict[str, Any], rule: dict[str, Any]) -> dict[str, Any]:
    pattern = rule.get("pattern") or {}
    interpretation = pattern.get("interpretation") or {}
    return {
        "rule_id": rule.get("rule_id"),
        "pattern_id": rule.get("rule_id"),
        "status": rule.get("status"),
        "scope": rule.get("scope") or "metric_resolution",
        "description": rule.get("description") or "; ".join(metric.get("observations", [])[:2]),
        "condition": rule.get("condition") or pattern.get("condition"),
        "action": rule.get("action") or interpretation or pattern,
        "confidence": rule.get("confidence"),
        "evidence_count": rule.get("evidence_count"),
        "source_file": "metric_patterns.json",
        "metric": metric.get("metric"),
        "canonical_unit": metric.get("canonical_unit"),
    }


def _rule_from_model(pattern: dict[str, Any]) -> dict[str, Any]:
    evidence = pattern.get("evidence") or {}
    return {
        "rule_id": pattern.get("rule_id") or pattern.get("pattern_id"),
        "pattern_id": pattern.get("pattern_id"),
        "status": pattern.get("status"),
        "scope": pattern.get("scope") or "workbook_mapping",
        "description": pattern.get("description") or f"Workbook pattern for {pattern.get('model_type')}",
        "condition": pattern.get("condition") or pattern.get("signals"),
        "action": pattern.get("action") or pattern.get("common_structure"),
        "confidence": pattern.get("confidence", 0.0),
        "evidence_count": pattern.get("evidence_count", evidence.get("evidence_count", 0)),
        "source_file": "model_patterns.json",
    }


def _rule_from_business(pattern: dict[str, Any]) -> dict[str, Any]:
    evidence = pattern.get("evidence") or {}
    return {
        "rule_id": pattern.get("rule_id") or pattern.get("pattern_id"),
        "pattern_id": pattern.get("pattern_id"),
        "status": pattern.get("status"),
        "scope": pattern.get("scope") or "business_plan",
        "description": pattern.get("description") or f"Business-plan pattern for {pattern.get('property_type')}",
        "condition": pattern.get("condition") or pattern.get("signals"),
        "action": pattern.get("action") or pattern.get("analysis_guidance"),
        "confidence": pattern.get("confidence", 0.0),
        "evidence_count": pattern.get("evidence_count", evidence.get("evidence_count", 0)),
        "source_file": "business_plan_patterns.json",
    }


def _iter_pattern_rules(base_dir: Path) -> list[dict[str, Any]]:
    patterns_dir = base_dir / "patterns"
    rules: list[dict[str, Any]] = []

    metric_payload = _read_json(patterns_dir / PATTERN_FILES["metric_patterns"])
    for metric in metric_payload.get("metrics", []):
        for rule in metric.get("rules", []):
            rules.append(_rule_from_metric(metric, rule))

    model_payload = _read_json(patterns_dir / PATTERN_FILES["model_patterns"])
    for pattern in model_payload.get("patterns", []):
        rules.append(_rule_from_model(pattern))

    business_payload = _read_json(patterns_dir / PATTERN_FILES["business_plan_patterns"])
    for pattern in business_payload.get("patterns", []):
        rules.append(_rule_from_business(pattern))

    # Auto-synthesized learning candidates (already in runtime-rule shape).
    # status=candidate is ignored at runtime; status=active (human-promoted) is
    # loaded like any curated active rule.
    learned_payload = _read_json(patterns_dir / LEARNED_FILE)
    for rule in learned_payload.get("rules", []):
        r = dict(rule)
        r.setdefault("source_file", LEARNED_FILE)
        rules.append(r)

    return rules


def validate_pattern(pattern: dict[str, Any]) -> list[str]:
    """
    Validate the minimal runtime schema. Invalid patterns are skipped and logged.

    Required fields:
    - rule_id or pattern_id
    - status
    - scope
    - description
    - confidence
    - evidence_count
    """
    errors: list[str] = []
    if not (pattern.get("rule_id") or pattern.get("pattern_id")):
        errors.append("missing rule_id or pattern_id")
    if not pattern.get("status"):
        errors.append("missing status")
    if pattern.get("scope") not in VALID_SCOPES:
        errors.append(f"invalid scope {pattern.get('scope')!r}")
    if not pattern.get("description"):
        errors.append("missing description")
    if pattern.get("confidence") is None:
        errors.append("missing confidence")
    if pattern.get("evidence_count") is None:
        errors.append("missing evidence_count")
    return errors


@lru_cache(maxsize=1)
def _load_pattern_catalog(base_dir_str: str = str(KNOWLEDGE_DIR)) -> dict[str, Any]:
    base_dir = Path(base_dir_str)
    rules = _iter_pattern_rules(base_dir)
    active: list[dict[str, Any]] = []
    ignored = 0
    invalid: list[dict[str, Any]] = []

    for rule in rules:
        errors = validate_pattern(rule)
        if errors:
            invalid.append({
                "rule_id": rule.get("rule_id") or rule.get("pattern_id"),
                "status": rule.get("status"),
                "source_file": rule.get("source_file"),
                "errors": errors,
            })
            log.warning("Invalid knowledge pattern skipped: %s", invalid[-1])
            continue

        status = str(rule.get("status")).lower()
        if status == "active":
            active.append({
                **rule,
                "confidence": _as_float(rule.get("confidence")),
                "evidence_count": _as_int(rule.get("evidence_count")),
            })
        else:
            if status in INACTIVE_STATUSES or status != "active":
                ignored += 1

    return {
        "active_patterns": active,
        "candidate_patterns_ignored": ignored,
        "invalid_patterns": invalid,
        "total_patterns": len(rules),
    }


def load_active_patterns(base_dir: Path | str = KNOWLEDGE_DIR) -> list[dict[str, Any]]:
    """
    Return only active runtime patterns. This never reads observations/.
    """
    return list(_load_pattern_catalog(str(Path(base_dir)))["active_patterns"])


def knowledge_diagnostics(base_dir: Path | str = KNOWLEDGE_DIR) -> dict[str, Any]:
    """Return counts and validation warnings for Analyst Bundle visibility."""
    catalog = _load_pattern_catalog(str(Path(base_dir)))
    return {
        "active_patterns_loaded": len(catalog["active_patterns"]),
        "candidate_patterns_ignored": catalog["candidate_patterns_ignored"],
        "invalid_patterns": catalog["invalid_patterns"],
    }


def build_runtime_knowledge_block(
    scopes: list[str] | None = None,
    base_dir: Path | str = KNOWLEDGE_DIR,
) -> str:
    """
    Convert active JSON rules into a concise prompt block.

    Inactive patterns and observations are excluded. Empty output is deliberate
    and safe: static fallback knowledge in re_knowledge.py still applies.
    """
    active = load_active_patterns(base_dir)
    if scopes:
        wanted = set(scopes)
        active = [p for p in active if p.get("scope") in wanted]
    if not active:
        return ""

    grouped: dict[str, list[dict[str, Any]]] = {}
    for pattern in active:
        grouped.setdefault(pattern["scope"], []).append(pattern)

    lines = ["ACTIVE JSON KNOWLEDGE PATTERNS (human-approved; do not override verified facts):"]
    for scope in ("workbook_mapping", "metric_resolution", "validation", "business_plan", "chat"):
        scoped = grouped.get(scope, [])
        if not scoped:
            continue
        lines.append(f"\n[{scope}]")
        for p in scoped:
            lines.append(
                "- "
                f"{p.get('rule_id') or p.get('pattern_id')}: "
                f"{p.get('description')} | "
                f"condition={p.get('condition')} | "
                f"action={p.get('action')} | "
                f"confidence={p.get('confidence')} | "
                f"evidence_count={p.get('evidence_count')}"
            )
    return "\n".join(lines)


def with_active_rules(system_prompt: str, scopes: list[str]) -> str:
    """
    Append the active-pattern runtime block to a GPT system prompt.

    No-op (returns the prompt unchanged) when no active patterns match the
    scopes — so wiring this into a call site never changes behavior until a
    human promotes a pattern to `active`.
    """
    block = build_runtime_knowledge_block(scopes)
    if not block:
        return system_prompt
    return (
        system_prompt
        + "\n\n===== ACTIVE KNOWLEDGE (human-approved rules; apply when relevant, "
          "never override verified facts) =====\n"
        + block
    )


def load_observations(base_dir: Path | str = KNOWLEDGE_DIR) -> list[dict[str, Any]]:
    """
    Debug/UI helper only. Do not call from extraction or prompt runtime paths.
    """
    observations_dir = Path(base_dir) / "observations"
    out: list[dict[str, Any]] = []
    for path in sorted(observations_dir.glob("*.json")):
        out.append(_read_json(path))
    return out


# =============================================================================
# Learning-candidate pipeline: user override -> observation -> candidate -> active
# (GPT never self-teaches; a human promotes candidates. Candidates and
#  observations NEVER influence runtime — only `active` patterns do.)
# =============================================================================

def record_observation(
    *,
    metric_id: str,
    metric_name: str,
    engine_value: Any,
    actual_value: Any,
    source_sheet: str | None = None,
    source_cell: str | None = None,
    source_file: str | None = None,
    base_dir: Path | str = KNOWLEDGE_DIR,
) -> str:
    """
    Capture a human override as a deal-specific observation (never runtime).
    Returns the observation_id.
    """
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    obs_id = f"{metric_id}_{ts}"
    obs = {
        "observation_id": obs_id,
        "created_at": _now_iso(),
        "status": "unreviewed",
        "review": {"source": "user_override", "reviewer": "human", "verified": True, "notes": ""},
        "file_context": {"source_file": source_file, "source_file_hash": None},
        "observation": {
            "metric": metric_id,
            "metric_name": metric_name,
            "engine_result": engine_value,
            "actual_source": {"sheet": source_sheet, "cell": source_cell},
            "actual_value": actual_value,
            "lesson": f"{metric_id}_corrected_by_user",
        },
    }
    _write_json(Path(base_dir) / "observations" / f"{obs_id}.json", obs)
    return obs_id


def load_learned_patterns(base_dir: Path | str = KNOWLEDGE_DIR) -> list[dict[str, Any]]:
    """All auto-synthesized learning rules (any status). UI/debug helper."""
    payload = _read_json(Path(base_dir) / "patterns" / LEARNED_FILE)
    return list(payload.get("rules", []))


def synthesize_candidates(min_evidence: int = 3,
                          base_dir: Path | str = KNOWLEDGE_DIR) -> list[dict[str, Any]]:
    """
    Distill repeated user-override observations into CANDIDATE rules (drafts for
    human review). status=candidate never affects runtime. A human promotes a
    candidate to `active` to make it influence extraction. Returns the current
    candidate list. Idempotent: re-running updates evidence counts in place and
    never disturbs a promoted/rejected candidate's status.
    """
    base = Path(base_dir)
    groups: dict[str, list[dict]] = {}
    for o in load_observations(base):
        if (o.get("review") or {}).get("source") != "user_override":
            continue
        mid = (o.get("observation") or {}).get("metric")
        if mid:
            groups.setdefault(mid, []).append(o)

    learned_path = base / "patterns" / LEARNED_FILE
    payload = _read_json(learned_path)
    by_id = {r.get("rule_id"): r for r in payload.get("rules", [])}
    changed = False

    for mid, obs_list in groups.items():
        if len(obs_list) < min_evidence:
            continue
        rule_id = f"{mid}_user_corrected"
        ids = [o["observation_id"] for o in obs_list]
        existing = by_id.get(rule_id)
        if existing:
            # Refresh evidence but never override a human decision.
            existing["evidence_count"] = len(obs_list)
            existing["evidence_observation_ids"] = ids
            changed = True
            continue
        last = obs_list[-1]["observation"]
        by_id[rule_id] = {
            "rule_id":        rule_id,
            "status":         "candidate",          # NOT active — human must promote
            "scope":          "metric_resolution",
            "description":    (
                f"'{last.get('metric_name', mid)}' was human-corrected "
                f"{len(obs_list)} time(s); engine commonly returned "
                f"'{last.get('engine_result')}', actual e.g. '{last.get('actual_value')}'. "
                f"Review whether a reusable extraction/validation rule is warranted."
            ),
            "condition":      f"metric == '{mid}'",
            "action":         "review extraction source / units for this metric",
            "confidence":     0.3,
            "evidence_count": len(obs_list),
            "contradiction_count": 0,
            "evidence_sources": ["user_override"],
            "evidence_observation_ids": ids,
            "created_at":     _now_iso(),
        }
        changed = True

    if changed:
        _write_json(learned_path, {
            "version": _now_iso(),
            "description": (
                "Auto-synthesized learning candidates from user overrides. "
                "status=candidate NEVER affects runtime; a human promotes to "
                "active after review."
            ),
            "rules": list(by_id.values()),
        })
        _load_pattern_catalog.cache_clear()

    return [r for r in by_id.values() if str(r.get("status")).lower() == "candidate"]


def set_pattern_status(rule_id: str, status: str,
                       base_dir: Path | str = KNOWLEDGE_DIR) -> bool:
    """
    Human decision on a learned candidate: 'active' (promote → influences
    runtime), 'rejected', or back to 'candidate'. Returns True if found.
    """
    learned_path = Path(base_dir) / "patterns" / LEARNED_FILE
    payload = _read_json(learned_path)
    rules = payload.get("rules", [])
    found = False
    for r in rules:
        if r.get("rule_id") == rule_id:
            r["status"] = status
            r["reviewed_at"] = _now_iso()
            found = True
    if found:
        _write_json(learned_path, payload)
        _load_pattern_catalog.cache_clear()
    return found
