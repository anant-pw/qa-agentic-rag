# Phase 6 Handoff — Evaluation, Observability, Caching, and Model Selection

## 1. Completion Status: **Complete**

Every claim below is backed by real command output produced in this phase's
chat history -- eval seed expansion verified against live Postgres, Tier A
deterministic generation checks run against a live `/generate` endpoint,
Tier B manual verdicts logged with real answer text, and the final model
decision backed by a genuine four-model comparison with real latency data
under real (not synthetic) concurrent load. Several claims made *during*
this phase turned out to be wrong once more evidence arrived (see Section
7) -- they are documented as corrected, not quietly dropped, because the
correction process is itself real evidence of how this project's discipline
is supposed to work.

---

## 2. What Was Implemented

- **Eval seed expansion**: `eval/eval_seed.json` grown from 12 to 17
  questions via `generate_eval_seed.py`, all DB-grounded (no hand-authored
  filler), adding two new question types the original 12 never covered:
  `dangling_reference_citation` (BUG-1013's citation of the never-ingested
  TC-0209) and `resolution_in_narrative` (BUG-1017's fix, present only as
  unlabeled prose in `description`, not a dedicated field). Also added a
  second, tie-free `structured_filter` question and a second standalone
  doc for both `single_doc_factual` bug and test-case coverage.
- **Tier A deterministic generation scoring**: `eval/run_eval_generation.py`,
  querying `document_references`/`error_codes` directly for 7 of the 17
  question types -- same source-of-truth tables `app/generation/context.py`
  reads from in production, so eval and generation share one ground truth
  rather than two independently-fallible ones. `single_doc_factual`
  questions are explicitly marked `MANUAL_REQUIRED`, never guessed at.
- **Tier B manual grading log**: `eval/manual_eval_log.py`, append-only,
  built specifically because `PHASE5_VERIFY.md` turned out to be a
  verification *script* with no real output ever captured in it -- the
  "11 real runs" phase-05-handoff.md describes existed only as prose
  summary in a different chat, not a reusable record. This phase's Tier B
  entries are the first real, structured record of that kind in the repo.
- **LLM-as-judge**: explicitly rejected, not built. A judge model checking
  the generator's work has no more access to ground truth than the
  generator did -- a wrong verdict is a second unverified opinion, not an
  independent check.
- **Langfuse**: explicitly rejected, not built. Self-hosted Langfuse v3+ is
  a 6-container stack (web, worker, ClickHouse, Redis/Valkey, MinIO, its
  own Postgres) with an officially documented 16GB RAM recommendation --
  directly incompatible with this project's local-first constraint and,
  as this phase's hardware investigation later confirmed empirically, this
  machine's actual 16GB ceiling. Langfuse Cloud was equally rejected as a
  silent SaaS dependency the project brief never authorized.
- **Structured JSON-line observability**: `app/observability/logger.py`,
  one line per `/generate` call to `logs/generation.jsonl`, with retrieval
  span (doc IDs, scores, latency) and generation span (latency, token
  count, and -- after a real gap was found -- which model produced the
  answer). Chosen as the direct, load-bearing replacement for Langfuse.
- **Redis response cache**: `app/generation/cache.py`, keyed on
  `(question, doc_type, module, status, current concrete OpenSearch index
  name, model name)`. Invalidates automatically on index rebuild (key
  changes) rather than a wall-clock TTL, and on model swap (model name is
  part of the key) -- both added after real incidents this phase where a
  stale key would have hidden a real change. `no_cache` is a per-request
  field on `GenerateRequest`, not a global flag, specifically so an eval
  run can never silently score cached, non-representative output.
- **Model selection**: `phi4:14b` is the final production default,
  replacing `phi4-mini:latest`. Full comparison and reasoning in Section 6.

---

## 3. Files Created or Changed

