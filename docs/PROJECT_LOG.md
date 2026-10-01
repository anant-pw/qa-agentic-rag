# qa-agentic-rag — Consolidated Project Log (Phases 1–7 + post-Phase-7 hardening)

**Snapshot date:** 2026-10-01
**Repo snapshot inspected:** `anant-pw/qa-agentic-rag` @ commit `f5922fe` ("phase 7 updates", 2026-09-28)
**Purpose of this file:** a single entry point for a Claude Code session to (a) understand what exists and what was actually verified, (b) audit weaknesses, and (c) plan the next work package. It summarizes the seven `phase-0X-handoff.md` files in the repo root plus post-Phase-7 work reported in chat sessions.

**Precedence rule:** the repo files beat this document. The phase handoffs beat this document on anything phase-specific. Anything tagged `[chat-reported]` below was reported in a chat session and is NOT in any handoff doc — verify it in the code before relying on it.

**Evidence tags used in this file**
- `[handoff]` — stated as verified with real output in a phase handoff doc
- `[repo]` — confirmed by directly inspecting the repo at `f5922fe`
- `[chat-reported]` — reported in a post-Phase-7 chat; not captured in a handoff
- `[unverified]` — claimed or planned, no evidence of a real run

---

## 0. Instructions for Claude Code (read first)

1. Read this file, then `phase-07-handoff.md`, then the files it lists in its Section 10. Do not re-read all seven handoffs unless a specific question needs one.
2. The project owner's non-negotiables: explain *why* before code; state trade-offs; never invent commands, numbers, or results; the owner runs commands and pastes real output, conclusions come from that output only; if a hypothesis is disproved, say so explicitly.
3. Never modify a verified baseline endpoint (`/search`, `/search/hybrid`, `/generate`). New behavior goes in a new endpoint or behind a config flag.
4. Any new config variable must be added in **three** places: `.env.example`, `app/config.py`, and the `fastapi` `environment:` block in `docker-compose.yml` (the compose file uses an explicit whitelist, not `env_file`). This has caused silent failures twice. `[handoff]`
5. After any source edit: `docker compose up -d --build fastapi`. There is no source volume mount; the container runs build-time code. `[handoff]`
6. Environment: Windows + Docker Desktop + Git Bash/PowerShell. Use bare `python`, not `python3`. Prefix commands with leading-slash args with `MSYS_NO_PATHCONV=1` in Git Bash. Use `127.0.0.1`, not `localhost`, for host-side Postgres (IPv6 resolution bug). Host Postgres port is `5433`. `[handoff]`
7. Eval runs must use `no_cache: true` (the eval script does this; manual `curl` checks do not get it for free). `[handoff]`
8. The 20-entry `eval/eval_seed.json` baseline is frozen. New routing/edge cases go in a separate check (e.g. `diagnostics/routing_check.py`), not into the baseline. `[chat-reported]`

---

## 1. System Snapshot

