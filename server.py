"""
server.py — FastAPI wrapper around the Collie engine (the move off Streamlit).

ONE container serves the engine API AND (later) the built front-end, so it deploys
as a single Cloud Run service — the cheapest path. The engine modules are unchanged;
this is a thin API layer over build_investment_read / assemble_fact_sheet / whatif.

  POST /api/analyze  model file (+ optional actuals)  -> {session_id, mode, read_md, fact_sheet}
  POST /api/chat     {session_id, message}            -> {reply}
  POST /api/whatif   {session_id, amount, funded_by?} -> recomputed returns

Run locally:  uvicorn server:app --reload
Container:    see Dockerfile (uvicorn on $PORT)
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

log = logging.getLogger("fb.server")
if not log.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("[fb.server] %(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

app = FastAPI(title="Collie", version="0.1.0")

MODEL_MAX_BYTES = 15 * 1024 * 1024      # underwriting workbooks: 15MB
ACTUALS_MAX_BYTES = 5 * 1024 * 1024     # financial statements: 5MB each

# Focused-analyst chat: a small per-session question budget, not an open chatbot.
CHAT_QUESTION_CAP = int(os.getenv("CHAT_QUESTION_CAP", "3"))

# Early-access sign-up capture → a Google Sheet via an Apps Script web-app URL. Kept
# server-side (not in the page source) so the webhook can't be scraped/spammed. Unset =
# sign-ups are logged only; access is still granted (auto-grant, never blocked on capture).
ACCESS_SHEET_WEBHOOK = os.getenv("ACCESS_SHEET_WEBHOOK", "")

_STATIC = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_STATIC):
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")

# In-memory session store (demo-grade). One session = one analyzed deal + its
# grounding (the fact sheet) so chat/what-if can reuse it without re-running.
_SESSIONS: dict[str, dict[str, Any]] = {}


async def _save(workdir: Path, up: UploadFile) -> Path:
    data = await up.read()
    if len(data) > ACTUALS_MAX_BYTES:
        raise HTTPException(413, f"'{up.filename}' is {len(data) / 1e6:.1f}MB — financial "
                             f"statements are limited to {ACTUALS_MAX_BYTES // (1024 * 1024)}MB.")
    p = workdir / Path(up.filename).name
    p.write_bytes(data)
    return p


def _limited_read(model_path: Path, model_filename: str, sid: str, note: str) -> dict:
    """Graceful degradation: we OPENED the file but couldn't validate a cash-flow
    engine (or a stage failed). Still report WHAT THE FILE IS — property identity +
    sheet inventory — so the user gets a useful result, never an opaque failure.
    The chat agent keeps its full workbook tools, so a limited read is still usable."""
    idline = ""
    try:
        from property_id import property_identity, identity_line
        idline = identity_line(property_identity(model_path))
    except Exception as e:  # best-effort — never let identity block the response
        log.warning("[%s] limited: property identity failed (%s)", sid, e)

    # Read the inventory straight from the file (a cheap read_only open) rather than
    # via list_sheets' UPLOAD_DIR name resolution — the whole point of limited mode
    # is that we already know the file is openable.
    sheets = []
    try:
        from wb_io import safe_load_workbook
        wb = safe_load_workbook(model_path)
        try:
            for name in wb.sheetnames:
                ws = wb[name]
                sheets.append({"name": name, "max_row": ws.max_row, "max_col": ws.max_column})
        finally:
            wb.close()
    except Exception as e:
        log.warning("[%s] limited: sheet inventory failed (%s)", sid, e)

    lines = [f"## We read your file — {Path(model_filename).name.split('__', 1)[-1]}", "", note, ""]
    if idline:
        lines += [f"**Property:** {idline}", ""]
    if sheets:
        lines.append(f"**Sheets ({len(sheets)}):**")
        lines += [f"- {s.get('name')}  ({s.get('max_row')}×{s.get('max_col')})" for s in sheets]
        lines.append("")
    lines.append("Ask about any sheet — rent roll, assumptions, returns — and I'll read it directly.")
    read_md = "\n".join(lines)

    fs = {"ok": False, "mode": "limited", "version": "limited", "reason": note,
          "property_line": idline, "sheets": sheets}
    return {"session_id": sid, "mode": "limited", "read_md": read_md,
            "detail_md": None, "fact_sheet": fs}


@app.post("/api/analyze")
async def analyze(model: UploadFile = File(...),
                  actuals: list[UploadFile] = File(default=[])):
    """Upload a model workbook (+ optional actuals statements) → the Investment Read,
    the validated fact sheet, and the metrics the UI grid renders.

    Robustness contract (WORKORDER_ingestion_robustness.md): a readable workbook must
    NEVER produce an opaque failure. The whole pipeline runs inside one workbook_cache()
    so each file loads at most once per mode; any engine/stage failure DEGRADES to a
    `mode: "limited"` payload (property + sheet inventory) instead of a 422 or a severed
    request. Only genuinely unreadable inputs (over the cap, not an xlsx) hard-error."""
    # Save the model into the tools' upload dir under a session-scoped name, so the
    # chat agent's file tools (search_file / read_sheet) can revisit it for the long
    # tail (rent roll, unit counts) that the engine doesn't extract.
    import tools as _tools
    from wb_io import WorkbookLoadError, workbook_cache
    model_bytes = await model.read()
    if len(model_bytes) > MODEL_MAX_BYTES:
        raise HTTPException(413, f"'{model.filename}' is {len(model_bytes) / 1e6:.1f}MB — "
                             f"underwriting models are limited to {MODEL_MAX_BYTES // (1024 * 1024)}MB.")
    _tools.UPLOAD_DIR.mkdir(exist_ok=True)
    sid = uuid.uuid4().hex
    model_filename = f"{sid}__{Path(model.filename).name}"
    model_path = _tools.UPLOAD_DIR / model_filename
    model_path.write_bytes(model_bytes)
    workdir = Path(tempfile.mkdtemp(prefix="collie_"))
    actuals_paths = [await _save(workdir, a) for a in actuals if a and a.filename]
    log.info("[%s] analyze START file=%s actuals=%d", sid, model.filename, len(actuals_paths))

    from deal_truth import build_deal_truth
    from deal_analysis import build_analysis
    from interpretation import assemble_fact_sheet, build_investment_read

    def _store(payload: dict, *, read_md: str | None, fs: dict, source: str | None,
               engine_sheet: str | None = None, phasing: dict | None = None,
               dt: dict | None = None, analysis: dict | None = None, perf: dict | None = None,
               detail_md: str | None = None) -> dict:
        _SESSIONS[sid] = {"model_path": str(model_path), "model_filename": model_filename,
                          "fact_sheet": fs, "read_md": read_md, "detail_md": detail_md,
                          "read_source": source, "history": None,
                          "engine_sheet": engine_sheet, "phasing": phasing, "turns": 0,
                          # stashed for PHASE 2 (/api/enrich); None for limited/gpt_read
                          "dt": dt, "analysis": analysis, "perf": perf, "enriched": dt is None}
        return payload

    # One request-scoped cache for every load below — the pipeline loads this file
    # many times; collapse that to one-per-mode so a bloated workbook can't stack
    # enough full loads to blow the request timeout.
    with workbook_cache():
        try:
            dt = build_deal_truth(model_path)
        except WorkbookLoadError as e:
            log.warning("[%s] analyze — deal-truth could not load workbook: %s", sid, e.reason)
            r = _limited_read(model_path, model_filename, sid,
                              "We opened your file but couldn't read it deeply enough to "
                              f"validate a cash-flow engine ({e.reason}). Here's what we can see:")
            return _store(r, read_md=r["read_md"], fs=r["fact_sheet"], source="limited")

        if not dt.get("engine_found", True):
            # Tier 2: the cash-flow oracle couldn't validate, but the summary tab may
            # still carry the whole story (people rush the summary and skip the model).
            # Try a GPT-read of the summary → full grid, labelled "not IRR-validated".
            log.warning("[%s] analyze — no validated engine; attempting GPT summary read: %s",
                        sid, dt.get("reason"))
            try:
                from tier2_read import build_gpt_read
                gr = build_gpt_read(model_path, model_filename, sid, dt)
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] tier-2 GPT read failed (%s) — limited mode", sid, e)
                gr = None
            if gr and gr.get("fact_sheet", {}).get("ok"):
                return _store(gr, read_md=gr["read_md"], fs=gr["fact_sheet"], source="gpt_read")
            r = _limited_read(model_path, model_filename, sid,
                              "We read your file but couldn't auto-validate a cash-flow engine "
                              "in it — here's what we can see; ask about any sheet.")
            return _store(r, read_md=r["read_md"], fs=r["fact_sheet"], source="limited")

        # PHASE 1 — DETERMINISTIC read only, returned FAST. The slow, sequential GPT work
        # (headline-fact review + the written memo) is deferred to PHASE 2 (/api/enrich),
        # which the frontend fires right after it renders this. This keeps the first
        # response well under the browser's ~60s connection cutoff. Crucially, today's
        # correctness fixes (unit total, cost≥debt, exit-not-a-comp) are all deterministic,
        # so the grid is already RIGHT here — enrichment only refines a field or two + memo.
        try:
            analysis = build_analysis(model_path, dt=dt)
            perf = None
            if actuals_paths:
                from perf_vs_plan_engine import build_perf_vs_plan
                perf = build_perf_vs_plan(model_path, actuals_paths)
                perf = perf if perf.get("ok") else None
            fs = assemble_fact_sheet(model_path, dt=dt, analysis=analysis,
                                     perf=perf, analysis_id=sid)
        except Exception as e:  # noqa: BLE001 - never let a stage sever the request
            log.exception("[%s] analyze — deterministic read failed after engine validated: %s", sid, e)
            r = _limited_read(model_path, model_filename, sid,
                              "We validated your file's engine but hit a snag building the full "
                              "read — here's what we can see; ask about any sheet.")
            return _store(r, read_md=r["read_md"], fs=r["fact_sheet"], source="limited")

    log.info("[%s] analyze DONE (phase 1, deterministic) mode=%s", sid, fs.get("mode"))
    payload = {"session_id": sid, "mode": fs.get("mode"),
               "read_md": None, "detail_md": analysis.get("md"),
               "fact_sheet": fs, "enriching": True}
    return _store(payload, read_md=None, fs=fs, source="deterministic", detail_md=analysis.get("md"),
                  engine_sheet=dt.get("cashflow_engine"),
                  phasing=(fs.get("deal", {}) or {}).get("phasing"),
                  dt=dt, analysis=analysis, perf=perf)


@app.post("/api/enrich")
async def enrich(session_id: str = Form(...)):
    """PHASE 2 — the deferred GPT work: audit the headline facts against the summary
    (correcting a weak pick) and write the investment-read memo. Fired by the frontend
    after it renders the fast deterministic grid, so the browser never blocks on it.
    Idempotent; degrades to the deterministic read on any failure."""
    sess = _SESSIONS.get(session_id)
    if not sess:
        raise HTTPException(404, "unknown session")
    if sess.get("enriched") or sess.get("dt") is None:
        return {"fact_sheet": sess.get("fact_sheet"), "read_md": sess.get("read_md"),
                "detail_md": sess.get("detail_md"), "enriched": True}

    from interpretation import assemble_fact_sheet, build_investment_read
    from wb_io import workbook_cache
    model_path = Path(sess["model_path"])
    dt, analysis, perf = sess["dt"], sess["analysis"], sess.get("perf")
    log.info("[%s] enrich START (phase 2)", session_id)
    try:
        with workbook_cache():
            try:
                from fact_review import review_headline_facts
                dt["canonical"] = review_headline_facts(model_path, dt)
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] headline review skipped (%s)", session_id, e)
            read = build_investment_read(model_path, dt=dt, analysis=analysis,
                                         perf=perf, analysis_id=session_id)
            fs = read.get("fact_sheet") or assemble_fact_sheet(
                model_path, dt=dt, analysis=analysis, perf=perf, analysis_id=session_id)
    except Exception as e:  # noqa: BLE001 - keep the deterministic read on failure
        log.exception("[%s] enrich failed — keeping deterministic read: %s", session_id, e)
        sess["enriched"] = True
        return {"fact_sheet": sess.get("fact_sheet"), "read_md": sess.get("read_md"),
                "detail_md": sess.get("detail_md"), "enriched": True}

    sess.update({"fact_sheet": fs, "read_md": read.get("md"), "detail_md": analysis.get("md"),
                 "read_source": read.get("source"), "enriched": True, "history": None})
    log.info("[%s] enrich DONE narrative_source=%s", session_id, read.get("source"))
    return {"fact_sheet": fs, "read_md": read.get("md"), "detail_md": analysis.get("md"),
            "enriched": True}


# Chat tools — the agent answers from the fact sheet first, REVISITS the workbook for
# the long tail (rent roll, unit/suite counts, one-off assumptions) the engine doesn't
# extract, and routes any return math to the deterministic what-if. The model filename
# is injected per session, so the agent never has to know it.
_CHAT_TOOLS = [
    {"type": "function", "function": {"name": "search_file",
        "description": "Search the deal's workbook for cells whose text matches a query "
        "(case-insensitive). Use this for facts NOT in the fact sheet — rent roll, unit/"
        "suite counts, occupancy, a specific assumption. Returns sheet, cell, and nearby "
        "value. Values are RAW cells (dollar amounts may be in $000s; counts are unscaled).",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Text to find, e.g. 'vacant', 'rent roll', 'units'."}},
            "required": ["query"]}}},
    {"type": "function", "function": {"name": "read_sheet",
        "description": "Read non-empty cells from a named sheet of the deal's workbook "
        "(e.g. 'Rent Roll'). Use to inspect a specific tab.",
        "parameters": {"type": "object", "properties": {
            "sheet_name": {"type": "string"}, "max_rows": {"type": "integer"}},
            "required": ["sheet_name"]}}},
    {"type": "function", "function": {"name": "list_sheets",
        "description": "List the workbook's sheet names and sizes.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "read_row_series",
        "description": "Return ONE row as a full period-by-period time series (with its "
        "date/period header) — the tool for TIMING questions: the riskiest month, when "
        "DSCR dips, the lease-up ramp, a draw/interest spike. Monthly models are too wide "
        "for read_sheet; use this on the cash-flow engine tab. Find the row first with "
        "search_file (e.g. 'Levered Cash Flow', 'NOI', 'DSCR'), then pass its sheet + label "
        "or row number here.",
        "parameters": {"type": "object", "properties": {
            "sheet_name": {"type": "string"},
            "row": {"type": "string", "description": "A row label (e.g. 'Levered Cash Flow') "
                    "or a 1-based row number."}},
            "required": ["sheet_name", "row"]}}},
    {"type": "function", "function": {"name": "what_if",
        "description": "Recompute IRR and equity multiple under an UPFRONT capex / "
        "additional-investment change funded by equity. Deterministic — call this for any "
        "return-impact question instead of estimating.",
        "parameters": {"type": "object", "properties": {
            "amount": {"type": "number", "description": "Dollars, e.g. 500000."},
            "funded_by": {"type": "string", "enum": ["equity"]}}, "required": ["amount"]}}},
]


def _chat_dispatch(name: str, args: dict, fname: str) -> Any:
    import tools as _tools
    if name == "search_file":
        return _tools.search_file(fname, args.get("query", ""), args.get("max_matches", 30))
    if name == "read_sheet":
        return _tools.read_sheet(fname, args.get("sheet_name", ""), args.get("max_rows", 80))
    if name == "list_sheets":
        return _tools.list_sheets(fname)
    if name == "read_row_series":
        return _tools.read_row_series(fname, args.get("sheet_name", ""), args.get("row", ""))
    if name == "what_if":
        return _tools.run_what_if_capex(args.get("amount", 0), args.get("funded_by", "equity"), fname)
    return {"error": f"unknown tool {name}"}


def _sheet_catalog(fname: str) -> str:
    """A one-line-per-tab index of the workbook, ROLE-RANKED so the agent consults
    the declaration tabs (summary / assumptions / charts / cash flow) first — the
    same hierarchy the deterministic engine uses — before diving into operating
    detail. Injected into the chat prompt so the agent never has to discover the
    file's shape, and looks in the right place first."""
    try:
        import tools as _tools
        info = _tools.list_sheets(fname)
        sheets = info.get("sheets", []) if isinstance(info, dict) else []
    except Exception:
        sheets = []
    if not sheets:
        return ""
    # Role map from the engine's orientation (summary/inputs/returns/support/model).
    roles: dict[str, str] = {}
    try:
        from workbook_orientation import orient_workbook
        rm = (orient_workbook(_tools.UPLOAD_DIR / fname) or {}).get("map", {})
        for role, names in rm.items():
            for n in names:
                roles[n] = role
    except Exception:
        rm = {}
    rank = {"summary": 0, "inputs": 1, "returns": 2, "support": 3, "other": 4, "model": 5}
    ordered = sorted(sheets, key=lambda s: rank.get(roles.get(s.get("name"), "other"), 4))
    lines = []
    for s in ordered:
        role = roles.get(s.get("name"), "")
        tag = " ← start here" if role in ("summary", "inputs", "returns") else ""
        rl = f"  [{role}]" if role else "  "
        lines.append(f"{rl} {s.get('name')}  ({s.get('max_row')}×{s.get('max_col')}){tag}")
    return ("WORKBOOK TABS (role-ranked — consult [summary]/[inputs]/[returns] tabs "
            "FIRST for headline facts; [model] tabs hold operating detail). Read any "
            "with read_sheet, or search across all with search_file:\n"
            + "\n".join(lines) + "\n\n")