| Path | Purpose |
|---|---|
| `eval/eval_seed.json` | 12 -> 17 questions, DB-grounded, verified against live Postgres |
| `generate_eval_seed.py` | 5 new query functions for the expanded question types |
| `eval/run_eval_generation.py` | Tier A deterministic scoring against live `/generate` |
| `eval/manual_eval_log.py` | Tier B append-only manual grading log |
| `app/observability/logger.py` | Structured JSON-line request logging (Langfuse replacement) |
| `app/generation/cache.py` | Redis response cache, index+model-aware key |
| `app/generation/llm.py` | `stream_chat()` gained `model`, `think`, `keep_alive` params; payload construction moved from `httpx` to `requests` (see Section 7) |
| `app/routers/generate.py` | Cache + logging wired in; streaming contract preserved on a miss |
| `app/config.py` | Added `redis_host`/`redis_port`, `ollama_chat_model`, `ollama_keep_alive` -- model and keep-alive are now one-line config changes, not code edits |
| `docker-compose.yml` | Added `redis` service; added `./logs:/app/logs` volume mount (see Section 7) |
| `requirements.txt` | Added `redis`, `requests` |
| `.env.example` | Added `REDIS_PORT`, `OLLAMA_CHAT_MODEL` |
| `app/generation/prompt.py` | Rules 2, 2b, 6 added; see Section 5 |
| `diagnostics/groq_probe.py` | New: standalone script reusing real retrieval/context/prompt code to A/B any two models on identical input -- the primary tool this phase's model investigation ran on |

---

## 4. Design Decisions and Why

- **Deterministic checks share ground-truth tables with production
  generation, not a separate eval-only query path.** `run_eval_generation.py`
  queries `document_references`/`error_codes` the same way
  `context.py`'s `fetch_references()` does. If the schema or the
  relationship data ever drifts, both break together and visibly, rather
  than the eval silently trusting stale assumptions.
- **Cache key includes the model name.** Without this, swapping
  `OLLAMA_CHAT_MODEL` would silently serve pre-swap cached answers from the
  old model until the index was separately rebuilt -- a real risk once
  this phase started comparing four different models against the same
  live cache.
- **`no_cache` is per-request, not global.** A global toggle is one
  forgotten flip away from an eval run scoring cached, non-representative
  output -- exactly the failure mode Phase 5's temperature-pinning
  decision was already trying to prevent for a different reason.
- **`keep_alive` is a per-request payload field in `llm.py`, not a host-level
  `OLLAMA_KEEP_ALIVE` environment variable.** Ollama runs natively on the
  host (not Docker), so a shell env var is fragile -- it resets on reboot,
  a fresh terminal, or a Windows update, and nothing in the repo would
  catch that it silently stopped applying. A per-request field is
  guaranteed every call and version-controlled.

---

## 5. Prompt Changes: Rules 2, 2b, 6 (`app/generation/prompt.py`)

Three real, evidence-driven changes, made in this order because each one's
evidence only existed after the previous fix was tested live:

1. **Original Rule 2** covered "no resolution documented" as a single,
   unscoped rule. Live testing showed it firing as a generic fallback for
   *any* uncertain question -- test-case citations, relationship
   questions, not just resolution questions.
2. **Rule 2 rescoped + Rule 2b added**, splitting "no fix documented" from
   "cited document not retrieved" into two distinct instructions.
   `diagnostics/groq_probe.py` (against Groq's `gpt-oss-120b`, same
   context/prompt held constant) confirmed both rules worked correctly on
   a larger model -- but a full live sweep against the actual production
   model (`phi4-mini` at the time) showed a **regression**: previously
   passing `cross_reference_bug_to_test_case` and `duplicate_resolution`
   questions started failing with the exact Rule 2 sentence, for questions
   that had nothing to do with resolutions.
3. **Root cause, confirmed not guessed**: the worked example added
   alongside the Rule 2/2b rewrite contained the literal negative phrase
   *"it does NOT say 'the retrieved documents do not state a
   resolution'"* -- the target sentence itself, negation or not, sitting
   directly in the prompt. A small model doing shallow pattern-matching
   can anchor on the salient string regardless of the negation wrapped
   around it. The example was rewritten to be purely positive (shows only
   correct output, never reproduces the sentence being suppressed). A
   follow-up sweep confirmed the regression was fully resolved.
