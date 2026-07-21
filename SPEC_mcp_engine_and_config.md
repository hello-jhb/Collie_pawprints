# Spec: MCP-expose the engine + externalize judgment to config

**Owner:** (you)
**For:** Claude Code
**Status:** Draft direction.
**Relates to:** `SPEC_investment_read_rate_and_reposition.md`,
`SPEC_deal_phasing_timeline.md` — those add judgment logic; this spec makes future judgment
changes shippable **without a code deploy**, and makes the engine callable by any model/client.

## The actual problem

"Every time some changes need to happen, code updates take too long." The slowness is not the
extraction — it's that **tunable judgment is entangled with code**. Archetype thresholds
(0.85 occupancy, 3% capex intensity, 0.20 NOI growth, 0.92 pct — `interpretation.py:87–117`),
lens copy (`_ARCHETYPE_LENS`, `:35`), guardrail wording, and the GPT system prompt
(`:657`) are all Python literals. Tuning any of them means edit → PR → redeploy Cloud Run.

Two separate fixes, one principle — **draw a hard seam between two layers:**

| Layer | Examples | Where it should live | Change cost today | Target |
|---|---|---|---|---|
| **Validated facts** (correctness-critical) | `deal_truth`, `cashflow_spine`, foot-validation, trust tiers, numeric grounding | **Stays as tested code** | code deploy | code deploy (fine — rarely changes) |
| **Judgment / narration** (tunable) | archetype thresholds, lens copy, phasing bands, guardrail templates, prompts, label→display | **Config, loaded at runtime** | code deploy | **edit config, hot-reload** |

MCP is orthogonal but complementary: it exposes the *validated facts* layer as tools/resources
so any client (Cowork, Claude, the existing GPT chat, a future UI) consumes one engine instead
of the logic being locked inside `server.py`.

**Non-negotiable:** the LLM never computes or overrides a validated number. Config tunes
*thresholds and words*; it does not let judgment freelance the facts. Foot-validation, trust
tiers, and `_check_numeric_grounding` stay exactly as they are.

---

## Part A — Externalize judgment to config (this is the latency fix)

**Goal.** Turn "redeploy to change a threshold or a sentence" into "edit a config file, reload."

1. Add a `config/` dir with versioned, schema-validated files (YAML or JSON):
   - `archetype.yaml` — the thresholds and the label set:
     `going_in_occ_max: 0.85`, `capex_intensity_min: 0.03`, `noi_growth_min: 0.20`,
     `core_pct_min: 0.92`, `core_occ_min: 0.90`, etc. — everything currently literal in
     `_classify_archetype`.
   - `lenses.yaml` — `_ARCHETYPE_LENS` copy, plus the label→display map (kills the
     "archetype" leakage: "value-add / repositioning" → "repositioning").
   - `phasing.yaml` — the V-trough / stabilization bands from the phasing spec.
   - `prompts/investment_read.md` — the `_SYSTEM_PROMPT` text (`interpretation.py:657`),
     externalized so copy/rules tune without a deploy.
   - `guardrails.yaml` — guardrail message templates.
2. Load once at process start into a typed `Settings` object (pydantic) with **schema
   validation and defaults** — a malformed config fails loudly at load, never silently
   mis-scores a deal. Keep the current literals as the built-in defaults so nothing breaks if
   a file is absent.
3. Add a `config_version` string, stamp it into the fact sheet (next to
   `FACT_SHEET_VERSION`) and logs, so every read is traceable to the config that produced it.
4. Hot-reload: a `POST /admin/reload-config` endpoint (auth-gated) that re-reads and
   re-validates, so you retune on a running service without a redeploy. (Cloud Run: also fine
   to bake config in the image and redeploy just the config — but the reload path is the fast
   loop.)
5. Refactor `_classify_archetype`, `_ARCHETYPE_LENS`, phasing, and `_SYSTEM_PROMPT` to read
   from `Settings` instead of literals. Behavior identical when config == current defaults
   (assert this in tests).

**Acceptance.** Changing an archetype threshold or a lens sentence is a config edit +
`/admin/reload-config`; no code change, no redeploy. A bad config is rejected at load with a
clear error, and the engine keeps serving the last good config.

---

## Part B — Expose the engine as an MCP server