def _chat_system(fs: dict, fname: str = "", read_md: str | None = None,
                 engine_sheet: str | None = None, phasing: dict | None = None) -> str:
    from interpretation import render_fact_sheet
    catalog = _sheet_catalog(fname) if fname else ""
    read_block = (
        "\nYOUR OWN INVESTMENT READ (you already wrote this for this deal — stay "
        "consistent with it; build on it in conversation, don't restate it verbatim):\n"
        + read_md + "\n\n"
    ) if read_md else ""
    # Point the agent at the monthly cash-flow engine + the deal's phases so a
    # TIMING/RISK question ("riskiest month", "when does coverage dip") is answered by
    # reasoning over the actual monthly stream — never deflected with "I only have the
    # summary." The engine tab is where the validated stream lives.
    reason_block = ""
    if engine_sheet:
        ph = ""
        if phasing and phasing.get("kind") and phasing["kind"] != "none":
            ph = ("  Deal phases (from the model): "
                  + "; ".join(f"{k}={v}" for k, v in phasing.items()
                              if k in ("kind", "delivery", "stabilization", "construction_end",
                                       "leaseup_end") and v) + ".\n")
        reason_block = (
            f"\nDEEPER REASONING — the monthly cash-flow ENGINE is tab '{engine_sheet}'. "
            "For any question about TIMING or RISK over the hold (riskiest month/period, "
            "when DSCR or cash flow dips, the lease-up ramp, a draw or rate spike, "
            "sensitivity to a delay), do NOT stop at the fact sheet: use read_row_series on "
            "the engine tab (find the row with search_file first) to pull the month-by-month "
            "series and REASON over it — identify the trough, the tightest coverage window, "
            "the exposure — then answer with the specific periods. Cite the tab/row.\n" + ph)
    return (
        "You are a sharp, curious asset manager with the deal's FULL underwriting workbook "
        "open in front of you. You have THREE sources and must use them appropriately:\n"
        "  (A) the VALIDATED FACT SHEET below — a conflict-resolved, full-dollar SUMMARY of "
        "the deal's economics (NOI, returns, capital stack, components),\n"
        "  (B) the INVESTMENT READ below (if present) — the memo you already wrote on this "
        "deal, with its own findings and judgment, and\n"
        "  (C) the LIVE WORKBOOK — everything else: rent roll, unit/tenant detail, property "
        "info, leases, every assumption and schedule. The tabs are listed below; reach into "
        "them with list_sheets / search_file / read_sheet, and read a full monthly series "
        "with read_row_series.\n\n"
        "Hard rules:\n"
        "1. The fact sheet is authoritative for what it covers — prefer it, obey its "
        "guardrails, never contradict it.\n"
        "2. \"It's not in the fact sheet\" is NOT an acceptable answer. The fact sheet is only "
        "a summary; the workbook has far more. If the fact sheet doesn't cover the question, "
        "go read the workbook (you already know the tabs from the catalog below) and answer "
        "from it. Decline ONLY after you have actually searched/read the relevant tabs and "
        "genuinely found nothing — and then say which tabs you checked.\n"
        "3. Respect the tab hierarchy: for a headline fact (property name/type/size, rent, "
        "occupancy, debt terms, a key assumption) read the [summary]/[inputs]/[returns] tabs "
        "FIRST — the declaration sheets. Go to [model]/operating-detail tabs only for line-item "
        "detail, and never quote an operating line (e.g. an R&M or occupied-rooms row) as a "
        "property-level figure.\n"
        "4. When you answer from the workbook, say so and cite the sheet (and cell if precise). "
        "Treat file reads as lower-confidence than the fact sheet. Raw dollar cells may be in "
        "$000s — note units; counts are unscaled.\n"
        "5. For any return-impact / what-if math, call the what_if tool — never estimate.\n"
        "6. Respect DATA CONFIDENCE in the fact sheet: a component marked T2-unfooted is a "
        "single line item, not a footed total — hedge it accordingly rather than stating it "
        "with T1 certainty.\n"
        "Never invent a number. Be concise, but be genuinely helpful — dig.\n\n"
        + catalog + render_fact_sheet(fs) + "\n\n" + reason_block + read_block).rstrip()