4. **Rule 6 added** after a live sweep showed a separate, unrelated
   failure: `structured_filter` ("how many bugs are Open in Login")
   correctly retrieved all matching documents but the model answered "The
   retrieved documents do not address this question" (Rule 4's sentence)
   instead of counting what it was given. Rule 6 explicitly names counting
   /listing from provided documents as a valid way to answer a question.

**General, transferable lesson from this phase, worth keeping**: negative
few-shot examples are a real risk on small local models, not just a style
choice, and any prompt change needs to be tested against the *actual
deployed model*, not a stand-in -- a fix confirmed on a 120B model provided
zero signal about whether it would help or hurt a 3.8B one.

---

## 6. Model Selection: `phi4:14b` (replacing `phi4-mini:latest`)

### The investigation, honestly, including the wrong turns

Five models were tested against two of the hardest questions in the eval
set (BUG-1017's implicit resolution extraction, and a Login/Open
structured count) before a full 17-question sweep was run on the two
finalists:

| Model | Correct? | Notes |
|---|---|---|
| `phi4-mini:latest` (original default) | No | Confirmed real capacity ceiling -- three separate prompt rewrites (Section 5) never fixed it; a same-context, same-prompt comparison against Groq's `gpt-oss-120b` isolated the variable and confirmed model capacity, not prompt wording, was the cause |
| `llama3.1:8b` | **No** | Fastest of all local candidates (~50-61s) but silently miscounted a structured_filter question (said 3, ground truth was 4) -- disqualified regardless of speed; a fast wrong answer is worse than a slow right one for this tool |
| `qwen3.5:9b` | Yes | Correct, but 500-670s per answer -- `total_tokens` (1200-1580 for a ~50-word visible answer) revealed an unrequested "thinking mode" trace. Fixed via Ollama's `think:false` param (added to `stream_chat()` specifically for this), which cut token count 6-13x -- but `time_to_first_token_sec` stayed a consistent ~112s across separate warm invocations, not explained by cold-load. Likely cause: immature CPU kernel support for this model's hybrid Gated DeltaNet attention architecture in the current Ollama/llama.cpp build. Not pursued further -- correct, but slower than the eventual winner even after the fix. |
| `gpt-oss:20b` | Yes | Correct on isolated single-question tests (~64-80s warm) -- **this number was wrong as a production estimate.** A live full-stack sweep (Postgres + OpenSearch + Redis + fastapi + Ollama all running concurrently, as production requires) showed latency ranging 90s to **2672s** for the same question type at different points in the same run. Root cause: `gpt-oss:20b` alone needs ~16GB RAM on a 16GB-total machine: the full Docker stack has nowhere to go but disk swap. Confirmed directly by stopping `fastapi`+`redis` (headroom-only test): latency dropped from 289s to 6.1s TTFT for the identical question -- but `fastapi` *is* `/generate`, so that configuration can never be the deployed one. GPU (Iris Xe) showed zero utilization throughout, confirming CPU-only inference and ruling out Vulkan/GPU-offload as a fix, since integrated graphics shares the same contended RAM pool anyway. |
| **`phi4:14b`** | **Yes** | ~9GB footprint -- meaningfully more headroom than `gpt-oss:20b`'s ~16GB on this hardware. Full 17-question sweep, full stack running: **13/13 Tier A PASS**, latency range 72.7s-304.9s (mean ~168s). `gpt-oss:20b`'s *average* latency (~357s) was worse than `phi4:14b`'s *worst case* (304.9s). Same correctness, dramatically more consistent and usable under real concurrent load. |

### The actual finding, not just the model name

The dominant cost on this hardware was never model architecture or
prompt-following ability once a model cleared the correctness bar -- it
was **total system RAM versus concurrent load**. `qwen3.5:9b`'s slowness
was architectural (kernel maturity); `gpt-oss:20b`'s was almost entirely
memory contention. A model's footprint relative to available headroom,
not its parameter count or benchmark reputation, was the variable that
actually predicted real-world latency here. This is worth keeping as a
general principle for any future model swap on this machine: test full
correctness first (`diagnostics/groq_probe.py`), but treat the *isolated*
latency number as a floor, not a production estimate, until confirmed
under the full concurrent stack.

### Final Tier A + Tier B results on `phi4:14b`

