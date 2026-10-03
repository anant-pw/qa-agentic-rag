# Pre-encoded context groups ("family KV cache"): experiment, 2026-10-04

**Status: passed its accept criteria as a diagnostic; not yet integrated into the app.**
**Hardware:** Core Ultra 7 155U, 16 GB, CPU-only.
**Model:** `qwen3:4b-instruct`, the same GGUF blob Ollama uses, served by the `llama-server.exe` bundled with Ollama (`-c 8192 -np 1 -fa on --slot-save-path`).
**Script:** `diagnostics/module_cache_probe.py`. Raw output: `eval/runs/module_cache_2026-10-04_*`.

## The idea

Measured on 2026-10-01:
- The LLM spends ~25 s per question reading ~700 tokens of retrieved context.
- Writing is fast only while the context is small (~6 tok/s at ~2k tokens, ~1 tok/s at 19k).
- Saving and restoring llama-server's KV state is nearly free.

Whole-corpus caching (CAG) failed on the second point. This design keeps contexts small and still skips the reading:

- **Offline.** Split the corpus into small groups. For each group, prefill `system prompt + all documents of the group` once and save the KV state to disk.
- **Online.**
  1. Normal retrieval (0.01–0.2 s) picks the group: the group of the named IDs, else the group of the top hybrid hit.
  2. Restore that group's KV file (~0.3 s).
  3. Prefill only the question (15–40 tokens).
  4. Generate.

Closest prior work known to the author: RAGCache and TurboRAG / CacheBlend. They cache per-document KV state, but in GPU serving stacks (TurboRAG also needs a fine-tuned model). This variant uses stock llama.cpp slot save/restore on a CPU and caches pre-composed groups, not single documents. No claim is made that it is unprecedented.

## Accept criteria (set before running)

- Holdout LLM-path ≥ 31/36, with near-duplicate questions 7/7.
- Frozen-seed LLM questions 9/9.
- Median end-to-end first token ≤ 5 s.
- Median end-to-end total ≤ 20 s.

"End to end" includes routing and the slot restore.

## Results

| | RAG today (qwen) | Run A: one group per module (10 groups, ≤ 30 docs) | **Run B: module × original/synthetic (16 groups, 3–21 docs)** | Run B repeat |
|---|---|---|---|---|
| Holdout LLM-path (hand-reviewed) | 31 / 36 | 29 / 36 | **31 / 36** | 31 / 36 (same 5 failures) |
| Near-duplicate | 7 / 7 | 7 / 7 | **7 / 7** | 7 / 7 |
| Frozen-seed LLM questions | 9 / 9 | 9 / 9 | **9 / 9** | 9 / 9 |
| Median first token | ~27 s | 4.6 s | **3.2 s** | 2.7 s |
| Median total | ~48 s | 21.5 s | **14.8 s** | 14.4 s |
| Verdict | — | Fails (accuracy, total) | **Passes all four** | Passes |

Run B details:
- **Build:** 808 s once (16 groups); 6.0 GB on disk (f16 KV).
- **Per question:** restore median 0.32 s; routing median 0.01 s; decode 5.4 tok/s.
- **On the same 36 holdout questions:** median total 13.7 s, against 47.8 s for RAG.

### Why Run A failed and Run B passed
With up to 30 near-identical documents in context, the 4B model lost precision:
- It cited BUG-2022 for BUG-1022's content.
- It answered "not addressed" with BUG-1008 in context.
- It padded a list until the token cap (100 s).

Splitting each module into its original documents and its synthetic family keeps each context close to what top-5 RAG shows.

### Accuracy is equal, not identical (Run B vs RAG)
- **Fixed by the cache path:**
  - A02: lists all 4 open sign-in bugs; no top-5 cap.
  - F04: corrects the false "10-minute" premise.
  - S04: the messy question now finds the fix in BUG-1017.
- **Broken by the cache path:**
  - A01: lists 2 of 3 Safari bugs.
  - P05: "not addressed" while citing BUG-1008.
  - G02: a `status=Open` filter passed as a sentence, not as a retrieval filter; the model listed everything and took 88 s.
- **Wrong in both:** U02, F01 (the 4B model does not correct false premises).

## phi4:14b with the same trick (feasibility probe, 3 questions)

- First token 4.5–14 s, instead of minutes.
- Decode 0.6–1.0 tok/s with 0.3–0.8 GB RAM free, so totals were 29–160 s.

The cache removes the reading time but cannot fix decode speed, which is bound by the 14B model's memory footprint on 16 GB. That is better than phi4 RAG (median 278 s) but not fast. A full phi4 run was not done.

## If this is integrated (not done yet)

- **Process:** a `llama-server` process becomes the generator for the fast path. Ollama stays for embeddings and as the fallback generator.
- **Routing rules:**
  - Use the fast path only when the named IDs or the top hit fall in exactly one group.
  - Questions with a status or doc_type filter (the G02 failure), or with IDs spanning groups, use the existing RAG path.
- **Cache keys:** slot files must be keyed by model and index version, and rebuilt when the index changes (808 s for this corpus). This is the same invalidation rule the Redis cache already uses.
- **Cost:** 6 GB disk (f16 KV); about 3 GB more RAM while both servers hold the model.
- **Acceptance:** the frozen 20 and the holdout must be re-run through the real endpoint before it is accepted.

## Caveats

- 45 questions, one corpus, two identical runs. Small sample.
- Group design used knowledge of this corpus (module field, synthetic-ID pattern). A different corpus needs its own grouping, and group size matters (Run A vs Run B).
- Measured through a diagnostic script, not through `/generate/agentic`.
