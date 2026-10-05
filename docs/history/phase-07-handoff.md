# Phase 7 Handoff — Agentic Layer (Guardrail, Router, Deterministic Shortcut)

## 1. Completion Status: **Complete — for the scope actually built, not the scope originally proposed**

The original Phase 7 brief specified four LangGraph nodes: guardrail, retrieve,
grade-and-retry, generate. Three were built and verified with real, live
output. The fourth (query rewrite + retry on weak retrieval) was
deliberately **not built** — not deferred by default, but ruled out by
real evidence gathered during this phase (Section 4). If that evidence
changes later, this decision should be revisited on its own merits, not
silently reversed.

Every claim below was checked against real command output in this
phase's chat, not assumed from how the code reads. Where something was
found broken, it's recorded as found-and-fixed with the real numbers on
both sides, not smoothed over.

---

## 2. What Was Implemented

- **`app/generation/guardrail.py`** — rejects out-of-domain questions
  using `vector_score` (raw cosine similarity from the embedding leg of
  retrieval), with an explicit-filter bypass for structured queries. Not
  an LLM call — reuses the score `hybrid_search()` already computes.
- **`app/generation/deterministic.py`** — answers count/list questions
  ("how many bugs are Open in X") directly from `search()`, zero LLM
  calls. Gated by `settings.deterministic_count_routing`.
- **`app/routers/generate_agentic.py`** — new endpoint, `POST
  /generate/agentic`. A 3-node LangGraph (`guardrail_and_route` →
  `deterministic` / `semantic_generate`). Retrieval runs once, in the
  first node; every downstream node reuses it. `/generate` itself is
  untouched — not imported, not modified, contract preserved exactly
  per the original brief's constraint.
- **`diagnostics/rrf_threshold_probe.py`** — new diagnostic script, run
  live against the stack four separate times as the guardrail signal
  was iterated. Not production code; kept as a reusable calibration
  tool for future threshold changes.
- **`eval/eval_seed.json`** — 3 new `out_of_domain` questions added
  (weather, capital of France, baking a cake), `requires_docs: []`.
- **`eval/run_eval.py`** — excludes `out_of_domain` from Recall/MRR
  scoring (same treatment as `structured_filter`, same reason: no
  gold-document set to score against).
- **`eval/run_eval_generation.py`** — added `--endpoint-path` CLI flag
  (default `/generate`, so existing behavior is unchanged unless
  overridden) and a new `check_out_of_domain()` checker.
- **`app/generation/prompt.py`** — Rule 5 rewritten to state the exact
  required citation format (`(Source: BUG-XXXX)`), which was previously
  only implied once, incidentally, inside Rule 2's example. A second,
  purely positive worked example added for list-shaped answers — no
  negative example used, per Phase 6's own already-documented finding
  that negative examples measurably backfire on this model.
- **`requirements.txt`** — added `langgraph`.
- **`app/main.py`** — registered the new router, same pattern as every
  existing endpoint.
- **`app/config.py`** — added `deterministic_count_routing: bool = True`
  (default flipped to `True` after real A/B evidence, see Section 4) and
  `vector_score_guardrail_threshold: float = 0.75`.
- **`docker-compose.yml`** — added both new settings to the `fastapi`
  service's `environment:` block (see Section 6, bug #5 — this was
  missing and silently broke the first attempt to turn on deterministic
  routing).
- **`eval/manual_eval_log_agentic.jsonl`** — new, separate Tier B log
  file (via `MANUAL_EVAL_LOG_PATH`), kept deliberately apart from Phase
  6's original `/generate` log so the two baselines don't conflate in
  one undifferentiated file.

---

## 3. Design Decisions and Why

**Guardrail signal: three real candidates tested, two rejected on real
data, not on inspection.**