| Layer | Implementation | Evidence |
|---|---|---|
| Infra | Docker Compose: `postgres:16-alpine` (host 5433), `opensearch:2.15.0` (security disabled, local-dev only), `redis`, `fastapi`. Ollama runs **natively on the host**, reached via `host.docker.internal` | `[handoff]` `[repo]` |
| Hardware | Machine A (reference): i5-1135G7, 16GB RAM, Iris Xe, CPU-only inference. Machine B: Core Ultra 7 155U, 16GB | `[chat-reported]` |
| Storage of record | Postgres, class-table inheritance: `documents` → `bug_reports` / `test_cases`; `document_references`; `error_codes` (`schema.sql`) | `[handoff]` |
| Search index | OpenSearch alias `qa_documents`, full rebuild + atomic alias swap | `[handoff]` |
| Lexical | BM25 `multi_match` (`title^2`, `cleaned_text`), `standard` analyzer, keyword fields w/ lowercase normalizer for IDs/error codes/metadata | `[handoff]` |
| Semantic | `nomic-embed-text` (768-dim, F16), `search_document:` / `search_query:` prefixes, `knn_vector` HNSW cosinesimil, **nmslib** engine | `[handoff]` |
| Fusion | RRF, k=60; k-NN oversampling `min(size*10, 150)` as a workaround for nmslib's lack of filtered k-NN | `[handoff]` `[repo]` |
| Chunking | Whole document = one chunk. `chunk_id` / `parent_document_id` schema exists; no splitting logic | `[handoff]` |
| Generation | `phi4:14b` via Ollama `/api/chat`, streaming, temperature 0.1, retrieve 10 → top 5 in context, structured per-doc-type context from Postgres, resolved `document_references` surfaced to the LLM | `[handoff]` |
| Prompt | `app/generation/prompt.py` SYSTEM_PROMPT Rules 1–7 (positive-only worked examples; Rule 7 = abstain on garbled input, no example) | `[handoff]` `[repo]` |
| Cache | Redis, key = (question, doc_type, module, status, concrete index name, model name); per-request `no_cache` | `[handoff]` |
| Observability | JSON lines to `logs/generation.jsonl` (retrieval span + generation span incl. model). Langfuse explicitly rejected (RAM) | `[handoff]` |
| Agentic | `POST /generate/agentic`: 3-node LangGraph (`guardrail_and_route` → `deterministic` / `semantic_generate`) + post-P7 ID-resolution route | `[handoff]` `[repo]` |
| UI | `app/static/index.html` served at `GET /ui` | `[repo]` |
| Corpus | 173 docs: 145 bug reports (BUG-1001..1025 original + BUG-2000..2119 synthetic), 28 test cases (3 original + 25 synthetic from `scripts/generate_synthetic_data.py`) | `[chat-reported]` |

### Endpoints
| Endpoint | Status | Notes |
|---|---|---|
| `GET /health` | Frozen | Connectivity check only, not readiness |
| `GET /search` | Frozen (Phase 3) | BM25 only |
| `GET /search/hybrid` | Frozen (Phase 4) | Returns `bm25_rank/score`, `vector_rank/score`, `rrf_score` per result |
| `POST /generate` | Frozen baseline (Phase 5/6) | Streaming, `---SOURCES---` marker after answer |
| `POST /generate/agentic` | Active (Phase 7+) | Same wire contract; **not** token-streamed (full answer accumulated in node) |
| `GET /ui` | Active (post-P7) | Demo UI for both generate endpoints |

---

## 2. Phase Ledger (what was built, what was proven)

### Phase 1 — Infrastructure `[handoff]`
- Built: Compose stack, `/health` with real per-service checks, `app/scripts/verify_infra.py` doing real round-trips.
- Key decision: Ollama moved **out** of Docker (Docker on Windows/Mac loses acceleration; native install already worked).
- Key bug: OpenSearch 2.12+ requires `OPENSEARCH_INITIAL_ADMIN_PASSWORD` even with security disabled.
- Left open: FastAPI container healthcheck (still absent at `f5922fe` `[repo]`), no migrations tooling, cold/warm latency split never executed.

### Phase 2 — QA-domain ingestion `[handoff]`
- Built: `schema.sql` (class-table inheritance), `ingest.py` (HTML cleanup, error-code + ID regex extraction, `duplicate_of` vs `related_to` classification from explicit text), `generate_eval_seed.py`.
- Key decision: explicit `BUG-XXXX` mentions as the primary duplicate signal. TF-IDF tested on real data: top pair (0.289) was a false positive; a real duplicate scored 0.143. TF-IDF + LLM-confirm left as unwired fallback.
- Bugs fixed: missing `scikit-learn`, hardcoded reference-repo creds, Windows IPv6 `localhost`, native Postgres squatting 5432 (→ host 5433), eval generator truncating categories, classifier not scanning `steps_to_reproduce`.
- Result: 25 bugs + 3 TCs, 4 unique error codes, 8 references (incl. dangling `BUG-1013 → TC-0209`), 12 eval questions.

