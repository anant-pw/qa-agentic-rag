# Phase 8 Handoff — Performance, Evaluation v2, and Project Close-out

## 1. Completion Status: **Complete. The project is closed at this phase.**

Phase 8 had three goals:
1. Make answers fast enough to use on a 16 GB laptop (all Phase 8 numbers: Machine B, Core Ultra 7 155U, CPU-only).
2. Replace the saturated, self-referential eval with real evidence on the 173-doc corpus.
3. Finish the project: reproducible setup, tests, CI and documentation.

All three are done. The remaining weaknesses are measured and listed in Section 7. Per the owner's direction, they are documented rather than chased; each fix got one bounded attempt with accept criteria set beforehand.

The detailed reports are the source of truth for numbers:
- `docs/PERF_RUN_2026-10-01.md`
- `docs/HOLDOUT_EVAL_2026-10-01.md`
- `docs/NEXT_STEPS_PLAN.md`

## 2. What Was Implemented

| Change | Where | Flag (default) |
|---|---|---|
| Ollama load / prefill / decode timing logged per answer | `app/generation/llm.py` `ollama_timings()` | — |
| **Field-lookup route**: single-ID field asks answered from Postgres, no LLM | `app/generation/field_lookup.py` | `DETERMINISTIC_FIELD_LOOKUP` (true) |
| **Error-code route**: "which bugs have ERR_X" from `document_error_codes`, with recorded relations only | `app/generation/error_code_lookup.py` | `DETERMINISTIC_ERROR_CODE_LOOKUP` (true) |
| **Reference expansion**: a named ID's cited TC, duplicates and relations are placed in context | `context.py` `fetch_reference_targets()`, router `_expand_references()` | `REFERENCE_EXPANSION` (true) |
| **Token streaming** on `/generate/agentic` via the LangGraph custom stream; same wire contract | `app/routers/generate_agentic.py` | `AGENTIC_TOKEN_STREAMING` (true) |
| **Default model** changed from `phi4:14b` to `qwen3:4b-instruct` | `.env` / `.env.example` | `OLLAMA_CHAT_MODEL` |
| Hand-written 41-question holdout and grader | `eval/holdout.json`, `eval/run_holdout.py` | — |
| Eval harness: a failed request records ERROR instead of aborting the run | `eval/run_eval_generation.py` | — |
| Routing check extended: field, error-code and reference rows (22 rows) | `diagnostics/routing_check.py` | — |
| **Data bug fixed**: module/status swapped on all 120 synthetic bugs | `scripts/generate_synthetic_data.py`, `data/bug_reports.csv`, DB + index rebuilt | — |
| Re-runnable ingest | `ingest.py --reset` | — |
| Repo hygiene: `.gitignore`, `data/` tracked, zips/pyc/logs untracked, `__init__ .py` rename, dead files removed | — | — |
| Pinned requirements, `requirements-dev.txt`, fastapi healthcheck | `requirements*.txt`, `docker-compose.yml` | — |
| 52 unit tests + GitHub Actions | `tests/`, `pytest.ini`, `.github/workflows/tests.yml` | — |
| README | `README.md` | — |

Frozen and unchanged: `/search`, `/search/hybrid`, `/generate`, `SYSTEM_PROMPT`, `eval/eval_seed.json`.

## 3. Design Decisions and Why

