"""
agent_loop.py — scenario-scoped agent using OpenAI gpt-4o function calling.

The shape: a chat-style conversation where the assistant can call tools,
read their results, and decide whether to keep going or reply to the user.
Each scenario gets its own system prompt and its own tool subset, so the
agent is naturally constrained to the chosen scenario.

Public API:
    AgentSession(scenario: str)
        .send(user_message: str) -> assistant_reply: str
        .messages -> list of all messages (for UI display)
        .last_tool_calls -> list of tool calls from the most recent turn
                            (for the "thinking" expander in the UI)
"""

from __future__ import annotations

import json
from typing import Any, Iterable

import tools
from scenarios._llm import get_client


# Agent model. This loop no longer just routes tool calls — deep dives now run
# THROUGH it (it reads workbook sheets with its tools and writes the analysis),
# and chat answers synthesize raw sheet content. That's comprehension work, so
# it gets the strong model; the per-deal cost is a few cents and the output is
# the product. Cost-sensitive bulk calls (classifier, insight pass, section
# reader) stay on MODEL_FAST in scenarios/_llm.py.
MODEL = "gpt-4o"
MAX_TOOL_ITERATIONS = 10  # cap on tool calls per single user message


# -----------------------------------------------------------------------------
# Scenario configuration
# -----------------------------------------------------------------------------

# Each scenario gets:
#   - a system prompt that fully defines its job
#   - a tool subset (so agent literally can't call the wrong scenario)
SCENARIO_CONFIG: dict[str, dict[str, Any]] = {
    "deal_review": {
        "display_name": "Deal Analysis",
        "tools": tools.TOOLS_FOR_DEAL_REVIEW,
        "system_prompt": """\
You are a real estate investment manager helping the user do a DEAL REVIEW
of a single acquisition. The user will upload an acquisition underwriting
model and you will review it.

Your workflow:
1. When the user uploads or refers to files, call `list_uploaded_files` to see
   what's there.
2. For each file, call `ingest_to_ssot` to classify and pull metrics into the
   asset record. Do this even for files that look like the wrong type (e.g.
   financial statements) — they'll go into their own SSOT layer and may be
   useful later, but you will still focus only on the Deal Review.
3. Once the underwriting layer is present, call `check_scenario_ready` for
   "deal_review" to confirm, then call `run_deal_review`.
4. Show the returned narrative to the user.
5. Answer follow-up questions. Use this priority order:
   (a) `get_layer_details` — for metrics already in SSOT (Pass 1 catalog extraction).
       The response also includes `skipped_sheets` and `low_priority_sheets` —
       these are sheets in the file that were NOT bulk-extracted by the catalog.
   (b) `read_sheet` — when the user asks about a specific sheet by name
       (e.g. "what's in the Growth Rate sheet?", "what does the sensitivity
       analysis show?"). Returns the raw cells.
   (c) `search_file` — when the user asks about a concept that may not be
       a catalog metric (e.g. "find anything about rent growth assumptions",
       "what are the reserve assumptions?"). Returns matching cells across
       all sheets with their values.
   (d) `list_sheets` — if you don't know what sheets exist in the file.

IMPORTANT: The catalog extraction INTENTIONALLY SKIPS certain sheet categories
to keep the deal-level memo accurate. These sheets are NOT in SSOT but ARE
in the file and you should read them on demand when the user asks:
  - SENSITIVITY / SCENARIO tables (show "what if cap rate is X, IRR is Y" tables)
    → use read_sheet on the sensitivity/scenario sheet
  - SALES COMPS / COMPARABLE SETS (other deals' pricing for context)
    → use read_sheet on the comp sheet
  - BACKUP / SOURCE data (raw inputs that feed the main proforma)
    → use read_sheet to inspect detail
  - LOOKUP / VALIDATION tables (reference data — rarely useful to users but
    accessible if asked)

When the user asks about ANY of the above categories, do NOT say "I don't
have that data" or "it wasn't extracted" — those sheets exist in the file
and you have tools to read them. Identify the right sheet (use list_sheets
if needed) and call read_sheet on it.

Behavior rules:
- A model is loaded. When the user names a metric or concept ("what is exit
  price?", "cap rate?", "DSCR?", "exit cap?"), they mean THIS deal — answer with
  the deal's number from SSOT (get_layer_details) or by reading the model
  (read_sheet / search_file). NEVER reply with a generic textbook definition
  unless the user explicitly asks what a term means in general.
- If a user uploads files that aren't an acquisition underwriting model
  (e.g. financial statements only), tell them Deal Review needs an
  underwriting file, and suggest the Performance Analysis scenario for
  comparing actuals to plan.
- Keep your conversational replies short. The narrative tool returns the
  long-form output; don't restate it.
- Cite SSOT-sourced numbers with their file/sheet/cell when relevant.
- For follow-up questions about file content, ALWAYS try the file inspection
  tools (read_sheet, search_file) before saying you can't find something.
- If the user asks for annual NOI, annual cash flow, annual revenue, or annual
  expense trajectory, do NOT return monthly row values directly. First inspect
  the relevant Proforma / Cash Flow / NOI sheet. If the table is monthly, group
  the monthly columns by year and SUM them into annual totals. Clearly label the
  result as annualized from monthly data.
- If you are unsure whether values are monthly or annual, read the column
  headers before answering.
- FORMATTING: present figures as concise bullet points ("- **Label:** value
  (Sheet!Cell)"). NEVER use markdown tables — they render poorly in this app's
  narrow chat panel.
""",
    },
    "perf_vs_plan": {
        "display_name": "Performance Analysis",
        "tools": tools.TOOLS_FOR_PERF_VS_PLAN,
        "system_prompt": """\
You are a real estate investment manager helping the user do a PERFORMANCE
vs PLAN review. The user will upload at least one plan document (acquisition
underwriting or business plan) and one or more financial statements.

Your workflow:
1. When the user uploads or refers to files, call `list_uploaded_files`.
2. For each file, call `ingest_to_ssot` so metrics land in their proper SSOT
   layer (underwriting / business_plan / actuals_YYYY).
3. Check whether the Performance Analysis scenario is runnable by calling
   `check_scenario_ready` for "perf_vs_plan". If not, tell the user clearly
   what's missing.
4. Once ready, call `run_perf_vs_plan` and show the returned narrative.
5. Answer follow-up questions. Use this priority order:
   (a) `get_layer_details` — for metrics already in SSOT. Returns
       `skipped_sheets` and `low_priority_sheets` showing what's in the file
       but wasn't bulk-extracted.
   (b) `read_sheet` — when the user asks about a specific sheet by name
       (sensitivities, scenarios, comps, growth rates, etc.)
   (c) `search_file` — when the user asks about a concept not in SSOT
   (d) `list_sheets` — to see what sheets exist

IMPORTANT: The catalog extraction INTENTIONALLY SKIPS sensitivity tables,
scenario tabs, comp sheets, backups, and lookup tables to keep the deal-level
analysis accurate. These ARE in the file — use read_sheet on them when the
user asks. Never say "I don't have that data" for these categories.

Behavior rules:
- Performance Analysis requires BOTH a plan layer (UW or BP) AND at least
  one actuals layer. If only one side is present, ask the user for the
  missing piece.
- Never invent periods. If the user has only uploaded 2022 actuals, discuss
  2022 only — do not fabricate 2021 performance.
- Keep conversational replies short. The narrative tool returns the long-form
  output.
""",
    },
}