| Signal | In-domain (rich) | In-domain (paraphrased) | Out-of-domain | Verdict |
|---|---|---|---|---|
| `rrf_score` | 0.0325–0.0328 | — | 0.0313–0.0328 | **Rejected** — overlapping, no usable gap |
| `bm25_score` | 50.3–54.5 | 3.4–9.0 | 0.5–4.8 | **Rejected** — paraphrased real questions scored *inside* the fake-question range |
| `vector_score` | 0.814–0.822 | 0.789–0.832 | 0.653–0.706 | **Shipped** — clean separation across every real test |

`rrf_score` failed because RRF fuses on *rank*, not *magnitude* — in a
28-document corpus, something always ranks #1 regardless of true
relevance, so a confident match and a garbage match look identical to
it. `bm25_score` failed because it only rewards literal shared
vocabulary — a real customer's casually-phrased question scored *lower*
than a fake question about quantum physics. `vector_score` (cosine
similarity) was the only signal that actually separated meaning, not
rank or wording, cleanly enough to threshold on.

**Threshold set at 0.75**, the midpoint of the closest real pair
observed (0.7063 out-of-domain vs. 0.7890 in-domain-paraphrased) —
calibrated from real probe data, not a round-number guess.

**Real regression found and fixed: structured/filtered questions
false-rejected.** Both `structured_filter` eval questions are generic,
templated ("How many bugs are Open in the X module?") — they don't
closely resemble any single document's specific content in embedding
space, regardless of which real module they name. Real numbers: Login
scored 0.7502 (technically above threshold, by 0.0002 — luck, not
margin); Payments scored 0.7207 (a real, confirmed FAIL in a live 20-
question eval run, where the baseline `/generate` had PASSED). Fix:
when the caller supplies an explicit `doc_type`/`module`/`status`
filter, the guardrail trusts that filter as the domain signal instead
of re-deriving domain membership from a signal proven unreliable for
this question shape. Only rejects a filtered query on zero hits.
**Documented, accepted trade-off, not hidden**: a nonsense question
paired with a real filter value would not be caught by this guardrail.
Accepted because neither tested signal actually distinguished that case
either, and `doc_type`/`module`/`status` are structured API parameters
in this system's real usage, not typically attacker-controlled text.

**Query rewrite/retry node: not built, on real evidence, not by
default.** `eval/run_eval.py --endpoint /search/hybrid` showed
Recall@5 = 100%, MRR = 1.000 on the real 15-question scored set — no
measured retrieval failure to fix. A targeted stress test (3
deliberately-reworded real questions, zero shared vocabulary with the
source documents, run through the live stack) still retrieved the
correct top document 3/3 times. No evidence exists that this node would
do anything. Building it now would be complexity without a demonstrated
problem — the opposite of this project's stated discipline. If future
real usage surfaces an actual retrieval failure, that's new evidence
and this decision should be revisited then, not before.

**Deterministic routing kept as a real, tested A/B, not assumed.**
Before this phase's real numbers, Rule 6 (LLM-based counting) already
passed both `structured_filter` questions — so the shortcut's value was
genuinely unproven until measured. Once measured (Section 5), the
latency delta was large enough (98.5% reduction, zero correctness
change) to justify flipping the default to `True` rather than leaving
it opt-in indefinitely.

**Wire contract deliberately mirrors `/generate`.** Same
`StreamingResponse` shape, same `---SOURCES---` marker — specifically so
`eval/run_eval_generation.py` needed only a `--endpoint-path` flag, not
a rewrite, to run the identical 20-question eval set against both
endpoints for a real, apples-to-apples comparison.

**Caching: semantic-path output only, same as `/generate`.** Guardrail
rejects and deterministic answers are never cached — both are already
free, and caching either risks masking a future threshold or logic
change behind a stale cached decision.

---

## 4. Retrieval and Generation Comparison — Real Numbers, Both Sides

