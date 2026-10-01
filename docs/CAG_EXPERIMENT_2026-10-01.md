# CAG + context compaction experiment (2026-10-01): rejected on this hardware

**Ideas tested:** `NEXT_STEPS_PLAN.md` Step 9, idea 1 (cache-augmented generation: the whole corpus in a persistent KV cache, no per-question context prefill) together with idea 5 (corpus compaction).
**Hardware:** Machine B, Core Ultra 7 155U, 16 GB, CPU-only.
**Model:** `qwen3:4b-instruct`, the same GGUF blob Ollama uses, served by the `llama-server.exe` that ships inside Ollama (`-c 32768 -np 1 -fa on -ctk q8_0 -ctv q8_0 --slot-save-path`; 7 threads auto-selected).
**Script:** `diagnostics/cag_probe.py`. Raw output: `eval/runs/cag_2026-10-01/`.

**Accept criteria, set before the run:**
- Holdout LLM-path ≥ 31/36, with near-duplicate questions 7/7.
- No new FAIL on the 9 frozen-seed LLM questions.
- Median first token ≤ 10 s.
- Median total ≤ 48 s.

## Results

**Compaction (idea 5):**
- Dropping the 120 synthetic bugs' templated steps, and their descriptions where they repeat the title, gave a whole-corpus context of **18,019 tokens** (55,902 characters).
- With the 1,400-token system prompt, the fixed prefix is **19,445 tokens**.

**Priming (one-time):** **6,638 s (1 h 51 min)**. Prompt processing slowed steadily as the context grew:

| Tokens processed | 2,048 | 4,096 | 6,144 | 8,192 | 10,240 | 12,288 | 14,336 | 16,384 |
|---|---|---|---|---|---|---|---|---|
| Cumulative tok/s | 24.1 | 16.4 | 10.9 | 7.8 | 6.0 | 4.9 | 4.2 | 3.6 |

**Persistence:** saving the slot took **2.4 s** (1.52 GB file); restoring it after a server restart took **1.0 s**. This part works well.

**Per question** (the server's prompt cache reused 19,433 tokens each time; only 20–30 new tokens were prefilled):

| Question | First token | Total | Output tokens | Graded | RAG on the same question (qwen, holdout run) |
|---|---|---|---|---|---|
| A01: list the Safari 17 DST bugs | 19.6 s | 266 s | — | **FAIL**: missed BUG-2039 and BUG-2091, included BUG-2001 | PASS (hand-reviewed), 111 s |
| A02: open "cannot sign in" bugs | 17.6 s | 303 s | 297 in 285 s | **PASS** (the whole corpus is visible, so no top-5 cap) | FAIL (retrieval cap), 73 s |
| N01: near-duplicate (Safari 17, > 29) | 23.8 s | 64 s | 44 in 41 s | PASS | PASS, 83 s |

The run stopped after 3 questions. The `llama-server` process hit the 2-hour background-task limit during question 2, and the restore test covered the remaining two. The criteria were already clearly failed, so the full 45 were not run.

## Why it fails here

- **Writing speed collapses with long context.** It runs at about **1.0–1.1 tokens/s** with a 19.4k-token prefix, against about 6 tokens/s for normal RAG with qwen (~2.1k-token prompts). On this CPU, attention over the whole prefix dominates every generated token, so CAG trades a ~25 s prefill for slower writing on every answer.
- **The first token improves only slightly** (18–24 s vs ~27 s). Even 20–30 new tokens are slow to prefill at position ~19k.
- **Quality is mixed.** A whole-corpus view fixes the top-5 list cap (A02), but on A01 the model lost track of near-identical documents in a long context.

## Decision

**Rejected for this hardware and model.** It fails the latency criteria by about 5–6× on total time and regresses A01.

What would change the verdict: a GPU or NPU backend, where long-context attention is cheap, or a much smaller corpus prefix. The persistence mechanism (save 2.4 s / restore 1.0 s) is proven and reusable if that happens.

**Idea 5 (compaction) on its own** was not tested in the RAG path. Applying it there would change `format_doc_context()`, which is shared with the frozen `/generate`. It stays on the shortlist as an untested idea.

**Side finding:** priming time grows super-linearly with context on CPU (24 → 3.6 tok/s by 16k tokens). This is the same reason RAG's ~700-token per-question context is the right size for this machine.