@app.post("/api/chat")
async def chat(session_id: str = Form(...), message: str = Form(...)):
    """Tiered agent: answers from the fact sheet, revisits the workbook (labeled) for the
    long tail, and routes return math to the deterministic what-if tool."""
    sess = _SESSIONS.get(session_id)
    if not sess:
        raise HTTPException(404, "unknown session")
    from scenarios._llm import MODEL, get_client
    client = get_client()
    if client is None:
        return {"reply": "Chat needs an API key. The validated fact sheet is still available."}

    # Per-session question budget (this is a focused analyst, not an open chatbot):
    # three substantive questions, then the session is capped. [[chat-reasoning-tiers]]
    sess["turns"] = int(sess.get("turns") or 0) + 1
    if sess["turns"] > CHAT_QUESTION_CAP:
        log.info("[%s] chat cap reached (%d)", session_id, CHAT_QUESTION_CAP)
        return {"reply": f"You've reached this session's {CHAT_QUESTION_CAP}-question limit. "
                "Re-run the analysis to start a fresh session, or work from the validated "
                "fact sheet and investment read above.", "capped": True,
                "questions_used": sess["turns"] - 1, "question_cap": CHAT_QUESTION_CAP}

    if not sess.get("history"):
        sess["history"] = [{"role": "system",
                            "content": _chat_system(sess["fact_sheet"], sess["model_filename"],
                                                    sess.get("read_md"), sess.get("engine_sheet"),
                                                    sess.get("phasing"))}]
    hist = sess["history"]
    hist.append({"role": "user", "content": message})
    fname = sess["model_filename"]
    log.info("[%s] chat turn %d/%d — message_chars=%d", session_id, sess["turns"],
             CHAT_QUESTION_CAP, len(message))

    for _ in range(6):
        try:
            # NOTE: gpt-5.4 rejects `reasoning_effort` together with `tools` on
            # /v1/chat/completions ("Function tools with reasoning_effort are not
            # supported … use /v1/responses instead"). So the tool-using chat loop
            # omits it and runs at the model's default effort. (Restoring effort
            # control here means migrating this loop to the Responses API.)
            resp = client.chat.completions.create(
                model=MODEL, messages=hist, tools=_CHAT_TOOLS,
                tool_choice="auto")
        except Exception as e:                             # pragma: no cover - defensive
            log.warning("[%s] chat FAILED (%s: %s)", session_id, type(e).__name__, e)
            return {"reply": f"Chat failed: {type(e).__name__}: {e}"}
        msg = resp.choices[0].message
        rec: dict[str, Any] = {"role": "assistant", "content": msg.content}
        if msg.tool_calls:
            rec["tool_calls"] = [{"id": tc.id, "type": "function",
                                  "function": {"name": tc.function.name,
                                               "arguments": tc.function.arguments}}
                                 for tc in msg.tool_calls]
        hist.append(rec)
        if not msg.tool_calls:
            log.info("[%s] chat replied (no further tool calls)", session_id)
            return {"reply": (msg.content or "").strip() or "I couldn't find that."}
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            log.info("[%s] chat tool_call %s(%s)", session_id, tc.function.name, args)
            res = _chat_dispatch(tc.function.name, args, fname)
            hist.append({"role": "tool", "tool_call_id": tc.id,
                         "content": json.dumps(res, default=str)[:4000]})
    log.warning("[%s] chat exhausted tool-call budget without a final reply", session_id)
    return {"reply": "I looked but couldn't pin that down — try naming the sheet."}


