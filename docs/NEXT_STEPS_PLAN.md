# Next Steps Plan: making the RAG fast on 16GB CPU hardware

**Written:** 2026-10-01, after the `PERF_RUN_2026-10-01.md` work package.
**Ground rules carried over:**
- Measure before changing anything.
- Leave frozen endpoints alone: `/search`, `/search/hybrid`, `/generate`.
- Every new setting goes in three places: `.env.example`, `config.py` and `docker-compose.yml`.
- Eval runs go in `eval/runs/<date>_<name>/`, never over the committed results file.

## Where things stand

- **Already answered without an LLM (0.1–6 s):** count questions, single-ID field asks, error-code aggregation, off-topic rejects, unknown-ID rejects. That covers 11 of the 20 eval questions.
- **Still needs an LLM:** about 9 of the 20 (duplicates, relationships, cross-references, narratives).
- **What decides speed for those 9:** (a) does the model fit in RAM, and (b) how many output tokens does it write.
- **Prompt-prefix caching already works.** Measured with qwen3:4b: a new question with the same system prompt took 23 s of prompt reading instead of 62 s. A cache fix is therefore not a lever.

## Step 1: Choose the default model (measured, this session)

See section "Model bake-off" below for the numbers. Decision rule:
1. Pick the fastest model with **0 Tier A FAIL** on the 20-question seed.
2. Its LLM-path answers must also pass a hand check against the database (Tier B).
3. Its infrastructure ERROR rate must be 0 (no swapping-induced timeouts).

Switching the model is a one-line `.env` change (`OLLAMA_CHAT_MODEL`). That is the owner's decision.

## Step 2: Stream tokens from `/generate/agentic`: ✅ DONE 2026-10-01

Implemented behind `AGENTIC_TOKEN_STREAMING` (default true; added in all 3 config places). It kept the LangGraph graph rather than the bypass design below: `run_semantic_generate()` emits tokens through `get_stream_writer()`, and the route consumes `graph.stream(stream_mode=["custom", "values"])`.

Verified with qwen3:4b-instruct:
- **Tier A:** 16/0/4/0, identical to the run before the change (`eval/runs/2026-10-01b_qwen3-4b-instruct_streaming/`).
- **Incremental delivery:** a semantic answer arrived in 83 chunks, first text at 27.3 s of a 40.2 s total.
- **Unchanged routes:** the no-LLM routes are still single-shot at ≤0.2 s.
- **Cache and log:** the cache hit on a repeat question took 0.1 s; `first_token_s` is logged.
- **`routing_check.py`:** 22/22.

Original design notes, kept for reference:

**Why:** total time doesn't change, but users see text after the prompt is read instead of after the whole answer. The UI already streams responses (`app/static/index.html` uses `resp.body.getReader()`), so no frontend change is needed.

**Design:**
1. Run only the routing node (`guardrail_and_route`) synchronously.
2. For the reject, deterministic, field_lookup and error_code routes, keep the current one-chunk response. They're already instant.
3. For `semantic`, build the context the same way and `yield` from `stream_chat()` directly inside the `StreamingResponse` generator. Then send `---SOURCES---`, then write the cache and the log after the stream ends. This mirrors `generate.py`, which is not modified.
4. The wire contract stays the same, so `run_eval_generation.py` needs no change.

**Acceptance:**
- The 20-question Tier A result is identical to the run before the change.
- The new log field `first_token_s` is recorded and is smaller than `latency_s` for every semantic answer.

## Step 3: Memory headroom on Machine A (owner actions, reversible)

1. `C:\Users\anant\.wslconfig` with `memory=3GB` and `autoMemoryReclaim=gradual`, then `wsl --shutdown` and restart Docker.
2. Close browsers and VS Code during demos; disable the Edge, OneDrive and Ollama auto-start entries.
3. Optional: set `OLLAMA_FLASH_ATTENTION=1` and `OLLAMA_KV_CACHE_TYPE=q8_0` as user environment variables and restart Ollama.

**Acceptance:** re-run the chosen model's eval and compare `ollama_eval_s` per token and the ERROR count against the bake-off numbers.

## Step 4: A hand-written holdout eval set (weakness W1, ≈ 1 day)

Prerequisite for any fine-tuning or retrieval tuning. The current seed is generated from the same reference graph the system uses, and its gold docs are all among the original 28.
- ~40 questions written by hand, not by `generate_eval_seed.py`:
  - questions about the synthetic BUG-2xxx/TC-2xxx documents
  - paraphrases with no shared vocabulary
  - multi-hop questions (bug → cited test case → its module)
  - near-miss distractors in the same module