| # | Decision | Evidence | Revisit when |
|---|---|---|---|
| D17 | Answer stored-value questions deterministically (fields, error codes), like counts | A field answer took 101–493 s through phi4 to reproduce a DB column verbatim; through the route it takes 0.1 s, verbatim | — |
| D18 | Trigger rules err toward missing | A miss costs one normal LLM answer; a false match returns the wrong field. 18/18 and 10/10 offline trigger tests | A real question misses: extend patterns then, not before |
| D19 | Expand context with referenced docs | Q1's top 5 lacked TC-0142, so both models guessed. After expansion phi4 and qwen quote TC-0142 exactly | — |
| D20 | `qwen3:4b-instruct` as default | Frozen 20: 16/0/4 (×2). Holdout LLM-path: 31/36 vs phi4 32/36. Median 44–48 s vs 278–382 s. phi4 needs ~10 GB of the 16 GB and caused OpenSearch/embedding timeouts | Hardware with ≥ 32 GB RAM, or a newer small model |
| D21 | `-instruct`, not the default `qwen3:4b` tag | The default tag is the reasoning build: 1,300–4,000 output tokens per answer even with `think=false` (633 s median) | — |
| D22 | OpenSearch heap stays 512m | 256m was tried: GC pauses of 1–6.6 s on swapped pages, one 500. The real cause was host RAM, not heap | — |
| D23 | Stream through LangGraph's custom stream rather than bypassing the graph | Keeps the graph as the single control flow; Tier A identical; 83 chunks, first at 27 s of 40 s | — |
| D24 | Prompt Rule 8 (correct false premises) **rejected** | 0 holdout questions changed; frozen 20 dropped 16→15 | Fine-tuning, or a model that follows it |
| D14′ | "No query-rewrite node" is **now contradicted by evidence** | Holdout S01–S04: short/messy questions rejected or key doc missed | First item of any future work |

## 4. Real Numbers

| | Before Phase 8 (phi4:14b, LLM for all non-count questions) | After |
|---|---|---|
| Frozen 20 Tier A | 16 / 0 / 4 (Phase 7, 28-doc corpus) | 16 / 0 / 4 / 0 ERROR (qwen, 173 docs, ×2 runs + streaming run) |
| Whole frozen-20 run | 96.7 min (with 1 infra ERROR) | 6.8–6.9 min |
| LLM-path answer, median / max | 382 s / 1,216 s | 44 s / 103 s (first token ~27 s) |
| Questions answered without LLM (of 20) | 5 | 11 |
| Holdout (41, hand-reviewed) | — | qwen 33/41; LLM-path qwen 31/36, phi4 32/36 |
| Unit tests | 0 | 52 |

## 5. Bugs Found and Fixed (all confirmed with live output)

1. **Synthetic data had module and status swapped** on all 120 synthetic bugs: `DictWriter` used hardcoded fieldnames under an existing header with a different column order. Counts and filters silently excluded every synthetic bug; for example, Open Login bugs were 4 and are really 8. The eval could not catch it because its checker reads the same DB.
2. **The eval harness aborted the whole run on one 500**, discarding hours of answers.
3. **`ingest.py` re-run failed** with `UniqueViolation`.
4. **`.gitignore` patterns matched nothing intended**, so zips, `.pyc` files and logs were committed.
5. **`app/generation/__init__ .py`** had a space in its filename, so the package had no real `__init__`.
6. **phi4:14b memory pressure** caused OpenSearch read timeouts and embedding timeouts. This was resolved by the model change (D20), not by tuning.

## 6. What Was Verified