### Phase 3 — BM25 + filters `[handoff]`
- Built: mapping, indexer with index-time ID/error-code extraction into keyword fields, alias-swap rebuild, BM25 query builder, `GET /search`, `verify_phase3.sh` (6 acceptance checks, all passed on real corpus).
- Key decisions: full rebuild not incremental (no `updated_at` column); `standard` not `english` analyzer; IDs extracted to keyword fields because `BUG-1013` tokenizes to `bug` + `1013`.
- Known limit: noisy tail on ID queries via bare-word `bug` matches.

### Phase 4 — Chunking, embeddings, hybrid, RRF `[handoff]`
- Built: `embeddings.py`, k-NN mapping, structured embedding input (not `cleaned_text`), `hybrid_search()`, `GET /search/hybrid`, `eval/run_eval.py` (Recall@k, MRR), `verify_phase4.sh`.
- Chunking decision: docs measured at 30–165 words → one chunk per doc. Splitting logic **not built**.
- Key bug: exact-ID boost only fired on single-token queries → `BUG-1015` at rank 12 for the ERR_401 question. Fixed via opt-in `extract_embedded_ids=True` on the hybrid BM25 leg only.
- Measured on the **28-doc** corpus:

| Metric | BM25 | Hybrid (post-fix) |
|---|---|---|
| Recall@3 | 66.7% | 81.8% |
| Recall@5 | 78.8% | 100.0% |
| Recall@10 | 92.4% | 100.0% |
| MRR | 0.939 | 1.000 |

### Phase 5 — Grounded streaming RAG `[handoff]`
- Built: `POST /generate`, `context.py` (`fetch_structured_fields`, `fetch_references`), `prompt.py`, `llm.py`, `PHASE5_VERIFY.md`.
- Key decisions: retrieve `size=10`, use top 5 (matches how Recall@5 was measured); sources emitted after the answer and only on success; temperature 0.1.
- Key bugs: nmslib rejects native `knn.filter` (500 on filtered calls) → post-filter bool query; duplicate classification was non-deterministic (1/4 correct) until the resolved reference graph was handed to the LLM + temperature pinned (then 5/5, 3/3); placeholder leak `[bug ID]`; doubled "of" in reference phrasing.
- Demonstrated risk: `BUG-1003` ranked 5th of 5 via single-leg RRF credit — one slot from being lost.

### Phase 6 — Eval, observability, caching, model choice `[handoff]`
- Built: eval seed 12 → 17; `eval/run_eval_generation.py` (Tier A deterministic, shares ground-truth tables with production); `eval/manual_eval_log.py` (Tier B); JSONL logger; Redis cache; `diagnostics/groq_probe.py` (A/B two models on identical context).
- Rejected with reasons: LLM-as-judge (no independent ground truth), Langfuse self-hosted (6 containers, 16GB recommendation = entire machine), Langfuse Cloud (unauthorized SaaS dependency).
- Model selection: `phi4-mini` (capacity ceiling), `llama3.1:8b` (fast but miscounted — disqualified), `qwen3.5:9b` (correct, ~112s TTFT warm), `gpt-oss:20b` (correct in isolation, 90–2672s under full stack due to RAM swap), **`phi4:14b` chosen**: 13/13 Tier A, 4/4 Tier B, latency 72.7–304.9s (mean ~168s) under full stack.
- Transferable lessons: negative few-shot examples backfire on small models; test prompts on the deployed model, not a stand-in; RAM headroom, not benchmark quality, predicted latency.

### Phase 7 — Agentic layer `[handoff]`
- Built: `guardrail.py` (cosine `vector_score` threshold 0.75, explicit-filter trust bypass), `deterministic.py` (count/list answers, zero LLM calls), `generate_agentic.py` (LangGraph), `diagnostics/rrf_threshold_probe.py`, 3 out-of-domain eval questions (seed → 20), `--endpoint-path` flag on the generation eval.
- Guardrail signals tested on real data: `rrf_score` rejected (rank-based, no gap), `bm25_score` rejected (paraphrases scored in the fake range), `vector_score` shipped (in-domain ≥0.789, out-of-domain ≤0.706, **12 data points**).
- **Not built:** query rewrite/retry node — justified by Recall@5=100% / MRR=1.000 on 15 scored questions and a 3/3 paraphrase stress test.