**Goal.** One engine, many clients. Today the tools live inline in `server.py`
(`_CHAT_TOOLS` `:117`, `_chat_dispatch` `:145`) and only the built-in GPT chat can use them.
An MCP server makes the same capabilities available to Cowork, Claude, and any MCP client —
and decouples tool definitions from the FastAPI app.

1. Add `mcp_server.py` using **FastMCP** (`pip install mcp`). Wrap the existing engine
   functions — do **not** reimplement them:
   - **Tools (actions):**
     - `analyze_model(path_or_upload)` → `build_investment_read` + `assemble_fact_sheet`
       (returns session id + fact sheet + read).
     - `get_trajectory(session, concept)` → NOI / revenue / opex / capex / occupancy / ADR
       series from the analysis.
     - `get_phasing(session)` → the phasing object (from the phasing spec).
     - `classify_archetype(session)` → label + signals + lens.
     - `dscr_health(session)` → the coverage object.
     - `what_if(session, amount, funded_by)` → `whatif.what_if_capex` (deterministic).
     - `search_file` / `read_sheet` / `list_sheets` → reuse `tools.py` verbatim (these are
       already the chat tools; move the definitions here so both the GPT chat and MCP clients
       share one source).
   - **Resources (read-only context):**
     - `fact_sheet://{session}` → the validated fact sheet.
     - `workbook_catalog://{session}` → the role-ranked tab index (`_sheet_catalog`).
2. Point the existing GPT chat loop at the same tool implementations (import from the shared
   module) so there's a single dispatch, not two copies to keep in sync.
3. Keep the FastAPI HTTP API (`/api/analyze` etc.) — MCP is additive, for agent/model clients;
   the REST API still serves the web UI.
4. **Boundary rule in the tool docstrings:** every tool returns validated facts + confidence
   tiers; the calling model narrates and may not alter numbers. Mirror the fact sheet's
   guardrails into the MCP tool descriptions so any client inherits them.

**Acceptance.** Cowork/Claude (or any MCP client) can run `analyze_model` on a workbook and
pull `fact_sheet` / `get_phasing` / `dscr_health`, getting the same validated numbers the web
app shows. The GPT chat and MCP clients share one tool implementation.

---

## What explicitly does NOT move

- Extraction, foot-validation, cashflow spine, trust-tier assignment, `_check_numeric_grounding`
  — stay as tested Python. MCP exposes them; config never overrides them.
- Any number an LLM sees is already validated. Config changes *which threshold fires* and
  *what words wrap it*, never *what the cash flow says*.

## Risks / guardrails for the implementation

- **Config as an attack/error surface:** schema-validate on load, fail closed to last-good,
  log `config_version` on every analysis. No unvalidated config reaches the classifier.
- **Two tool copies drifting:** Part B must make the GPT chat and MCP server import one shared
  dispatch — don't fork `_chat_dispatch`.
- **Over-abstraction:** don't config-ify the extraction/validation constants (foot tolerances,
  trust-tier rules). Only judgment thresholds, copy, and prompts move to config.
- **Model portability:** the chat loop is OpenAI-specific (`gpt-5.4`, chat.completions). MCP
  tool schemas are model-agnostic, so exposing via MCP also unblocks pointing Claude/Cowork at
  the engine without rewriting the loop.

## Tests

- **Golden-config test:** with config == current defaults, every existing fixture (St. Regis,
  lease-up, core) produces byte-identical fact sheets/claims to pre-refactor — proves the
  externalization changed nothing.
- **Bad-config test:** malformed `archetype.yaml` is rejected at load with a clear error; engine
  serves last-good.
- **Reload test:** change a threshold via config + reload flips the archetype on a borderline
  fixture with no restart.
- **MCP parity test:** `analyze_model` over MCP returns the same fact sheet as `/api/analyze`
  for the same file.
- Keep `_check_numeric_grounding` green across all of the above.

## Suggested order

1. **Part A first** — it's the direct latency fix and touches only internal wiring
   (literals → `Settings`). Ship with the golden-config test proving no behavior change.
2. **Part B** — MCP server wrapping the now-config-driven engine; unify the tool dispatch.
3. Wire the phasing/rate specs' new thresholds through config from the start so they're tunable
   on day one.
