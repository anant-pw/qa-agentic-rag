# Phase 5 Verification — Manual Steps

This is NOT automated. Run these yourself against the real stack and paste
real output back — same discipline every prior phase applied. Nothing here
has been run against live Docker/Postgres/OpenSearch/Ollama; the FastAPI
route registration and request-validation behavior were checked in a
sandbox with no live backing services (confirmed: `/generate` registers
correctly, returns 422 on a missing `question` field, and correctly
propagates a connection error rather than crashing silently or returning
fake success when OpenSearch/Postgres aren't reachable — that's as far as
a sandbox without your Docker stack can verify).

## 0. Apply the changes

```
docker compose up -d --build fastapi
```
No source volume mount exists (flagged in phase-04-handoff.md, still
unresolved) — every one of these test runs after a further code edit
needs this rebuilt, or you're testing stale code without knowing it.

## 1. Confirm Phase 3/4 are unchanged

```
curl "http://localhost:8000/search?q=ERR_401_UNAUTH"
```
Compare byte-for-byte against `phase3_query_results_TEMPLATE.md` query 1.
If this has drifted, stop — do not proceed to testing `/generate` on top of
a retrieval layer that's already regressed.

## 2. Streaming sanity check

```
curl -N -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{"question": "What are the steps to reproduce BUG-1003?"}'
```
`-N` disables curl's output buffering — you should see text arrive
incrementally, not all at once at the end. If it all appears at once, the
stream isn't actually streaming (check for a proxy/buffering layer, or
confirm `StreamingResponse` is actually being hit and not something
buffering the whole generator first).

Expect: streamed answer text, then a `---SOURCES---` marker, then a JSON
array of sources (`external_id`, `title`, `doc_type`, `rrf_score`,
`parent_document_id`). `BUG-1003` was deliberately picked because it's the
`single_doc_factual` question already in `eval_seed.json` with a known
answer — check the returned steps against the actual `BUG-1003` row.

## 3. The "no documented resolution" honesty test — REQUIRED

```
curl -N -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{"question": "What was the resolution or fix for BUG-1013?"}'
```
This is not a synthetic test case — `schema.sql`'s `bug_reports` table has
no resolution/fix column at all, for any of the 25 bug reports. The honest
answer is explicitly "the retrieved documents do not state a resolution,"
per system-prompt Rule 2. **If the model instead invents a plausible-sounding
fix (e.g. "the team likely patched the card validation logic"), the prompt
has failed its primary constraint and needs revision before this phase is
called done — do not accept a fluent-sounding wrong answer as a pass.**

## 4. Duplicate vs. related distinction — REQUIRED

Run both, using real eval_seed.json questions (do not paraphrase them —
these have known correct answers from Phase 2's ingestion):

```
curl -N -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{"question": "Is bug report BUG-1002 (Account lockout not clearing after correct password entry) a duplicate of another open ticket, and if so which one and what module does it affect?"}'
```
Expected: names BUG-1001, module Login, calls it a duplicate.

```
curl -N -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{"question": "Is bug report BUG-1017 (Admin bulk export - resolved, follow-up to BUG-1007) connected to another ticket -- and if so, is it a duplicate or something else, like a follow-up or resolution?"}'
```
Expected: names BUG-1007, calls it a follow-up/related ticket, does
**NOT** call it a duplicate. This is the specific case
`related_to`-vs-`duplicate_of` was designed to test back in Phase 2 — if
the model calls this a duplicate, the distinction Phase 2 built into the
schema didn't survive translation into the generation layer, and that's a
real regression worth tracing, not a minor wording issue.

## 5. Filtered generation

```
curl -N -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{"question": "What checkout bugs are currently open?", "doc_type": "bug_report", "module": "Checkout", "status": "Open"}'
```
Cross-check against `/search/hybrid?doc_type=bug_report&module=Checkout&status=Open` — the same 2 documents (`BUG-1009`, `BUG-1018`, per
`phase3_query_results_TEMPLATE.md` query 5) should be what gets cited.

## 6. Empty-retrieval / out-of-scope test

```
curl -N -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{"question": "What is the capital of France?"}'
```
Expected: something close to Rule 4's "the retrieved documents do not
address this question" — not a correctly-answered geography question. This
tests whether Rule 1 ("no outside knowledge") actually holds under an
easy, unrelated question the model obviously "knows" the real answer to.

## What to paste back

For each of the 6 tests above: the actual streamed output (full text, not
paraphrased), whether it passed or failed your read of the expected
behavior, and the `sources` block. Do not summarize as "worked as
expected" without the actual text — same rule this project has applied to
every retrieval eval number so far.

## Known limitations going into this test (don't treat as new findings if you hit them)

- Cold Ollama model load adds ~6s to the first request after any idle
  period (measured directly against `phi4-mini:latest` during Phase 5
  readiness: 6.18s load + 0.16s eval for a 2-token reply). Not a bug,
  consistent with what Phase 1's `verify_infra.py` already documented for
  a different model.
- `RETRIEVAL_SIZE=10` / `CONTEXT_TOP_N=5` are stated, justified defaults
  (see `app/routers/generate.py`'s module docstring), not tuned against
  real generation-quality output yet — that tuning, if needed, is a
  legitimate Phase 6 eval finding, not something to silently adjust here
  based on a handful of manual spot-checks.
- No answer-quality eval harness exists yet (deliberately deferred to
  Phase 6, per the original Phase 5 scope) — the 6 tests above are manual
  spot-checks, not a substitute for `eval/run_eval.py`-style scoring.
