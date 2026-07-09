# Work Order — Ingestion robustness: stop hard-failing to "Load failed"

**Owner:** Claude Code
**Priority:** High (user-facing regression; erodes trust in the product)
**Filed:** 2026-07-09
**Repro assets:** two real underwriting models that a prior version could read but the
current version cannot — attach/commit as fixtures (see §5):
- `425 Colorado Proforma_03.08.2022_Adept.xlsx`
- `Westview Austin - Model_02.19.2022.xlsx`

---

## 1. Problem statement

A user uploads a valid, healthy `.xlsx` underwriting model and the app dies with
**"Load failed."** From the user's seat this reads as *"this thing can't even open my
file"* — worst-case churn ("I'll just use a consumer LLM"). The files are **not**
corrupt: both open with `openpyxl` in `read_only` mode in under 0.3s and every sheet,
value, formula, and summary section is intact. **The reader is what's failing, not the
file.** A previous version ingested these same two files (imperfectly, but it reported
what they were). The current build regressed to a hard failure.

The fix is two-fold:
1. **Never let a readable workbook produce an opaque failure.** If the deep engine can't
   validate a cash-flow model, still open the file and report *what it is* (property,
   sheet inventory, detected summary) — degrade, don't reject.
2. **Remove the performance/timeout cliff** that these "bloated" workbooks trigger.

---

## 2. Evidence / root-cause analysis

### 2a. "Load failed" is a network-layer error, not a clean server error
`static/index.html` (~line 394) treats `/load failed|failed to fetch|network error/i`
as a **network** failure and shows *"Could not reach the server…"*. This string is
thrown by the browser `fetch()` in `postAnalyze()` when the `/api/analyze` request is
**severed** — i.e. the request timed out, the instance OOM-crashed, or Cloud Run killed
it mid-flight. So the server isn't returning a handled error; the request is **dying**.

### 2b. The two files are pathological but valid — "defined-name bloat"
Measured on the actual files:

| File | Sheets | Defined names | of which `#REF!` (dead) | Names actually used by a formula | External links |
|---|---|---|---|---|---|
| 425 Colorado | 12 | **23,828** | 2,572 | **~10** | **34** (orphaned) |
| Westview Austin | 15 | **8,642** | 5,516 | ~55 | 0 |

The names are junk (`_`, `__`, `___mg3`, `________TX3`…), the classic residue of copying
sheets between workbooks repeatedly; each copy duplicates every named range and old ones
rot to `#REF!`. `xl/workbook.xml` in the Colorado file is **2.6 MB** (a healthy one is a
few KB). The 34 external links in the Colorado file are **orphaned** — no worksheet
formula references them (`[n]`-style refs = 0), so Excel/openpyxl still tries to resolve
them on a full parse for nothing.

### 2c. The performance cliff: full (non-`read_only`) loads
`read_only=True` skips the defined-name / external-link object graph; a full load builds
it. Measured:

| File | `read_only=True` | full `load_workbook` |
|---|---|---|
| 425 Colorado | 0.28s | **2.49s** (~9×) |
| Westview | 0.14s | **1.25s** (~9×) |

2.5s once is survivable, but the pipeline does **many** loads, several of them **full**:

- `formula_tracer.py:76-77` — **two full loads back-to-back** (`data_only=False` and
  `data_only=True`) on the same file (~5s combined on Colorado).
- Full-load call sites: `section_reader.py:164`, `flexible_extractor.py:517/758/882/1046`,
  `tools.py:1273`, `workbook_map.py:441`, `concept_fallback.py:96`,
  `metric_resolver_gpt.py:437`, `metric_fallback.py:83`.

Stacked across `build_deal_truth` → `build_analysis` → `build_investment_read`, plus the
LLM calls below, wall-clock can exceed the Cloud Run request timeout → request severed →
browser "Load failed."

### 2d. Regression window — the gpt-5.4 migration (commit `6b05a46`, today)
`Migrate gpt-4o → gpt-5.4; temperature → reasoning_effort` swapped every narrative/lookup
call to reasoning models. `scenarios/_llm.py` creates the OpenAI client with **no
`timeout=`** set (SDK default is long). If any of the now-reasoning calls in the analyze
path (`concept_fallback`, `metric_resolver_gpt`, `metric_fallback`, `section_reader`,
`sheet_classifier`, `trust_engine`, …) hang or slow down, the whole request blows the
Cloud Run timeout → "Load failed." This is the most likely *trigger* of the regression;
the name-bloat slowness is the *amplifier*.

### 2e. Even on a clean success path, rejection is too aggressive
`server.py:88-91`: if `build_deal_truth` returns `engine_found=False`, the endpoint
raises **HTTP 422** and the user gets nothing — not even "here's what your file is." That
is the opposite of graceful degradation.

