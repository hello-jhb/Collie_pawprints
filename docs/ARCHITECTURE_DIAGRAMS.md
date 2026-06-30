# Collie v2 Current Architecture Diagrams

These diagrams reflect the current code layout after reviewing the main runtime
paths in `app.py`, `agent_loop.py`, `tools.py`, `workbook_map.py`,
`deal_truth.py`, `deal_analysis.py`, `model_brief.py`, `trust_engine.py`,
`investment_intel.py`, and `ssot.py`.

## Current Architecture

```mermaid
flowchart TB
    User["User"] --> UI["Streamlit UI\napp.py"]

    UI --> Uploads["Uploaded workbooks\nuploads/"]
    UI --> Session["Session state\nscenario, batch, brief, truth, analysis"]
    UI --> Agent["Scenario agent\nagent_loop.AgentSession"]

    Agent --> ToolSchemas["Tool registry + schemas\ntools.py"]
    ToolSchemas --> IngestTools["Ingestion tools\nclassify_file, extract_from_file,\ningest_to_ssot"]
    ToolSchemas --> InspectTools["Inspection tools\nlist_sheets, read_sheet, search_file"]
    ToolSchemas --> ScenarioTools["Scenario runners\nrun_deal_review, run_perf_vs_plan"]

    Uploads --> DeterministicEngine["Deterministic deal engine"]
    DeterministicEngine --> WorkbookMap["Structural workbook map\nworkbook_map.py\nroles, blocks, provenance, candidates"]
    DeterministicEngine --> Parser["Table + cash-flow readers\nfinancial_model_parser.py\ncashflow_spine.py\ncashflow_rollup.py"]
    WorkbookMap --> DealTruth["Canonical deal truth\ndeal_truth.py\ncash-flow oracle, reconciliation,\nidentity checks, guardrails"]
    Parser --> DealTruth
    DealTruth --> GroundedAnalysis["Integrated grounded analysis\ndeal_analysis.py\ncapital structure, returns,\nNOI/cash flow, CapEx"]

    Uploads --> NarrativeEngine["LLM narrative layer"]
    NarrativeEngine --> Orientation["Workbook orientation\nworkbook_orientation.py"]
    Orientation --> ModelBrief["Comprehension brief\nmodel_brief.py\nwhole-tab read + cited facts"]
    ModelBrief --> TrustEngine["Fact trust scoring\ntrust_engine.py\nground, authority, reconcile, challenge"]
    TrustEngine --> FinalBrief["Finalized brief\nmodel_brief.finalize_brief"]
    DealTruth --> FinalBrief
    DealTruth --> Intel["Layer 3 investment view\ninvestment_intel.py\ncomputed analytics + guarded interpretation"]
    TrustEngine --> Intel

    IngestTools --> LegacyExtraction["Legacy bounded extraction path\nflexible_extractor, section_reader,\nmetric_resolver, formula_tracer"]
    LegacyExtraction --> SSOT["Single source of truth\nssot.py"]
    ScenarioTools --> SSOT
    FinalBrief --> SSOT
    Intel --> SSOT
    GroundedAnalysis --> UI
    FinalBrief --> UI
    Intel --> UI
    SSOT --> UIPanels["SSOT, analyst bundle,\nmodel tables, diagnostics panels"]
    UIPanels --> UI

    Agent --> LLM["OpenAI chat/function calling\nscenarios/_llm.py"]
    ModelBrief --> LLM
    TrustEngine --> LLM
    Intel --> LLM
    ScenarioTools --> LLM

    Cache["Disk caches\ncache/orientation, cache/briefs,\nextraction_cache"] -.-> Orientation
    Cache -.-> ModelBrief
    Cache -.-> LegacyExtraction
```

### Architecture Notes

- `app.py` is the product shell: scenario selection, upload persistence,
  session-level caching, rendering, and orchestration.
- `agent_loop.py` provides scenario-scoped chat with a constrained tool subset.
  The agent can ingest files, inspect sheets, run scenario summaries, and answer
  follow-ups.
- The current deal-review happy path leads with the deterministic engine:
  `deal_truth.py` reconstructs canonical facts from workbook structure and
  validated cash-flow streams, then `deal_analysis.py` renders the grounded
  analysis.
- The LLM narrative layer still exists, but it is secondary in the UI: it reads
  authoritative tabs, scores cited facts, finalizes only trusted assertions, and
  feeds Layer 3 investment interpretation.
- `ssot.py` is the durable internal record for verified facts, brief text,
  initial view analytics, reports, layers, and identity.
- The legacy bounded metric extraction path is retained for no-key fallback,
  diagnostics, and performance-vs-plan workflows.