- **Tier A**: 13/13 deterministic PASS (4 `single_doc_factual` questions
  have no deterministic ground truth by design, routed to Tier B).
- **Tier B**: 4/4 manual PASS, verdicts logged 2026-09-10, content
  spot-checked against `data/bug_reports.csv` and `data/test_cases/*.md`
  directly, not assumed from the "pass" label alone. BUG-1003's answer in
  particular matched byte-for-byte across three different models tested
  this phase (`phi4-mini`, `gpt-oss:20b`, `phi4:14b`) -- strong
  corroboration that the retrieval/context layer itself is stable and the
  variation observed was purely in generation quality, not retrieval.

**Total: 17/17 questions resolved, 13 by deterministic check, 4 by
verified manual read.**

---

## 7. Real Bugs Found and Fixed This Phase (all confirmed via live output)

1. **`requirements.txt` missing newline** -- `opensearch-py==2.7.1` and a
   new `redis` line had silently concatenated into one broken requirement
   string. Caught before it ever reached a container build.
2. **Eval script never sent structured filters to `/generate`** --
   `call_generate()`'s original version only sent `{question, no_cache}`,
   so `structured_filter` questions ran through completely unfiltered
   retrieval. Root-caused via live sweep output, not assumed; fixed by
   parsing and forwarding `module`/`status` from the question text.
2. **Redis cache silently never fired for an entire test round** -- root
   cause was `docker compose up -d` without `--build`, so the container
   kept running Phase 5's image (no `redis` package, no new code).
   Pydantic silently ignoring the unrecognized `no_cache` field made this
   invisible until `redis-cli KEYS` came back empty and `import redis`
   failed inside the container directly.
3. **`stream_chat()` tuple-unpacking regression** -- an interface change
   (plain string yields -> `(content, done_chunk)` tuples) was applied to
   `diagnostics/groq_probe.py`'s call site but not to
   `app/routers/generate.py`'s, meaning **production `/generate` was
   producing corrupted or crashing output** independent of any prompt/model
   investigation happening at the same time. Found by reading the diff
   before assuming test results were trustworthy, not by observing a
   crash first.
4. **Negative few-shot example backfired** -- see Section 5, item 3.
5. **`logs/generation.jsonl` invisible on host** -- written to `/app/logs/`
   inside the container with no volume mount; fixed with `./logs:/app/logs`
   in `docker-compose.yml`.
6. **Observability log never recorded which model generated a response** --
   directly caused real confusion mid-investigation (a sweep's results and
   latencies were ambiguous about whether `gpt-oss:20b` or the still-default
   `phi4-mini` had actually produced them). Fixed by adding `model` to the
   generation span.
7. **Unicode hyphen false-negatives in Tier A checks** -- `gpt-oss` models
   (confirmed on both the Groq-hosted and local Ollama versions) render
   document IDs using U+2011 (non-breaking hyphen) rather than ASCII `-`.
   A live sweep on `gpt-oss:20b` showed 10 of 13 deterministic checks
   failing simultaneously; a `repr()`-based inspection was planned to
   confirm the theory, and the fix (`_normalize_dashes()`, applied before
   every ID-substring comparison) was applied proactively since it could
   only make matching more correct, never less. The subsequent re-run
   confirmed it: 13/13 PASS with zero other changes.
8. **`run_eval_generation.py`'s `MANUAL_REQUIRED` branch never called
   `/generate` at all** -- it labeled questions for manual review and
   `continue`d before fetching an answer, so `results.json` had no
   `answer` key for those rows and Tier B grading was structurally
   impossible until fixed.
9. **Manual eval log entries with placeholder/mismatched content** -- not
   a code bug, but a real data-quality issue caught by treating "N pass /
   0 fail" as a number to audit, not trust: one entry was submitted with
   literal `"..."` in both `question` and `answer`; another had its
   `--note` argument populated with the question text instead of a
   grading rationale; two entries initially cited the wrong source file
   (`bug_reports.csv` instead of the correct `TC-0301.md`/`TC-0302.md`) in
   their notes, later corrected after being asked to confirm the check was
   actually done against the right file.

