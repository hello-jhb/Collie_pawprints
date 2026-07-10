# The Learning Loop

Collie's engine is deterministic Python. When it can't resolve something on its own, GPT
steps in. The **learning loop** records every one of those GPT resolutions so that a
pattern you'd otherwise fix by hand — the same mis-read on file after file — becomes
visible and, once you approve it, gets coded into the deterministic layer.

**It only ever captures. It never changes the engine on its own.** Promotion (turning a
recorded pattern into a Python rule / alias) is always a human decision — yours and mine.

---

## What gets recorded

Every time GPT weighs in on a fact, one line is written with: which layer asked, the
concept, the decision, the file (+ a content hash so re-uploads of the same file group
together), the label GPT keyed on, and the before/after values.

Three layers feed it today:

| Layer | When it fires | Decisions it records |
|---|---|---|
| `fact_review` | GPT audits the engine's headline facts against the summary every analyze | **agreed** (GPT confirms), **corrected** (GPT fixed a weak pick), **rejected** (GPT's suggestion failed an invariant — e.g. cost < debt — so it was thrown out) |
| `concept_fallback` | The catalog found *zero* candidates for a concept and GPT read one | **filled** — the matched label is a prime candidate for a new catalog alias |
| `tier2_read` | No cash-flow engine could be validated; GPT read the summary instead | **read** — the file fell through to an unvalidated summary read |

The **rejected** decisions matter as much as the corrections: they're the record of an
invariant catching a bad GPT answer, which is exactly the guarantee that keeps today's
fixes safe.

## What a "promotion candidate" is

A single correction is a one-off. The **same** `(layer, concept, label)` resolution
recurring across **≥2 distinct files** is a pattern worth coding — that's a promotion
candidate. Example: if `# of Units` tables labeled `Total / Wtd. Avg.` get corrected to
the true total on several files, that label belongs in the deterministic reader (which is
exactly the fix we shipped for 425 Colorado).

---

## How to check it (do this from time to time)

**In the app** — the dashboard at `/learning?token=YOURTOKEN`:
- Backend status (is capture live and reachable?)
- Totals by layer / decision
- Promotion candidates (the patterns to consider coding)
- Recent decisions (a live feed of what GPT is doing)

The dashboard is **off unless you set `COLLIE_ADMIN_TOKEN`** — that keeps the public
Cloud Run URL from leaking your captured deal values. Set the env var, then visit
`/learning?token=<that value>`.

**On the command line** (local, no token needed):
```
python3 learning_store.py
```
Prints the same summary + promotion candidates.

---

## The promote workflow (human-in-the-loop, by design)

1. You (or I) open the dashboard and look at the promotion candidates.
2. For a candidate that's clearly a real rule, I write the deterministic change — a new
   alias-catalog entry, an invariant, or reader logic — and a test that pins it.
3. It ships like any other fix. The loop keeps recording; if the pattern is truly fixed,
   the corrections for it stop appearing.

Nothing auto-promotes. The store is memory and evidence; the judgment stays human.

---

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `COLLIE_LEARNING` | `1` | Set `0` to disable capture entirely |
| `COLLIE_LEARNING_BACKEND` | `local` | `local` (JSONL file) or `firestore` (durable, for Cloud Run) |
| `COLLIE_LEARNING_DIR` | `./learning` | Where the local JSONL lives |
| `COLLIE_LEARNING_COLLECTION` | `collie_learning_events` | Firestore collection name |
| `COLLIE_ADMIN_TOKEN` | *(unset → dashboard off)* | Token required to view `/learning` and `/api/learning` |

### Production (Firestore) setup
The local JSONL is fine for dev, but Cloud Run's filesystem is ephemeral — it won't
survive an instance recycle or a deploy. For durable capture:

1. Enable the Firestore API in the GCP project and create a database (Native mode).
2. Grant the Cloud Run service account the **Datastore User** role (writes use the
   instance's Application Default Credentials automatically — no keys in code).
3. Deploy with `COLLIE_LEARNING_BACKEND=firestore` (and optionally
   `COLLIE_GCP_PROJECT` / `COLLIE_LEARNING_COLLECTION`).

Each decision becomes one document in the collection — **browsable and filterable
directly in the Firestore console**, in addition to the in-app dashboard. If the
dependency or credentials are ever missing, capture degrades silently (a broken store
must never block an analysis).