@app.post("/api/whatif")
async def whatif(session_id: str = Form(...), amount: float = Form(...),
                 funded_by: str = Form("equity")):
    """Deterministic return-impact: perturb the validated cash-flow stream and
    recompute XIRR/EM (no GPT)."""
    sess = _SESSIONS.get(session_id)
    if not sess:
        raise HTTPException(404, "unknown session")
    from whatif import what_if_capex
    return what_if_capex(sess["model_path"], amount, funded_by=funded_by)


@app.post("/api/access")
async def access(name: str = Form(...), email: str = Form(...)):
    """Capture an early-access sign-up (name + company email) to the configured Google
    Sheet. Best-effort and NON-blocking: the page grants access regardless, so a Sheet
    outage never locks anyone out. Returns 200 always."""
    name = (name or "").strip()[:200]
    email = (email or "").strip()[:200]
    if not name or "@" not in email or "." not in email.split("@")[-1]:
        return {"ok": False, "reason": "name and a valid email are required"}
    if not ACCESS_SHEET_WEBHOOK:
        log.info("access signup (not persisted — ACCESS_SHEET_WEBHOOK unset): %s <%s>", name, email)
        return {"ok": True, "stored": False}
    import urllib.request
    try:
        body = json.dumps({"name": name, "email": email,
                           "ts": __import__("datetime").datetime.utcnow().isoformat() + "Z"}).encode()
        req = urllib.request.Request(ACCESS_SHEET_WEBHOOK, data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=5)     # follows Apps Script's redirect
        log.info("access signup captured: %s <%s>", name, email)
        return {"ok": True, "stored": True}
    except Exception as e:  # noqa: BLE001 - never block access on a capture failure
        log.warning("access signup capture failed (%s: %s): %s <%s>", type(e).__name__, e, name, email)
        return {"ok": True, "stored": False}