| | `/generate` | `/generate/agentic` |
|---|---|---|
| Tier A (20 q) | 13 PASS / 3 FAIL (OOD) / 4 MANUAL | 16 PASS / 0 FAIL / 4 MANUAL |
| Tier B | 4/4 | 8/8 |
| Out-of-domain | ~84s, answered anyway | ~0.4s, rejected |
| Structured filter | ~118s (LLM) | ~0.4s (deterministic) |
| Ordinary questions | — | ~1% overhead |

### Post-Phase-7 hardening (no handoff doc exists yet) `[chat-reported]`, code presence `[repo]`
- Corpus expanded 28 → 173 via `scripts/generate_synthetic_data.py`, schema reset, re-ingest, rebuild.
- Count truncation bug found: `doc_type=bug_report` returned "100 match" with 45 silently missing → fixed via `count_documents()` (OpenSearch `_count`), `MAX_LISTED_IDS=100` with explicit "(N more not shown)". `[repo]` confirms `count_documents` and `MAX_LISTED_IDS`.
- Domain-anchored count patterns; `\bhow much\b` removed as an off-topic bypass; k-NN oversampling added `[repo]`; UI doc_type dropdown wired; generic client-safe error on Ollama timeout; Rule 7 added `[repo]`; UI latency readout.
- ID-resolution route (`app/search/id_resolution.py` `[repo]`): existing BUG-/TC- IDs pinned to context and accepted; non-existent IDs rejected with "no document with ID". Machine A: offline self-test 29/29, live `routing_check` 19/19, frozen baseline sweep 16/0/4. **Machine B not updated.**
- nmslib → lucene migration attempted and **reverted**: fixed narrow-filter case but all 3 OOD questions started passing (one cited a real doc for "capital of France"). Likely cause: different score reporting under lucene, invalidating the 0.75 threshold (not confirmed).

---

## 3. Decision Register (defend these in interviews)

| # | Decision | Why | Revisit when |
|---|---|---|---|
| D1 | Native Ollama, not containerized | Acceleration loss in Docker Desktop; existing working install | Moving to Linux host with GPU |
| D2 | Class-table inheritance | Bug/TC field shapes differ; no permanently-null columns | New doc type (SRS) arrives |
| D3 | Explicit-mention regex over TF-IDF for duplicates | Measured: TF-IDF false-positived, missed real dupes | Corpus without explicit cross-references |
| D4 | Full rebuild + alias swap | No `updated_at`; rebuild takes seconds | Rebuild time stops being trivial |
| D5 | IDs as keyword fields, not text/embeddings | Analyzer splits IDs; IDs have no semantic neighborhood | ID format changes |
| D6 | One chunk per doc | Docs 30–165 words | SRS/spec docs ingested |
| D7 | RRF k=60 | Rank fusion avoids BM25/cosine scale mismatch; no tuning evidence | Eval can discriminate (see W1) |
| D8 | Surface resolved reference graph to LLM | LLM re-deriving from ambiguous prose was 1/4 correct | — |
| D9 | Temp 0.1 | Consistency; avoid 0.0 repetition loops | Broader sweep evidence |
| D10 | No LLM-as-judge | Judge lacks independent ground truth | Tier B volume becomes unmanageable |
| D11 | No Langfuse | 16GB RAM ceiling | More RAM / different host |
| D12 | `phi4:14b` | Only model both correct and within RAM headroom | New model optimized for this RAM class |
| D13 | Cosine guardrail + filter trust | Only signal with a real gap; templated filter questions false-rejected | Engine change or corpus growth (see W5) |
| D14 | No query-rewrite node | No measured retrieval failure | Eval shows a real failure (see W1) |
| D15 | Deterministic count routing default on | 98.5% latency cut, zero correctness change | — |
| D16 | New endpoints alongside frozen ones | Baseline comparability | — |

---

## 4. Weakness Audit (prioritized)

Severity reflects interview/portfolio risk plus correctness risk. Each item states the evidence and a proposed fix; fixes are proposals, not decisions.

