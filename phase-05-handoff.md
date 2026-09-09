# Phase 5 Handoff — Grounded Streaming RAG Endpoint with Source Attribution

## 1. Completion Status: **Complete — verified against the real stack, including three real regressions found and fixed during verification**

A readiness report was produced and approved before any code was written,
confirming Phase 4's `hybrid_search()` / `/search/hybrid` were correctly
integrated and identifying one real risk in advance (RRF candidate-pool
narrowing at low `size`) before it could become a silent bug. Code was then
written, and verification surfaced three additional real, previously-latent
defects — one in Phase 4's own `queries.py` (never triggered until Phase 5
became the first caller to combine filters with the k-NN leg), and two in
Phase 5's own prompt/generation design. All three are fixed and confirmed
against live output, not assumed fixed from code review alone.

---

## 2. What Was Implemented

- `POST /generate` — a new FastAPI endpoint, streaming, retrieval-then-
  generation, with source attribution and explicit grounding constraints.
- Retrieval: calls `hybrid_search()` (Phase 4, unmodified in its public
  behavior) directly as a function, requesting `size=10` internally and
  truncating to the top 5 fused results for the prompt — deliberately NOT
  requesting `size=5` directly, because `hybrid_search()` passes `size` to
  both the BM25 and k-NN legs before RRF fusion, so a native `size=5` call
  would fuse over a narrower per-leg candidate pool than the one Phase 4's
  Recall@5=100% eval number was actually measured against.
- Structured, doc_type-aware context construction (`app/generation/`):
  OpenSearch's `_source` only stores flattened `cleaned_text` (confirmed
  by reading `indexer.py`'s `build_os_document()` — the structured fields
  are used to build the embedding input at index time but never written
  into the OpenSearch document itself), so a new, additive Postgres
  lookup (`fetch_structured_fields()`) retrieves `description`/
  `steps_to_reproduce` (bug reports) or `preconditions`/`steps`/
  `expected_result` (test cases) per retrieved document, and the prompt
  presents them by field, not as raw text.
- **Resolved cross-reference surfacing** (`fetch_references()`): a second
  Postgres lookup against `document_references`, added mid-phase after a
  real failure (Section 6) showed the LLM could not reliably re-derive
  `duplicate_of` vs. `related_to` classifications from ambiguous free
  text alone. Now the resolved classification — already determined
  deterministically by Phase 2's `resolve_explicit_bug_mentions()` — is
  handed to the model directly as a `"Recorded cross-references"` line
  per document, in both directions (source and target).
- System prompt (`app/generation/prompt.py`) with five explicitly ordered
  grounding rules: (1) retrieved-documents-only, no outside knowledge;
  (2) explicit abstention on undocumented resolutions, using the real
  document ID, never a placeholder; (3) report recorded cross-references
  verbatim rather than re-inferring from prose; (4) distinguish "not in
  what was retrieved" from "corpus doesn't address this" ; (5) cite
  document IDs for stated facts.
- Streaming via Ollama's native `/api/chat` (`stream: true`) wrapped in a
  Python generator, consumed by FastAPI's `StreamingResponse`. Source
  attribution (`external_id`, `title`, `doc_type`, `rrf_score`,
  `parent_document_id`) is emitted as a structured JSON block **after**
  the streamed answer text, and is **skipped entirely** if generation
  fails mid-stream (attributing sources to an incomplete answer would be
  misleading, not just incomplete).
