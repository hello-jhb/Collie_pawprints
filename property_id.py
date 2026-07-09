"""
property_id.py — Tier-3 property identity (Name · Type · Size) for the Investment
Summary header.

This is NOT validated like the cash-flow spine: it's a best-effort read of labelled
cells, so every field carries a status (found | not_found | conflict). Per the
Phase-0 decision, a miss is stated explicitly ("not found" / "conflict") rather than
left blank — an absent identity is itself a model-quality signal.

Universal, not tuned to one workbook: asset TYPE and SIZE are inferred from the
unit-of-measure the model counts in (keys → hotel, units → multifamily, SF/GLA →
commercial), which is layout-agnostic; NAME comes from an explicit label or a
prominent cover/title cell.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from cashflow_rollup import _load_grids

_TOP_ROWS = 120          # identity lives near the top of a sheet

_NAME_LABEL = re.compile(r"\b(property|deal|project|asset|investment)\s*name\b", re.I)
_TYPE_LABEL = re.compile(r"\b(property|asset)\s*(type|class)\b|\bproduct type\b|\bsector\b", re.I)

# Asset-class words, and the unit each class is counted in.
_CLASS_WORDS = [
    ("Hotel",        re.compile(r"\bhotel\b|hospitality|\bresort\b|\blodging\b", re.I)),
    ("Multifamily",  re.compile(r"multifamily|multi-family|apartment|\bresidential\b", re.I)),
    ("Office",       re.compile(r"\boffice\b", re.I)),
    ("Retail",       re.compile(r"\bretail\b|shopping cent|\bmall\b", re.I)),
    ("Industrial",   re.compile(r"industrial|warehouse|logistics|distribution", re.I)),
    ("Self-Storage", re.compile(r"self[- ]storage|\bstorage\b", re.I)),
    ("Student",      re.compile(r"student housing", re.I)),
    ("Senior",       re.compile(r"senior living|senior housing|assisted living", re.I)),
]

# Unit-of-measure → (class hint, unit label, plausible value range).
_SIZE_KINDS = [
    ("keys",  re.compile(r"\b(hotel\s*)?keys\b|\b(total\s*)?rooms\b|room count|# ?of ?rooms|"
                         r"number of rooms", re.I),
     "Hotel", (5, 10000)),
    ("units", re.compile(r"# ?of ?units|number of units|unit count|total units|\bunits\b", re.I),
     "Multifamily", (2, 20000)),
    ("SF",    re.compile(r"\bnra\b|\bgla\b|net rentable|rentable (sf|area|square)|\brsf\b|"
                         r"total sf|square (feet|footage)|gross leasable", re.I),
     "Commercial", (1000, 50_000_000)),
]

# Operating-count rows (a monthly figure, NOT the property's fixed size) that share
# the size vocabulary — e.g. "Occupied Rooms", "Leased Units". Excluded everywhere.
_OPERATING_COUNT = re.compile(r"occupied|available|sold|vacant|leased|absorb|in service", re.I)

# $/rate/revenue/expense rows that mention a unit but carry a dollar or per-unit
# figure, not a count — e.g. "Total Rooms Bonus & Incentives", "$ per GLA", and the
# classic false friend "R&M - Locks & Keys" (a repairs line, not hotel keys).
_SIZE_NOISE = re.compile(r"bonus|incentive|revenue|\brev\b|expense|payroll|income|\badr\b|"
                         r"\bcost\b|rate|\bper\b|\$|amount|margin|profit|"
                         r"r&m|repair|maintenance|\block(s)?\b|janitor|cleaning|supplies", re.I)

# An explicit class and a unit-of-measure vote agree when they share a FAMILY (Office
# is the specific form of the SF-counted "Commercial" vote) — then the more specific
# explicit label wins. They conflict only across families (e.g. label Office vs keys).
_FAMILY = {"Hotel": "keys", "Multifamily": "units", "Student": "units", "Senior": "units",
           "Office": "SF", "Retail": "SF", "Industrial": "SF", "Self-Storage": "SF",
           "Commercial": "SF"}
_VOTE_FAMILY = {"Hotel": "keys", "Multifamily": "units", "Commercial": "SF"}

# A label that denotes a PROPERTY-LEVEL total (vs one tower / one line).
_TOTAL_HINT = re.compile(r"\btotal\b|hotel keys|# ?of ?(units|keys)|number of (units|keys|rooms)",
                         re.I)


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.replace(",", "").replace("$", "").strip()
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _neighbors(grid, r, c):
    """Cell values to the right (next 3) and directly below a label — where its
    value usually sits."""
    out = []
    row = grid[r]
    for cc in range(c + 1, min(c + 4, len(row))):
        if row[cc] not in (None, ""):
            out.append(row[cc])
    if r + 1 < len(grid) and c < len(grid[r + 1]) and grid[r + 1][c] not in (None, ""):
        out.append(grid[r + 1][c])
    return out


# Identity is read ONLY from declaration sheets, in this order — never from `model`
# tabs (the operating proforma / historicals / expense detail, where rows like
# "R&M-Locks & Keys" or "Occupied Rooms" live and would be misread as the asset's
# size). This mirrors the engine's static-fact discipline (workbook_map only scans
# summary/inputs/returns/support roles, not model).
_ROLE_RANK = {"summary": 0, "inputs": 1, "returns": 2, "support": 3, "other": 4}


def _sheet_roles(file_path: str | Path) -> dict[str, int]:
    """{sheet: role-rank} from the engine's orientation; lower rank = higher
    priority. Sheets with role 'model' are omitted (excluded from identity)."""
    try:
        from workbook_orientation import orient_workbook
        rm = (orient_workbook(file_path) or {}).get("map", {})
    except Exception:
        rm = {}
    out: dict[str, int] = {}
    for role, rank in _ROLE_RANK.items():
        for s in rm.get(role, []):
            out[s] = rank
    return out


def _scan(file_path: str | Path):
    grids = _load_grids(Path(file_path))
    roles = _sheet_roles(file_path)
    cells = []           # (sheet, r, c, text, grid)
    for sheet, grid in grids.items():
        if "comp" in sheet.lower():        # comparables describe OTHER properties
            continue
        if roles and sheet not in roles:   # a `model`/operating sheet — not for identity
            continue
        for r in range(min(_TOP_ROWS, len(grid))):
            for c, cell in enumerate(grid[r]):
                if isinstance(cell, str) and cell.strip():
                    cells.append((sheet, r, c, cell.strip(), grid))
    # Declaration sheets first (summary/inputs before support/other), so the most
    # authoritative value is seen first by every picker.
    cells.sort(key=lambda t: (roles.get(t[0], 5), t[0], t[1], t[2]))
    return cells


def _field(value, source, status="found", alternatives=None):
    return {"value": value, "source": source, "status": status,
            "alternatives": alternatives or []}


_NAME_NOISE = re.compile(r"balance|currency|\bloan\b|\bmax\b|%|company|management|city|state",
                         re.I)
# Generic header/placeholder values that are never a real property name.
_NAME_STOP = {"date", "name", "address", "n/a", "na", "tbd", "none", "total", "year",
              "type", "property", "asset", "deal", "project", "-", "—", "period",
              "month", "months", "quarter", "actual", "actuals", "budget", "forecast",
              "proforma", "pro forma", "summary", "input", "inputs", "value", "amount"}


def _good_name(v: str) -> bool:
    s = v.strip()
    return (2 < len(s) < 60 and _num(s) is None and "/" not in s and ":" not in s
            and not _NAME_NOISE.search(s) and s.lower() not in _NAME_STOP)


# Generic spreadsheet/software tab words that recur as a sheet prefix but are NOT a
# property name ("Argus Drop"/"Argus Occ" → Argus; "Cash Flows" → Cash).
_SHEET_GENERIC = {"argus", "cash", "debt", "model", "summary", "monthly", "annual",
                  "chart", "charts", "input", "inputs", "assumption", "assumptions",
                  "waterfall", "comp", "comps", "historical", "historicals", "proforma",
                  "desktop", "lease", "rent", "data", "output", "sources", "uses",
                  "disclaimer", "cover", "index", "schedule", "summ", "main", "deal"}


def _name_from_sheets(sheetnames) -> str | None:
    """The property name is often the common leading word across its tabs
    ("Chapman Rent Roll", "Chapman Lease Activity", …) — a reliable identifier when
    it isn't a generic Argus/Cash/Debt tab word."""
    from collections import Counter
    firsts = []
    for s in sheetnames:
        w = re.split(r"[\s\-_]+", str(s).strip())[0]
        if len(w) >= 3 and w.isalpha() and w.lower() not in _NAME_STOP \
                and w.lower() not in _SHEET_GENERIC:
            firsts.append(w)
    if not firsts:
        return None
    word, n = Counter(firsts).most_common(1)[0]
    return word if n >= 2 else None