### W1 — CRITICAL: The retrieval eval is saturated, mostly self-referential, and stale for the current corpus
- Evidence `[repo]`: every `requires_docs` gold ID in `eval/eval_seed.json` is one of the **original 28** documents (17 distinct IDs). Zero questions target any of the 145 synthetic docs.
- Evidence `[handoff]`: questions are generated by `generate_eval_seed.py` from the same `document_references` graph and regexes the system itself uses — the eval tests the pipeline against its own assumptions.
- Evidence: Recall@5=100% / MRR=1.000 was measured on the 28-doc corpus `[handoff]`. No evidence was found that `eval/run_eval.py` was re-run after the expansion to 173 docs `[unverified]`.
- Consequence: a metric at ceiling cannot show improvement or regression. Decision D14 (skip query rewrite) and D7 (RRF k=60) rest on this instrument.
- Also: 20 entries but 19 distinct questions (BUG-1024 duplicate question appears twice with different `requires_docs`) `[chat-reported]`; `requires_docs` is a list for most entries and a string sentinel (`"filter:module+status"`) for structured ones `[repo]` — mixed types in one field.
- Proposed fix: (1) re-run `run_eval.py` on the 173-doc corpus for both endpoints and record real numbers; (2) add a hand-authored holdout set not produced by `generate_eval_seed.py`: paraphrased questions with zero lexical overlap, questions whose gold docs are synthetic BUG-2xxx/TC-2xxx, multi-hop questions, near-miss distractors in the same module; (3) keep the frozen 20 as a regression set, report the holdout separately.

### W2 — HIGH: No automated tests and no CI in an SDET portfolio project
- Evidence `[repo]`: zero `test_*.py` files; verification lives in `verify_phase3.sh`, `verify_phase4.sh`, diagnostics scripts, and manual `curl`.
- Consequence: the most likely interview question for a Staff SDET — "how is this tested?" — has no crisp answer. Refactors (e.g. W4) have no safety net.
- Proposed fix: `pytest` unit tests for pure functions that need no Docker (`reciprocal_rank_fusion`, `build_filters`, `build_search_query` incl. `extract_embedded_ids`, guardrail decision logic, `is_countable_question`, ID resolution parsing, cache key construction, `REFERENCE_TYPE_PHRASING`); a small integration tier marked `@pytest.mark.integration` for live-stack contract tests on each endpoint; GitHub Actions running the unit tier only.

### W3 — HIGH: The repo is not reproducible from a clean clone
- Evidence `[repo]`: no README; `data/` is gitignored and absent, yet the same data ships inside three committed zips; `requirements.txt` is unpinned except `opensearch-py` (Phase 1 had pinned versions — this regressed); re-running `ingest.py` on a loaded DB fails with `UniqueViolation` `[chat-reported]`; Ollama version differences between machines changed behavior `[chat-reported]`.
- Proposed fix: README with a verified bootstrap sequence; commit the synthetic generator's output or a fixed seed so data is reproducible; pin all requirements (`pip freeze` from the working container); make ingestion idempotent (`ON CONFLICT` on `documents`, or explicit `--reset`); document the pinned Ollama version (0.34.0 per chat) and model tags.

### W4 — MEDIUM: Repo hygiene defects `[repo]`
- `.gitignore` patterns are wrong: `.zip` only matches a file literally named `.zip` (should be `*.zip`); `/__pycache__` only matches the root (should be `__pycache__/`). Result: `app.zip`, `rag.zip`, `ragprod.zip` (~1MB total) and many `__pycache__/*.pyc` files are committed.
- `logs/generation.jsonl` (694 lines) is committed.
- `app/generation/__init__ .py` — the filename contains a space, so `app/generation` has no real `__init__.py`. It likely works only as an implicit namespace package `[unverified]`.
- Dead/duplicate files: root `search.py` (duplicate of `app/routers/search.py`), `diagnostics/groq_probe copy.py`, `app/scripts/build_index.py` vs `scripts/build_index.py`, `manual4.txt`, `phase3_query_results_TEMPLATE.md` (filled in but still named TEMPLATE).
- `GROQ_API_KEY` in `.env.example` — acceptable for the diagnostics-only probe, but should be documented as optional so it doesn't read as a cloud dependency in a local-first project.
- Proposed fix: one cleanup commit; use `git rm --cached` for tracked files that become ignored.

