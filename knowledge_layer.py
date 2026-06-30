"""
Compatibility wrapper for the JSON knowledge layer.

New runtime code should import knowledge_store directly. This module keeps the
older public functions available while delegating to the safe active-pattern
store. Observations are never loaded here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from knowledge_store import (
    KNOWLEDGE_DIR,
    build_runtime_knowledge_block,
    knowledge_diagnostics,
    load_active_patterns as _load_active_pattern_list,
)


def load_active_patterns(base_dir: Path | str = KNOWLEDGE_DIR) -> dict[str, Any]:
    active = _load_active_pattern_list(base_dir)
    return {
        "model_patterns": [p for p in active if p.get("scope") == "workbook_mapping"],
        "metric_patterns": [
            p for p in active
            if p.get("scope") in ("metric_resolution", "validation", "chat")
        ],
        "business_plan_patterns": [p for p in active if p.get("scope") == "business_plan"],
    }


def active_metric_rules(metric_name: str | None = None) -> list[dict[str, Any]]:
    rules = [
        p for p in _load_active_pattern_list()
        if p.get("scope") in ("metric_resolution", "validation", "chat")
    ]
    if metric_name:
        rules = [p for p in rules if p.get("metric") == metric_name]
    return rules


def knowledge_summary() -> dict[str, Any]:
    diagnostics = knowledge_diagnostics()
    return {
        "active_patterns_loaded": diagnostics["active_patterns_loaded"],
        "candidate_patterns_ignored": diagnostics["candidate_patterns_ignored"],
        "invalid_patterns": len(diagnostics["invalid_patterns"]),
    }


__all__ = [
    "active_metric_rules",
    "build_runtime_knowledge_block",
    "knowledge_summary",
    "load_active_patterns",
]