| | Baseline `/generate` | `/generate/agentic` |
|---|---|---|
| Retrieval Recall@5 / MRR | 100% / 1.000 | unchanged — same retrieval code, not modified |
| Tier A (deterministic) | 13 PASS / 3 FAIL / 4 MANUAL (of 20 — 3 FAILs are the out-of-domain questions; `/generate` has no mechanism to reject them, by design) | 16 PASS / 0 FAIL / 4 MANUAL (of 20) |
| Tier B (manual) | 4/4 PASS (Phase 6 baseline, unchanged) | 8/8 PASS (4 original + 4 re-confirmed after the citation-format fix) |
| Out-of-domain handling | Structurally impossible; full retrieval + generation cost paid regardless (avg. ~84s/question) | Correctly rejected, avg. ~0.4s/question — **99.2% reduction** |
| Structured-filter questions | ~118s avg via Rule 6 (LLM) | ~0.4s avg via deterministic shortcut — **98.5% reduction** |
| Overhead on ordinary questions | — | ~1%, within normal run-to-run noise |

This is the real, measured answer to the original brief's "don't assume
agentic is better, prove it on your own eval numbers."

---

## 5. Bugs Found and Fixed This Phase (all confirmed via live output)

1. **`rrf_score` as guardrail signal** — real, confirmed failure via
   `diagnostics/rrf_threshold_probe.py`; never shipped.
2. **`bm25_score` as guardrail signal** — same script, real, confirmed
   failure; never shipped.
3. **Structured-filter false-reject (Payments module)** — a real
   production regression, caught by a full 20-question eval sweep
   (FAIL where the baseline had PASS), not by inspection. Root-caused
   to templated questions scoring systematically lower on
   `vector_score`; fixed via explicit-filter trust (Section 3).
4. **Postgres connection failure mid-eval** — root-caused to a real
   ~12.5 hour Docker Desktop/WSL2 sleep gap; WAL auto-recovery
   completed cleanly on its own; no code fix, environmental quirk noted
   for future reference (Postgres needs a moment after a wake-from-sleep
   even though `docker compose ps` reports healthy immediately).