- Store as `eval/holdout.json`. Report it separately from the frozen 20.
- Also re-run `eval/run_eval.py` (Recall@k / MRR) on the 173-doc corpus. Those numbers have never been measured.

## Step 5: Semantic answer cache (≈ half a day)

- Before generation, embed the question (already done for retrieval). Compare it with cached question embeddings for the same filters and index. Above a high cosine similarity (to be calibrated, e.g. 0.95), return the cached answer.
- **Storage:** `redis:7-alpine` has no vector search. Use a small Redis hash plus an in-process cosine over ≤ a few hundred entries. That needs no new container.
- **Risk:** near-identical wording can mean different IDs. Never reuse an answer across questions with different BUG-/TC-/ERR_ IDs.
- **Acceptance:** a probe set of paraphrase pairs hits the cache, and different-ID pairs never do.

## Step 6: Fine-tune a small model (only if Steps 1 and 4 show a real quality gap, ≈ 3–5 days)

- **Method:** RAFT-style LoRA fine-tuning with **Unsloth** on a free Colab/Kaggle T4. The laptop has no usable GPU.
- **Training data:**
  1. phi4:14b answers from `logs/generation.jsonl` that a human graded correct.
  2. Questions generated per document, with the gold doc plus 3–4 distractor docs in the context, in the same format as `format_doc_context()`.
  3. Target ~1,000 examples.
- **Export:** Unsloth `save_pretrained_gguf` (q4_K_M), then `ollama create qa-rag-4b -f Modelfile`, then switch via `OLLAMA_CHAT_MODEL`.
- **Evaluate on the Step 4 holdout only.** Training on corpus-derived questions makes the frozen 20 a leaked test.
- **Risk:** it must be retrained when the corpus changes much. It does not make the model faster; it only recovers quality at a smaller size.

## Step 7: Machine B acceleration test (≈ half a day)

Machine B has a Core Ultra 7 155U with an Arc iGPU and an NPU. Machine A's Iris Xe gives no benefit (an external benchmark agrees, and Phase 6 saw 0% GPU use).
1. Bring Machine B to the current commit and apply the data fix (`UPDATE` + `build_index`).
2. Compare the same model on stock Ollama (CPU), IPEX-LLM Ollama (SYCL, iGPU) and OpenVINO GenAI. Record prompt-read time, writing speed and Tier A for each.

## Step 8: Carry-over hygiene (from PROJECT_LOG weaknesses W2–W4)

- Fix the `.gitignore` patterns (`*.zip`, `__pycache__/`). `git rm --cached` the zips, `.pyc` files and `logs/generation.jsonl`.
- Pin `requirements.txt` from the working container. Add a README with the verified bootstrap. Make `ingest.py` re-runnable.
- `pytest` unit tests for the pure functions added this week: `requested_fields`, `error_code_question`, `fetch_reference_targets` ordering, `ollama_timings`. Add GitHub Actions for the unit tier.

## Step 9: Latency research shortlist (2026-10-01; estimates, not measurements)

Retrieval is not the bottleneck (median 0.16 s). The time goes into the LLM reading ~700 tokens of context (~25–80 s) and writing (~10–15 s). Most ideas below are only viable because this corpus is tiny: 173 docs, ~67k characters.

