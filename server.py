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

_STATIC = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_STATIC):
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")

# In-memory session store (demo-grade). One session = one analyzed deal + its
# grounding (the fact sheet) so chat/what-if can reuse it without re-running.
_SESSIONS: dict[str, dict[str, Any]] = {}


async def _save(workdir: Path, up: UploadFile) -> Path:
    p = workdir / Path(up.filename).name
    p.write_bytes(await up.read())
    return p


@app.post("/api/analyze")
async def analyze(model: UploadFile = File(...),
                  actuals: list[UploadFile] = File(default=[])):
    """Upload a model workbook (+ optional actuals statements) → the Investment Read,
    the validated fact sheet, and the metrics the UI grid renders."""
    # Save the model into the tools' upload dir under a session-scoped name, so the
    # chat agent's file tools (search_file / read_sheet) can revisit it for the long
    # tail (rent roll, unit counts) that the engine doesn't extract.
    import tools as _tools
    _tools.UPLOAD_DIR.mkdir(exist_ok=True)
    sid = uuid.uuid4().hex
    model_filename = f"{sid}__{Path(model.filename).name}"
    model_path = _tools.UPLOAD_DIR / model_filename
    model_path.write_bytes(await model.read())
    workdir = Path(tempfile.mkdtemp(prefix="collie_"))
    actuals_paths = [await _save(workdir, a) for a in actuals if a and a.filename]
    log.info("[%s] analyze START file=%s actuals=%d", sid, model.filename, len(actuals_paths))

    from deal_truth import build_deal_truth
    from deal_analysis import build_analysis
    from interpretation import assemble_fact_sheet, build_investment_read

    dt = build_deal_truth(model_path)
    if not dt.get("engine_found", True):
        log.warning("[%s] analyze REJECTED — no validated cash-flow engine: %s",
                   sid, dt.get("reason"))
        raise HTTPException(422, dt.get("reason", "no validated cash-flow engine in this workbook"))
    analysis = build_analysis(model_path, dt=dt)

    perf = None
    if actuals_paths:
        from perf_vs_plan_engine import build_perf_vs_plan
        perf = build_perf_vs_plan(model_path, actuals_paths)
        perf = perf if perf.get("ok") else None

    read = build_investment_read(model_path, dt=dt, analysis=analysis, perf=perf, analysis_id=sid)
    fs = read.get("fact_sheet") or assemble_fact_sheet(model_path, dt=dt, analysis=analysis,
                                                        perf=perf, analysis_id=sid)
    log.info("[%s] analyze DONE mode=%s narrative_source=%s", sid, fs.get("mode"), read.get("source"))

    _SESSIONS[sid] = {"model_path": str(model_path), "model_filename": model_filename,
                      "fact_sheet": fs, "read_md": read.get("md"),
                      "read_source": read.get("source"), "history": None}
    return {"session_id": sid, "mode": fs.get("mode"),
            "read_md": read.get("md"), "detail_md": analysis.get("md"),
            "fact_sheet": fs}


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


def _chat_system(fs: dict, fname: str = "", read_md: str | None = None) -> str:
    from interpretation import render_fact_sheet
    catalog = _sheet_catalog(fname) if fname else ""
    read_block = (
        "\nYOUR OWN INVESTMENT READ (you already wrote this for this deal — stay "
        "consistent with it; build on it in conversation, don't restate it verbatim):\n"
        + read_md + "\n\n"
    ) if read_md else ""
    return (
        "You are a sharp, curious asset manager with the deal's FULL underwriting workbook "
        "open in front of you. You have THREE sources and must use them appropriately:\n"
        "  (A) the VALIDATED FACT SHEET below — a conflict-resolved, full-dollar SUMMARY of "
        "the deal's economics (NOI, returns, capital stack, components),\n"
        "  (B) the INVESTMENT READ below (if present) — the memo you already wrote on this "
        "deal, with its own findings and judgment, and\n"
        "  (C) the LIVE WORKBOOK — everything else: rent roll, unit/tenant detail, property "
        "info, leases, every assumption and schedule. The tabs are listed below; reach into "
        "them with list_sheets / search_file / read_sheet.\n\n"
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
        + catalog + render_fact_sheet(fs) + "\n\n" + read_block).rstrip()


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

    if not sess.get("history"):
        sess["history"] = [{"role": "system",
                            "content": _chat_system(sess["fact_sheet"], sess["model_filename"],
                                                    sess.get("read_md"))}]
    hist = sess["history"]
    hist.append({"role": "user", "content": message})
    fname = sess["model_filename"]
    log.info("[%s] chat turn — message_chars=%d", session_id, len(message))

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


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def index():
    page = os.path.join(_STATIC, "index.html")
    if os.path.isfile(page):
        return FileResponse(page)
    return HTMLResponse(
        "<h1>Collie API</h1><p>POST a model workbook to <code>/api/analyze</code>.</p>")
