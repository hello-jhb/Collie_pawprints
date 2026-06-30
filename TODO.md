# TODO — deal-reconstruction engine

## Tier 1 proforma parsing (operating trajectory) — DONE
- [x] Gap-immune Tier 1 scanner (`_scan_model_sheets_for_ops` in `deal_truth.py`):
      reads operating rows directly from model-role sheets the generic table
      parser misses (formatted pro formas whose section gaps stop the parser).
      Finds the period header, scans the whole sheet, picks the single best
      consolidated row (most periods, then largest magnitude) — never sums
      (avoids double-counting before/after stages and component+total rows).
- [x] Reconcile guard: a Tier-1 NOI is accepted only if `NOI/exit_cap ≈ exit_value`.
      A component-only row (one asset of a mixed-use deal) won't reconcile, so the
      consolidated stream-derived NOI (Tier 1b) wins. On **1425** this pulls opex
      from the operating model while NOI correctly stays the consolidated stream
      (1425 has no single consolidated NOI row in its pro forma — the consolidated
      view lives in the cash-flow stream and on the summary).
- [x] `financial_model_parser` left untouched (no risk to tables it already parses).

## Related items — DONE
- [x] Cross-check `acquisition cost` / `total cost` against the cash flow itself
      (unlevered cumulative outflows / first-period outflow). When they agree the
      fact is promoted to **cash-flow-validated** (✓CF). 1425: both pass.
- [x] Conflict detection recognizes part-of-whole (total equity = LP + GP) and no
      longer flags it as a conflict; genuine discrepancies still flag.
- [x] `deal_truth` canonical facts + guardrails now feed the GPT brief
      (`finalize_brief`), so the brief's narrative — not just Layer 3 — runs off
      the validated spine.

## Future (not yet scoped)
- [ ] Component/subtotal structure detection in pro formas (recognize that a
      "Total X" row is the sum of the component rows above it), so a consolidated
      NOI row can be read directly even when the workbook lists both. Today the
      reconcile guard handles correctness; this would add a true Tier-1 NOI on
      models that expose one.
