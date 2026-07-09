# Spec: teach the Investment Read to see rate-driven repositionings

**Owner:** (you)
**For:** Claude Code
**Status:** Draft direction — implement in priority order, land #1 and #3 first.

## Context / why

The Investment Read misread a hotel repositioning (St. Regis Kauai → 1 Hotel). It called
the deal an occupancy-driven value-add lease-up. It is actually a **rate-driven
repositioning through a renovation trough**:

- ADR steps up ~50% around the rebrand (~$525 → $786, → $874 by exit); **occupancy
  falls** 79% → 67% before recovering to ~74–79%. The value lever is rate, not occupancy.
- Disrupted NOI runs $12.3M (2018 in-place) → **$2.3M (2020, rooms offline for reno)** →
  $9.7M → $22.2M → $27.0M. The trough is a renovation, not a lease-up from zero.
- Reported NOI is propped by NOI-guarantee and disruption-credit lines; the workbook
  carries a separate `Undisrupted NOI` row that the parser ignores.

These are **not GPT hallucinations** — they are structural gaps in the deterministic layer
(`deal_analysis.py` trajectory + `interpretation.py` claims/prompt) that force the wrong
narration. GPT obeyed the fact sheet it was handed; the fact sheet was blind.

Keep the existing architecture (fact sheet → computed claims → binding guardrails → GPT
narration). These changes add signals and fix labels; they do not restructure the pipeline.

---

## Priority 1 — Represent the rate (ADR/RevPAR) lever; stop assuming occupancy

**Problem.** The value lever is hard-coded to occupancy:
- `_classify_archetype` and the `thesis` claim only read occupancy + capex + NOI ramp
  (`interpretation.py:286–304`).
- System prompt Rule 5 asserts "occupancy is the lever behind the ramp" (`interpretation.py:668`).
- No ADR/RevPAR series exists anywhere; `cashflow_rollup.py:49` discards ADR as
  "not summable."

**Change.**
1. Add an **ADR (and, if present, RevPAR) level series** to the trajectory, alongside
   occupancy. Treat it exactly like occupancy: a level/rate series, not a summable flow.
   - Reuse the occupancy plumbing: `_forward_year_avg`, `_select_occupancy`,
     `_occupancy_bookends` in `deal_analysis.py`. Generalize these to a
     `_level_series(kind=...)` helper or add sibling functions `_select_rate_series` /
     `_rate_bookends` so ADR/RevPAR get going-in + stabilized bookends and a `by_year`.
   - Surface under `operating["adr"]` / `operating["revpar"]` in `assemble_fact_sheet`
     (`interpretation.py:505–509`), mirroring how `operating["occupancy"]` is carried.
2. Compute a **RevPAR bridge decomposition**: split the RevPAR change from going-in to
   stabilized into the portion from ADR vs. the portion from occupancy
   (RevPAR = ADR × Occupancy; use the standard rate-vs-volume attribution). Store the
   dominant driver, e.g. `traj["revpar_bridge"] = {"rate_share": 0.9, "occ_share": -0.1,
   "lever": "rate"}`.
3. Make the `thesis` claim **name the actual lever** instead of assuming occupancy. Replace
   the hard-coded occupancy string (`interpretation.py:289–299`) with logic that reads the
   RevPAR bridge: `"rate-led"` when rate_share dominates (and especially when occupancy is
   flat/down), `"occupancy-led"`, or `"rate + occupancy"`. The `why_matters` copy must not
   claim occupancy is the lever when it isn't.
4. **Relax System Prompt Rule 5** (`interpretation.py:668`) to: *"Tie the NOI trend to
   whichever operating lever is actually moving — rate (ADR/RevPAR) or occupancy — per the
   thesis claim. Do not assume occupancy; if occupancy is flat or falling while rate rises,
   this is a rate-led story."*
5. **Add a guardrail** naming the computed lever so GPT can't default to occupancy, e.g.
   `"The value lever is RATE (ADR +X%); occupancy is flat/down. Do not attribute the ramp
   to occupancy."` Emit it from the `thesis` claim's `guardrail` field.

**Acceptance.** On the St. Regis model the `thesis` claim reads rate-led, the render shows
an ADR bookend line, and the narration credits ADR — not occupancy — for the ramp.

---

## Priority 2 — Fix "going-in NOI" anchoring (don't land on a stub / disrupted year)

**Problem.** Going-in NOI is the first forward operating year (`_forward_year_noi`, offset
~0, `deal_analysis.py:128`). On St. Regis it reported $7.5M (27.8% of stabilized); true
in-place 2018 NOI is $12.3M. The anchor landed on a partial close-stub or the first
renovation-dip year, understating the in-place base and inflating the "value to be created."

**Change.**
- Define going-in NOI as the **last undisrupted in-place operating year at/around close**,
  not merely the first forward window.
- Guard against (a) a partial stub year (coverage < ~11 months) and (b) any year flagged as
  renovation-disrupted (see Priority 3). If the first window is a stub/disrupted, step to the
  nearest clean in-place year.