### W5 — MEDIUM: Guardrail calibration predates the 6x corpus expansion and is coupled to the k-NN engine
- Evidence `[handoff]`: threshold 0.75 calibrated from 12 data points on the 28-doc corpus. `[chat-reported]`: two open false-rejects ("test cases" alone; "where is the bug steps in bug-1011"); lucene migration broke the threshold.
- Consequence: the threshold is an implicit contract with nmslib's score semantics and the corpus distribution. Neither dependency is written down in code.
- Proposed fix: re-run `diagnostics/rrf_threshold_probe.py` on the 173-doc corpus with a larger labeled probe set (in-domain, paraphrased, short-fragment, OOD); record the distribution; add an assertion/comment in `guardrail.py` that the threshold is engine-specific.

### W6 — MEDIUM: ID/error-code regex duplicated across files and already diverged
- Evidence `[repo]`: `ingest.py` and `app/search/indexer.py` compile `BUG-\d{4}` / `TC-\d{4}` case-sensitively; `app/search/queries.py` compiles them with `re.IGNORECASE`; `ERR_` pattern likewise differs. `id_resolution.py` adds another consumer.
- Consequence: the drift the Phase 3 handoff warned about has already happened. Probably harmless today because keyword fields use a lowercase normalizer `[unverified]`, but it is exactly the class of bug that surfaces after a format change.
- Proposed fix: one `app/patterns.py` module; all callers import from it; a unit test pinning behavior on mixed-case inputs.

### W7 — MEDIUM: Latency is not demo-safe
- Evidence `[chat-reported]`: free-text answers often 100–480s, 600s timeouts hit; `[handoff]` Phase 6 mean ~168s. `ollama_chat_wall_clock_timeout` default is 2400s `[repo]`.
- Consequence: a stranger using a tunnel link will likely assume it is broken.
- Proposed levers (each needs measurement, none applied): lower `CONTEXT_TOP_N` / explicit `num_ctx`; OpenSearch heap 512m → 256m (proposed in Phase 6, never tested); shorter `OLLAMA_KEEP_ALIVE`; single-flight request queue with a visible "position in queue"; honest latency messaging in the UI.

### W8 — MEDIUM: The "QA-aware chunking" and SRS/spec scope exists only on paper
- Evidence `[handoff]`: one chunk per doc; no splitting logic; no SRS/specification documents ever ingested, though both the project brief and the project description list them.
- Consequence: an interviewer asking "walk me through your chunking strategy" gets a schema, not an algorithm. Defensible as a measured decision, but only if the long-document path is demonstrated at least once.
- Proposed fix: ingest 2–3 real-shaped SRS/spec documents, implement section-aware splitting (heading → requirement ID boundaries), measure retrieval on questions targeting those docs with parent-document context expansion.

### W9 — LOW: Carried-over technical debt
- No FastAPI container healthcheck (open since Phase 1) `[repo]`.
- `/generate/agentic` is not token-streamed `[handoff]`.
- `_get_pg_connection()` duplicated in `generate.py` and `generate_agentic.py` `[handoff]`.
- nmslib post-filter + `min(size*10,150)` oversampling is a scale ceiling `[repo]`.
- `manual_eval_log.py` entries not tagged by model `[handoff]`.
- Context header metadata bleeding into answers (Phase 5 bug 6) — not confirmed fixed `[unverified]`.
- JSONL observability has no analysis/aggregation script (p50/p95 latency, cache hit rate, reject rate) `[repo]`.
- Rule 7 mislabels the wrong-module-filter case; `deterministic_count_routing` default mismatch cosmetic item `[chat-reported]`.
- Machine B lacks the ID-resolution route and showed differing retrieval scores / one garbled answer; cause unconfirmed `[chat-reported]`.