| # | Idea | Expected effect | Cost / risk |
|---|---|---|---|
| 1 | **Cache-augmented generation (CAG):** put the whole corpus in a persistent KV cache once; per question, only the question is prefilled ([hhhuang/CAG](https://github.com/hhhuang/CAG)) | Corpus ≈ 17–18k tokens → KV ≈ 2.6 GB f16 / 1.3 GB q8 for qwen3-4b (36 layers × 8 KV heads × 128). First token ~27 s → ~1–2 s (estimate) | Needs `llama-server` (`--slot-save-path`; Ollama cannot persist a slot). Quality risk on near-duplicate docs over a long context |
| 2 | **phi4 answer bank:** generate likely questions per doc offline (HyPE/doc2query), answer them overnight with phi4:14b, serve them by embedding match | phi4 quality at ~0.2 s for covered questions; phi4 never runs at request time | Match threshold must be strict; invalidate on index change (the cache key already includes the index) |
| 3 | **n-gram speculative decoding** (`--spec-type ngram-simple` in llama.cpp) | Faster writing for extractive answers that copy IDs, titles and fields; no extra RAM | Unmeasured on this CPU; some reports of slowdowns |
| 4 | **Prefill while typing:** the UI sends the partial question; the server retrieves and prefills system + docs while the user types | Hides most of the context prefill behind typing time | Wasted work when the final retrieval differs (costs the same as today) |
| 5 | **Context compaction:** drop the synthetic docs' boilerplate steps (~3.5k tokens corpus-wide) and the description when it repeats the title | ~30–40% shorter contexts for synthetic-heavy questions; also shrinks idea 1's cache | Must not drop real content (original docs keep everything) |
| 6 | **In-process query embedding** (FastEmbed ONNX nomic, 130 MB quantized) | Removes the Ollama round trip and contention (retrieval p95 3.7 s, outliers 14–20 s) | Vectors differ slightly, so the 0.75 guardrail must be recalibrated |
| 7 | **Warm-up on startup:** prefill the system prompt when the container starts | The first question after a restart no longer takes ~95 s | Trivial |

Ruled out: TurboRAG / CacheBlend / LMCache (GPU serving stacks; TurboRAG also needs a fine-tuned model) and llama-server `--cache-reuse` (strict prefix only).

**Being tried first:** ideas 1 + 5 as one bounded experiment (see `docs/CAG_EXPERIMENT_2026-10-01.md`).

## Not planned (researched, poor fit for this machine)

| Idea | Why not |
|---|---|
| Speculative decoding | It only speeds up writing. Ollama doesn't expose it, and the extra draft model costs RAM, which is the scarce resource. |
| Iris Xe GPU (IPEX-LLM / OpenVINO) on Machine A | No measured advantage over the CPU; it shares the same RAM pool. |
| LLMLingua prompt compression | It runs an extra model on the CPU, risks dropping document IDs (which breaks citations), and the context is already only ~700 tokens per question. |
| Gemma 4 e2b/e4b | Listed by Ollama at 4.6–9.5 GB, no smaller than llama3.1:8b. |

## Model bake-off (measured 2026-10-01, Machine A, `/generate/agentic`, 20-question frozen seed)

| Run (`eval/runs/…`) | Tier A | Whole run | LLM-path median / max | Notes |
|---|---|---|---|---|
| `2026-10-01_phi4-14b`* | 15 / 0 FAIL / 4 M / **1 ERROR** | 96.7 min | 382 s / 1,216 s | 10 GB loaded on a 16 GB host → swapping; OpenSearch and embedding timeouts |
| `2026-10-01b_llama3.1-8b` | 16 / 0 / 4 / 0 | 9.3 min | 60 s / 168 s | Tier B: content correct; Q4 calls BUG-1006 "open" (it is In Progress); Q1/Q2 cite the bug report, not TC-0142 |
| `2026-10-01b_qwen3-4b` (default tag = reasoning build) | 16 / 0 / 4 / 0 | 82.5 min | 633 s / 814 s | Writes its reasoning in the answer: 1,300–4,000 output tokens per answer even with `think=false` |
| **`2026-10-01b_qwen3-4b-instruct`** | **16 / 0 / 4 / 0** | **6.9 min** | **44 s / 103 s** | 38–110 output tokens; ~6 tok/s writing; Tier B 9/9 correct against the DB |
| `2026-10-01b_qwen3-4b-instruct_run2` | 16 / 0 / 4 / 0 | 6.8 min | 44 s / 103 s | Repeat run; same result |

\* The phi4 run predates the error-code route, reference expansion and data fix, so it sent 2 more questions to the LLM. It was not re-run: ~1.5 h of swapping, and its RAM footprint, not its accuracy, is the problem.

**Recommendation: `qwen3:4b-instruct` as the default `OLLAMA_CHAT_MODEL`.**
- **Same Tier A as every other model**, and the best hand-checked answers.
- **About 9× faster than phi4:14b on LLM questions** (median 44 s vs 382 s) and **14× faster per full run** (6.9 vs 96.7 min).
- **Uses 2.5 GB**, which leaves ~7 GB of RAM headroom.

**Caveats:**
- The 20-question seed is small and corpus-derived (W1). Confirm on the Step 4 holdout before treating this as settled.
- To switch, the owner changes `.env` (`OLLAMA_CHAT_MODEL=qwen3:4b-instruct`) and runs `docker compose up -d fastapi`.
- **Disk cleanup once the choice is settled** (`ollama rm`): the reasoning build `qwen3:4b` (2.5 GB) is no longer needed; `llama3.1:8b` (4.9 GB) is the fallback.