---

## 3. Reproduction

1. Run the API locally: `uvicorn server:app --reload` (set `OPENAI_API_KEY`).
2. `POST /api/analyze` with `model=425 Colorado Proforma_03.08.2022_Adept.xlsx`.
3. Watch server logs for the `[sid] analyze START … DONE` span and time it. Expect either
   (a) a long hang that a client timeout would sever, or (b) a 422/500. Repeat with the
   Westview file.
4. Add timing instrumentation around each `load_workbook` and each LLM call to see where
   the wall-clock goes. Confirm which stage dominates before changing anything.

---

## 4. Fix plan

### 4a. One robust loader helper (do this first)
Add a single `safe_load_workbook(path, *, data_only, need_formulas=False)` (e.g. in a new
`wb_io.py`) and route **all** call sites through it. It must:
- Default to `read_only=True` wherever the caller only reads cell values (the vast
  majority). Only use a full load when the caller genuinely needs the mutable object
  model, and document why at that call site.
- Guard with a wall-clock **timeout** and a clear, catchable exception on breach.
- Never raise an opaque error to the top: on failure, return a typed result the caller can
  handle (see 4c).
- Optionally, for pathological workbooks, **defensively strip** dead `definedName` entries
  (those containing `#REF!`) and orphaned external links *in memory / on a temp copy*
  before a full load, so full loads stop paying for junk. (Do NOT mutate the user's
  original file.) A pre-pass over `xl/workbook.xml` that drops `#REF!` names cut Colorado
  from 23,828 → ~250 names in local testing and removes the external-link resolution cost.

### 4b. Collapse redundant loads
- `formula_tracer.py`: load once with `data_only=False` and read cached values from the
  same pass, or load each mode once and reuse — don't reload per call. Cache the parsed
  workbook for the lifetime of a single `/api/analyze` request (keyed by path+mode).

### 4c. Graceful degradation — always report what the file is
- Split "**we opened your file**" from "**we found a validated cash-flow engine**."
- When `build_deal_truth` returns `engine_found=False`, **do not 422**. Return HTTP 200
  with a `mode: "limited"` payload containing at minimum: property name/address (from the
  summary sheet), sheet inventory, and any orientation the engine *did* recover, plus a
  plain-language note ("We read your file but couldn't auto-validate a cash-flow engine —
  here's what we can see; ask about any sheet."). The chat path already revisits the
  workbook for the long tail, so a limited mode is still useful.
- Reserve hard errors for genuinely unreadable inputs (not a valid xlsx, over the size
  cap), and make those messages specific.

### 4d. LLM call safety (regression trigger)
- Set an explicit, short `timeout=` (and a small `max_retries`) on the OpenAI client in
  `scenarios/_llm.py`.
- Wrap every analyze-path LLM call so a slow/failed model call **degrades** (skip that
  enrichment, keep going) instead of stalling the whole request. Confirm the `gpt-5.4` /
  `gpt-5.4-mini` model IDs and `reasoning_effort` values are valid on the deployed API;
  fall back cleanly if a call 4xx/5xx's.

### 4e. Frontend messaging
- In `static/index.html`, distinguish a true cold-start network failure from a
  server-side processing failure. When the server returns a handled error or `limited`
  mode, show the specific message, not the generic "Could not reach the server."

---

## 5. Regression fixtures & tests
- Commit both files under `uploads/` (or `tests/fixtures/`) as permanent fixtures. (Note:
  an older `westview.xlsx` already exists in `uploads/` at ~1,989,984 bytes — the newly
  supplied Westview is ~1,989,944 bytes; keep the exact file from this work order.)
- Add tests asserting:
  1. `safe_load_workbook` opens both fixtures in `read_only` mode in < 1s.
  2. `/api/analyze` on both returns HTTP 200 (either full or `limited` mode) and **never**
     a severed request or 422.
  3. A workbook with tens of thousands of `#REF!` names still analyzes within the timeout.
  4. `limited` mode includes property identity + sheet inventory.

---

## 6. Acceptance criteria
- Uploading either fixture yields a useful result in the UI — **never** "Load failed."
- Total `/api/analyze` wall-clock for each fixture is comfortably under the Cloud Run
  request timeout, with headroom.
- No `load_workbook` call in the analyze path runs in full mode unless it must, and no
  file is loaded more than once per mode per request.
- A workbook the engine can't validate still returns `mode: "limited"` describing the file.
- LLM calls have explicit timeouts and degrade rather than stall.

## 7. Out of scope
- "Cleaning" the user's files (stripping names) as a *deliverable* — the app must ingest
  files as-is. Any stripping is an internal, in-memory optimization on a copy only.