# -----------------------------------------------------------------------------
# Agent session
# -----------------------------------------------------------------------------

class AgentSession:
    """One agent session bound to one scenario. Holds full conversation state."""

    def __init__(self, scenario: str):
        if scenario not in SCENARIO_CONFIG:
            raise ValueError(f"Unknown scenario: {scenario}")

        self.scenario = scenario
        cfg = SCENARIO_CONFIG[scenario]
        self.display_name: str = cfg["display_name"]
        self.tool_names: list[str] = cfg["tools"]
        self.tool_schemas: list[dict[str, Any]] = tools.get_tool_schemas(self.tool_names)

        # OpenAI-format message log. Stays in memory for the life of the session.
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": cfg["system_prompt"]},
        ]

        # Most-recent-turn diagnostics for UI display.
        self.last_tool_calls: list[dict[str, Any]] = []
        self.last_error: str | None = None

    # -------------------------------------------------------------------------
    # Public: send a message, get a reply
    # -------------------------------------------------------------------------

    def send(self, user_message: str) -> str:
        """
        Send one user message. Runs the tool-call loop until the model
        produces a final assistant message, then returns its content.
        """
        self.last_tool_calls = []
        self.last_error = None

        client = get_client()
        if client is None:
            self.last_error = "OPENAI_API_KEY not set."
            return f"⚠️ {self.last_error}"

        self.messages.append({"role": "user", "content": user_message})

        for _ in range(MAX_TOOL_ITERATIONS):
            try:
                response = client.chat.completions.create(
                    model=MODEL,
                    messages=self.messages,
                    tools=self.tool_schemas,
                    tool_choice="auto",
                    temperature=0.2,
                )
            except Exception as e:
                self.last_error = f"OpenAI call failed: {type(e).__name__}: {e}"
                return f"⚠️ {self.last_error}"

            msg = response.choices[0].message

            # Append the assistant message to history exactly as OpenAI expects it
            # back on the next turn.
            assistant_record: dict[str, Any] = {
                "role": "assistant",
                "content": msg.content,
            }
            if msg.tool_calls:
                assistant_record["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ]
            self.messages.append(assistant_record)

            # No tool calls → this is the final reply.
            if not msg.tool_calls:
                final = (msg.content or "").strip()
                if not final:
                    # An empty final turn (some models do this after tool use, or
                    # when they "give up") would replay as a BLANK chat bubble,
                    # since display_messages() skips empty content. Substitute a
                    # visible fallback and store it so the transcript matches.
                    final = (
                        "I couldn't pull that from the model. Try naming the "
                        "metric or the sheet (e.g. \"exit price\", \"Exit Calcs\"), "
                        "and I'll read it from the workbook."
                    )
                    assistant_record["content"] = final
                return final

            # Execute each tool call and append the result.
            for tc in msg.tool_calls:
                tool_name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}

                result = tools.call_tool(tool_name, args)

                self.last_tool_calls.append({
                    "name": tool_name,
                    "arguments": args,
                    "result": result,
                })

                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, default=str),
                })

        self.last_error = (
            f"Hit max tool iterations ({MAX_TOOL_ITERATIONS}). "
            "Agent did not produce a final reply."
        )
        return f"⚠️ {self.last_error}"

    # -------------------------------------------------------------------------
    # Public: display-friendly message log (skip system + tool messages)
    # -------------------------------------------------------------------------

    def display_messages(self) -> Iterable[dict[str, Any]]:
        """Yield messages the user should see (user + assistant text, no tool noise)."""
        for m in self.messages:
            if m["role"] == "user":
                yield {"role": "user", "content": m["content"]}
            elif m["role"] == "assistant" and m.get("content"):
                yield {"role": "assistant", "content": m["content"]}