def _pick_name(cells, sheetnames):
    # 1) An explicit Property/Deal/Asset Name label — but NOT on a comps sheet
    #    (those name comparable properties, not the subject).
    cands = []           # (value, source)
    for sheet, r, c, text, grid in cells:
        if "comp" in sheet.lower():
            continue
        if _NAME_LABEL.search(text):
            for nb in _neighbors(grid, r, c):
                if isinstance(nb, str) and _good_name(nb):
                    cands.append((nb.strip(), f"{sheet}!R{r+1}C{c+2}"))
                    break
    seen: dict[str, str] = {}
    for v, s in cands:
        seen.setdefault(v.lower(), (v, s))
    uniq = list(seen.values())
    if len(uniq) == 1:
        return _field(uniq[0][0], uniq[0][1])
    if len(uniq) > 1:
        return _field(uniq[0][0], uniq[0][1], "conflict", [v for v, _ in uniq[1:5]])
    # 2) Else a non-generic common sheet-name prefix ("Chapman Rent Roll", …).
    sn = _name_from_sheets(sheetnames)
    if sn:
        return _field(sn, "sheet-name prefix")
    return _field(None, None, "not_found")          # SPA falls back to the filename


def _pick_type(cells):
    # 1) A CLEAN explicit "Property/Asset Type" label (not an "Asset Type - X %"
    #    allocation breakdown) whose neighbour names a class.
    explicit = None
    for sheet, r, c, text, grid in cells:
        if not _TYPE_LABEL.search(text) or "%" in text or "allocation" in text.lower():
            continue
        for nb in _neighbors(grid, r, c):
            if isinstance(nb, str) and "%" not in nb:
                for cls, pat in _CLASS_WORDS:
                    if pat.search(nb):
                        explicit = (cls, f"{sheet}!R{r+1}C{c+2}")
                        break
            if explicit:
                break
        if explicit:
            break
    # 2) Infer from the unit-of-measure the model COUNTS in (layout-agnostic, robust
    #    to allocation-field noise) — the dominant vote wins.
    votes: dict[str, int] = {}
    src: dict[str, str] = {}
    for sheet, r, c, text, grid in cells:
        if _OPERATING_COUNT.search(text) or _SIZE_NOISE.search(text):
            continue
        for _, pat, cls_hint, (lo, hi) in _SIZE_KINDS:
            if pat.search(text) and any((n := _num(nb)) is not None and lo <= n <= hi
                                        for nb in _neighbors(grid, r, c)):
                votes[cls_hint] = votes.get(cls_hint, 0) + 1
                src.setdefault(cls_hint, f"{sheet}!R{r+1}C{c+1}")
    voted = max(votes, key=votes.get) if votes else None
    # A clean explicit label wins when there's no strong signal, or when it's in the
    # SAME family as the vote (it just refines it, e.g. "Office" under an SF vote).
    if explicit and (not voted or max(votes.values()) < 2
                     or _FAMILY.get(explicit[0]) == _VOTE_FAMILY.get(voted)):
        return _field(explicit[0], explicit[1]), explicit[0]
    if voted:
        return _field(voted, f"inferred from unit-of-measure ({src[voted]})"), voted
    if explicit:
        return _field(explicit[0], explicit[1]), explicit[0]
    return _field(None, None, "not_found"), None


