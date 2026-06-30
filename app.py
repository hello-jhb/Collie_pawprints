"""
app.py — v2 frontend.

Flow:
  1. Landing screen: scenario picker (4 cards, 2 active + 2 "coming soon").
  2. After picking a scenario, scoped chat workspace:
     - "← Back to scenarios" header
     - File uploader (auto-clears previous batch + resets SSOT)
     - Chat thread with the scenario-bound agent
     - SSOT panel in an expander

The agent does all the heavy lifting (classify, ingest, run scenario, answer
follow-ups). This file is just orchestration + presentation.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

import ssot
import tools
from agent_loop import AgentSession, SCENARIO_CONFIG


# Which scenario tool to run deterministically per scenario key.
_SCENARIO_RUNNER = {
    "deal_review": tools.run_deal_review,
    "perf_vs_plan": tools.run_perf_vs_plan,
}


# =============================================================================
# Page config & global CSS
# =============================================================================

st.set_page_config(
    page_title="Moose HQ",
    page_icon="🏢",
    layout="wide",
)

st.markdown(
    """
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

  html, body, .stApp { font-family: "Inter", system-ui, sans-serif; }
  .block-container { padding-top: 2rem; max-width: 1500px; }

  /* Hero */
  .hero-title { font-size: 32px; font-weight: 700; margin-bottom: 4px; }
  .hero-sub   { font-size: 15px; color: #6b7280; margin-bottom: 24px; }

  /* Scenario cards */
  .scenario-card {
    border: 1px solid #e5e7eb;
    border-radius: 12px;
    padding: 20px;
    background: #ffffff;
    height: 100%;
    transition: border-color 0.2s, box-shadow 0.2s;
  }
  .scenario-card.active:hover {
    border-color: #2563eb;
    box-shadow: 0 4px 14px rgba(37, 99, 235, 0.08);
  }
  .scenario-card.disabled { opacity: 0.55; background: #fafafa; }
  .scenario-card .label {
    display: inline-block;
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    padding: 2px 8px;
    border-radius: 4px;
    margin-bottom: 12px;
  }
  .scenario-card .label.live    { background: #dcfce7; color: #166534; }
  .scenario-card .label.soon    { background: #f3f4f6; color: #6b7280; }
  .scenario-card .title  { font-size: 18px; font-weight: 600; margin-bottom: 6px; }
  .scenario-card .desc   { font-size: 13px; color: #4b5563; line-height: 1.5; min-height: 56px; }

  /* Deal Analyzer (3-column) */
  .da-header { display: flex; align-items: baseline; gap: 14px; }
  .da-title  { font-size: 26px; font-weight: 700; }
  .da-date   { font-size: 13px; color: #6b7280; }
  .da-col-title {
    font-size: 12px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.06em; color: #6b7280; margin-bottom: 10px;
  }

  /* Hide the chat avatar icons (robot / person) — content only */
  [data-testid^="stChatMessageAvatar"] { display: none !important; }
  [data-testid="stChatMessage"] { padding-left: 8px; gap: 0; }

  /* Suggested-prompt chips above the chat box (scoped by container key) */
  .st-key-dd_chips .stButton button {
    border-radius: 999px;
    padding: 2px 14px;
    font-size: 12px;
    min-height: 30px;
    border: 1px solid #0f766e;
    background: #f0fdfa;
    color: #0f766e;
  }
  .st-key-dd_chips .stButton button:hover {
    border-color: #14b8a6;
    background: #ccfbf1;
  }

  /* Square corners everywhere EXCEPT buttons (mockup spec) */
  [data-testid="stVerticalBlockBorderWrapper"],
  [data-testid="stForm"],
  [data-testid="stExpander"] details,
  [data-testid="stExpander"] summary,
  [data-testid="stChatMessage"],
  [data-testid="stFileUploaderDropzone"],
  [data-testid="stAlert"],
  [data-testid="stDataFrame"],
  [data-testid="stDataEditor"],
  .stTextArea textarea, .stTextInput input,
  div[data-baseweb="textarea"], div[data-baseweb="input"],
  div[data-baseweb="base-input"] {
    border-radius: 0 !important;
  }

  /* Scoped workspace header */
  .ws-header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 18px; }
  .ws-title  { font-size: 22px; font-weight: 600; }
  .ws-scen   { font-size: 12px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.06em; }

  /* SSOT pills */
  .ssot-pill {
    display: inline-block;
    padding: 3px 10px;
    border-radius: 12px;
    background: #eef2ff;
    color: #3730a3;
    font-size: 11px;
    font-weight: 500;
    margin: 2px 4px 2px 0;
  }

  /* Tool-trace items */
  .tool-trace {
    font-family: "JetBrains Mono", "Fira Code", monospace;
    font-size: 11px;
    color: #4b5563;
    margin: 2px 0;
  }
</style>
""",
    unsafe_allow_html=True,
)


# =============================================================================
# Session state
# =============================================================================

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)


def _ui_mode() -> str:
    """'customer' (clean, outcome-only) or 'dev' (full panels). Resolved from the
    ?mode= query param, then st.secrets['COLLIE_UI'], then the COLLIE_UI env var;
    defaults to 'dev'. The deployed customer app sets COLLIE_UI=customer."""
    import os
    val = ""
    try:
        val = st.query_params.get("mode") or ""
    except Exception:
        val = ""
    if not val:
        try:
            val = st.secrets.get("COLLIE_UI", "") or ""
        except Exception:
            val = ""
    return (val or os.environ.get("COLLIE_UI", "") or "dev").strip().lower()

# Default values for every session_state key the app uses. Defined as a single
# dict so we can both initialize on first load AND defensively fall back to
# defaults if any key is missing later (e.g. after a Streamlit error / rerun
# that somehow lost state).
_SESSION_DEFAULTS: dict = {
    "active_scenario":    None,
    "agent_session":      None,
    "uploaded_filenames": set(),
    "last_auto_message":  None,
    # Files whose auto-classification failed and are awaiting a user choice.
    # Shape: {filename: error_message}
    "pending_overrides":  {},
    # Set of batches (frozensets of filenames) we've already run the scenario for.
    "completed_batches":  set(),
    # --- Stage-1 Audit Appendix verification gate (deal_review only) ---
    # AAM extraction result for the current batch: {metric_name: record}
    "aam_records":          {},
    # Which batch (frozenset of filenames) aam_records belongs to.
    "aam_batch_id":         None,
    # Batches the user has reviewed + confirmed in the audit appendix.
    "aam_confirmed_batches": set(),
    # Bumped each time GPT blank-fill runs, so the data_editor rebuilds cleanly
    # from the updated records instead of replaying a stale edit delta.
    "aam_fill_version":      0,
    # Batches for which the user explicitly requested analysis (staged: facts
    # are confirmed first, then analysis is a separate deliberate step).
    "aam_analysis_requested": set(),
    # --- Workbook Orientation (runs BEFORE AAM extraction) ---
    # Orientation result for the current batch: the workbook map (sheet roles,
    # confidences, signals) from workbook_orientation.orient_workbook.
    "wb_orientation":        None,
    # Which batch (frozenset of filenames) wb_orientation belongs to.
    "wb_orientation_batch":  None,
    # --- Model Brief (comprehension-first read; step 1 of the rebuild) ---
    # build_model_brief result for the current batch, and its batch id.
    "model_brief":           None,
    "model_brief_batch":     None,
    # --- Trust engine (step 2): per-fact verdicts over the brief's facts ---
    "trust_scored":          None,
    "trust_scored_batch":    None,
    # --- Finalized brief (step 4): narrative regenerated from verified facts ---
    "finalized_brief":       None,
    "finalized_brief_batch": None,
    # --- Deal Truth (deal_truth.py): canonical facts reconstructed + validated
    # from the cash flow; the source of truth Layer 3 + chat now read from. ---
    "deal_truth":            None,
    "deal_truth_batch":      None,
    # --- Integrated analysis (deal_analysis.py): one grounded analysis (capital
    # structure / returns / cash flow-NOI / capex) shown in the Outcome. ---
    "deal_analysis":         None,
    "deal_analysis_batch":   None,
    # --- Perf vs Plan (perf_vs_plan_engine.py): actuals statement(s) uploaded
    # SEPARATELY and compared to THIS model's projected NOI. Optional add-on — the
    # Deal Analyzer works standalone without it; the perf section only appears
    # once a statement is uploaded. ---
    "actuals_filenames":     set(),
    "perf_vs_plan":          None,
    "perf_vs_plan_key":      None,
    # --- Investment Read (interpretation.py): GPT narrates the validated fact
    # sheet (+ perf, when actuals exist) into an IC-memo-style read; leads the
    # Outcome. Cached per (model + actuals). ---
    "investment_read":       None,
    "investment_read_key":   None,
    # --- Deal Analyzer 3-column frontend (deal_review) ---
    # Batch whose deal context has been seeded into the chat agent's history
    # (so follow-up Q&A is grounded in THIS model, not answered generically).
    "agent_grounded_batch": None,
    # Findings folded into the Outcome (middle column) from the chat / deep-dives.
    # Shape: list of {"id": str, "title": str, "content": str}
    "outcome_findings":   [],
    # Content signatures already folded into the Outcome (dedupe the +Add button).
    "outcome_added":      set(),
    # Generated Snapshot narrative shown in the Outcome (middle column).
    "snapshot_md":        None,
    # The memorialized final report — this is what becomes the persisted SSOT.
    "final_report_md":    None,
}


def _ensure_session_state() -> None:
    """
    Initialize every session_state key that doesn't already exist.
    Called both at module load AND defensively at the top of every render
    function so a missing-attr error can never trigger from these keys.
    """
    for key, default in _SESSION_DEFAULTS.items():
        if key not in st.session_state:
            # Use a fresh copy of mutable defaults so all sessions don't share
            # the same dict/set instance.
            if isinstance(default, (set, dict, list)):
                st.session_state[key] = type(default)()
            else:
                st.session_state[key] = default


# Run once on module load — every script rerun re-executes this module
_ensure_session_state()


# Layer options the user can pick from the manual-override dropdown.
# Must match ssot.KNOWN_LAYERS exactly. Ordered: most common choices first.
_MANUAL_LAYER_OPTIONS = [
    "underwriting",
    "business_plan",
    "actuals_recent",
    "actuals_2020",
    "actuals_2021",
    "actuals_2022",
    "actuals_2023",
    "actuals_2024",
    "actuals_2025",
    "rent_roll",
    "debt",
]


# =============================================================================
# Helpers
# =============================================================================

def _wipe_uploads_and_reset_ssot() -> None:
    """Clean slate for a new analysis."""
    for p in UPLOAD_DIR.iterdir():
        if p.is_file():
            try:
                p.unlink()
            except OSError:
                pass
    ssot.reset_ssot()
    st.session_state.uploaded_filenames = set()
    st.session_state.pending_overrides = {}
    st.session_state.completed_batches = set()
    st.session_state.aam_records = {}
    st.session_state.aam_batch_id = None
    st.session_state.aam_confirmed_batches = set()
    st.session_state.aam_fill_version = 0
    st.session_state.aam_analysis_requested = set()
    st.session_state.wb_orientation = None
    st.session_state.wb_orientation_batch = None
    st.session_state.model_brief = None
    st.session_state.model_brief_batch = None
    st.session_state.trust_scored = None
    st.session_state.trust_scored_batch = None
    st.session_state.finalized_brief = None
    st.session_state.finalized_brief_batch = None


def _activate_scenario(scenario_key: str) -> None:
    """User clicked a scenario card — start a fresh session for it."""
    _wipe_uploads_and_reset_ssot()
    st.session_state.active_scenario = scenario_key
    st.session_state.agent_session = AgentSession(scenario_key)
    st.session_state.last_auto_message = None


def _back_to_landing() -> None:
    st.session_state.active_scenario = None
    st.session_state.agent_session = None


def _ssot_panel() -> None:
    """Show what's currently in SSOT."""
    summary = ssot.ssot_summary()
    layers = summary["layers_present"]
    files = summary["ingested_files"]

    if not layers and not files:
        st.caption("No files ingested yet.")
        return

    # Show last ingested timestamp — makes stale SSOT data immediately visible
    last_update = summary.get("updated_at")
    if last_update:
        from datetime import datetime, timezone
        try:
            ts = datetime.fromisoformat(last_update)
            age = datetime.now(timezone.utc) - ts
            hours = int(age.total_seconds() // 3600)
            age_str = f"{hours}h ago" if hours < 48 else f"{age.days}d ago"
            st.caption(f"Last ingested: {age_str}")
        except Exception:
            st.caption(f"Last ingested: {last_update[:10]}")

    st.markdown("**Layers in SSOT:**")
    if layers:
        st.markdown(
            " ".join(f'<span class="ssot-pill">{layer}</span>' for layer in layers),
            unsafe_allow_html=True,
        )
    else:
        st.caption("(none)")

    st.markdown("**Files ingested:**")
    if files:
        for f in files:
            st.markdown(f"- {f}")
    else:
        st.caption("(none)")

    # Catalog improvement suggestions from Pass 2 GPT gap-fill.
    # These are labels GPT found in the file that aren't in the catalog yet.
    # Adding them as aliases means the next file won't need GPT to find them.
    all_suggestions = []
    s = ssot.load_ssot()
    for layer_data in s.get("layers", {}).values():
        all_suggestions.extend(layer_data.get("catalog_suggestions", []))

    if all_suggestions:
        with st.expander(f"💡 {len(all_suggestions)} catalog alias suggestion(s)", expanded=False):
            st.caption(
                "GPT found these metrics under labels not in the catalog. "
                "Add them to Snapshot Metric.xlsx to avoid needing GPT for future files."
            )
            for s_ in all_suggestions:
                st.markdown(
                    f"**{s_['metric_name']}** — add alias: `{s_['found_as_label']}` "
                    f"(sheet: {s_.get('sheet', '?')})"
                )

    # JSON Knowledge diagnostics — which human-approved patterns are live.
    # Only `active` patterns influence runtime prompts; candidates/invalid don't.
    try:
        from knowledge_store import (
            knowledge_diagnostics, load_observations, load_learned_patterns,
            set_pattern_status,
        )
        kd = knowledge_diagnostics()
        n_active = kd.get("active_patterns_loaded", 0)
        n_cand = kd.get("candidate_patterns_ignored", 0)          # int count
        n_inv = len(kd.get("invalid_patterns", []) or [])
        observations = load_observations()
        learned = load_learned_patterns()
        candidates = [r for r in learned if str(r.get("status")).lower() == "candidate"]
        with st.expander(
            f"🧠 JSON Knowledge — {n_active} active / {n_cand} candidate / {n_inv} invalid "
            f"· {len(observations)} obs",
            expanded=False,
        ):
            st.caption(
                "Only **active** patterns are injected into runtime prompts (AAM "
                "extraction, blank-fill, resolver, workbook mapping, business-plan). "
                "Your corrections at the gate are captured as observations; repeated "
                "ones become candidates you can promote. GPT never self-promotes."
            )
            st.json(kd)

            if candidates:
                st.markdown("**Learned candidates** — promote to let them influence extraction:")
                for c in candidates:
                    st.markdown(
                        f"- `{c['rule_id']}` · evidence **{c.get('evidence_count')}** — "
                        f"{c.get('description', '')[:200]}"
                    )
                    cprom, crej, _sp = st.columns([1, 1, 5])
                    if cprom.button("✅ Promote", key=f"promote_{c['rule_id']}"):
                        set_pattern_status(c["rule_id"], "active")
                        st.rerun()
                    if crej.button("✕ Reject", key=f"reject_{c['rule_id']}"):
                        set_pattern_status(c["rule_id"], "rejected")
                        st.rerun()
            else:
                st.caption(f"No learned candidates yet ({len(observations)} observation(s) "
                           "accumulating; a metric needs ≥3 corrections to surface one).")
    except Exception:
        pass

    # Model Tables — the table-centric read: which tables Collie found and the
    # periodicity each one (and its rows) carries.
    try:
        uw = ssot.load_ssot().get("layers", {}).get("underwriting", {}) or {}
        mt = uw.get("model_tables") or {}
        if mt.get("tables"):
            with st.expander(
                f"📐 Model Tables — {mt.get('count', 0)} parsed · "
                f"{mt.get('tagged_metrics', 0)} metric(s) tagged with periodicity",
                expanded=False,
            ):
                st.caption(
                    "Each table's date header sets its periodicity; every row "
                    "inherits it. Flow metrics (NOI, revenue, expenses) extracted "
                    "from a table cell are tagged accordingly."
                )
                st.dataframe(
                    [
                        {
                            "sheet": t["sheet"],
                            "table": t.get("title") or "—",
                            "type": t["table_type"],
                            "periodicity": t["periodicity"],
                            "periods": t["n_periods"],
                            "rows": t["n_rows"],
                        }
                        for t in mt["tables"]
                    ],
                    width="stretch",
                )
    except Exception:
        pass

    # Analyst Bundle — the reviewable run package: what Collie looked at,
    # what it believes, and what it refused to trust. The trust bridge.
    try:
        from analyst_bundle import build_analyst_bundle
        bundle = build_analyst_bundle("underwriting")
        if "error" not in bundle:
            ss = bundle.get("status_summary", {})
            n_issues = len(bundle.get("issues", []))
            n_verified = len(bundle.get("verified_facts", []))
            with st.expander(
                f"🔎 Analyst Bundle — {n_verified} verified / {n_issues} to review",
                expanded=False,
            ):
                metadata = bundle.get("run_metadata", {})
                if metadata:
                    st.markdown("**Run Metadata**")
                    st.json(metadata)

                wm = bundle.get("workbook_map", {})
                st.markdown("**Workbook Map** — which tabs Collie read vs skipped")
                st.caption(
                    f"{len(wm.get('all_sheets', []))} sheets · "
                    f"{len(wm.get('skipped_sheets', []))} skipped "
                    f"({', '.join(wm.get('skipped_sheets', [])[:6])}{'…' if len(wm.get('skipped_sheets', []))>6 else ''})"
                )
                content_roles = wm.get("content_roles") or {}
                authoritative_tabs = wm.get("authoritative_tabs") or {}
                if content_roles:
                    st.markdown("**Content Roles** — workbook mapper classifications")
                    st.dataframe(
                        [
                            {
                                "sheet": sheet,
                                "role": info.get("role"),
                                "confidence": info.get("confidence"),
                                "tier": info.get("implied_tier"),
                            }
                            for sheet, info in content_roles.items()
                        ],
                        width="stretch",
                    )
                if authoritative_tabs:
                    st.markdown("**Authoritative Tabs** — section-reader source map")
                    st.json(authoritative_tabs)

                st.markdown("**Status Summary** — QC health check")
                st.json(ss)

                identity_checks = bundle.get("identity_checks") or []
                if identity_checks:
                    st.markdown("**Identity Checks** — final reconciled values only")
                    st.dataframe(identity_checks, width="stretch")

                knowledge_usage = bundle.get("knowledge_usage") or {}
                if knowledge_usage:
                    st.markdown("**Knowledge Usage** — active JSON patterns")
                    st.json(knowledge_usage)

                extraction_plan = bundle.get("extraction_plan") or []
                if extraction_plan:
                    with st.expander("Extraction Plan", expanded=False):
                        st.dataframe(
                            [
                                {
                                    "metric": row.get("metric"),
                                    "section": row.get("section"),
                                    "expected_roles": ", ".join(row.get("expected_source_roles") or []),
                                    "actual_sheet": (row.get("actual_source_used") or {}).get("sheet"),
                                    "actual_cell": (row.get("actual_source_used") or {}).get("cell"),
                                    "status": (row.get("actual_source_used") or {}).get("status"),
                                    "method": (row.get("actual_source_used") or {}).get("method"),
                                }
                                for row in extraction_plan
                            ],
                            width="stretch",
                        )

                bpr = bundle.get("business_plan_read", {})
                if any(bpr.values()):
                    st.markdown("**Business-Plan Read** — GPT interpretation (not facts)")
                    st.json({k: v for k, v in bpr.items() if v})

                st.markdown("**Issues / QC Flags** — what Collie refused to trust")
                issues = bundle.get("issues", [])
                if issues:
                    st.dataframe(
                        [{"metric": r["metric"], "status": r["status"],
                          "value": r["value"], "source": f"{r.get('source_sheet')}!{r.get('source_cell')}"}
                         for r in issues],
                        width="stretch",
                    )
                else:
                    st.caption("None — every bounded metric verified.")

                st.markdown("**Verified Facts** — what Collie believes, with provenance")
                st.dataframe(
                    [{"metric": r["metric"], "value": r["value"],
                      "period": r.get("period"), "status": r["status"],
                      "source": f"{r.get('source_sheet')}!{r.get('source_cell')}"}
                     for r in bundle.get("verified_facts", [])],
                    width="stretch",
                )
                source_audit = bundle.get("source_audit") or []
                if source_audit:
                    with st.expander("Source Audit", expanded=False):
                        st.caption(
                            "Full source audit is saved in the Analyst Bundle JSON. "
                            "Showing a compact summary here to keep the app responsive."
                        )
                        st.dataframe(
                            [
                                {
                                    "metric": row.get("metric"),
                                    "accepted": (
                                        f"{(row.get('accepted_source') or {}).get('sheet')}!"
                                        f"{(row.get('accepted_source') or {}).get('cell')}"
                                    ),
                                    "rejected": len(row.get("rejected_candidates") or []),
                                    "conflicts": len(row.get("conflicts") or []),
                                    "alternates": len(row.get("alternate_candidates") or []),
                                }
                                for row in source_audit
                            ],
                            width="stretch",
                        )
                if bundle.get("bundle_path"):
                    st.caption(f"Saved: {bundle['bundle_path']}")
    except Exception:
        pass


# =============================================================================
# Landing view — scenario picker
# =============================================================================

def render_landing() -> None:
    _ensure_session_state()  # defensive safety net
    st.markdown(
        '<div class="hero-title">Moose HQ</div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="hero-sub">Pick an analysis to start. Each scenario is a '
        'scoped workspace — upload the relevant files and chat with the agent.</div>',
        unsafe_allow_html=True,
    )

    # Card grid: 2 columns x 2 rows
    cards = [
        {
            "key": "deal_review",
            "label": "live",
            "title": "Deal Analysis",
            "desc": "Summarize an acquisition from a single underwriting model. "
                    "Going-in basis, NOI, IRR, exit value, debt terms.",
            "active": True,
        },
        {
            "key": "perf_vs_plan",
            "label": "live",
            "title": "Performance Analysis",
            "desc": "Compare actuals against the underwriting or business plan. "
                    "Year-by-year variance with driver attribution.",
            "active": True,
        },
        {
            "key": "lease_review",
            "label": "soon",
            "title": "Lease Review",
            "desc": "Reconcile tenant-level data between leases and rent rolls. "
                    "Flag discrepancies in term, base rent, escalations.",
            "active": False,
        },
        {
            "key": "debt_analysis",
            "label": "soon",
            "title": "Debt Analysis",
            "desc": "DSCR, debt yield, LTV against loan covenants. "
                    "Refinance and maturity outlook.",
            "active": False,
        },
    ]

    row1 = st.columns(2, gap="medium")
    row2 = st.columns(2, gap="medium")

    for card, col in zip(cards, [*row1, *row2]):
        with col:
            cls = "scenario-card active" if card["active"] else "scenario-card disabled"
            label_cls = "live" if card["active"] else "soon"
            label_text = "Available" if card["active"] else "Coming soon"

            st.markdown(
                f"""
<div class="{cls}">
  <span class="label {label_cls}">{label_text}</span>
  <div class="title">{card['title']}</div>
  <div class="desc">{card['desc']}</div>
</div>
""",
                unsafe_allow_html=True,
            )

            if card["active"]:
                st.button(
                    f"Start →",
                    key=f"start_{card['key']}",
                    on_click=_activate_scenario,
                    args=(card["key"],),
                    width="stretch",
                )
            else:
                st.button(
                    "Not available yet",
                    key=f"disabled_{card['key']}",
                    disabled=True,
                    width="stretch",
                )


# =============================================================================
# Scenario view — file uploader + chat
# =============================================================================

def render_scenario() -> None:
    # Defensive: ensure all session_state keys exist before any access.
    # Streamlit *should* keep them across reruns, but a partial-execution
    # error can occasionally leave state in a half-initialized place.
    _ensure_session_state()

    scenario_key = st.session_state.active_scenario
    cfg = SCENARIO_CONFIG[scenario_key]
    agent: AgentSession = st.session_state.agent_session

    # deal_review and perf_vs_plan both use the 3-column Deal Analyzer (the model
    # is analysed there; perf_vs_plan adds a separate statements uploader). The
    # legacy single-column agent-loop path below is kept only as a safeguard and
    # is no longer reached from the UI.
    if scenario_key in ("deal_review", "perf_vs_plan"):
        if _ui_mode() == "customer":
            _render_customer_analyzer(agent)
        else:
            _render_deal_analyzer(agent)
        return

    # Header
    left, right = st.columns([4, 1])
    with left:
        st.markdown(
            f'<div class="ws-header"><div>'
            f'<div class="ws-scen">{cfg["display_name"]}</div>'
            f'<div class="ws-title">Workspace</div>'
            f'</div></div>',
            unsafe_allow_html=True,
        )
    with right:
        st.button("← Back to scenarios", on_click=_back_to_landing, width="stretch")

    st.divider()

    # File uploader
    uploaded = st.file_uploader(
        "Upload files for this analysis",
        type=["xlsx", "xlsm"],
        accept_multiple_files=True,
        key=f"upload_{scenario_key}",
    )

    # Detect a new upload batch (different filenames than what we've already
    # processed in this session). Save the new files to disk.
    new_files = []
    if uploaded:
        current_names = {f.name for f in uploaded}
        if current_names != st.session_state.uploaded_filenames:
            new_files = [f for f in uploaded if f.name not in st.session_state.uploaded_filenames]
            for uf in new_files:
                (UPLOAD_DIR / uf.name).write_bytes(uf.getbuffer())
            st.session_state.uploaded_filenames = current_names

    # SSOT panel
    with st.expander("📂 SSOT — Asset record", expanded=False):
        _ssot_panel()

    st.divider()

    # Replay chat history so the workspace looks consistent across reruns.
    for m in agent.display_messages():
        with st.chat_message(m["role"]):
            st.markdown(m["content"])

    # ---------------------------------------------------------------------
    # Deterministic orchestration (legacy scenarios only; deal_review uses the
    # 3-column Deal Analyzer and returns earlier).
    # ---------------------------------------------------------------------

    # Phase 1: ingest any new files (queues manual-override candidates).
    if new_files:
        _ingest_new_files(new_files)

    # Phase 2: if files need manual classification, show the override form and
    # stop — we can't run the scenario until layers are resolved.
    if st.session_state.get("pending_overrides"):
        _render_manual_override_ui()
        user_input = st.chat_input("Ask a follow-up question...")
        _handle_chat_input(agent, user_input)
        return

    # Phase 3: run the scenario if we haven't already for this batch.
    if st.session_state.uploaded_filenames:
        batch_id = frozenset(st.session_state.uploaded_filenames)
        if batch_id not in st.session_state.completed_batches:
            _run_scenario_for_batch(agent, scenario_key)

    # User chat input — this is where the agent earns its keep (Q&A).
    user_input = st.chat_input("Ask a follow-up question...")
    _handle_chat_input(agent, user_input)


# =============================================================================
# Deal Analyzer — 3-column workspace (process | Outcome | Chat)
# =============================================================================

_REPORT_SYSTEM = (
    "You are a real estate analyst writing the final deal memo. You are given the "
    "verified facts, the Deal Brief, the Initial View (the investment reasoning — "
    "return composition, value creation, leverage risk, what breaks the deal), the "
    "snapshot, and the analyses the user chose to keep. "
    "Memorialize them into one clean, well-structured markdown report. Use ONLY "
    "the content provided — do not invent figures. PRESERVE the Initial View's "
    "judgment (prefer facts over adjectives — keep the return-source attribution "
    "and risk flags; never soften them into generic praise). Keep section "
    "headings; tighten prose; remove redundancy. Present figures as bullet points; "
    "NEVER use markdown tables. This report is the deal's source of truth."
)


def _render_deal_analyzer(agent: AgentSession) -> None:
    """Three-column Deal Analyzer: process controls | Outcome | Chat."""
    import datetime as _dt

    hl, hr = st.columns([4, 1])
    with hl:
        st.markdown(
            f'<div class="da-header"><span class="da-title">Deal Analyzer</span>'
            f'<span class="da-date">Date: {_dt.date.today():%Y-%m-%d}</span></div>',
            unsafe_allow_html=True,
        )
    with hr:
        st.button("← Back to scenarios", on_click=_back_to_landing, width="stretch")
    st.divider()

    files = st.session_state.uploaded_filenames
    batch_id = frozenset(files) if files else None
    confirmed = bool(batch_id) and batch_id in st.session_state.aam_confirmed_batches
    analysis_req = bool(batch_id) and batch_id in st.session_state.aam_analysis_requested

    # Once facts are confirmed, the underwriting layer is in SSOT — ground the
    # chat agent in this deal so follow-up Q&A reads THIS model (not generic
    # answers). Cheap + idempotent (seeds once per batch).
    if confirmed:
        _ensure_agent_grounded(agent, batch_id)

    left, mid, right = st.columns([1.1, 2.3, 1.5], gap="large")

    # Left renders in TWO phases: the upload + extraction progress must run
    # BEFORE the middle column builds the appendix from the extracted records,
    # while the Proceed button needs the edited appendix the middle returns.
    # Streamlit places output by column, so the visual order stays clean.
    with left:
        _render_process_inputs(files, batch_id, confirmed)
    with mid:
        gate_edited = _render_outcome_column(agent, batch_id, confirmed, analysis_req)
    with left:
        _render_process_actions(agent, batch_id, confirmed, analysis_req, gate_edited)
    with right:
        _render_chat_column(agent)

    with st.expander("📂 SSOT — Asset record (debug)", expanded=False):
        _ssot_panel()


# =============================================================================
# Customer view — single split screen (files + progress | outcome + prompt)
# =============================================================================
# Same engine and caches as the dev Deal Analyzer; presentation only. The GPT
# "deal brief", the orientation panel, the 22-field checklist, the trust panel and
# the SSOT debug are all invisible here — only the deterministic, cash-flow-
# validated Deal Analysis shows. Perf-vs-plan is step 4 when actuals are uploaded.


def _render_customer_analyzer(agent: AgentSession) -> None:
    import datetime as _dt
    st.markdown(
        f'<div class="da-header"><span class="da-title">Collie</span>'
        f'<span class="da-date">Deal analysis · {_dt.date.today():%Y-%m-%d}</span></div>',
        unsafe_allow_html=True,
    )
    st.divider()

    files = st.session_state.uploaded_filenames
    batch_id = frozenset(files) if files else None
    dt = _ensure_deal_truth(batch_id) if batch_id else None
    if batch_id:
        try:
            _ensure_agent_grounded(agent, batch_id)
        except Exception:                          # grounding is best-effort
            pass

    left, mid = st.columns([1, 2.4], gap="large")
    with left:
        _customer_inputs(files, batch_id, dt)
    with mid:
        _customer_outcome(batch_id, dt, agent)


def _customer_inputs(files, batch_id, dt) -> None:
    st.markdown("**Files**")
    up = st.file_uploader("Upload model", type=["xlsx", "xlsm"],
                          accept_multiple_files=True, key="cust_model_upload",
                          label_visibility="collapsed")
    if up:
        for uf in up:
            dest = UPLOAD_DIR / uf.name
            if not dest.exists():
                dest.write_bytes(uf.getbuffer())
        current = {f.name for f in up}
        if current != st.session_state.uploaded_filenames:
            st.session_state.uploaded_filenames = current
            st.rerun()

    if batch_id:
        st.caption("Actuals — track vs plan (optional)")
        act = st.file_uploader("Upload statement(s)", type=["xlsx", "xlsm"],
                               accept_multiple_files=True, key="cust_actuals_upload",
                               label_visibility="collapsed")
        if act:
            for uf in act:
                dest = UPLOAD_DIR / uf.name
                if not dest.exists():
                    dest.write_bytes(uf.getbuffer())
            current = {f.name for f in act}
            if current != st.session_state.actuals_filenames:
                st.session_state.actuals_filenames = current
                st.rerun()

    st.markdown("**Progress**")
    engine_ok = bool(dt and not dt.get("error") and dt.get("engine_found", True))
    failed = bool(batch_id and dt and (dt.get("error") or not dt.get("engine_found", True)))
    st.markdown(f"{':green[✓]' if batch_id else ':gray[○]'} Read the model")
    st.markdown(f"{':green[✓]' if engine_ok else (':orange[!]' if failed else ':gray[○]')} "
                "Reconstruct & validate")
    st.markdown(f"{':green[✓]' if engine_ok else ':gray[○]'} Deal analysis")
    if st.session_state.actuals_filenames:
        st.markdown(":blue[●] **How are we tracking?**")


def _customer_analysis(batch_id, dt):
    """The integrated analysis, cached per batch (same cache keys as the dev view)."""
    cache = st.session_state.get("deal_analysis")
    if st.session_state.get("deal_analysis_batch") == batch_id and cache:
        return cache
    files = sorted(st.session_state.uploaded_filenames)
    if not files:
        return None
    from deal_analysis import build_analysis
    try:
        with st.spinner("Building the analysis…"):
            cache = build_analysis(UPLOAD_DIR / files[0], dt=dt)
    except Exception as e:                         # pragma: no cover - defensive
        return {"md": f"_Analysis unavailable: {e}_"}
    st.session_state.deal_analysis = cache
    st.session_state.deal_analysis_batch = batch_id
    return cache


def _customer_outcome(batch_id, dt, agent) -> None:
    if not batch_id:
        st.info("Upload an underwriting model on the left to begin.")
        return
    if not dt or dt.get("error"):
        st.warning("Couldn't reconstruct this model from its cash flow."
                   + (f" ({dt.get('error')})" if dt and dt.get("error") else ""))
        return

    # The Investment Read leads — the synthesized IC-memo read; Deal Analysis below
    # is the supporting detail it's grounded in.
    _customer_analysis(batch_id, dt)            # warm the deal-analysis cache first
    _render_investment_read(batch_id, dt)

    with st.container(border=True):
        if dt.get("engine_found", True):
            st.markdown(":green[✓ Reconstructed & validated from the cash flow]")
        analysis = _customer_analysis(batch_id, dt)
        if analysis and analysis.get("md"):
            st.markdown(analysis["md"])
        elif dt.get("brief_facts"):
            _render_nonnegotiables_md(dt)

    if st.session_state.actuals_filenames:
        res = _ensure_perf_vs_plan(batch_id)
        if res:
            with st.container(border=True):
                if res.get("ok"):
                    st.markdown(res["md"])
                else:
                    st.markdown("### How are we tracking?")
                    st.warning(res.get("reason", ""))
                    for f in (res.get("validation") or {}).get("failures", [])[:6]:
                        st.markdown(f"- {f.get('identity')} [{f.get('period')}]: {f.get('detail')}")

    st.markdown("##### Ask about this deal")
    for m in agent.display_messages():
        with st.chat_message(m["role"]):
            st.markdown(m["content"])
    _handle_chat_input(agent, st.chat_input("Ask about this deal…"))


def _render_process_inputs(files, batch_id, confirmed: bool) -> None:
    """Left column, phase 1: upload, extraction progress bar, progress checklist."""
    st.markdown('<div class="da-col-title">Input &amp; analysis</div>', unsafe_allow_html=True)

    uploaded = st.file_uploader(
        "Upload workbook", type=["xlsx", "xlsm"],
        accept_multiple_files=True, key="da_upload",
    )
    if uploaded:
        # Streamlit Cloud's filesystem is ephemeral — a rerun or redeploy can
        # wipe uploads/ while the uploader still holds the bytes in memory. So
        # ALWAYS re-persist any file whose disk copy is missing, not just on a
        # new upload. Otherwise downstream file reads hit FileNotFoundError.
        for uf in uploaded:
            dest = UPLOAD_DIR / uf.name
            if not dest.exists():
                dest.write_bytes(uf.getbuffer())
        current = {f.name for f in uploaded}
        if current != st.session_state.uploaded_filenames:
            st.session_state.uploaded_filenames = current
            st.rerun()

    # --- Actuals statement(s) — optional add-on for "How are we tracking?".
    # Placed right under the model upload so it is the FIRST thing visible once a
    # model loads (otherwise it gets buried under the orientation + progress
    # panels). Separate uploader; the Deal Analyzer works standalone without it.
    if batch_id:
        st.markdown("**📊 Actuals — track vs plan (optional)**")
        st.caption("Upload monthly operating statement(s) to compare actual NOI to "
                   "this model's projection.")
        act = st.file_uploader(
            "Upload statement(s)", type=["xlsx", "xlsm"],
            accept_multiple_files=True, key="da_actuals_upload",
            label_visibility="collapsed",
        )
        if act:
            for uf in act:
                dest = UPLOAD_DIR / uf.name
                if not dest.exists():
                    dest.write_bytes(uf.getbuffer())
            current = {f.name for f in act}
            if current != st.session_state.actuals_filenames:
                st.session_state.actuals_filenames = current
                st.rerun()
        st.divider()

    # Workbook Orientation runs FIRST — understand the workbook's structure
    # before searching it for metrics — and its map is shown here so the user
    # can verify the engine oriented correctly before extraction output appears.
    if batch_id:
        _ensure_workbook_oriented()
        _render_orientation_panel(batch_id)

    # Run the AAM extraction here (pre-confirm) so its progress bar lives in
    # this box — the middle column then renders from the cached records.
    if batch_id and not confirmed:
        _ensure_aam_extracted()

    st.markdown("**Progress**")
    steps = [
        ("File uploaded",        bool(files)),
        ("Facts verified",       confirmed),
        ("Snapshot generated",   bool(st.session_state.snapshot_md)),
        (f"Analyses ({len(st.session_state.outcome_findings)})",
                                 bool(st.session_state.outcome_findings)),
        ("Report complete",      bool(st.session_state.final_report_md)),
    ]
    for label, done in steps:
        st.markdown(f"{'✅' if done else '⬜'} {label}")
    st.divider()


def _render_process_actions(
    agent: AgentSession, batch_id, confirmed: bool,
    analysis_req: bool, gate_edited,
) -> None:
    """Left column, phase 2: the stage-advance buttons (+ ingest progress bar)."""
    if not batch_id:
        st.caption("Upload a workbook to begin.")
        return

    if not confirmed:
        if _brief_available(batch_id):
            st.caption("The verified Deal Brief above carries straight through — "
                       "running analysis makes it the record (no re-extraction).")
            if st.button("Run analysis →", type="primary", width="stretch"):
                _run_analysis_from_brief(agent, batch_id)
        else:
            st.caption("No Deal Brief (set OPENAI_API_KEY for the comprehension "
                       "read). Facts come from the detail checklist below.")
            if st.button("Run analysis →", type="primary", width="stretch"):
                verified = _collect_verified(st.session_state.aam_records, gate_edited) \
                    if gate_edited is not None else {}
                _confirm_aam_and_ingest(agent, verified)
        return

    # Post-confirm. Brief path advances straight to analysis (no second click);
    # legacy path keeps the explicit Snapshot step.
    if not analysis_req:
        if st.button("Run analysis (Snapshot)", type="primary", width="stretch"):
            st.session_state.aam_analysis_requested.add(batch_id)
            st.rerun()
        return

    st.caption("Pick a suggested analysis in the Chat panel, or ask a question.")


def _render_outcome_column(agent: AgentSession, batch_id, confirmed: bool, analysis_req: bool):
    """
    Middle column: the Outcome. Before confirm it's the editable AAM appendix
    (returned so the Proceed button can read it). After confirm it's the locked
    AAM summary + Snapshot + folded findings + final report. Whatever lands here
    is what becomes the SSOT when the report is generated.
    """
    st.markdown('<div class="da-col-title">Outcome</div>', unsafe_allow_html=True)

    if not batch_id:
        st.info("Upload an underwriting workbook (left) to begin.")
        return None

    # The Investment Read leads the Outcome: the synthesized IC-memo read off the
    # validated fact sheet (+ perf, when actuals are present). The detailed panels
    # (variance, deal analysis) follow as the supporting evidence it's grounded in.
    _render_investment_read(batch_id, _ensure_deal_truth(batch_id))

    # If actuals statement(s) were uploaded, show the plan-vs-actual variance detail.
    if st.session_state.actuals_filenames:
        _render_perf_vs_plan(batch_id)

    if not confirmed:
        _render_model_brief(batch_id)
        # The brief + fact check above is the product. The 22-field checklist is
        # demoted to an optional detail review — its body still executes (so the
        # no-brief Run-analysis path has records to confirm), it's just collapsed.
        with st.expander("Review the 22-field detail checklist (optional)", expanded=False):
            return _render_aam_appendix(batch_id)

    # Post-confirm. Brief path: the Deal Brief stays the hero — the same verified
    # read carries through, no swap to a separately-extracted AAM summary.
    brief_path = bool(
        st.session_state.finalized_brief_batch == batch_id
        and st.session_state.finalized_brief
    )
    if brief_path:
        mb = st.session_state.model_brief or {}
        # LEAD with the grounded analysis; the GPT narrative is tucked away.
        _render_grounded_analysis(batch_id)
        if st.session_state.trust_scored:
            _render_initial_view(batch_id, mb, st.session_state.trust_scored)
        with st.expander("📝 Narrative read (GPT comprehension)", expanded=False):
            fin = st.session_state.finalized_brief or {}
            st.markdown(fin.get("brief_md") or mb.get("brief_md", "_(empty)_"))
            if st.session_state.trust_scored:
                _render_trust_panel(st.session_state.trust_scored)
    else:
        _render_aam_summary()
        if analysis_req:
            _ensure_snapshot_generated(agent)
        if st.session_state.snapshot_md:
            with st.container(border=True):
                st.markdown("#### Snapshot")
                st.markdown(st.session_state.snapshot_md)

    for f in st.session_state.outcome_findings:
        with st.container(border=True):
            htxt, hbtn = st.columns([8, 1])
            htxt.markdown(f"#### {f['title']}")
            if hbtn.button("✕", key=f"rm_{f['id']}", help="Remove from Outcome"):
                st.session_state.outcome_findings = [
                    x for x in st.session_state.outcome_findings if x["id"] != f["id"]
                ]
                st.session_state.outcome_added.discard(f["id"])
                st.rerun()
            st.markdown(f["content"])

    if st.session_state.final_report_md:
        with st.container(border=True):
            st.markdown("#### 📄 Final Report — SSOT")
            st.markdown(st.session_state.final_report_md)

    return None


def _render_aam_summary() -> None:
    """Locked, verified AAM facts read back from the SSOT (post-confirm).
    Rendered as grouped bullet points — easier to read than a table."""
    import aam
    from metric_catalog import load_metric_catalog

    bm = ((ssot.load_ssot().get("layers", {}) or {})
          .get("underwriting", {}) or {}).get("bounded_metrics", {}) or {}
    name_by_id = {m["metric_id"]: m["metric_name"] for m in load_metric_catalog()}

    lines: list[str] = []
    current_group = None
    for mid in aam.AAM_METRIC_IDS:
        rec = bm.get(name_by_id.get(mid, ""))
        if not rec:
            continue
        display = rec.get("display_value")
        if display in (None, "", "—"):
            continue
        group = aam.group_of(mid) or ""
        if group != current_group:
            lines.append(f"\n**{group}**")
            current_group = group
        lines.append(f"- {rec.get('metric_name', mid)}: **{display}**")
    with st.container(border=True):
        st.markdown("#### AAM Summary — verified facts")
        if lines:
            st.markdown("\n".join(lines))
        else:
            st.caption("No verified AAM facts in the SSOT yet.")


def _ensure_snapshot_generated(agent: AgentSession) -> None:
    """Generate the Snapshot once (post-confirm) and store it for the Outcome."""
    if st.session_state.snapshot_md is not None:
        return
    batch_id = frozenset(st.session_state.uploaded_filenames)
    if batch_id in st.session_state.completed_batches:
        return
    with st.spinner("Generating Snapshot…"):
        readiness = tools.check_scenario_ready("deal_review")
        if not readiness.get("ready"):
            st.caption(readiness.get("reason", "Not enough data for the Snapshot yet."))
            st.session_state.completed_batches.add(batch_id)
            return
        result = tools.run_deal_review()
    st.session_state.completed_batches.add(batch_id)
    if "error" in result:
        st.caption(f"Couldn't generate the Snapshot: {result['error']}")
        return
    st.session_state.snapshot_md = result.get("narrative", "")


def _finding_sig(content: str) -> str:
    return str(abs(hash(content.strip())))[:10]


def _finding_title(content: str) -> str:
    """Pull a short title from a finding: first markdown heading, else first line."""
    for line in content.strip().splitlines():
        s = line.strip().lstrip("#").strip()
        if s:
            return s[:60]
    return "Finding"


def _fold_to_outcome(content: str) -> None:
    sig = _finding_sig(content)
    st.session_state.outcome_findings.append({
        "id": sig, "title": _finding_title(content), "content": content,
    })
    st.session_state.outcome_added.add(sig)


def _ensure_agent_grounded(agent: AgentSession, batch_id) -> None:
    """Seed the Deal Analyzer chat agent ONCE per confirmed batch with the deal
    it's looking at — the files, the verified SSOT facts, and how to answer.

    Without this the agent only has its system prompt: it doesn't know a model
    is loaded, so a question like "what is exit price?" gets a generic textbook
    definition instead of THIS deal's number. (The legacy scenarios get this via
    _seed_agent_history; deal_review returns before that runs, so it needs its
    own seed.) Injected as a `system` message so it grounds the model WITHOUT
    showing up as a chat bubble — display_messages() skips system + tool roles."""
    if st.session_state.get("agent_grounded_batch") == batch_id:
        return
    files = sorted(st.session_state.uploaded_filenames)
    if not files:
        return

    # The verified facts the Outcome is built from (same source as the AAM
    # summary / report) — so the agent can answer most questions directly and
    # knows whether a metric like exit price is even in the model.
    s = ssot.load_ssot()
    uw = ((s.get("layers", {}) or {}).get("underwriting", {}) or {})
    bm = uw.get("bounded_metrics", {}) or {}
    fact_lines: list[str] = []
    for name, rec in bm.items():
        disp = rec.get("display_value")
        if disp in (None, "", "—"):
            continue
        sheet, cell = rec.get("source_sheet"), rec.get("source_cell")
        src = f" ({sheet}!{cell})" if sheet and cell else (f" ({sheet})" if sheet else "")
        flag = "" if rec.get("status") == "verified" else " [unverified]"
        fact_lines.append(f"- {rec.get('metric_name', name)}: {disp}{src}{flag}")
    facts_block = "\n".join(fact_lines) if fact_lines else "(no metrics resolved yet)"

    # The validated FACT SHEET — the SAME object the Investment Read runs off (reuse
    # the cached one). It carries the full-$ trajectory, foot-validated components,
    # archetype, computed claims, and the FILTERED guardrails (the broken noi/rate
    # conflict rails removed). This is the authoritative grounding; the bounded_metrics
    # above are kept only as lower-confidence, cell-derived supplements it overrides.
    dt = _ensure_deal_truth(batch_id)
    fs_block = ""
    fs = (st.session_state.get("investment_read") or {}).get("fact_sheet")
    if not fs and dt and not dt.get("error"):
        try:
            from interpretation import assemble_fact_sheet
            perf = _ensure_perf_vs_plan(batch_id) if st.session_state.actuals_filenames else None
            perf = perf if (perf and perf.get("ok")) else None
            fs = assemble_fact_sheet(UPLOAD_DIR / files[0], dt=dt, perf=perf)
        except Exception:
            fs = None
    if fs and fs.get("ok"):
        from interpretation import render_fact_sheet
        fs_block = render_fact_sheet(fs)

    context = (
        "DEAL CONTEXT — the deal under review is already loaded; do NOT call "
        "ingest_to_ssot or run_deal_review again, that work is done.\n"
        f"Uploaded & ingested file(s): {', '.join(files)}.\n\n"
        "═══ VALIDATED FACT SHEET — authoritative. Every number here is correct "
        "(full dollars, conflict-resolved); PREFER it over any cell you might read. "
        "It already contains the BINDING GUARDRAILS (obey every one, never contradict) "
        "and the computed CLAIMS (explain them; do not flip an attribution):\n"
        f"{fs_block}\n\n"
        "Additional extracted metrics (cell-derived, LOWER confidence — the fact sheet "
        f"OVERRIDES these where they differ; verify before relying on them):\n{facts_block}\n\n"
        "Answering rules: when the user names a metric or concept (e.g. \"what is "
        "exit price?\", \"cap rate?\", \"DSCR?\", \"exit cap?\") they mean THIS "
        "deal — answer with the value from the facts above, or read the model with "
        "get_layer_details / read_sheet / search_file. NEVER reply with a generic "
        "textbook definition unless the user explicitly asks what a term means. If "
        "a value genuinely isn't in the model after you've looked, say so plainly "
        "and name where you looked.\n"
        "NOI / revenue / opex: ALWAYS use the validated trajectory in the FACT SHEET "
        "above (full dollars). Do NOT read raw NOI cells with read_sheet/search_file — "
        "the model holds many conflicting NOI cells, usually in $000s, and they are "
        "wrong/unscaled. For an NOI bridge or annual-NOI question, use the going-in→"
        "stabilized→exit values and the annual-by-year series given above. For a "
        "'what's driving opex' question, use the FOOT-VALIDATED opex components.\n"
        "Return-impact math (a capex overrun, extra spend, or more equity → effect on "
        "IRR / equity multiple): call run_what_if_capex to compute it deterministically. "
        "Do NOT estimate, describe it qualitatively, or say you'd need to re-run the "
        "model — the tool does the XIRR math on the validated streams and returns both "
        "levered and unlevered old-vs-new figures. Report those numbers."
    )
    agent.messages.append({"role": "system", "content": context})
    st.session_state.agent_grounded_batch = batch_id


def _render_chat_column(agent: AgentSession) -> None:
    """Right column: chat transcript with Add-to-Outcome, suggested-analysis
    chips, the chat form, and the Generate Report action."""
    st.markdown('<div class="da-col-title">Chat</div>', unsafe_allow_html=True)

    msgs = list(agent.display_messages())
    with st.container(height=420):
        if not msgs:
            st.caption("Ask a question or run an analysis — results appear here.")
        for i, m in enumerate(msgs):
            with st.chat_message(m["role"]):
                st.markdown(m["content"])
                if m["role"] == "assistant":
                    sig = _finding_sig(m["content"])
                    if sig in st.session_state.outcome_added:
                        st.caption("✓ in Outcome")
                    elif st.button("Add to Outcome", key=f"add_{i}_{sig}"):
                        _fold_to_outcome(m["content"])
                        st.rerun()

    # Prompt box: the bordered card holds the chat input. The per-topic analyses
    # (capital structure / cash flow-NOI / capex / returns) are no longer separate
    # buttons here — they render as ONE grounded integrated analysis in the Outcome
    # (_render_integrated_analysis).
    with st.container(border=True, key="chat_box"):
        with st.form("da_chat", clear_on_submit=True, border=False):
            txt = st.text_area(
                "Ask", height=90, label_visibility="collapsed",
                placeholder="Ask a follow-up question…",
            )
            sent = st.form_submit_button("Send", type="primary", width="stretch")
    if sent and txt.strip():
        try:
            with st.spinner("Thinking…"):
                agent.send(txt.strip())
        except Exception as e:
            st.caption(f"Chat failed: {e}")
        st.rerun()

    st.divider()
    can_report = bool(
        st.session_state.finalized_brief or st.session_state.snapshot_md
        or st.session_state.outcome_findings
    )
    if st.button("Generate Report → SSOT", width="stretch", disabled=not can_report,
                 help="Memorialize the Outcome into a report and save it as the SSOT."):
        _generate_report()
        st.rerun()


def _generate_report() -> None:
    """Memorialize the Outcome (verified facts + snapshot + findings) into one
    report via GPT (deterministic concat fallback) and persist it as the SSOT."""
    import aam
    from metric_catalog import load_metric_catalog

    bm = ((ssot.load_ssot().get("layers", {}) or {})
          .get("underwriting", {}) or {}).get("bounded_metrics", {}) or {}
    name_by_id = {m["metric_id"]: m["metric_name"] for m in load_metric_catalog()}

    parts: list[str] = []
    # Lead with the comprehension brief (the deal read) when present.
    fin = st.session_state.get("finalized_brief") or {}
    brief_md = fin.get("brief_md") or (st.session_state.get("model_brief") or {}).get("brief_md")
    if brief_md:
        parts.append("## Deal Brief\n" + brief_md)
    # Carry the Layer 3 reasoning (Initial View) into the report, right after the
    # read. Prefer the live session view, fall back to the persisted SSOT copy.
    iv = (st.session_state.get("investment_view") or {}).get("view_md")
    if not iv:
        iv = (((ssot.load_ssot().get("layers", {}) or {})
               .get("underwriting", {}) or {}).get("initial_view"))
    if iv and iv.strip():
        iv = iv.replace("### Initial View", "", 1).strip()
        parts.append("## Initial View\n" + iv)
    fact_lines = []
    for mid in aam.AAM_METRIC_IDS:
        rec = bm.get(name_by_id.get(mid, ""))
        if rec and (rec.get("display_value") not in (None, "—")):
            fact_lines.append(f"- {rec.get('metric_name', mid)}: {rec.get('display_value')}")
    if fact_lines:
        parts.append("## Verified Facts (AAM)\n" + "\n".join(fact_lines))
    if st.session_state.snapshot_md:
        parts.append("## Snapshot\n" + st.session_state.snapshot_md)
    for f in st.session_state.outcome_findings:
        parts.append(f"## {f['title']}\n{f['content']}")
    raw = "\n\n".join(parts)

    report = raw
    try:
        from scenarios._llm import client, MODEL, llm_available
        if llm_available() and raw.strip():
            with st.spinner("Memorializing the Outcome into a report…"):
                resp = client.chat.completions.create(
                    model=MODEL, temperature=0.2,
                    messages=[
                        {"role": "system", "content": _REPORT_SYSTEM},
                        {"role": "user",   "content": raw},
                    ],
                )
                report = resp.choices[0].message.content.strip()
    except Exception as e:
        st.caption(f"(Report polish skipped, using raw Outcome: {e})")

    st.session_state.final_report_md = report
    # Whatever ends up in the Outcome becomes the SSOT — persist the report.
    try:
        s = ssot.load_ssot()
        lyr = s.setdefault("layers", {}).setdefault("underwriting", {})
        lyr["deal_report"] = report
        ssot.save_ssot(s)
        st.toast("Report generated and saved to the SSOT.")
    except Exception as e:
        st.caption(f"(Saved to view; SSOT write skipped: {e})")


def _handle_chat_input(agent: AgentSession, user_input: str | None) -> None:
    if not user_input:
        return
    with st.chat_message("user"):
        st.markdown(user_input)
    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            reply = agent.send(user_input)
        st.markdown(reply)
    _render_tool_trace(agent)


_SCENARIO_DEFAULT_LAYER: dict[str, str] = {
    # When a file can't be classified by name, fall back to the layer that
    # makes most sense for the active scenario.
    "deal_review":  "underwriting",
    "perf_vs_plan": "actuals_recent",
}


def _ingest_new_files(new_files: list) -> None:
    """
    Phase 1: ingest each newly-uploaded file.

    Classification strategy (in order):
      1. Auto-classify from filename (proforma, financial statement, etc.)
      2. Scenario-aware fallback — if filename gives no signal, use the layer
         that matches the active scenario (deal_review → underwriting, etc.)
      3. Only ask the user to manually classify if the scenario itself is unclear.

    Files that still can't be resolved are stashed in pending_overrides.
    """
    pseudo_user_msg = "Uploaded: " + ", ".join(sorted(f.name for f in new_files))
    with st.chat_message("user"):
        st.markdown(pseudo_user_msg)

    failed_to_classify: dict[str, str] = {}
    scenario_key = st.session_state.active_scenario
    scenario_fallback = _SCENARIO_DEFAULT_LAYER.get(scenario_key)

    with st.chat_message("assistant"):
        with st.status("Ingesting files...", expanded=True) as status:
            for uf in new_files:
                status.update(label=f"Ingesting {uf.name}...")
                result = tools.ingest_to_ssot(uf.name)

                if result.get("needs_manual_classification") and scenario_fallback:
                    # Filename gave no signal — use the scenario context as the layer.
                    result = tools.ingest_to_ssot_with_layer(uf.name, scenario_fallback)
                    if "error" not in result:
                        st.markdown(
                            f"✅ **{uf.name}** → `{scenario_fallback}` "
                            f"(auto-assigned from scenario — "
                            f"{result['metric_count']} metrics extracted)"
                        )
                    else:
                        failed_to_classify[uf.name] = result.get("error", "")
                        st.markdown(f"❌ **{uf.name}** — {result['error']}")

                elif result.get("needs_manual_classification"):
                    failed_to_classify[uf.name] = result.get("error", "")
                    st.markdown(f"⚠️ **{uf.name}** — needs manual classification")

                elif "error" in result:
                    st.markdown(f"❌ **{uf.name}** — {result['error']}")

                else:
                    insight_status = result.get("insight_pass", "?")
                    insight_emoji = "🤖" if "completed" in insight_status else "⚠️"
                    st.markdown(
                        f"✅ **{uf.name}** → `{result['layer']}` "
                        f"({result['metric_count']} metrics extracted) "
                        f"· {insight_emoji} Pass 2: {insight_status}"
                    )

            if failed_to_classify:
                status.update(
                    label=f"{len(failed_to_classify)} file(s) need manual classification",
                    state="error",
                )
            else:
                status.update(label="Ingest complete", state="complete")

    if failed_to_classify:
        st.session_state.pending_overrides = failed_to_classify


# =============================================================================
# Stage-1 Audit Appendix verification gate (deal_review)
# =============================================================================

# Units whose values are numeric (editable as raw numbers; the Display column
# carries the human-readable form). Everything else (date/text) edits as a string.
_NUMERIC_UNITS = {"USD", "ratio", "percent", "months", "years", "count", "sf"}


def _find_rec_by_id(records: dict, metric_id: str) -> dict | None:
    for rec in records.values():
        if rec.get("metric_id") == metric_id:
            return rec
    return None


def _aam_source(rec: dict) -> str:
    sheet, cell = rec.get("source_sheet"), rec.get("source_cell")
    if sheet and cell:
        return f"{sheet}!{cell}"
    if cell:
        # Derived metrics carry their identity formula here (no sheet), e.g.
        # "Purchase Price × Going-in Cap Rate" — show it, not "—".
        return str(cell)
    return "—"


def _aam_editable_value(rec: dict) -> str:
    """
    Seed for the single editable Value column: the FORMATTED display the human
    reads (e.g. "$192.00M", "8.17%", "2.00x"), or '' when missing/blank so the
    cell shows empty and invites a fill. (The separate raw Value column was
    removed — Display is what the analyst verifies.)
    """
    if rec.get("status") == "missing" or rec.get("normalized_value") is None:
        return ""
    return rec.get("display_value") or "—"


def _coerce_value(rec: dict, s: str):
    """
    Turn an edited string into a stored value.

    The editable column now shows the FORMATTED value, so the parser must invert
    the display: "8.17%" -> 0.0817, "$192.00M" -> 192000000, "2.00x" -> 2.0,
    "1.0 years" -> 1.0. parse_numeric_value (the same routine used for GPT output)
    handles all of these. Returns (value, ok); for numeric metrics that fail to
    parse we keep the original normalized value (ok=False) so a typo can never
    feed a string into downstream arithmetic.
    """
    from metric_resolver import parse_numeric_value
    unit = (rec or {}).get("unit")
    value, ok = parse_numeric_value(s, unit)
    if unit in _NUMERIC_UNITS and not ok:
        return (rec or {}).get("normalized_value"), False
    return value, ok


def _collect_verified(records: dict, edited) -> dict:
    """Build the verified-overrides dict from the edited appendix table."""
    verified: dict[str, dict] = {}
    for _, row in edited.iterrows():
        name = row["Metric"]
        rec = records.get(name) or {}
        val_str = str(row["Value"] or "").strip()
        if val_str in ("", "—"):
            continue  # left blank → stays missing, not asserted
        value, ok = _coerce_value(rec, val_str)
        # Change detection. The column shows the rounded display (e.g. "8.17%"),
        # so coercing it back won't EXACTLY equal the stored normalized value
        # (0.081650…). Treat unchanged if either (a) the text matches the shown
        # display verbatim — covers an untouched cell incl. dates/text — or
        # (b) the parsed number agrees within tolerance (covers a re-typed
        # equivalent like "192000000" for "$192.00M"). Otherwise it's a real edit.
        from metric_resolver import _values_disagree
        shown = rec.get("display_value") or ""
        unchanged = (
            val_str == shown
            or (ok and not _values_disagree(value, rec.get("normalized_value")))
        )
        if unchanged:
            # Keep the FULL-PRECISION original — the column only showed a rounded
            # display, so the re-parsed number would lose precision (0.0817 vs
            # 0.081650…). Verifying must not silently truncate the stored value.
            note, display, value = (
                "Human-verified via audit appendix.",
                rec.get("display_value") or val_str,
                rec.get("normalized_value"),
            )
        elif not ok:
            note = (f"Human entered '{val_str}' but it could not be parsed as a "
                    f"number; kept original {rec.get('normalized_value')}.")
            display = rec.get("display_value") or val_str
        else:
            note = f"Human-corrected via audit appendix (was {rec.get('normalized_value')})."
            display = val_str
        verified[name] = {
            "value":        value,
            "display":      display,
            "source_sheet": rec.get("source_sheet"),
            "source_cell":  rec.get("source_cell"),
            "note":         note,
            "metric_id":    rec.get("metric_id"),
            "corrected":    (not unchanged) and ok,   # real human correction
            "engine_value": rec.get("display_value") or rec.get("normalized_value"),
        }
    return verified


_NOI_RULE_IDS = {"net_operating_income_noi", "exit_noi"}


def _rederive_noi_from_verified(records: dict, verified: dict) -> None:
    """
    Re-derive NOI from the HUMAN-VERIFIED pricing inputs at confirm time.

    NOI is derived from pricing at extraction, but the human may correct the
    price / cap / exit value at the gate. This overlays the verified values onto
    a copy of the AAM records, re-runs the pricing derivation, and folds the
    refreshed NOI back into `verified` so the persisted SSOT reflects the
    corrected inputs. A NOI the human edited directly is left as their value.

    Mutates `verified` in place.
    """
    import copy
    from aam_extractor import _derive_noi_from_pricing, _by_id

    overlay = copy.deepcopy(records)
    # Apply each verified value onto the overlay (human confirmation → verified).
    for name, v in verified.items():
        rec = overlay.get(name)
        if rec is None:
            continue
        rec["normalized_value"] = v.get("value")
        rec["status"] = "verified"

    # Respect a NOI the human explicitly corrected — don't re-derive over it.
    skip = {
        v.get("metric_id")
        for v in verified.values()
        if v.get("corrected") and v.get("metric_id") in _NOI_RULE_IDS
    }
    _derive_noi_from_pricing(overlay, skip_ids=skip)

    # Fold the refreshed NOI values back into `verified`.
    for noi_id in _NOI_RULE_IDS - skip:
        rec = _by_id(overlay, noi_id)
        if not rec or rec.get("status") != "derived":
            continue
        verified[rec["metric_name"]] = {
            "value":        rec.get("normalized_value"),
            "display":      rec.get("display_value"),
            "source_sheet": None,
            "source_cell":  rec.get("source_cell"),  # the identity formula
            "note":         (rec.get("validation_notes") or
                             ["Derived from verified pricing."])[0],
            "metric_id":    noi_id,
            "corrected":    False,
            "engine_value": rec.get("display_value"),
        }


def _ensure_workbook_oriented() -> dict | None:
    """
    Run Workbook Orientation once per batch, BEFORE AAM extraction. Lightweight,
    deterministic, read-only — it classifies each sheet's role (summary / inputs
    / returns / model / support) from content and structure so extraction knows
    where to look first. Non-destructive: on failure the orientation is stored
    with an error and extraction falls back to today's behavior.
    """
    batch_id = frozenset(st.session_state.uploaded_filenames)
    if st.session_state.wb_orientation_batch == batch_id and st.session_state.wb_orientation:
        return st.session_state.wb_orientation
    primary = sorted(st.session_state.uploaded_filenames)[0]
    note = st.empty()
    note.caption(f"Orienting: reading {primary}'s structure before extraction…")
    from workbook_orientation import orient_workbook
    try:
        st.session_state.wb_orientation = orient_workbook(UPLOAD_DIR / primary)
    except Exception as e:
        st.session_state.wb_orientation = {"error": f"{type(e).__name__}: {e}"}
    st.session_state.wb_orientation_batch = batch_id
    note.empty()
    return st.session_state.wb_orientation


def _orientation_tier_map_for(batch_id) -> dict | None:
    """The orientation tier map for this batch, or None (→ legacy behavior)."""
    if st.session_state.wb_orientation_batch != batch_id:
        return None
    from workbook_orientation import orientation_tier_map
    return orientation_tier_map(st.session_state.wb_orientation)


_ROLE_LABELS = [
    ("summary", "Summary"), ("inputs", "Inputs"), ("returns", "Returns"),
    ("model", "Financial Model"), ("support", "Support"), ("other", "Other"),
]


def _render_orientation_panel(batch_id) -> None:
    """Visible Workbook Orientation section (debug): the workbook map, per-sheet
    roles, confidences, and the signals behind them — shown before extraction
    output so the user can verify the engine understood the workbook."""
    ori = st.session_state.wb_orientation
    if not ori or st.session_state.wb_orientation_batch != batch_id:
        return
    if ori.get("error"):
        with st.expander("🧭 Workbook Orientation — failed", expanded=False):
            st.caption(f"{ori['error']} — extraction will use name-based sheet tiers.")
        return

    sheets = ori.get("sheets", {})
    rmap = ori.get("map", {})
    n_mapped = sum(len(v) for k, v in rmap.items() if k != "other")
    with st.expander(f"🧭 Workbook Orientation — {n_mapped}/{len(sheets)} sheets mapped",
                     expanded=False):
        st.caption(
            "Sheet roles read from content & structure (names are only a weak "
            "signal). Extraction searches Summary → Inputs → Returns first."
        )
        for role_key, label in _ROLE_LABELS:
            names = rmap.get(role_key) or []
            if not names:
                continue
            st.markdown(
                f"**{label}:** " + " · ".join(
                    f"{n} ({sheets[n]['confidence']:.2f})" for n in names
                )
            )
        st.dataframe(
            [
                {
                    "sheet": name,
                    "role": info["role"],
                    "confidence": info["confidence"],
                    "keywords": ", ".join(
                        f"{k}:{v}" for k, v in info["signals"]["kw"].items() if v
                    ) or "—",
                    "hardcode %": int(info["signals"]["hardcode_ratio"] * 100),
                    "pulls": info["signals"]["pulls_from_other_sheets"],
                    "referenced by": info["signals"]["referenced_by_other_sheets"],
                    "ts rows": info["signals"]["time_series_rows"],
                }
                for name, info in sheets.items()
            ],
            width="stretch",
            hide_index=True,
        )


def _ensure_aam_extracted() -> dict:
    """Extract the AAM once per batch (deterministic, fast, free) and return the
    records dict. GPT fires only on the explicit blank-fill action. Shows a
    progress bar + description in the column it's called from (the left box)."""
    batch_id = frozenset(st.session_state.uploaded_filenames)
    if st.session_state.aam_batch_id != batch_id or not st.session_state.aam_records:
        primary = sorted(st.session_state.uploaded_filenames)[0]
        bar = st.progress(5, text="Reading workbook…")
        note = st.empty()
        note.caption(f"Mapping sheets and extracting audit-appendix metrics from {primary}.")
        from aam_extractor import extract_aam
        # The Workbook Orientation map (computed before this) drives sheet
        # priority: Summary → Inputs → Returns first. None → legacy tiers.
        st.session_state.aam_records = extract_aam(
            UPLOAD_DIR / primary,
            use_gpt_gap_fill=False,
            sheet_tier_map=_orientation_tier_map_for(batch_id),
        )
        bar.progress(70, text="Tagging table periodicity…")
        note.caption("Detecting model tables so each metric inherits its table's periodicity.")
        # Tag flow metrics with their table's periodicity (cached parse).
        try:
            from financial_model_parser import parse_workbook_tables_cached, tag_metric_periodicity
            tag_metric_periodicity(
                parse_workbook_tables_cached(UPLOAD_DIR / primary),
                st.session_state.aam_records,
            )
        except Exception:
            pass
        bar.progress(100, text="Audit appendix ready")
        note.caption("Review and correct the facts in the Outcome panel.")
        st.session_state.aam_batch_id = batch_id
        st.session_state.aam_fill_version = 0
    return st.session_state.aam_records


def _ensure_model_brief(batch_id) -> dict:
    """Build the comprehension-first Model Brief once per batch (cached on disk
    by file hash). Returns the brief dict (may carry an 'error')."""
    if st.session_state.model_brief_batch == batch_id and st.session_state.model_brief:
        return st.session_state.model_brief
    primary = sorted(st.session_state.uploaded_filenames)[0]
    bar = st.progress(15, text="Reading the deal (comprehension pass)…")
    note = st.empty()
    note.caption(f"Reading {primary}'s authoritative tabs the way an analyst does.")
    from model_brief import build_model_brief
    try:
        brief = build_model_brief(UPLOAD_DIR / primary)
    except Exception as e:
        brief = {"error": f"{type(e).__name__}: {e}"}
    bar.progress(100, text="Deal brief ready")
    note.empty()
    st.session_state.model_brief = brief
    st.session_state.model_brief_batch = batch_id
    return brief


def _render_model_brief(batch_id) -> None:
    """LEAD with the grounded analysis (deterministic deal_truth + roll-up — the
    accurate, fast view). The GPT comprehension read runs in the background and is
    tucked into a collapsed expander; the analysis above is authoritative."""
    # 1. Grounded analysis first — no GPT extraction needed.
    dt = _ensure_deal_truth(batch_id)
    engine_ok = _render_grounded_analysis(batch_id, dt)

    from scenarios._llm import llm_available
    if not llm_available():
        return
    if not engine_ok:
        return                            # engine-not-found notice already shown

    # 2. GPT comprehension read + Layer 3 reasoning — secondary, below.
    brief = _ensure_model_brief(batch_id)
    if brief.get("error"):
        return
    scored = _ensure_trust_scored(batch_id, brief)
    if scored is not None:
        _ensure_finalized_brief(batch_id, brief, scored)
        _render_initial_view(batch_id, brief, scored)   # Layer 3
    with st.expander("📝 Narrative read (GPT comprehension) — the analysis above "
                     "is the authoritative, grounded version", expanded=False):
        st.caption("Read from " + ", ".join(brief.get("sheets_read", []) or ["—"]))
        fin = st.session_state.get("finalized_brief") or {}
        st.markdown(fin.get("brief_md") or brief.get("brief_md") or "_(empty)_")
        if scored is not None:
            _render_trust_panel(scored)


def _render_nonnegotiables_md(dt: dict) -> None:
    """The required deal facts — acquisition cost / going-in cap, total cost,
    LTV·debt·equity, hold period, sales price, exit cap, and returns (levered +
    unlevered IRR & EM). Deterministic and ALWAYS complete: every item shows a
    value (✅ when cash-flow-validated) or an explicit 'not found'."""
    if not dt.get("engine_found", True):
        st.warning("⚠ **Cash-flow engine not found.** No cash-flow stream "
                   "reproduced the model's stated IRR, so the deal could **not** be "
                   "reconstructed — nothing below is validated. "
                   + (dt.get("reason") or ""))
        return
    lines = ["**Non-negotiables** — reconstructed & validated from the cash flow"]
    for b in dt.get("brief_facts", []):
        if not b.get("found"):
            lines.append(f"- {b['label']}: _not found_")
            continue
        cf = " ✅" if b.get("cf_validated") else ""
        note = f" — _{b['note']}_" if b.get("note") else ""
        lines.append(f"- **{b['label']}:** {b['display']}{cf} "
                     f"`{b.get('source','')}`{note}")
    st.markdown("\n".join(lines))


def _ensure_deal_truth(batch_id) -> dict | None:
    """Reconstruct the canonical deal from the cash flow (deal_truth.py) once per
    batch. Deterministic, no GPT. Cached in session — this is the validated fact
    spine Layer 3 and the chat read from."""
    if (st.session_state.get("deal_truth_batch") == batch_id
            and st.session_state.get("deal_truth")):
        return st.session_state.deal_truth
    files = sorted(st.session_state.uploaded_filenames)
    if not files:
        return None
    from deal_truth import build_deal_truth
    with st.spinner("Reconstructing the deal from the cash flow (validate returns · reconcile sources)…"):
        try:
            dt = build_deal_truth(UPLOAD_DIR / files[0])
        except Exception as e:
            dt = {"error": f"{type(e).__name__}: {e}"}
    st.session_state.deal_truth = dt
    st.session_state.deal_truth_batch = batch_id
    return dt


def _ensure_perf_vs_plan(batch_id):
    """Compute the plan-vs-actual variance once per (model batch + actuals upload).
    Deterministic — no GPT, no API key. The model is the workbook already loaded in
    the Deal Analyzer; the statement(s) come from the separate actuals uploader."""
    files = sorted(st.session_state.uploaded_filenames)
    actuals = sorted(st.session_state.actuals_filenames)
    if not files or not actuals:
        return None
    key = (frozenset(files), frozenset(actuals))
    if (st.session_state.get("perf_vs_plan_key") == key
            and st.session_state.get("perf_vs_plan")):
        return st.session_state.perf_vs_plan
    from perf_vs_plan_engine import build_perf_vs_plan
    with st.spinner("Reconciling actuals · matching the NOI definition · computing variance…"):
        try:
            res = build_perf_vs_plan(UPLOAD_DIR / files[0],
                                     [UPLOAD_DIR / a for a in actuals])
        except Exception as e:
            res = {"ok": False, "blocked": "error", "reason": f"{type(e).__name__}: {e}"}
    st.session_state.perf_vs_plan = res
    st.session_state.perf_vs_plan_key = key
    return res


def _render_perf_vs_plan(batch_id) -> None:
    """Outcome add-on: the plan-vs-actual NOI variance. Renders the reconciliation
    banner + variance when trustworthy, or the explicit block reason + the specific
    footing failures when the statement can't be trusted (the refusal IS the product
    — never a silent comparison)."""
    res = _ensure_perf_vs_plan(batch_id)
    if res is None:
        return
    with st.container(border=True):
        if res.get("ok"):
            st.markdown(res["md"])
        else:
            st.markdown("## How are we tracking? — plan vs actual")
            st.warning(f"**Comparison withheld ({res.get('blocked', 'blocked')}).** "
                       f"{res.get('reason', '')}")
            for f in (res.get("validation") or {}).get("failures", [])[:6]:
                st.markdown(f"- ✗ {f.get('identity')} [{f.get('period')}]: {f.get('detail')}")


def _ensure_investment_read(batch_id, dt):
    """The Investment Read (interpretation layer) — once per (model + actuals). GPT
    narrates the validated fact sheet (+ perf, in performance mode) under binding
    guardrails; deterministic fallback when no API key. Reuses the cached deal
    analysis + perf so it never recomputes them."""
    files = sorted(st.session_state.uploaded_filenames)
    if not files or not dt or dt.get("error") or not dt.get("engine_found", True):
        return None
    actuals = sorted(st.session_state.actuals_filenames)
    key = (frozenset(files), frozenset(actuals))
    if (st.session_state.get("investment_read_key") == key
            and st.session_state.get("investment_read")):
        return st.session_state.investment_read
    analysis = (st.session_state.get("deal_analysis")
                if st.session_state.get("deal_analysis_batch") == batch_id else None)
    perf = _ensure_perf_vs_plan(batch_id) if actuals else None
    perf = perf if (perf and perf.get("ok")) else None
    from interpretation import build_investment_read
    with st.spinner("Writing the investment read…"):
        try:
            res = build_investment_read(UPLOAD_DIR / files[0], dt=dt,
                                        analysis=analysis, perf=perf)
        except Exception as e:                             # pragma: no cover - defensive
            res = {"ok": False, "reason": f"{type(e).__name__}: {e}"}
    st.session_state.investment_read = res
    st.session_state.investment_read_key = key
    return res


def _render_investment_read(batch_id, dt) -> None:
    """LEADS the Outcome: the IC-memo-style read synthesized from the validated fact
    sheet. The Deal Analysis panel below is the supporting detail."""
    res = _ensure_investment_read(batch_id, dt)
    if not res:
        return
    with st.container(border=True):
        if res.get("ok"):
            st.markdown(res["md"])
        else:
            st.caption(f"Investment read unavailable: {res.get('reason', '')}")


def _ensure_investment_view(batch_id, brief: dict, scored: dict) -> dict:
    """Layer 3 — compute the deal analytics and write the Initial View once per
    batch (cached in session). Reasons over the canonical deal-truth facts
    (validated against the cash flow) when available, and is BOUND by the
    deal-truth guardrails; falls back to the brief's verified facts otherwise."""
    if (st.session_state.get("investment_view_batch") == batch_id
            and st.session_state.get("investment_view")):
        return st.session_state.investment_view
    from investment_intel import build_investment_view

    facts = list(scored.get("facts") or [])
    guardrails = None
    dt = _ensure_deal_truth(batch_id)
    if dt and not dt.get("error"):
        from deal_truth import to_intel_facts, guardrail_lines
        # canonical (validated) facts FIRST: Layer 3 takes the first fact per
        # concept, so they win over the thinner brief facts.
        facts = to_intel_facts(dt) + facts
        guardrails = guardrail_lines(dt)
    try:
        view = build_investment_view({"facts": facts}, brief.get("identity"),
                                     guardrails=guardrails)
    except Exception as e:
        view = {"view_md": "", "analytics": {}, "llm": False, "error": str(e)}
    st.session_state.investment_view = view
    st.session_state.investment_view_batch = batch_id
    return view


_DT_LABELS = {
    "total_cost": "Total cost", "purchase_price": "Purchase price", "debt": "Debt",
    "equity": "Equity", "ltc": "LTC", "ltv": "LTV", "exit_cap": "Exit cap",
    "exit_value": "Exit value", "noi": "Stabilized NOI", "yield_on_cost": "Yield on cost",
    "levered_irr": "Levered IRR", "unlevered_irr": "Unlevered IRR",
    "equity_multiple": "Equity multiple", "debt_service": "Debt service",
    "interest_rate": "Interest rate", "dscr": "DSCR", "debt_yield": "Debt yield",
    "revenue": "Revenue",
}
_DT_RATE = {"exit_cap", "ltc", "ltv", "levered_irr", "unlevered_irr", "yield_on_cost",
            "interest_rate", "debt_yield"}
_DT_MULT = {"equity_multiple", "dscr"}


def _fmt_canon(concept: str, v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    if concept in _DT_RATE:
        return f"{v * 100:.2f}%" if abs(v) <= 1.5 else f"{v:.2f}%"
    if concept in _DT_MULT:
        return f"{v:.2f}x"
    return f"${v:,.0f}"


def _render_deal_truth_panel(dt: dict) -> None:
    """Validation detail behind the brief's non-negotiables: deal type + engine,
    the NOI trajectory provenance, the identity checks that confirm the deal hangs
    together, and the guardrails that bind the narrative. (The non-negotiable
    facts themselves are rendered in the Deal Brief above.)"""
    with st.container(border=True):
        st.markdown("#### Deal Truth — validation detail")
        if not dt.get("engine_found", True):
            st.caption("No validated cash-flow engine — see the notice above.")
            return
        st.caption(f"Deal type: **{dt.get('deal_type','?')}** · cash-flow engine: "
                   f"`{dt.get('cashflow_engine') or '—'}`")

        op = (dt.get("operating_series") or {}).get("noi") or {}
        if op.get("provenance"):
            gi, tm = op.get("going_in"), op.get("terminal")
            if isinstance(gi, (int, float)) and isinstance(tm, (int, float)):
                st.caption(f"NOI trajectory: ${gi:,.0f} → ${tm:,.0f} "
                           f"(source: {op['provenance']})")

        ids = dt.get("identities", [])
        checked = [i for i in ids if i.get("checked")]
        if checked:
            marks = " · ".join(f"{'✓' if i['passed'] else '✗'} {i['name']}" for i in checked)
            st.caption("Identity checks: " + marks)

        grs = dt.get("guardrails", [])
        if grs:
            with st.expander(f"⚖ Guardrails ({len(grs)}) — what the write-up may NOT claim"):
                for g in grs:
                    st.markdown(f"- {g['message']}")


def _render_integrated_analysis(batch_id, dt: dict) -> None:
    """One integrated analysis — Capital Structure · Return Profile · Cash Flow /
    NOI · CapEx — grounded in the validated spine + the full-read roll-up, in
    place of the four separate GPT deep-dives. Deterministic, so it always loads.
    Cached per batch."""
    if not dt.get("engine_found", True):
        return
    cache = st.session_state.get("deal_analysis")
    if not (st.session_state.get("deal_analysis_batch") == batch_id and cache):
        files = sorted(st.session_state.uploaded_filenames)
        if not files:
            return
        from deal_analysis import build_analysis
        try:
            with st.spinner("Building the integrated analysis…"):
                cache = build_analysis(UPLOAD_DIR / files[0], dt=dt)
        except Exception as e:
            st.caption(f"Integrated analysis unavailable: {e}")
            return
        st.session_state.deal_analysis = cache
        st.session_state.deal_analysis_batch = batch_id
    if cache.get("md"):
        with st.container(border=True):
            st.markdown(cache["md"])


def _render_grounded_analysis(batch_id, dt=None) -> bool:
    """LEAD with the deterministic, grounded analysis — the non-negotiables, the
    Deal Truth panel, and the integrated analysis (Capital Structure / Returns /
    Cash Flow-NOI / CapEx / Summary cross-check). No GPT extraction, so it's the
    accurate, fast view. Returns whether a cash-flow engine was found."""
    dt = dt if dt is not None else _ensure_deal_truth(batch_id)
    if not dt or dt.get("error"):
        return False
    with st.container(border=True):
        st.markdown("#### Deal Analysis")
        if dt.get("brief_facts"):
            _render_nonnegotiables_md(dt)
    _render_deal_truth_panel(dt)
    _render_integrated_analysis(batch_id, dt)
    return dt.get("engine_found", True)


def _render_initial_view(batch_id, brief: dict, scored: dict) -> None:
    """Layer 3 — what the validated facts MEAN (return composition, value
    creation, leverage, what breaks it), reasoned over the deal-truth facts and
    bound by its guardrails."""
    view = _ensure_investment_view(batch_id, brief, scored)
    md = (view or {}).get("view_md")
    if not md:
        return
    with st.container(border=True):
        st.markdown(md)
        flags = (view.get("analytics") or {}).get("flags") or []
        tail = f"{len(flags)} fragility flag(s)" if flags else "no fragility flags"
        engine = "written from computed analytics" if view.get("llm") else "deterministic analytics"
        st.caption(f"Layer 3 · {engine} over {view.get('n_verified', 0)} verified facts · {tail}.")


def _ensure_finalized_brief(batch_id, brief: dict, scored: dict) -> dict:
    """Regenerate the brief narrative from verified facts once per batch."""
    if (st.session_state.get("finalized_brief_batch") == batch_id
            and st.session_state.get("finalized_brief")):
        return st.session_state.finalized_brief
    from model_brief import finalize_brief
    # Feed the cash-flow-validated deal truth into the brief so its narrative
    # asserts the validated numbers and obeys the guardrails (not just Layer 3).
    canonical, guardrails = None, None
    dt = _ensure_deal_truth(batch_id)
    if dt and not dt.get("error"):
        from deal_truth import to_intel_facts, guardrail_lines
        canonical, guardrails = to_intel_facts(dt), guardrail_lines(dt)
    try:
        result = finalize_brief(brief, scored, canonical=canonical, guardrails=guardrails)
    except Exception as e:
        result = {"brief_md": brief.get("brief_md", ""), "finalized": False,
                  "counts": {}, "error": str(e)}
    st.session_state.finalized_brief = result
    st.session_state.finalized_brief_batch = batch_id
    return result


def _ensure_trust_scored(batch_id, brief: dict):
    """Run the trust engine over the brief's facts once per batch (cached in
    session). Returns the scored result, or None when there's nothing to score."""
    if brief.get("error") or not brief.get("facts"):
        return None
    if st.session_state.get("trust_scored_batch") == batch_id and st.session_state.get("trust_scored"):
        return st.session_state.trust_scored
    primary = sorted(st.session_state.uploaded_filenames)[0]
    with st.spinner("Verifying facts (grounding · reconcile · challenge)…"):
        from trust_engine import score_facts
        try:
            scored = score_facts(brief, UPLOAD_DIR / primary, st.session_state.wb_orientation)
        except Exception as e:
            scored = {"error": f"{type(e).__name__}: {e}", "facts": [], "summary": {}}
    st.session_state.trust_scored = scored
    st.session_state.trust_scored_batch = batch_id
    return scored


_VERDICT_MARK = {"show": "✓", "flag": "⚠", "omit": "✕"}


def _render_trust_panel(scored: dict) -> None:
    """Show the per-fact verdicts: what the engine would trust without a human,
    what it flags, and what it omits — the confidence score that replaces the gate."""
    with st.container(border=True):
        if scored.get("error"):
            st.caption(f"Fact check unavailable: {scored['error']}")
            return
        s = scored.get("summary", {})
        st.markdown("#### Fact check — what the engine trusts")
        st.caption(
            f"{s.get('high', 0)} high · {s.get('medium', 0)} medium · "
            f"{s.get('flagged', 0)} flagged · {s.get('omitted', 0)} omitted. "
            "High-confidence facts need no human; only flagged/omitted want a look."
        )
        for f in scored.get("facts", []):
            t = f.get("trust", {})
            v = t.get("verdict", "show")
            mark = _VERDICT_MARK.get(v, "·")
            src = f"{f.get('sheet')}!{f.get('cell')}"
            line = f"{mark} **{f.get('field')}**: {f.get('display', f.get('value'))}  ({src})"
            if t.get("notes"):
                line += f" — {'; '.join(t['notes'])[:160]}"
            st.markdown(line)


def _render_aam_appendix(batch_id) -> "pd.DataFrame":
    """
    Render the editable Audit Appendix table (the AAM summary being verified)
    plus the GPT blank-fill action. Returns the edited DataFrame so the caller
    (the left-column Proceed button) can collect the human's verified values.
    The confirm/proceed action lives in the process column, not here.
    """
    import aam
    import pandas as pd

    records = _ensure_aam_extracted()

    st.caption(
        "Core facts Collie extracted — verify each value and its source, correct "
        "anything wrong in the **Value** column, then Proceed (left)."
    )

    rows = []
    for mid in aam.AAM_METRIC_IDS:
        rec = _find_rec_by_id(records, mid)
        if rec is None:
            continue
        source = _aam_source(rec)
        if rec.get("_via_aam_gpt") and source != "—":
            source = f"{source} (GPT)"
        rows.append({
            "Group":   aam.group_of(mid) or "",
            "Metric":  rec.get("metric_name", mid),
            "Value":   _aam_editable_value(rec),
            "Source":  source,
            "Status":  rec.get("status", "missing"),
        })

    edited = st.data_editor(
        pd.DataFrame(rows),
        key=f"aam_editor_{abs(hash(batch_id))}_{st.session_state.aam_fill_version}",
        width="stretch",
        hide_index=True,
        disabled=["Group", "Metric", "Source", "Status"],
        column_config={
            "Value": st.column_config.TextColumn(
                "Value (editable)",
                help="The formatted value Collie extracted. Correct it inline if "
                     "it's wrong (e.g. type $150M, 7.5%, 2.1x, or a plain number).",
            ),
        },
    )

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["Status"]] = counts.get(r["Status"], 0) + 1
    st.caption("Status — " + " · ".join(f"{k}: {v}" for k, v in sorted(counts.items())))

    # Matches aam_extractor._GAP_STATUSES: GPT fills true blanks and flagged-wrong
    # rows only — it never re-fills candidate_pool (deterministic values stand).
    n_blanks = sum(counts.get(s, 0) for s in ("missing", "suspicious"))
    from scenarios._llm import llm_available
    if not llm_available():
        st.button("Fill blanks with GPT", disabled=True, width="stretch",
                  help="Set OPENAI_API_KEY to enable GPT blank-fill.")
    elif n_blanks == 0:
        st.button("Fill blanks with GPT", disabled=True, width="stretch",
                  help="No blanks to fill — every AAM field resolved.")
    elif st.button(f"Fill {n_blanks} blank(s) with GPT", width="stretch"):
        _fill_aam_blanks(records)

    return edited


def _fill_aam_blanks(records: dict) -> None:
    """Run the focused GPT gap-fill over current AAM blanks, then refresh the table."""
    primary = sorted(st.session_state.uploaded_filenames)[0]
    bar = st.progress(20, text="Filling blanks with a focused GPT read…")
    from aam_extractor import fill_aam_blanks
    # Scope the fill by the same orientation map the extraction used.
    filled = fill_aam_blanks(
        UPLOAD_DIR / primary, records,
        sheet_tier_map=_orientation_tier_map_for(frozenset(st.session_state.uploaded_filenames)),
    )
    bar.progress(80, text="Re-tagging table periodicity…")
    # Re-tag periodicity so newly-filled flow metrics inherit it too (cached).
    try:
        from financial_model_parser import parse_workbook_tables_cached, tag_metric_periodicity
        tag_metric_periodicity(parse_workbook_tables_cached(UPLOAD_DIR / primary), records)
    except Exception:
        pass
    bar.progress(100, text=f"GPT filled {filled} field(s)")
    st.session_state.aam_records = records
    st.session_state.aam_fill_version += 1  # force the editor to rebuild
    st.toast(f"GPT filled {filled} field(s) — review before confirming.")
    st.rerun()


def _brief_available(batch_id) -> bool:
    """True when a verified Deal Brief exists for this batch — the comprehension
    path is live (LLM present, brief built, facts scored)."""
    mb = st.session_state.model_brief
    return bool(
        st.session_state.model_brief_batch == batch_id
        and mb and not mb.get("error") and (mb.get("facts") or [])
        and st.session_state.trust_scored_batch == batch_id
        and st.session_state.trust_scored
    )


def _persist_brief_to_ssot(batch_id) -> None:
    """Persist the verified Deal Brief facts as the underwriting SSOT — the brief
    IS the extraction. No separate bounded re-extraction, so the snapshot, deep
    dives, and report all read ONE fact set (the brief's) instead of a competing
    code/catalog set. Omitted facts are dropped; flagged facts persist as
    'suspicious' so downstream knows not to lean on them."""
    from metric_resolver import parse_numeric_value
    files = sorted(st.session_state.uploaded_filenames)
    brief = st.session_state.model_brief or {}
    scored = st.session_state.trust_scored or {}
    finalized = st.session_state.finalized_brief or {}
    facts = scored.get("facts") or brief.get("facts") or []

    metrics: list[dict] = []
    bounded: dict[str, dict] = {}
    for f in facts:
        verdict = f.get("trust", {}).get("verdict", "show")
        field = f.get("field")
        if verdict == "omit" or not field:
            continue
        num, ok = parse_numeric_value(f.get("display", f.get("value")))
        value = num if ok else f.get("value")
        status = "verified" if verdict == "show" else "suspicious"
        sheet, cell = f.get("sheet"), f.get("cell")
        metrics.append({
            "metric_name": field, "value": value,
            "sheet": sheet, "value_cell": cell, "confidence": status,
        })
        bounded[field] = {
            "metric_name": field,
            "raw_value": f.get("value"),
            "normalized_value": value,
            "display_value": f.get("display", str(value)),
            "source_sheet": sheet, "source_cell": cell,
            "status": status,
            "validation_notes": f.get("trust", {}).get("notes", []),
            "from_brief": True,
        }

    ssot.write_layer(
        layer="underwriting",
        metrics=metrics,
        source_file=files[0] if files else "brief",
        bounded_metrics=bounded,
        extraction_metadata={"source": "model_brief", "brief_version": brief.get("version")},
    )
    ident = brief.get("identity", {}) or {}
    try:
        ssot.update_identity({
            "name": ident.get("asset"),
            "property_type": ident.get("property_type"),
            "location": ident.get("location"),
            "sf_or_units": ident.get("size"),
        })
    except Exception:
        pass
    try:
        s = ssot.load_ssot()
        uw = s.setdefault("layers", {}).setdefault("underwriting", {})
        uw["deal_brief"] = finalized.get("brief_md") or brief.get("brief_md")
        # Layer 3 — persist the Initial View + computed analytics (the system
        # remembers the reasoning, not just the facts).
        view = st.session_state.get("investment_view") or {}
        if view.get("view_md"):
            uw["initial_view"] = view["view_md"]
            uw["deal_analytics"] = view.get("analytics")
        ssot.save_ssot(s)
    except Exception:
        pass


def _run_analysis_from_brief(agent: AgentSession, batch_id) -> None:
    """Brief path: carry the verified brief into the SSOT as the source of truth
    and unlock deep dives + report. No re-extraction, no separate snapshot — the
    brief IS the analysis."""
    bar = st.progress(30, text="Carrying the verified brief into the record…")
    _persist_brief_to_ssot(batch_id)
    bar.progress(100, text="Brief is the source of truth")
    st.session_state.aam_confirmed_batches.add(batch_id)
    st.session_state.aam_analysis_requested.add(batch_id)  # brief is the analysis
    st.rerun()


def _confirm_aam_and_ingest(agent: AgentSession, verified: dict) -> None:
    """Legacy / no-LLM path: run the full bounded ingest, apply verified values,
    unlock analysis. Used when no Deal Brief is available (no API key)."""
    batch_id = frozenset(st.session_state.uploaded_filenames)
    files = sorted(st.session_state.uploaded_filenames)

    bar = st.progress(5, text="Ingesting workbook…")
    note = st.empty()

    # The gate already resolved most checklist metrics for the PRIMARY file —
    # hand those records to the ingest so it skips their section reads / GPT
    # fallbacks instead of re-extracting what the human just verified.
    primary = files[0] if files else None
    gate_records = (
        st.session_state.aam_records
        if st.session_state.aam_batch_id == batch_id else None
    )

    for i, fn in enumerate(files):
        bar.progress(5 + int(30 * (i / max(len(files), 1))), text=f"Ingesting {fn}…")
        note.caption(f"Classifying sheets and extracting the full metric checklist from {fn}.")
        result = tools.ingest_to_ssot_with_layer(
            fn, "underwriting",
            prefilled_bounded=gate_records if fn == primary else None,
        )
        if "error" in result:
            note.caption(f"{fn}: {result['error']}")
        else:
            note.caption(f"{fn} → underwriting layer ({result['metric_count']} metrics).")

    if verified:
        bar.progress(40, text="Applying verified facts…")
        # Re-derive NOI from the human-verified pricing inputs (the cap / price /
        # exit value may have been corrected at the gate) before persisting, so
        # NOI always reflects the confirmed pricing.
        _rederive_noi_from_verified(st.session_state.aam_records, verified)
        ssot.apply_verified_aam("underwriting", verified)
        note.caption(f"Applied {len(verified)} human-verified value(s) — your confirmations win.")
        # NOTE: learning-capture (override → observation → candidate) is
        # UNHOOKED. To re-enable, restore the record_observation /
        # synthesize_candidates loop here over `verified` corrected entries
        # (helpers in knowledge_store.py are intact). Reading active
        # patterns into prompts is independent and remains on.

    # Stage 2: trace formulas out from the verified anchors to reach related
    # (non-AAM) metrics. Additive enrichment — never blocks.
    try:
        bar.progress(60, text="Tracing formulas from verified cells…")
        note.caption("Following the model's own formula references outward from the verified anchors.")
        from formula_tracer import trace_from_verified, FORMULA_TRACER_VERSION
        anchors = {
            n: r for n, r in st.session_state.aam_records.items()
            if r.get("status") != "missing"
        }
        trace = trace_from_verified(UPLOAD_DIR / files[0], anchors)
        trace["version"] = FORMULA_TRACER_VERSION
        ssot.attach_formula_trace("underwriting", trace)
        # Fold traced metrics into bounded_metrics so the snapshot AND
        # deep-dives use them (fill-only; never clobbers verified facts).
        _, n_merged = ssot.merge_traced_metrics("underwriting", trace)
        n_reached = len(trace.get("reached_metrics", {}))
        if n_reached:
            note.caption(
                f"Traced {n_reached} related metric(s) from verified cells "
                f"({n_merged} checklist gaps filled)."
            )
    except Exception as e:
        note.caption(f"Formula trace skipped: {e}")

    # Table-centric parse: detect model tables + tag each metric with its
    # table's periodicity (so NOI etc. inherit monthly/annual, not guessed).
    try:
        bar.progress(85, text="Reading model tables…")
        note.caption("Detecting model tables and tagging metrics with their table's periodicity.")
        from financial_model_parser import parse_workbook_tables_cached, MODEL_PARSER_VERSION
        tables = parse_workbook_tables_cached(UPLOAD_DIR / files[0])
        _, n_tagged = ssot.attach_model_tables(
            "underwriting", tables, version=MODEL_PARSER_VERSION
        )
        if tables:
            note.caption(
                f"Parsed {len(tables)} model table(s); tagged {n_tagged} "
                f"metric(s) with table periodicity."
            )
    except Exception as e:
        note.caption(f"Model-table parse skipped: {e}")

    bar.progress(100, text="Verified, ingested & traced")
    st.session_state.aam_confirmed_batches.add(batch_id)
    st.rerun()


def _run_scenario_for_batch(agent: AgentSession, scenario_key: str) -> None:
    """
    Phase 3: readiness check + scenario run. Idempotent: marks the batch as
    completed when done so reruns don't repeat the work.
    """
    pseudo_user_msg = "Uploaded: " + ", ".join(sorted(st.session_state.uploaded_filenames))

    with st.chat_message("assistant"):
        with st.status("Generating analysis...", expanded=True) as status:
            readiness = tools.check_scenario_ready(scenario_key)
            if not readiness.get("ready"):
                status.update(label="More data needed", state="error")
                missing_msg = (
                    f"**Can't run {SCENARIO_CONFIG[scenario_key]['display_name']} yet.**\n\n"
                    f"{readiness.get('reason', 'Missing required layers.')}\n\n"
                    f"- Layers in SSOT now: `{readiness.get('layers_present', [])}`\n"
                    f"- Example of what's still needed: `{readiness.get('example_missing', [])}`"
                )
                st.markdown(missing_msg)
                _seed_agent_history(agent, pseudo_user_msg, missing_msg, [], None)
                st.session_state.completed_batches.add(frozenset(st.session_state.uploaded_filenames))
                return

            runner = _SCENARIO_RUNNER[scenario_key]
            scenario_result = runner()

            if "error" in scenario_result:
                status.update(label="Analysis failed", state="error")
                err_msg = f"**Couldn't generate the analysis:** {scenario_result['error']}"
                st.markdown(err_msg)
                _seed_agent_history(agent, pseudo_user_msg, err_msg, [], None)
                st.session_state.completed_batches.add(frozenset(st.session_state.uploaded_filenames))
                return

            status.update(label="Done", state="complete")

        st.markdown(scenario_result["narrative"])

    st.session_state.completed_batches.add(frozenset(st.session_state.uploaded_filenames))
    _seed_agent_history(agent, pseudo_user_msg, scenario_result["narrative"], [], scenario_result)


def _render_manual_override_ui() -> None:
    """Show a form letting the user classify any files that auto-classification missed."""
    scenario_key = st.session_state.active_scenario
    st.divider()
    st.markdown("### Manual classification")
    st.caption(
        "These files couldn't be classified by name. Tell me what each one is, "
        "and I'll ingest them into the right SSOT layer."
    )

    # Suggest a sensible default based on the active scenario.
    default_layer = {
        "deal_review": "underwriting",
        "perf_vs_plan": "actuals_recent",
    }.get(scenario_key, "underwriting")

    with st.form(key="manual_override_form"):
        choices: dict[str, str] = {}
        for filename in sorted(st.session_state.get("pending_overrides", {})):
            choices[filename] = st.selectbox(
                f"📄 {filename}",
                options=_MANUAL_LAYER_OPTIONS,
                index=_MANUAL_LAYER_OPTIONS.index(default_layer),
                key=f"override_{filename}",
            )
        submitted = st.form_submit_button("Ingest with these layers", type="primary")

    if submitted:
        # Run the override ingests.
        with st.status("Ingesting with manual layers...", expanded=True) as status:
            for filename, layer in choices.items():
                status.update(label=f"Ingesting {filename} as {layer}...")
                result = tools.ingest_to_ssot_with_layer(filename, layer)
                if "error" in result:
                    st.error(f"❌ {filename}: {result['error']}")
                else:
                    st.markdown(f"✅ **{filename}** → `{layer}` ({result['metric_count']} metrics)")
            status.update(label="Done", state="complete")

        # Clear the override queue and invalidate the completed-batches cache so
        # the scenario runs on the next rerun (which happens automatically after form submit).
        st.session_state.pending_overrides = {}
        st.session_state.completed_batches = set()
        st.rerun()


def _seed_agent_history(
    agent: AgentSession,
    user_msg: str,
    assistant_msg: str,
    ingest_results: list[dict],
    scenario_result: dict | None,
) -> None:
    """
    Append the work-just-done into the agent's message history so it has full
    context for any follow-up Q&A. The agent won't re-run ingest or scenario
    because it can see they already happened.
    """
    # Build a tool-summary line so the agent knows what's in SSOT.
    layers_now = ssot.list_layers()
    context_note = (
        f"[System: I (the host app) already ingested the uploaded files and "
        f"ran the scenario. Current SSOT layers: {layers_now}. "
        f"Do not call ingest_to_ssot or run_<scenario> again for these files. "
        f"For follow-up questions, use get_layer_details or get_ssot_summary.]"
    )
    agent.messages.append({"role": "user", "content": user_msg})
    agent.messages.append({"role": "assistant", "content": assistant_msg})
    agent.messages.append({"role": "user", "content": context_note})
    # Have the model acknowledge so the next true user message lands cleanly.
    agent.messages.append({"role": "assistant", "content": "Acknowledged. Ready for follow-up questions."})


def _render_tool_trace(agent: AgentSession) -> None:
    """Show what tools the agent called in its last turn (for transparency)."""
    if not agent.last_tool_calls:
        return
    with st.expander(f"🔧 Tool calls this turn ({len(agent.last_tool_calls)})", expanded=False):
        for tc in agent.last_tool_calls:
            args_preview = ", ".join(f"{k}={v!r}" for k, v in tc["arguments"].items())
            st.markdown(
                f'<div class="tool-trace">→ <b>{tc["name"]}</b>({args_preview})</div>',
                unsafe_allow_html=True,
            )
            result = tc["result"]
            if isinstance(result, dict) and "error" in result:
                st.markdown(f'<div class="tool-trace">  ❌ {result["error"]}</div>', unsafe_allow_html=True)
            elif isinstance(result, dict):
                # Compact summary based on result keys
                preview_keys = [k for k in ("filename", "layer", "metric_count", "layers_now_present",
                                            "files", "ready", "narrative") if k in result]
                if "narrative" in preview_keys:
                    st.markdown('<div class="tool-trace">  ✓ narrative generated</div>', unsafe_allow_html=True)
                else:
                    preview = {k: result[k] for k in preview_keys}
                    st.markdown(f'<div class="tool-trace">  ✓ {preview}</div>', unsafe_allow_html=True)
            else:
                st.markdown(f'<div class="tool-trace">  ✓ {result}</div>', unsafe_allow_html=True)


# =============================================================================
# Router
# =============================================================================

# Customer mode is a single deal-analysis workspace — skip the scenario picker and
# drop straight into the analyzer.
if _ui_mode() == "customer" and st.session_state.active_scenario is None:
    st.session_state.active_scenario = "deal_review"
    if st.session_state.get("agent_session") is None:
        st.session_state.agent_session = AgentSession("deal_review")

if st.session_state.active_scenario is None:
    render_landing()
else:
    render_scenario()