- Generation temperature pinned to `0.1` (Ollama's default is ~0.8),
  added mid-phase after a demonstrated real problem — see Section 6.
- `PHASE5_VERIFY.md` — a manual verification guide with six required test
  cases, written before implementation and used, unmodified in intent,
  to actually catch the regressions documented below.

---

## 3. Files Created or Changed

| Path | Status | Purpose |
|---|---|---|
| `app/generation/__init__.py` | Created | Makes `generation` a package |
| `app/generation/context.py` | Created | `fetch_structured_fields()`, `fetch_references()` — Postgres lookups OpenSearch can't serve |
| `app/generation/prompt.py` | Created | `SYSTEM_PROMPT`, `format_doc_context()`, `build_messages()` |
| `app/generation/llm.py` | Created | `stream_chat()` — Ollama `/api/chat` streaming client, `GENERATION_TEMPERATURE` |
| `app/routers/generate.py` | Created | `POST /generate` endpoint |
| `app/main.py` | Changed | 2-line addition: import + `include_router(generate_router)`. Confirmed via diff — no other line touched. |
| `app/search/queries.py` | Changed | `build_knn_query()`'s filtered branch only — see Section 6, bug 2. `build_search_query()`, `build_filters()`, `reciprocal_rank_fusion()` confirmed byte-identical to pre-Phase-5 via diff. |
| `PHASE5_VERIFY.md` | Created | Manual verification guide, repo root |

**Not created or changed:** `app/routers/search.py`, `app/search/service.py`, `app/search/mapping.py`, `app/search/indexer.py`, `schema.sql`, `ingest.py`, `docker-compose.yml`. `/search` and `/search/hybrid` confirmed unchanged both by file diff and by live output matching `phase3_query_results_TEMPLATE.md` exactly during verification.

---

## 4. Design Decisions and Why

- **`POST /generate`, not `GET`.** Unlike `/search`'s short filter-style
  params, `question` is free-text and can be long — a JSON body is the
  right shape, not a query string.
- **`hybrid_search()` called as a direct function import, not a
  self-HTTP round-trip.** Calling your own server's HTTP endpoint from
  inside the same process adds serialization overhead and a new failure
  mode (the server being unreachable from itself) for no benefit over a
  plain function call.
- **Retrieve at `size=10`, use top 5 in the prompt** — not a default,
  justified against real numbers: `eval_seed.json`'s largest gold set is
  3 documents (`error_code_aggregation`), and Phase 4's own Recall@5 eval
  was measured by calling the endpoint at `size=10` and slicing the top 5
  afterward (`eval/run_eval.py` hard-codes `size=10`). Requesting
  `size=5` directly from `hybrid_search()` would have replayed a
  *different*, narrower fusion than the one actually benchmarked — see
  Section 6 for how close this came to mattering in practice (Section 6,
  finding re: `BUG-1003` ranking last of 5 via single-leg-only credit).
- **Doc-type-aware structured context, not raw `cleaned_text`.** Bug
  reports and test cases have deliberately different field shapes
  (Phase 2's class-table inheritance exists specifically because they
  don't share a shape) — collapsing them back into one blob for the LLM
  would discard exactly the structure Phase 2 was built to preserve.
- **Resolved cross-references surfaced explicitly, not left to the LLM
  to infer from prose.** Added mid-phase, not part of the original
  design — see Section 6 for the concrete failure that motivated this.
  The principle: if a classification is already resolved deterministically
  somewhere in the pipeline (Phase 2's `document_references`), a
  downstream LLM should be handed that resolution, not asked to
  re-derive it from the same ambiguous text that made a separate,
  explicit resolution step necessary in the first place.
- **Post-filter k-NN (`bool.must` + `filter`), not native `knn.filter`.**
  `mapping.py` configures `chunk_vector` with the `nmslib` engine, which
  does not support engine-level filtering — confirmed via a real 400
  error (Section 6, bug 2), not discovered by inspection. Trade-off
  stated explicitly in the code: post-filtering is not ANN-candidate-
  aware, so a highly selective filter combined with a small corpus could
  in principle under-return; irrelevant at 28 documents, the first thing
  to revisit if the corpus grows.
- **Temperature pinned to 0.1, not left at Ollama's default.** A grounded
  QA system's job is consistency, not creative variety — the default
  (~0.8) is tuned for the opposite. Not set to exactly `0.0`, to avoid
  some models' tendency to degrade into repetition loops at zero.
- **Sources block emitted after the streamed answer, and only on
  success.** Before-stream sources would imply confidence in an answer
  that hasn't been generated yet; sources on a failed generation would
  misattribute an incomplete or absent answer to real documents.

---

## 5. What Was NOT Done (deliberately out of scope, per original phase brief)

Redis, Langfuse, LangGraph, Airflow, Slack, Telegram — all correctly
Phase 6+/7 territory. No automated eval harness for generation quality
(deliberately deferred to Phase 6) — all verification in this phase is
manual, real, and documented, not a substitute for `eval/run_eval.py`-style
scoring.

---

## 6. Bugs Found and Fixed During Verification (real, from actual runs against the live stack)

**1. RRF candidate-pool narrowing (design-stage finding, not a live bug):**
identified and designed around before implementation — see Section 4.
Confirmed relevant in practice during Step 2 verification: the single
correct document for a `single_doc_factual` question (`BUG-1003`) ranked
**last** of 5 returned sources, with `rrf_score=0.01639 (=1/61)` versus
`~0.032` for unrelated documents — meaning it scored via the BM25 leg's
exact-ID boost only and did not appear in the k-NN leg's top-10 at all,
while several topically-unrelated documents outranked it by appearing
weakly on both legs. The answer was still correct because `CONTEXT_TOP_N=5`
caught it — but it was one slot from not. **Not fixed in this phase** —
a real, now-demonstrated (not theoretical) instance of the RRF limitation
Phase 4 already flagged as a possibility. Flagged for Phase 6 as a
retrieval-quality finding worth quantifying with real eval numbers, not
fixed by guesswork here.

**2. Filtered `/generate` calls crashed with a real 500.** Confirmed via
container logs:
```
opensearchpy.exceptions.RequestError: RequestError(400,
'search_phase_execution_exception',
'failed to create query: Engine [NMSLIB] does not support filters')
```
Root cause: `build_knn_query()`'s original design (native `knn.filter`)
requires the `lucene` or `faiss` k-NN engine; `mapping.py` configures
`nmslib`. This code path existed since Phase 4 but was never exercised
with `doc_type`+`module`+`status` filters combined with the k-NN leg until
`/generate` became the first caller to do so — a real, previously-latent
Phase 4 defect, not something Phase 5 introduced. **Fixed**: post-filter
`bool` query (`filter` + `must: [{knn: ...}]`) instead of native
`knn.filter`. Confirmed fixed via live re-run: filtered generation for
`doc_type=bug_report, module=Checkout, status=Open` returned exactly
`BUG-1009` and `BUG-1018`, matching `phase3_query_results_TEMPLATE.md`'s
query 5 exactly.

**3. Rule 2 boilerplate leak with an unfilled placeholder.** First version
of Rule 2 used the literal template `"...for [bug ID]."` in its own
example text. Confirmed via a real run: asked a duplicate-classification
question (not about resolution at all), the model prepended *"The
retrieved documents do not state a resolution for [bug ID]."* verbatim,
with the placeholder never substituted. **Fixed**: Rule 2 reworded to
scope the disclaimer to resolution-type questions only, with an explicit
instruction to substitute the real document ID and never emit a
placeholder. Confirmed fixed — no recurrence in any of the ~10 subsequent
real runs of duplicate/related questions in this phase.

**4. Duplicate/related classification was genuinely non-deterministic and
frequently wrong.** The same `BUG-1002` duplicate-classification question,
run four times with identical retrieval and an unchanged prompt, produced
four different answers ("not stated," "not stated but hedges," "related
to," "not stated but affects Login") — only one of four correct. Root
cause, confirmed by inspection of the actual source text: `BUG-1002`'s
description ("Might be same root cause as BUG-1001, filed separately") is
genuinely ambiguous prose that a human could defensibly read as either
"related" or "duplicate" — but Phase 2's `resolve_explicit_bug_mentions()`
had already resolved this deterministically into `document_references` as
`duplicate_of`. The original Phase 5 design never surfaced that resolved
graph to the LLM, so it was being asked to re-solve, from ambiguous text
alone, a classification problem the pipeline had already solved. A
secondary, compounding factor: Ollama's default temperature (~0.8) added
sampling noise on top of the missing information. **Fixed, two parts**:
(a) `fetch_references()` added to `context.py`, surfacing the resolved
`document_references` graph as explicit `"Recorded cross-references"`
lines; Rule 3 rewritten to require reporting a recorded reference verbatim
rather than re-inferring from prose. (b) `GENERATION_TEMPERATURE=0.1`
pinned in `llm.py`. **Confirmed fixed** via 5 consecutive re-runs of the
`BUG-1002` question (all correct, all consistent: "BUG-1002 is a duplicate
of BUG-1001, Login module") and 3 consecutive re-runs of the corresponding
`related_to` case (`BUG-1017`/`BUG-1007`, all correct, all consistent,
correctly NOT called a duplicate).

**5. Doubled "of" in the reference line template.** A direct consequence
of fix 4: the first version of `fetch_references()`'s line template was
`f"{source_ext} is recorded as {ref_type} of {target_label}"`, and
`reference_type` values already contain "of"/"to" (`duplicate_of`,
`related_to`) — producing grammatically broken lines like *"BUG-1002 is
recorded as duplicate_of of BUG-1001."* Confirmed via real output: the
model faithfully reproduced the malformed phrase verbatim in its answer,
which is actually the "report it verbatim" instruction working correctly
on bad input, not a new model failure. **Fixed**: `REFERENCE_TYPE_PHRASING`
mapping (`duplicate_of` → `"is a duplicate of"`, `related_to` →
`"is related to (NOT a duplicate of)"`, `test_case_citation` → `"cites"`).
Confirmed fixed via 2 consecutive re-runs of `BUG-1002` post-fix — clean
phrasing, no recurrence.

**6. Cosmetic: context block header metadata bleeding into an answer.**
Asked for `BUG-1003`'s reproduction steps, the model's final step read
*"...ERR_504_TIMEOUT (doc_type=bug_report, module=Checkout,
status=In Progress)"* — it fused the context block's header suffix onto
the last line of the steps content. Content was still factually correct.
**Not fixed in this phase** — flagged as a real but low-severity finding
for Phase 6/a future formatting pass on `format_doc_context()`'s
header/body separation.

---

## 7. What Was Verified (real commands, real output — not assumed)

- `GET /search` unchanged: matches `phase3_query_results_TEMPLATE.md`
  query 1 exactly, real curl output.
- `GET /search/hybrid` and `build_search_query()`/`build_filters()`/
  `reciprocal_rank_fusion()` confirmed byte-identical to pre-Phase-5 via
  file diff, not just "should be unaffected."
- `POST /generate` route registration, request validation (422 on missing
  `question`), and connection-error propagation (not a silent crash)
  confirmed in a sandbox without live Docker services, before any live
  testing began.
- Streaming confirmed real (not buffered) via `curl -N`.
- `BUG-1003` single-doc factual: correct content, real output, one
  demonstrated retrieval-ranking finding (Section 6, bug 1).
- `BUG-1013` no-documented-resolution abstention: correct, single real run
  (not re-verified after later fixes — Rule 2's wording changed but its
  behavior on this exact case was not re-tested; low risk, since the fix
  narrowed Rule 2's scope rather than altering its abstention logic, but
  stated as unverified rather than assumed).
- `BUG-1002` duplicate classification: **11 real runs total** across the
  debugging cycle (4 pre-fix, inconsistent and mostly wrong; 5 post
  references+temperature fix, consistent and correct; 2 post phrasing
  fix, consistent, correct, and grammatically clean).
- `BUG-1017` related-not-duplicate classification: **4 real runs total**
  (1 pre-fix, correct by luck of unambiguous source phrasing; 3 post-fix,
  consistent and correct, confirming the duplicate-classification fix did
  not overcorrect into false-positive duplicates).
- Filtered generation (`doc_type=bug_report, module=Checkout,
  status=Open`): failed with a real 500 pre-fix (Section 6, bug 2),
  succeeded and matched expected output exactly post-fix.
- Out-of-scope question ("capital of France"): correct abstention, single
  real run, not re-tested after later fixes (Rule 1/Rule 4 were not
  touched by any fix in this phase, so risk is low, but stated as
  unverified-after-change rather than assumed still-correct).

**Not verified / explicitly deferred:**
- No automated eval harness run against generation (Phase 6 scope).
- `test_case_citation` reference phrasing (the "cites" branch of
  `REFERENCE_TYPE_PHRASING`) was implemented and unit-checked for correct
  string output, but never exercised end-to-end through a live
  `/generate` call in this phase — e.g., a question touching `BUG-1013`'s
  dangling citation of the never-ingested `TC-0209`. Worth a real test in
  Phase 6, not assumed working.
- Temperature=0.1's effect was validated via 5+3 consecutive same-question
  runs on two questions, not a broader sweep across `eval_seed.json`'s
  full 12 questions or multiple temperature values.

---

## 8. Known Issues, Deferred Work

- RRF single-leg-only ranking (Section 6, bug 1) — real, demonstrated,
  not fixed. First candidate for Phase 6's retrieval-quality eval work.
- Context block header/body formatting bleed (Section 6, bug 6) —
  cosmetic, not fixed.
- Stray duplicate `search.py` at the repo root (flagged in the Phase 5
  readiness report, confirmed byte-identical to `app/routers/search.py`,
  not imported by anything) — still present, still dead code, still a
  drift risk if one copy is ever edited and not the other. Not deleted
  in this phase; trivial cleanup for whenever someone next touches
  routing.
- `test_case_citation` phrasing unverified end-to-end (Section 7).
- No generation-quality eval harness — deliberately Phase 6 scope from
  the original phase brief, not an oversight here.
- `GENERATION_TEMPERATURE=0.1` is a stated, reasoned default, not
  empirically tuned against a broader question set — legitimate Phase 6
  finding if it needs adjustment, not something to silently change later
  based on a handful of spot-checks.

---

## 9. Conflicts With Phase 1–4

**None structural.** `/search` and `/search/hybrid` confirmed unchanged
both by direct file diff and by live output. `app/search/queries.py`'s
only change is to `build_knn_query()`'s filtered branch, which errored
with a hard 400 before this fix — nothing that previously worked was
altered; a previously-broken, previously-untested code path was fixed.
`app/main.py`'s only change is the 2-line router registration, confirmed
via diff against the Phase 4 version.

---

## 10. Files Phase 6 Should Inspect First

1. `app/generation/prompt.py` — `SYSTEM_PROMPT` and `format_doc_context()`,
   the grounding logic Phase 6's eval work needs to score against, and
   the concrete evidence (Section 6, bugs 3–5) of what happens when
   grounding wording is underspecified.
2. `app/generation/context.py` — `fetch_structured_fields()` and
   `fetch_references()`, particularly the latter, since it's the fix for
   the single largest correctness gap found in this phase and Phase 6's
   eval harness will likely want to score against the same resolved
   `document_references` data this function already surfaces.
3. `app/routers/generate.py` — `RETRIEVAL_SIZE`/`CONTEXT_TOP_N` constants
   and the endpoint contract Phase 6's eval harness will call against.
4. `app/generation/llm.py` — `GENERATION_TEMPERATURE`; Phase 6 may want
   to validate this value against a broader question set than the two
   spot-checked here.
5. `eval/eval_seed.json` — reuse as the Phase 6 question set, especially
   `duplicate_resolution`/`related_resolution` types now that the
   grounding gap that made those specifically fragile has real fix
   evidence behind it, not just a design intention.
6. `PHASE5_VERIFY.md` and this document's Section 6/7 — the full real-run
   history (11 runs on one question, 4 on another) is the closest thing
   this phase has to an eval trace; a starting point for what to
   formalize into `run_eval.py`-style automated scoring, not a
   substitute for building it.
7. Where anything here conflicts with the live repo, the repo is correct
   — this is a summary of what was verified, not a replacement for
   reading the actual files.
