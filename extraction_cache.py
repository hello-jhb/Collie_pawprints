"""
extraction_cache.py — file-hash based cache for Phase 1 bounded-metric extraction.

Cache layout:
  cache/extractions/<cache_key>.json

Where cache_key = sha256(file_hash + catalog_version + extractor_version + resolver_version).
Any version bump invalidates all stale entries automatically.

Stored content:
  {
    "cache_key":     "...",
    "file_name":     "...",
    "file_hash":     "...",
    "catalog_version":   "...",
    "extractor_version": "...",
    "resolver_version":  "...",
    "cached_at":     "ISO-8601 timestamp",
    "bounded_metrics": { metric_name: record_dict, ... }
  }
"""
from __future__ import annotations
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CACHE_DIR = Path("cache/extractions")


def file_sha256(file_path: Path, chunk_size: int = 65_536) -> str:
    """Compute SHA-256 of a file's binary content."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def cache_path(cache_key: str) -> Path:
    """Where a given cache entry lives on disk."""
    return CACHE_DIR / f"{cache_key}.json"


def load_cached(cache_key: str) -> dict | None:
    """
    Return the cached entry for this key, or None if no entry exists.
    Handles corrupt files by returning None (silent re-extract).
    """
    p = cache_path(cache_key)
    if not p.exists():
        return None
    try:
        with open(p, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_cache(
    cache_key: str,
    file_name: str,
    file_hash: str,
    catalog_version: str,
    extractor_version: str,
    resolver_version: str,
    bounded_metrics: dict[str, Any],
) -> Path:
    """Write a cache entry. Returns the path on disk."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "cache_key":         cache_key,
        "file_name":         file_name,
        "file_hash":         file_hash,
        "catalog_version":   catalog_version,
        "extractor_version": extractor_version,
        "resolver_version":  resolver_version,
        "cached_at":         datetime.now(timezone.utc).isoformat(),
        "bounded_metrics":   bounded_metrics,
    }
    p = cache_path(cache_key)
    with open(p, "w") as f:
        json.dump(entry, f, indent=2, default=str)
    return p


def clear_all() -> int:
    """Delete every cache entry. Returns count of files removed."""
    if not CACHE_DIR.exists():
        return 0
    count = 0
    for p in CACHE_DIR.glob("*.json"):
        try:
            p.unlink()
            count += 1
        except OSError:
            pass
    return count
