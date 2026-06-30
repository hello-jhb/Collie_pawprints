# Knowledge Layer

This directory stores Collie's structured learning artifacts.

## Runtime Layer

`patterns/*.json` contains distilled, reusable knowledge:

- `model_patterns.json` helps workbook mapping.
- `metric_patterns.json` helps section reading and deterministic validation.
- `business_plan_patterns.json` helps narrative analysis after facts are verified.

Only this layer should be loaded on normal analysis runs.

Runtime loading is handled by `knowledge_store.py`. It reads only
`patterns/*.json`, validates each pattern, and returns only entries with
`status: "active"`. Candidate, draft, rejected, superseded, archived, and
invalid patterns are ignored.

## Observation Layer

`observations/*.json` contains reviewed learning candidates created after QC.
These files are deal-specific evidence, not runtime instructions.

An observation can be:

- accepted when a human or trusted fixture verifies it
- rejected when it is wrong or not reusable
- superseded when a better rule replaces it

## Promotion

Patterns become runtime rules only after enough trusted observations accumulate.
The current JSON policy uses:

- minimum evidence count
- contradiction count / contradiction rate
- verified evidence source
- explicit `status`

GPT can propose a pattern, but it does not decide that the pattern is true. The
system promotes rules only after repeated reviewed evidence.

The runtime bridge is intentionally small:

`active JSON patterns -> prompt/validation guidance -> Analyst Bundle audit`

Raw observations must not be imported from extraction, validation, mapper, or
memo code paths.
