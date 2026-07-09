# Spec: state the deal phasing (development / renovation / lease-up periods)

**Owner:** (you)
**For:** Claude Code
**Status:** Draft direction.
**Depends on:** `SPEC_investment_read_rate_and_reposition.md` — specifically P3 (the
reposition / V-trough detector) and P2 (the disruption/stub flag on going-in NOI). Land those
first; this spec surfaces the phase boundaries P3 already has to compute.

## Context / why

After the rate + reposition fixes, the Investment Read can **classify** a deal correctly
(development / lease-up vs. value-add repositioning vs. classic value-add). What it still
can't do is **state the phasing** — the reader gets a label but not a timeline:

- how long the build or renovation runs,
- when the asset delivers / reopens,
- how long lease-up or re-stabilization takes,
- the stabilization date and how much hold remains after it.

The ingredients already exist but are only used internally for classification, never narrated:
`_noi_appearance_months` (`interpretation.py:53`), `stabilization_month` / `min_dscr_month`
in `_dscr_health` (`deal_analysis.py:226`), the capex `by_year` series, and `hold.months`.
This spec derives an explicit **phase timeline** from those series and surfaces it as a claim.

**Goal example (development):**
> "≈22-month construction; delivers Q2 2021, then ~14-month lease-up to stabilization by
> Q3 2022 — ~3.5 years of the 7-year hold are post-stabilization."

**Goal example (repositioning):**
> "≈18-month renovation with rooms offline through 2020; re-stabilizes by 2022, leaving
> ~4 years of stabilized hold before exit."

## Non-goals

- Do not re-derive the archetype or the value lever — that's the prior spec. This only adds
  the timeline for whatever the deal already is.
- No new GPT computation. Phases are computed deterministically and handed over as a claim
  + guardrail.

---

## What to build

### 1. A phase-timeline computer (deterministic)

Add a function in `deal_analysis.py` (next to `_dscr_health` / `_occupancy_bookends`), e.g.
`_deal_phasing(noi_t, capex_t, hold, archetype_signals) -> dict | None`. It reads the dated
NOI and capex series and returns phase boundaries as **dates + durations in months**:

```
{
  "kind": "development" | "repositioning" | "none",
  "close": "2019-01",
  "build_or_reno": {"start": "2019-01", "end": "2020-12", "months": 24,
                    "capex_share": 0.86},      # share of total capex in this window
  "delivery_or_reopen": "2021-01",             # first material NOI after the trough/gap
  "leaseup": {"start": "2021-01", "end": "2022-09", "months": 20},
  "stabilization": "2022-09",                  # from the DSCR/NOI ≥95%-of-stabilized rule
  "post_stab_hold_months": 15,
  "source": "Model!row13 / Model!row16",       # NOI + capex rows behind the read
  "confidence": "T2"                           # derived from validated series, label honestly
}
```

Boundary rules (reuse existing logic, don't reinvent):

- **development** (no prior in-place NOI): `build_or_reno.start` = close; `end` /
  `delivery_or_reopen` = first month NOI is material (`_noi_appearance_months` boundary);
  `leaseup.end` = `stabilization`.
- **repositioning** (V-trough with prior in-place NOI, from P3): `build_or_reno` = the
  capex-concentrated trough window P3 already identifies (trough entry → recovery start);
  `delivery_or_reopen` = trough exit (first recovery month); `leaseup` = reopen →
  `stabilization`.
- **stabilization**: the month TTM NOI first reaches ≥95% of stabilized NOI — the same rule
  `_dscr_health` already uses (`stabilization_month`). Reuse it; do not add a second
  definition.
- **post_stab_hold_months**: `hold.months` − months(close → stabilization).
- Return `{"kind": "none"}` for core / core-plus / stabilized acquisitions (no build or reno
  phase to state) so the claim simply omits.

Guards: require full date coverage for any window you report; if a boundary can't be pinned
(partial series, no capex row, no debt-service flow for stabilization), return that field as
`None` and label the phase confidence down rather than guessing.

### 2. Carry it on the fact sheet

In `assemble_fact_sheet` (`interpretation.py:444+`), attach the result under
`deal["phasing"]` (alongside `archetype`, `strategy`, `targets`). Add a line to
`render_fact_sheet` so reviewers can see it in the text dump, e.g.:

```
PHASING: 24-mo reno (86% of capex) · reopen 2021-01 · stabilize 2022-09 · 15 mo post-stab hold
```

### 3. A `phasing` claim

Add to `_acquisition_claims` (`interpretation.py:274`). Only emit when
`phasing["kind"] != "none"`. Shape it like the other claims (headline / why / why_matters /
implication / sources / guardrail):

- **headline**: the phase summary in words (build/reno months, reopen, stabilize, post-stab hold).
- **why_matters**: tie to risk proportional to the archetype — for development, delivery +
  absorption timing; for repositioning, renovation duration/cost and whether rate holds after
  reopening. (This complements, not duplicates, the coverage/DSCR claim.)
- **guardrail**: `"State the phasing exactly as computed (build/reno X mo, stabilize <date>).
  Do not invent or round dates the fact sheet doesn't carry."` — so GPT narrates the timeline
  but can't fabricate it. The numeric-grounding check (`_check_numeric_grounding`) should also
  catch stray dates/durations.

### 4. Prompt + render

- Update `_SYSTEM_PROMPT` (`interpretation.py:657`) so the read may open the thesis with the
  phasing when present: one clause naming the build/reno period and stabilization, then the
  lever and the risk. Keep it to the existing 2 sections; the phasing is a sentence inside
  Key Findings, not a new section.
- Keep it deterministic-fallback safe: `_deterministic_read` already renders each claim's
  headline, so the phasing claim shows even with no API key.

---

## Acceptance

- **St. Regis (repositioning):** read states an ~18–24 month renovation with the capex share,
  a reopen year, a stabilization year, and post-stabilization hold remaining — sourced to the
  NOI + capex rows.
- **Ground-up development fixture:** read states construction months → delivery → lease-up →
  stabilization, and does **not** call the pre-delivery gap a value-add trough.
- **Core / stabilized acquisition:** no phasing claim emitted (kind = "none"); read is
  unchanged.
- Any boundary that can't be pinned renders as "not determinable from the model" rather than a
  guessed date. Confidence labeled per trust tier.

## Tests

- Extend the St. Regis fixture from the prior spec: assert phasing kind = repositioning,
  a non-null reno window with capex_share > 0.5, a stabilization date, and post_stab_hold_months > 0.
- Add a development fixture: assert kind = development, build window starts at close, delivery
  after the NOI gap, lease-up ends at stabilization.
- Add a core fixture: assert phasing kind = "none" and no phasing claim.
- Keep `_check_numeric_grounding` green — no dates/durations in the narration that aren't on
  the fact sheet.

## Suggested order

1. `_deal_phasing` computer + fact-sheet wiring + render line (produces the data).
2. `phasing` claim + guardrail (binds the narration).
3. Prompt tweak + fixtures.