- `diagnostics/routing_check.py`: 22/22 (after every change, and on the final build).
- Frozen 20 on qwen3:4b-instruct: three runs, all 16/0/4/0 (two before streaming, one after).
- Holdout on qwen (41) and phi4 (36). Every FAIL was hand-read; Tier B for the 9 LLM answers of the frozen 20 was checked against the DB.
- Data fix: 0 swapped rows; Postgres and OpenSearch `_count` agree (8 Open Login bugs).
- `pytest`: 52 passed on Python 3.14 (host) and in a clean `python:3.12-slim` container.
- FastAPI container healthcheck reports healthy.
- **Clean-clone bootstrap (2026-10-03).** A fresh `git clone` from GitHub went through:
  - a new venv: 52 tests pass;
  - `docker build` from the clone: the app imports;
  - `ingest.py --reset` against the live DB: identical to the pre-reset baseline (173 docs, 704 references, 19 error-code links, 8 Open Login bugs, same md5 over every document's ID/module/status/title);
  - `build_index`: 6 min 27 s, alias swapped;
  - `routing_check`: 22/22;
  - frozen 20: 16/0/4/0 (`eval/runs/2026-10-03_after-clean-reset/`).
- **GitHub Actions** `unit-tests` on `c2132ae`: success.
- **Bootstrap notes found while doing this:**
  - The README's `cp .env.example .env` step is required before `build_index`: `app.config` needs the model settings.
  - One `.pyc` with a space in its path had stayed tracked; it is now removed.
  - Windows Application Control intermittently blocked `psycopg2`'s DLL once; it succeeded on retry with no change.
- **Not repeated:** installing Ollama and pulling the models (already present on this machine).

## 7. Known Limitations (measured, not hidden)

1. **Short or messy questions are rejected by the cosine guardrail.** This is 3 of 41 holdout questions ("checkout 504", "dup notif bug??", "lockout after 3 tries…").
2. **List answers are capped by top-5 context** (holdout A02: 2 of 4 open sign-in bugs never retrieved).
3. **Messy wording misses the key document** (S04: "export broke >5000 rows fixed??" did not retrieve BUG-1017).
4. **qwen does not correct false premises** (F01, F04); phi4 does. Rule 8 was tried and reverted.
5. **Both models overclaim on M02:** they say BUG-1002 "reports ERR_401_UNAUTH", but only BUG-1001 states it.
6. **About 230 words of developer notes are inside the `SYSTEM_PROMPT` string** and are sent on every call. It was left in because `/generate` is frozen.
7. **Generation is ~45 s per LLM answer on CPU.** The 0.75 guardrail threshold is nmslib-specific.
8. **No auth or rate limiting; OpenSearch security is disabled.** Local use only.
9. **`app/scripts/build_index.py` differs from `scripts/build_index.py`** (the documented one). It was not removed without knowing which is canonical.

## 8. Deliberately Deferred (future work, ranked by measured value)

1. Query rewrite for short/messy questions (addresses limitations 1 and 3).
2. Status-word → filter routing, plus top-8 context for list questions (addresses 2).
3. Fine-tuning a small model with RAFT + Unsloth on Colab (addresses 4). Only if 1–2 don't close the gap.
4. OpenVINO / IPEX-LLM test on Machine B's Arc iGPU / NPU. All Phase 8 numbers were measured on Machine B (Core Ultra 7 155U) CPU-only, so this applies to the reference numbers directly. Bring Machine A to this commit and apply the data fix.
5. SRS / long-document chunking. It does not improve the current corpus; it is a capability for new document types.
6. Auth and rate limiting before any public tunnel.

## 9. Conflicts With Earlier Phases

- **Phase 6 D12 (`phi4:14b`)** is superseded by D20. The Phase 6 rejection of `llama3.1:8b` (miscounting) no longer applies on `/generate/agentic`, because counts are deterministic. It now scores 16/0 and is the fallback model.
- **Phase 7 D14 (no query rewrite)** now has the counter-evidence it was waiting for (D14′).
- **Phase 4 / 7 retrieval numbers** (Recall@5 = 100%) were measured on 28 docs. The holdout shows real retrieval misses on 173 docs. `eval/run_eval.py` was still not re-run on the 173-doc corpus.

## 10. Files to Read First

1. `README.md`: setup, settings, limitations.
2. `docs/HOLDOUT_EVAL_2026-10-01.md`: what the system gets wrong and why.
3. `app/routers/generate_agentic.py`: the whole pipeline; the routes are in `guardrail_and_route()`.
4. `app/generation/field_lookup.py` and `error_code_lookup.py`: the trigger-rule docstrings explain the miss-over-mismatch policy.
5. `docs/NEXT_STEPS_PLAN.md`: future work with effort estimates and accept criteria.
6. This document. Where it conflicts with the repo, the repo is correct.