## Engine Flow

```mermaid
flowchart TD
    Start["Workbook uploaded"] --> Persist["Persist file to uploads/\nreset batch-scoped state"]
    Persist --> Batch["Create batch id from uploaded filenames"]
    Batch --> TruthCached{"Deal truth already\ncomputed for batch?"}

    TruthCached -- "yes" --> SessionTruth["Use session deal_truth"]
    TruthCached -- "no" --> BuildTruth["build_deal_truth(file)\ndeal_truth.py"]

    BuildTruth --> Map["build_workbook_map(file)\n- orient sheets\n- parse tables\n- infer economic blocks\n- trace formula provenance\n- collect concept candidates"]
    Map --> ParseTables["parse_workbook_tables_cached(file)\nfinancial_model_parser.py"]
    ParseTables --> Spine["find_spine(file)\ncashflow_spine.py"]
    Spine --> SpineOK{"Cash-flow stream\nreproduces stated IRR?"}

    SpineOK -- "no" --> NoEngine["Return engine_not_found\nNo reconstruction is trusted"]
    NoEngine --> RenderWarn["Render warning in app.py"]

    SpineOK -- "yes" --> Oracle["Cash-flow oracle\n- canonical levered/unlevered streams\n- recomputed IRR\n- recomputed equity multiple"]
    Oracle --> Hold["Detect hold period\nfrom actual sale event"]
    Hold --> Reconcile["Canonical reconciliation\n- pick concept-appropriate source\n- prefer model-used inputs\n- derive stack/sale from validated stream"]
    Reconcile --> Ops["Operating trajectory\n- NOI/revenue/opex/capex\n- prefer cash-flow engine\n- reconcile units"]
    Ops --> Identities["Identity checks\nDebt+Equity approx Cost\nExitValue approx NOI/Cap\nIRR approx stated IRR"]
    Identities --> Guardrails["Build guardrails\nmissing facts, conflicts,\nsummary mismatch, unsupported claims"]
    Guardrails --> BuiltTruth["deal_truth result\ncanonical facts, oracle,\noperating series, identities, guardrails"]
    SessionTruth --> Truth["Authoritative deal truth"]
    BuiltTruth --> Truth

    Truth --> NonNeg["Render non-negotiables\napp._render_nonnegotiables_md"]
    Truth --> Detail["Render validation detail\napp._render_deal_truth_panel"]
    Truth --> Analysis["build_analysis(file, dt)\ndeal_analysis.py"]
    Analysis --> RenderAnalysis["Render grounded analysis\ncapital structure, returns,\nNOI/cash flow, CapEx"]

    Truth --> LLMAvailable{"OpenAI key available\nand engine found?"}
    LLMAvailable -- "no" --> Done["User sees deterministic result"]
    LLMAvailable -- "yes" --> Brief["build_model_brief(file)\norient + whole authoritative-tab read"]
    Brief --> Score["score_facts(brief)\ntrust_engine.py"]
    Score --> Finalize["finalize_brief(brief, scored,\ncanonical truth, guardrails)"]
    Truth --> IntelFacts["to_intel_facts(dt)\ncanonical facts win"]
    IntelFacts --> InvestmentView["build_investment_view(...)\ninvestment_intel.py\ncomputed analytics + guarded prose"]
    Score --> InvestmentView
    Finalize --> RenderNarrative["Render collapsed narrative read\nand trust panel"]
    InvestmentView --> RenderIntel["Render Layer 3 initial view"]

    RenderAnalysis --> PersistSSOT{"User runs / persists\nanalysis?"}
    RenderIntel --> PersistSSOT
    RenderNarrative --> PersistSSOT
    PersistSSOT -- "yes" --> WriteSSOT["write_layer/update_identity\nssot.py"]
    PersistSSOT -- "no" --> Done
    WriteSSOT --> Followups["Follow-up chat, deep dives,\nfinal report read from SSOT\nand workbook inspection tools"]
```

### Engine Invariants

- The deterministic engine does not use GPT for extraction. Its trust anchor is
  the workbook's own cash-flow stream matching the model's stated IRR.
- If no validated cash-flow spine is found, the engine refuses to reconstruct
  the deal instead of filling gaps from weak summary labels.
- Canonical returns are recomputed from validated streams, not copied from
  possibly stale or mislabeled summary cells.
- Formula provenance separates a displayed number from the source cell the model
  actually uses.
- Guardrails are generated from detected evidence in the workbook and are passed
  into later narrative layers so GPT cannot assert unsupported conclusions.
- The LLM brief/trust path is additive: it improves narrative and interpretation,
  while deal truth remains the authoritative fact spine when present.