### W10 — LOW (only matters before public exposure): tunnel security posture
- No auth, no rate limiting, OpenSearch security disabled. Acceptable for localhost; a public tunnel exposes a single-worker, multi-minute LLM endpoint to anyone who finds the URL.
- Proposed fix before any tunnel: shared-secret header or basic auth on the tunnel, per-IP rate limit, request queue cap.

---

## 5. Open Items Inherited From Chats `[chat-reported]`
1. Two false-rejects not root-caused: "test cases" (no filter); "where is the bug steps in bug-1011".
2. Rule 7 mislabels the wrong-module-filter case (low priority).
3. `deterministic_count_routing` default mismatch — cosmetic cleanup.
4. Repo hygiene (now detailed in W4).
5. Machine B: apply ID-resolution route, re-verify, root-cause score divergence (check corpus count and configured model first).
6. No handoff doc exists for post-Phase-7 work — this file partially fills that gap; a formal `phase-08-handoff.md` is still required when Phase 8 closes.

---

## 6. Recommended Next Work Packages (in order)

The order is deliberate: measurement before optimization, reproducibility before exposure.

**WP-A — Repo hygiene + reproducibility (W3, W4, W6, W9 healthcheck).** Smallest risk, unblocks everything else, and a clean clone is a prerequisite for CI.
Acceptance: fresh clone + README steps reach a healthy stack and a passing `verify_phase3.sh`/`verify_phase4.sh`; `git ls-files` shows no zip, pyc, or log files; all requirements pinned; ingestion re-runnable without a schema reset.

**WP-B — Test suite + CI (W2).**
Acceptance: unit tier runs in GitHub Actions without Docker; integration tier runs locally against the stack; coverage of every function named in W2.

**WP-C — Eval v2 (W1, W5).** This is the phase that makes every earlier number defensible.
Acceptance: real Recall@k/MRR for `/search`, `/search/hybrid` on the 173-doc corpus; a hand-authored holdout set reported separately; guardrail re-probed on the new corpus with the distribution recorded. Only after this: revisit D7 (RRF k) and D14 (query rewrite) on evidence.

**WP-D — Long-document path (W8).** SRS/spec ingestion + real chunking, measured on WP-C's eval.

**WP-E — Production hardening (W7, W10, remaining W9).** Queueing, latency messaging, auth/rate limit for tunnel, observability aggregation, pinned Ollama, agentic streaming. This matches the separately planned hardening session.

A QA-learning session (testing techniques mapped to each phase) is also planned `[chat-reported]`; WP-B is the natural place to do it, since writing the tests is the learning exercise.

---

## 7. Known Commands (all from handoffs — do not invent others)
```bash
docker compose up -d --build fastapi            # after ANY source change
python ingest.py                                  # load Postgres (fails if already loaded, see W3)
python -m scripts.build_index                     # rebuild index with embeddings
python -m scripts.build_index --no-embed          # BM25-only rebuild
bash verify_phase3.sh
bash verify_phase4.sh
MSYS_NO_PATHCONV=1 python -m eval.run_eval --endpoint /search
MSYS_NO_PATHCONV=1 python -m eval.run_eval --endpoint /search/hybrid
MSYS_NO_PATHCONV=1 python -m eval.run_eval_generation --endpoint-path /generate/agentic
docker exec rag-fastapi env                       # confirm config actually reached the container
docker exec -it rag-fastapi python -m app.scripts.verify_infra
```
Invocation form for `run_eval_generation` is inferred from the `--endpoint-path` flag described in the Phase 7 handoff; confirm against the script's argparse before use.

---

## 8. Recurring Failure Patterns (checklist for every future change)
1. Config added in two places, not three → silently ignored in the container.
2. Code edited, container not rebuilt → testing stale code.
3. Manual `curl` without `no_cache: true` → stale cached answers mistaken for real results.
4. `python3` in scripts → Windows Store stub.
5. Leading-slash CLI args in Git Bash → MSYS path mangling.
6. Prompt fix validated on a larger stand-in model → no signal for `phi4:14b`.
7. Negative few-shot examples → the suppressed phrase gets emitted.
8. Latency measured in isolation → not a production estimate; measure under the full stack.
9. Thresholds calibrated on one engine/corpus → silently invalid after either changes.