def _column_total(grid, r, c, lo, hi):
    """When a size label is a COLUMN HEADER atop a table (e.g. '# of Units' over a
    unit-mix grid), the property total is that column's 'Total' row — NOT the first
    data cell directly below it. Walk column `c` down from the header; if a row whose
    left-label reads 'Total…' carries an in-range number, that's the answer. Returns
    the total or None (None → caller falls back to the adjacent-cell heuristic, so a
    lone 'Units: 8' statement is unaffected)."""
    # Must look like a header: the cell immediately below is numeric.
    below = grid[r + 1][c] if (r + 1 < len(grid) and c < len(grid[r + 1])) else None
    if _num(below) is None:
        return None
    seen = 0
    for rr in range(r + 1, min(r + 60, len(grid))):
        row = grid[rr]
        n = _num(row[c]) if c < len(row) else None
        if n is None:
            if seen:      # end of the contiguous numeric column
                break
            continue
        # Left-label for this row (nearest text scanning left).
        label = ""
        for cc in range(c - 1, max(-1, c - 9) - 1, -1):
            if cc < len(row) and isinstance(row[cc], str) and row[cc].strip():
                label = row[cc]
                break
        if seen >= 1 and re.search(r"\btotal\b", label, re.I) and lo <= n <= hi:
            return round(n)
        seen += 1
    return None