- Recompute `going_in_noi_pct_of_stabilized` from the corrected base and confirm the
  `thesis` "value to be created is the ramp" framing scales down accordingly.

**Acceptance.** Going-in NOI on St. Regis reads ≈ $12M (not $7.5M) and the "% of stabilized"
signal updates to match.

---

## Priority 3 — Distinguish renovation disruption from lease-up

**Problem.** Any early NOI trough is treated as lease-up (expected, ignorable):
- `_noi_appearance_months` (`interpretation.py:53`) and `_dscr_health`
  (`deal_analysis.py:226`) assume a pre-stabilization dip is absorption.
- The `structural_risk` claim states the low DSCR was "during lease-up, not stabilized
  operations" (`interpretation.py:326–327`).
- No reposition/renovation concept exists. St. Regis is an operating asset taken **offline
  to renovate** — same low DSCR, different risk (reno timing + re-rate execution, not
  absorption of new space). The workbook even carries `Undisrupted NOI` /
  `Renovation Disruption` rows the parser skips.

**Change.**
1. Add a **reposition/renovation detector**. Signal set: a mid-hold **V-shaped NOI trough**
   (NOI healthy → deep dip → recovery to a new, higher plateau) with **capex concentrated in
   the trough years**, and non-trivial going-in occupancy (asset was operating). Distinguish
   from lease-up, which is monotonic from ~zero with no prior in-place NOI.
2. Add an archetype label + lens for it in `_ARCHETYPE_LENS` / `_classify_archetype`
   (`interpretation.py:35–133`), e.g. **"value-add / repositioning"**: *"Value comes from
   re-rating the asset after a capital renovation; the mid-hold NOI trough is planned
   downtime, not weak demand. Watch renovation timing/cost and whether the new rate holds
   post-reopening — not lease-up absorption."*
3. In `_dscr_health` and the `structural_risk` claim, **label the low-coverage window as
   "renovation disruption"** (not "lease-up") when the reposition pattern fires. Keep the
   coverage-first framing — only the label/lens changes.

**Acceptance.** St. Regis classifies as repositioning, the DSCR narrative says the 0.41×
trough is renovation disruption, and the stated principal risk is reno execution + re-rate,
not lease-up absorption.

---

## Priority 4 — Surface the clean (undisrupted) NOI when the model supports it

**Problem.** The NOI trajectory bakes in NOI-guarantee and disruption-credit lines, so part
of the "earnings growth" is underwritten support, not organic operations. Nothing separates
them.

**Change.**
- When the model carries `Undisrupted NOI` / NOI-guarantee / renovation-disruption lines,
  parse them and carry an **undisrupted NOI trajectory in parallel** with the reported one.
- Add a signal for **how much of the ramp is guarantee/disruption-supported** vs. organic,
  and expose it as a quality-of-ramp note (a `why_matters` add or a low-key claim). Respect
  the existing trust tiers — mark it T2/T3 with source if it's a single-line read.

**Acceptance.** Where the model exposes it, the read notes the organic vs. supported split
(e.g. undisrupted NOI $12.3M → $28.8M, ~15% CAGR) rather than treating the propped figure as
fully organic.

---

## Cleanup (cheap, do alongside)

- **Kill "archetype" leakage into prose.** Map internal labels to plain phrases before they
  reach the reader: `thesis` headline injects `a['label'].title()` (`interpretation.py:294`)
  and `render_fact_sheet` prints `ARCHETYPE:`. Add a `LABEL_DISPLAY` map
  ("value-add / repositioning" → "repositioning", "opportunistic / development" →
  "development / lease-up", etc.).
- **Tighten length.** Prompt currently asks for 3–5 Key Findings bullets; drop to **3** and
  reinforce Rule 12 (the facts card above already carries the numbers — no restating).

## Guardrails for the implementation itself

- Deterministic layer only — GPT still never computes. New signals are computed in
  `deal_analysis.py` / `interpretation.py` and handed to GPT as claims + guardrails.
- Preserve trust tiers (T1 spine/validated, T2 footed components, T3 labeled cell reads).
  ADR/RevPAR and undisrupted-NOI reads are likely T2/T3 — label confidence honestly.
- Occupancy-driven deals must **not** regress: when occupancy genuinely leads, the thesis
  must still read occupancy-led.

## Tests

- Add a fixture from the St. Regis workbook (or a trimmed proforma) asserting:
  rate-led thesis, going-in NOI ≈ $12M, repositioning archetype, DSCR trough labeled
  "renovation disruption."
- Add/keep a monotonic lease-up fixture asserting it still classifies development/lease-up
  and occupancy-led — guards against over-firing the reposition detector.
- Extend `test_deal_truth.py` / `test_perf_vs_plan_engine.py` patterns; keep the numeric-
  grounding check (`_check_numeric_grounding`) green.

## Suggested order

1. P1 ADR/RevPAR series + lever-aware thesis + Rule 5 (fixes the headline misread).
2. P3 reposition detector + DSCR label (fixes the risk misread).
3. P2 going-in anchor (depends on P3's disruption flag).
4. P4 undisrupted NOI + cleanup.