---

## 8. Known Limitations (real, not hedged)

- **`manual_eval_log.py` does not tag entries by model.** The log now
  contains manual verdicts from both `gpt-oss:20b`'s and `phi4:14b`'s
  grading passes with no field distinguishing them -- this phase's Section
  6 numbers rely on the specific 2026-09-10 timestamped entries, not the
  full log contents. Worth adding a `model` field before the next phase
  does another manual grading round.
- **`qwen3.5:9b`'s consistent ~112s TTFT (even warm, even with `think:false`)
  is unexplained beyond a plausible hypothesis** (immature CPU kernel
  support for its hybrid attention architecture in the current Ollama
  build). Not investigated further since `phi4:14b` already won on both
  axes -- would need actual profiling tools this project doesn't have
  access to in order to confirm.
- **This machine's 16GB RAM ceiling is a hard, load-bearing constraint on
  any future model choice**, not just this phase's decision. Confirmed via
  direct Task Manager observation and a controlled before/after test
  (stopping `fastapi`+`redis` dropped `gpt-oss:20b`'s TTFT from 289s to
  6.1s). Any model at or above ~14-16GB footprint should be assumed
  unreliable under real concurrent load on this hardware without further
  testing, regardless of correctness in isolation.
- **`OLLAMA_KEEP_ALIVE` trade-off is real and unresolved by default**:
  set to `24h` currently, which pins `phi4:14b`'s ~9GB resident
  continuously. This is a deliberate choice to avoid repeated cold-load
  costs (100-270s+ observed), but it does mean less RAM is available for
  anything else running on this machine for the full 24 hours, not just
  during active use. A shorter value (`2h`-`4h`) would recover most of the
  practical benefit with less permanent RAM commitment -- left at `24h`
  per explicit instruction, not because it's clearly optimal.
- **GPU/Vulkan acceleration was ruled out, not merely skipped.** Iris Xe
  integrated graphics shares the same system RAM pool as the CPU -- it is
  not a separate fast memory pool the way discrete VRAM is, so offloading
  layers to it would not address the actual bottleneck (total RAM vs.
  concurrent load), only move where the contention happens.
- **`OPENSEARCH_JAVA_OPTS` heap reduction (512MB -> 256MB) was proposed but
  not applied or tested this phase** -- a small, free lever left for
  whoever next touches memory headroom on this machine.

---

## 9. Deliberately Deferred to Phase 7

Per the original phase brief and this phase's explicit constraints:
LangGraph, query rewriting, retrieval grading, guardrail nodes,
Slack/Telegram integration. None of this phase's work assumes or blocks
any particular Phase 7 architecture.

Additionally, **not** deferred by oversight but by explicit decision this
phase: no further model search beyond the four tested. `phi4:14b` is the
production default; revisiting model choice should require a specific new
hypothesis (e.g. a model release specifically optimized for this RAM
class), not an open-ended search resumed by default.

---

## 10. Files Phase 7 Should Inspect First

1. **This document** -- where anything here conflicts with the live repo,
   the repo is correct.
2. **`app/generation/prompt.py`** -- Rules 2/2b/6 and their docstring
   evidence trail; Phase 7's guardrail/grading nodes will sit adjacent to
   this logic and should not silently duplicate or conflict with it.
3. **`app/generation/cache.py` / `app/observability/logger.py`** -- any
   Phase 7 agentic step that calls `/generate` (or a new equivalent
   endpoint) should decide deliberately whether it goes through the
   existing cache/logging path or needs its own, not inherit it by
   accident.
4. **`eval/run_eval_generation.py` and `eval/eval_seed.json`** -- Phase 7's
   brief requires re-running the Phase 6 eval set through the agentic
   pipeline and proving improvement with real numbers, not assuming it.
   This is the baseline those numbers get compared against.
5. **Section 6 and Section 8 of this document** -- the RAM ceiling is a
   real constraint on any additional LLM calls Phase 7's agentic loop
   might introduce (e.g. a query-rewrite call before the main generation
   call would double concurrent memory pressure at exactly the wrong
   moment). Budget for this explicitly rather than discovering it the way
   this phase did.