@app.get("/healthz")
def healthz():
    return {"ok": True}


_LEARNING_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Collie · Learning Loop</title><style>
 body{font:14px/1.5 -apple-system,system-ui,sans-serif;margin:0;padding:24px;color:#1a1a1a;background:#fafafa}
 h1{font-size:20px;margin:0 0 4px} .sub{color:#666;margin:0 0 20px}
 .card{background:#fff;border:1px solid #e5e5e5;border-radius:10px;padding:16px 18px;margin:0 0 16px}
 .lbl{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:#888;margin:0 0 8px}
 .pill{display:inline-block;padding:2px 8px;border-radius:12px;font-size:12px;margin:2px 6px 2px 0;background:#eef}
 .ok{color:#0a7d33} .bad{color:#b42318}
 table{border-collapse:collapse;width:100%;font-size:13px} th,td{text-align:left;padding:6px 10px;border-bottom:1px solid #eee;vertical-align:top}
 th{color:#666;font-weight:600} .mono{font-family:ui-monospace,Menlo,monospace;font-size:12px}
 .cand{background:#fffdf5} .tag{font-size:11px;padding:1px 6px;border-radius:6px;background:#eee;margin-right:4px}
 .warn{background:#fff4e5;border-color:#f0c98a}
</style></head><body>
<h1>Collie · Learning Loop</h1>
<p class="sub">Read-only. Captures every GPT resolution; <b>promotion into Python stays a human decision</b> — nothing here changes the engine.</p>
<div id="status" class="card">Loading…</div>
<div class="card"><div class="lbl">Promotion candidates — patterns recurring across ≥2 distinct files</div><div id="cands">—</div></div>
<div class="card"><div class="lbl">Recent decisions</div><div id="recent">—</div></div>
<script>
const qs=new URLSearchParams(location.search), token=qs.get('token')||'';
const esc=s=>String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const money=v=>typeof v==='number'?(Math.abs(v)>=1e6?'$'+(v/1e6).toFixed(1)+'M':(''+v)):esc(v);
fetch('/api/learning?token='+encodeURIComponent(token)+'&recent=80').then(r=>r.json()).then(d=>{
 const b=d.backend||{}, s=d.summary||{};
 const reach=b.reachable?'<span class="ok">reachable</span>':'<span class="bad">unreachable</span>';
 document.getElementById('status').innerHTML=
   '<div class="lbl">Backend</div><b>'+esc(b.backend)+'</b> · '+reach+(b.collection?' · '+esc(b.collection):'')+(b.path?' · <span class="mono">'+esc(b.path)+'</span>':'')+
   '<div style="margin-top:10px" class="lbl">Totals</div>'+
   '<span class="pill">'+ (s.total||0) +' events</span><span class="pill">'+(s.distinct_files||0)+' files</span>'+
   Object.entries(s.by_decision||{}).map(([k,v])=>'<span class="pill">'+esc(k)+': '+v+'</span>').join('');
 const c=d.promotion_candidates||[];
 document.getElementById('cands').innerHTML = c.length? '<table><tr><th>Concept</th><th>Label seen</th><th>Layer</th><th>Files</th><th>Decisions</th><th>Example</th></tr>'+
   c.map(x=>'<tr class="cand"><td><b>'+esc(x.concept)+'</b></td><td>'+esc(x.label)+'</td><td>'+esc(x.layer)+'</td><td>'+x.distinct_files+'</td><td>'+esc(JSON.stringify(x.decisions))+'</td><td class="mono">'+(x.examples&&x.examples[0]?money(x.examples[0].prior_value)+' → '+money(x.examples[0].chosen_value)+' @ '+esc(x.examples[0].chosen_cell):'')+'</td></tr>').join('')+'</table>'
   : '<span style="color:#888">None yet — a pattern needs the same correction on ≥2 different files.</span>';
 const r=d.recent||[];
 document.getElementById('recent').innerHTML = r.length? '<table><tr><th>When</th><th>Layer</th><th>Concept</th><th>Decision</th><th>Prior→Chosen</th><th>Reason</th></tr>'+
   r.map(e=>'<tr><td class="mono">'+esc(e.ts)+'</td><td>'+esc(e.layer)+'</td><td>'+esc(e.concept)+'</td><td><span class="tag">'+esc(e.decision)+'</span></td><td class="mono">'+money(e.prior_value)+' → '+money(e.chosen_value)+'</td><td>'+esc(e.reason)+'</td></tr>').join('')+'</table>'
   : '<span style="color:#888">No decisions captured yet.</span>';
}).catch(e=>{document.getElementById('status').innerHTML='<span class="bad">Failed to load: '+esc(e.message)+'</span> — check ?token=.';});
</script></body></html>"""


# --- Learning-loop dashboard (read-only, capture-only — NEVER promotes) ------
# Safe-by-default: serves your captured data ONLY when COLLIE_ADMIN_TOKEN is set and
# the request presents it. Unset → 403, so a public Cloud Run URL can't leak the
# underwriting values/filenames the store captures.
def _check_admin(token: str | None) -> None:
    want = os.getenv("COLLIE_ADMIN_TOKEN")
    if not want:
        raise HTTPException(403, "Learning dashboard is disabled — set COLLIE_ADMIN_TOKEN "
                            "in the environment to enable it.")
    if token != want:
        raise HTTPException(401, "invalid or missing token")


@app.get("/api/learning")
def learning_api(token: str = "", min_files: int = 2, recent: int = 50):
    """JSON: what the loop has captured + which patterns recur enough to promote."""
    _check_admin(token)
    import learning_store as L
    return {"backend": L.backend_status(), "summary": L.summarize(),
            "promotion_candidates": L.promotion_candidates(min_files=min_files),
            "recent": list(reversed(L.read_events(limit=recent)))}


@app.get("/learning")
def learning_dashboard(token: str = ""):
    """A plain read-only dashboard so you can eyeball the loop from time to time. It
    fetches /api/learning with the same token and renders it — no promotion controls,
    by design (promotion stays a human decision you and I make together)."""
    _check_admin(token)
    return HTMLResponse(_LEARNING_HTML)


@app.get("/")
def index():
    page = os.path.join(_STATIC, "index.html")
    if os.path.isfile(page):
        return FileResponse(page)
    return HTMLResponse(
        "<h1>Collie API</h1><p>POST a model workbook to <code>/api/analyze</code>.</p>")