5. **`docker-compose.yml` environment whitelist gap** —
   `DETERMINISTIC_COUNT_ROUTING` and `VECTOR_SCORE_GUARDRAIL_THRESHOLD`
   were set in `.env` but never forwarded into the `fastapi` container,
   because the compose file's `environment:` block is an explicit
   per-variable whitelist, not `env_file: .env`. Confirmed via `docker
   exec rag-fastapi env`. Fixed by adding both variables explicitly,
   with safe defaults. Same failure class as the `no_cache` bug logged
   in an earlier phase — flagged as a recurring pattern worth a
   standing checklist item: *any new config variable needs three
   places updated, not two* (`.env.example`, `app/config.py`, **and**
   `docker-compose.yml`'s environment block).
6. **Stale-cache false-negative during manual verification** — after
   fixing bug #5, a repeated manual `curl` (without `no_cache: true`)
   returned a stale cached semantic-path answer from before the fix,
   making the fix look like it hadn't worked. Root-caused by noticing
   the response was byte-for-byte identical to the pre-fix output.
   Resolved by re-testing with `no_cache: true` explicit — the same
   discipline `run_eval_generation.py` already enforces on every call,
   which manual spot-checks don't get for free.
7. **Citation-format defect (TC-0302)** — found via Tier B manual
   grading, not Tier A (automated checks don't verify citation format
   at all). `(Document ID: TC-0302)` instead of the `(Source: X)`
   convention every other answer used. Root-caused to Rule 5 never
   actually specifying the required format — it was only implied once,
   incidentally, inside an unrelated Rule 2 example. Fixed with an
   explicit format spec plus a new, purely positive worked example
   (no negative example used, respecting Phase 6's own documented
   finding that negative examples measurably backfire on this model).
   Full 20-question regression sweep confirmed clean afterward; all 4
   Tier B questions re-verified against real Postgres fields post-fix.

---

## 6. What Was Verified (real commands, real output, this phase)

- Guardrail signal calibration: 4 separate live probe runs against the
  real stack (`diagnostics/rrf_threshold_probe.py`), 12 total real data
  points across in-domain, paraphrased, out-of-domain, and
  filtered/structured question shapes.
- Full 20-question Tier A sweep run against both `/generate` and
  `/generate/agentic`, twice (once pre-fix showing the real Payments
  regression, once post-fix showing it resolved) — real transcripts on
  record in this conversation and in `run_eval_generation_results.json`.
- Tier B: all 4 `single_doc_factual` questions manually graded against
  real Postgres field content (joined via `documents` ↔
  `bug_reports`/`test_cases`, class-table inheritance), re-verified a
  second time after the citation-format fix.
- Deterministic routing: config wiring confirmed live via `docker exec
  ... env`, both structured-filter questions confirmed answering
  correctly via the shortcut with real latency numbers on both sides of
  the fix.
- Out-of-domain rejection: proven inside the actual eval framework
  (`eval_seed.json` + `run_eval_generation.py`), not just ad-hoc
  `curl` checks — deliberately including a run against the baseline
  `/generate` to show the 3 FAILs there are structural, not a fluke.

---

## 7. Known Limitations (real, not hedged)

- **No true token-by-token streaming on `/generate/agentic`.** The
  wire contract matches `/generate`, but the full answer is accumulated
  inside the LangGraph node before being sent as one chunk, not streamed
  as it's generated. Deliberate v1 scope cut — wiring `graph.stream()`
  instead of `graph.invoke()` is real, separate work, intentionally not
  done before the routing logic itself was verified correct.
- **Guardrail's filter-trust bypass has a real, accepted gap**: a
  nonsense question paired with a valid filter value is not caught.
- **`is_countable_question()` is a small keyword-regex heuristic, not
  NLU.** Untested rephrasings of "how many"/"list all" could miss.
  Extend only when a real miss is observed, not speculatively.
- **`vector_score_guardrail_threshold` (0.75) is calibrated from 12
  real data points** — a real, evidence-based number, but a small
  sample. Worth widening if real production traffic surfaces edge cases
  this eval set didn't cover.
- **`_get_pg_connection()` is duplicated** between `generate.py` and
  `generate_agentic.py` (8 lines, underscore-private in both). Same
  class of accepted technical debt as the ID-extraction regex
  duplication already flagged in earlier phases. Worth a shared
  `app/db.py` if a third caller ever needs it.

---

## 8. Deliberately Deferred, Not Forgotten

- Query rewrite/retry node — see Section 3's reasoning. Revisit only on
  new evidence of real retrieval failure.
- True token-by-token streaming for `/generate/agentic`.
- Nothing about Phase 8's scope has been defined in any chat to date.

---

## 9. Conflicts With Earlier Phases

None structural. `/generate`'s contract was preserved exactly throughout
— never imported, never modified by any file in this phase.
`hybrid_search()`, `stream_chat()`, `fetch_structured_fields()`,
`fetch_references()`, `build_messages()`, and `format_doc_context()` are
all reused unmodified except for the Rule 5 citation-format fix in
`prompt.py`, which was regression-tested against the full existing eval
set and confirmed to not disturb any previously-passing question.

---

## 10. Files a Phase 8 Chat Should Inspect First

1. `app/routers/generate_agentic.py` — the actual pipeline; read its
   module docstring first, it explains the wire-contract and
   non-streaming trade-offs inline.
2. `app/generation/guardrail.py` — the guardrail signal history is in
   its docstring; don't re-litigate `rrf_score`/`bm25_score` without
   reading why they were already rejected with real numbers.
3. `app/generation/deterministic.py` — the corpus-size-ceiling
   correctness trap it avoids is explained inline; don't reintroduce it
   if this logic is ever touched.
4. `eval/eval_seed.json` and `eval/manual_eval_log_agentic.jsonl` — the
   real, current eval set and Tier B record this phase's numbers came
   from.
5. `diagnostics/rrf_threshold_probe.py` — reusable if the threshold
   ever needs recalibrating against new real data.
6. This document — where anything here conflicts with the live repo,
   the repo is correct; this is a summary, not a replacement for reading
   the files directly.
