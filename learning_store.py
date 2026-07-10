"""
learning_store.py — the feed-forward memory for GPT resolution decisions.

Today a human-found extraction bug becomes a one-off Python patch. This store closes
that loop: every time GPT resolves an ambiguity the deterministic engine couldn't
(reviewing a headline fact, filling a missing concept, reading an unvalidated summary),
we append a structured record of WHAT was seen and WHAT was decided. Over many files
those records reveal recurring patterns — a label that keeps mapping to the same
concept, a sheet that keeps getting excluded — which a human (or a later job) can
PROMOTE into the deterministic layer (an alias-catalog entry, an invariant, a rule).

Design:
- Append-only JSONL, one decision per line — human-inspectable, trivially aggregatable,
  never blocks analyze (all writes are best-effort; a failure is swallowed).
- Path is env-configurable (`COLLIE_LEARNING_DIR`); disable with `COLLIE_LEARNING=0`.
- `promotion_candidates()` surfaces patterns recurring across DISTINCT files — a one-off
  is noise, the same correction on three different files is a rule worth coding.

PRODUCTION NOTE: Cloud Run's filesystem is ephemeral — a local JSONL is fine for local
runs and for a single warm instance, but does NOT persist across instances/deploys.
For durable capture in production, point `COLLIE_LEARNING_DIR` at a mounted GCS volume
(gcsfuse) or swap `_append` for a GCS/Firestore writer. The record schema is unchanged.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

log = logging.getLogger("fb.learning")
if not log.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("[fb.learning] %(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

LEARNING_STORE_VERSION = "learning_store.v1"

_LOCK = threading.Lock()


def _dir() -> Path:
    """Resolved each call so COLLIE_LEARNING_DIR can be set at runtime / in tests."""
    return Path(os.getenv("COLLIE_LEARNING_DIR", str(Path(__file__).parent / "learning")))


def _events_file() -> Path:
    return _dir() / "events.jsonl"


def _enabled() -> bool:
    return os.getenv("COLLIE_LEARNING", "1").lower() not in ("0", "false", "no")


# --- Backend selection ------------------------------------------------------
# Local JSONL is the default (dev/tests). In production set COLLIE_LEARNING_BACKEND
# =firestore to durably capture across ephemeral Cloud Run instances. Everything
# degrades to a no-op if the backend is misconfigured — capture must never block
# analyze, and a missing dependency/credential must not crash the app.
_FS_MAX_READ = 5000
_fs_client = None


def _backend() -> str:
    return os.getenv("COLLIE_LEARNING_BACKEND", "local").lower()


def _fs_collection() -> str:
    return os.getenv("COLLIE_LEARNING_COLLECTION", "collie_learning_events")


def _firestore():
    """Lazy Firestore client (uses Application Default Credentials — automatic on
    Cloud Run via the service account). Raises if the lib/creds are absent; callers
    catch and degrade."""
    global _fs_client
    if _fs_client is None:
        from google.cloud import firestore  # lazy import — optional dependency
        _fs_client = firestore.Client(project=os.getenv("COLLIE_GCP_PROJECT") or None)
    return _fs_client

# Decision vocabulary — what GPT did relative to the deterministic pick.
AGREED = "agreed"        # GPT confirmed the engine's value
CORRECTED = "corrected"  # GPT replaced a weak engine value (adopted)
REJECTED = "rejected"    # GPT proposed a change that failed an invariant/range (not adopted)
FILLED = "filled"        # GPT supplied a value the engine had none for
READ = "read"            # GPT read a whole unvalidated summary (Tier 2)


def file_fingerprint(path: str | Path) -> str | None:
    """Short content hash so re-uploads of the same workbook group together (the same
    file analyzed 5 times is ONE source of truth, not five votes for a pattern)."""
    try:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return "sha1:" + h.hexdigest()[:16]
    except Exception:
        return None


def _append(event: dict) -> None:
    """Best-effort append of one decision. NEVER raises — a broken store (missing
    dependency, no credentials, read-only FS) must not break analyze."""
    if not _enabled():
        return
    try:
        if _backend() == "firestore":
            _firestore().collection(_fs_collection()).add(event)
            return
        d = _dir()
        d.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, default=str, ensure_ascii=False)
        with _LOCK, open(d / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:  # pragma: no cover - defensive
        log.debug("learning append skipped (%s backend, %s: %s)", _backend(),
                  type(e).__name__, e)


def record_resolution(
    *, layer: str, concept: str, decision: str,
    file: str | None = None, file_hash: str | None = None, label: str | None = None,
    prior_value: Any = None, prior_source: str | None = None,
    chosen_value: Any = None, chosen_cell: str | None = None,
    confidence: str | None = None, reason: str | None = None,
    extra: dict | None = None,
) -> None:
    """Record one GPT resolution decision. `label` is the promotable signal — the cell
    label / context that drove the decision, which is what a future rule keys on."""
    ev = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "v": LEARNING_STORE_VERSION,
        "layer": layer, "concept": concept, "decision": decision,
        "file": file, "file_hash": file_hash, "label": label,
        "prior_value": prior_value, "prior_source": prior_source,
        "chosen_value": chosen_value, "chosen_cell": chosen_cell,
        "confidence": confidence, "reason": reason,
    }
    if extra:
        ev["extra"] = extra
    _append(ev)


def backend_status() -> dict:
    """What the store is writing to right now + whether it's reachable — surfaced in
    the dashboard so you can see at a glance that capture is live in production."""
    b = _backend()
    st = {"backend": b, "enabled": _enabled()}
    if b == "firestore":
        st["collection"] = _fs_collection()
        try:
            _firestore().collection(_fs_collection()).limit(1).get()
            st["reachable"] = True
        except Exception as e:
            st["reachable"] = False
            st["error"] = f"{type(e).__name__}: {e}"
    else:
        st["path"] = str(_events_file())
        st["reachable"] = _dir().exists() or True   # created lazily on first write
    return st


def read_events(limit: int | None = None) -> list[dict]:
    """All recorded events (oldest first), or the last `limit`. Reads from whichever
    backend is active; returns [] on any read error (a broken store never breaks a
    report)."""
    if _backend() == "firestore":
        try:
            q = _firestore().collection(_fs_collection()).order_by("ts").limit(_FS_MAX_READ)
            out = [d.to_dict() for d in q.stream()]
            return out[-limit:] if limit else out
        except Exception as e:  # pragma: no cover - defensive
            log.warning("firestore read failed (%s: %s)", type(e).__name__, e)
            return []
    ev_file = _events_file()
    if not ev_file.exists():
        return []
    out: list[dict] = []
    try:
        with open(ev_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except OSError:
        return []
    return out[-limit:] if limit else out


def summarize() -> dict:
    """Counts by layer, decision, and concept — a quick health read of the loop."""
    ev = read_events()
    by: dict[str, Any] = {"total": len(ev),
                          "by_layer": defaultdict(int),
                          "by_decision": defaultdict(int),
                          "by_concept": defaultdict(int),
                          "files": set()}
    for e in ev:
        by["by_layer"][e.get("layer")] += 1
        by["by_decision"][e.get("decision")] += 1
        by["by_concept"][e.get("concept")] += 1
        if e.get("file_hash"):
            by["files"].add(e["file_hash"])
    by["distinct_files"] = len(by["files"])
    del by["files"]
    by["by_layer"] = dict(by["by_layer"])
    by["by_decision"] = dict(by["by_decision"])
    by["by_concept"] = dict(by["by_concept"])
    return by


def _norm(s: str | None) -> str:
    return " ".join((s or "").lower().split())


def promotion_candidates(min_files: int = 2) -> list[dict]:
    """Patterns worth promoting into the deterministic layer: the same
    (layer, concept, label) resolution recurring across >= min_files DISTINCT files.

    A correction seen once is a one-off; the same label→concept mapping on several
    unrelated files is a rule the alias catalog / engine should own. Returns groups
    sorted by distinct-file count, each with a representative example."""
    groups: dict[tuple, dict] = {}
    for e in read_events():
        if e.get("decision") not in (CORRECTED, FILLED, REJECTED):
            continue
        key = (e.get("layer"), e.get("concept"), _norm(e.get("label")))
        g = groups.setdefault(key, {"layer": key[0], "concept": key[1], "label": e.get("label"),
                                    "decisions": defaultdict(int), "files": set(),
                                    "examples": []})
        g["decisions"][e.get("decision")] += 1
        if e.get("file_hash"):
            g["files"].add(e["file_hash"])
        if len(g["examples"]) < 3:
            g["examples"].append({k: e.get(k) for k in
                                  ("file", "prior_value", "chosen_value", "chosen_cell", "reason")})
    out = []
    for g in groups.values():
        nfiles = len(g["files"]) or 1
        if nfiles >= min_files:
            out.append({"layer": g["layer"], "concept": g["concept"], "label": g["label"],
                        "distinct_files": nfiles, "decisions": dict(g["decisions"]),
                        "examples": g["examples"]})
    out.sort(key=lambda x: (-x["distinct_files"], x["concept"]))
    return out


def main() -> None:
    """`python3 learning_store.py` — the human review surface: loop health + the
    patterns recurring across enough files to be worth promoting into Python."""
    s = summarize()
    print(f"Learning store backend: {backend_status()}")
    print(f"  events={s['total']}  distinct_files={s['distinct_files']}")
    print(f"  by layer:    {s['by_layer']}")
    print(f"  by decision: {s['by_decision']}")
    print(f"  by concept:  {s['by_concept']}")
    cands = promotion_candidates(min_files=int(os.getenv("PROMOTE_MIN_FILES", "2")))
    print(f"\nPromotion candidates (recur across ≥{os.getenv('PROMOTE_MIN_FILES', '2')} "
          f"distinct files): {len(cands)}")
    for c in cands:
        print(f"  • [{c['layer']}] {c['concept']}  label={c['label']!r}  "
              f"files={c['distinct_files']}  {c['decisions']}")
        for ex in c["examples"][:2]:
            print(f"      e.g. {ex.get('file')}: {ex.get('prior_value')} -> "
                  f"{ex.get('chosen_value')} @ {ex.get('chosen_cell')}  ({ex.get('reason')})")


if __name__ == "__main__":
    main()