def _pick_size(cells, type_cls):
    from collections import Counter
    # Prefer the size kind matching the inferred class, else the strongest signal.
    order = sorted(_SIZE_KINDS, key=lambda k: 0 if k[2] == type_cls else 1)
    for unit, pat, _cls, (lo, hi) in order:
        vals, totals = [], []        # (value, source); totals = property-level labels
        for sheet, r, c, text, grid in cells:
            if not pat.search(text) or _OPERATING_COUNT.search(text) or _SIZE_NOISE.search(text):
                continue
            # A column-header count (unit-mix / key-count table) → take the column's
            # 'Total' row, not the first data row directly under the header.
            ct = _column_total(grid, r, c, lo, hi)
            if ct is not None:
                rec = (ct, f"{sheet}!R{r + 1}C{c + 1}")
                vals.append(rec)
                totals.append(rec)
                continue
            for nb in _neighbors(grid, r, c):
                n = _num(nb)
                if n is not None and lo <= n <= hi:
                    rec = (round(n), f"{sheet}!R{r+1}C{c+2}")
                    vals.append(rec)
                    if _TOTAL_HINT.search(text):
                        totals.append(rec)
                    break
        if totals:
            # A property-level "total"/"# of" label is the whole asset — take the
            # largest (a consolidated multi-property total beats one tower).
            v, s = max(totals, key=lambda x: x[0])
            return _field({"amount": int(v), "unit": unit}, s)
        if vals:
            # Else the size is a FIXED figure repeated across sheets — the mode,
            # tie-broken to the larger; not the max, which grabs an operating peak.
            cnt = Counter(v for v, _ in vals)
            top = max(cnt.items(), key=lambda kv: (kv[1], kv[0]))[0]
            src = next(s for v, s in vals if v == top)
            return _field({"amount": int(top), "unit": unit}, src)
    return _field(None, None, "not_found")


def property_identity(file_path: str | Path) -> dict[str, Any]:
    """{name, type, size} — each {value, source, status}. Tier-3, best-effort."""
    cells = _scan(file_path)
    sheetnames = sorted({c[0] for c in cells})
    name = _pick_name(cells, sheetnames)
    type_f, type_cls = _pick_type(cells)
    size = _pick_size(cells, type_cls)
    return {"name": name, "type": type_f, "size": size}


def identity_line(idf: dict) -> str:
    """One-line summary for the render, stating misses explicitly."""
    def show(f, kind):
        st = f.get("status")
        if st == "not_found" or f.get("value") in (None, ""):
            return f"{kind} not found"
        if kind == "Size":
            v = f["value"]
            return f"{v['amount']:,} {v['unit']}" + (" (conflict)" if st == "conflict" else "")
        return f"{f['value']}" + (" (conflict)" if st == "conflict" else "")
    return f"{show(idf['name'],'Name')} · {show(idf['type'],'Type')} · {show(idf['size'],'Size')}"
