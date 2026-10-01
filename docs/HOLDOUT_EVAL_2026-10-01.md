# Holdout eval: qwen3:4b-instruct vs phi4:14b (2026-10-01)

**Question:** what does the small model (qwen3:4b-instruct, now the default) get wrong compared to the larger phi4:14b?
**Set:** `eval/holdout.json`, 41 questions written by hand against the 173-doc corpus. Every expected fact was read from Postgres. Runner: `eval/run_holdout.py`.
**Runs:** `eval/runs/holdout_2026-10-01_qwen3-4b-instruct/` (all 41) and `eval/runs/holdout_2026-10-01_phi4-14b/` (the 36 questions that reached the LLM; the other 5 were answered by a no-LLM route and are identical for both models).

## 1. Results on the 36 LLM-path questions

| | qwen3:4b-instruct | phi4:14b |
|---|---|---|
| Automatic grader | 30 / 36 | 32 / 36 |
| **After reading every FAIL by hand** | **31 / 36** | **32 / 36** |
| Median / max time per question | **48 s / 122 s** | 278 s / 751 s |
| Total time for the 36 | **33 min** | 186 min |

The grader was too strict in two places, and both were corrected:
- **Abstain phrase list:** "do not address this question" was missing. This was fixed in `ABSTAIN_PHRASES`, and the qwen run was re-graded.
- **A01 (qwen):** the grader flags any mention of a forbidden ID, but qwen named BUG-2052/2001 only to explain why they were excluded. Its list is exactly right.

Conclusion: **the accuracy gap is small (1 question in 36), the speed gap is large (~5.7×).** The gap is concentrated in one behaviour, described below.

## 2. Per-category comparison

| Category | n | qwen | phi4 | Notes |
|---|---|---|---|---|
| Paraphrase, single doc | 7 | 7 | 7 | Equal |
| Paraphrase, multi doc | 1 | 1 | 1 | Equal |
| Near-duplicate discrimination (synthetic families) | 7 | 7 | 7 | Both pick the right one of many near-identical bugs |
| Multi-hop (bug → cited TC, duplicates → error code) | 4 | 4 | 4 | Both overclaim slightly on M02: they say BUG-1002 "reports ERR_401_UNAUTH", but only BUG-1001 states it |
| Compare two docs | 2 | 2 | 2 | Equal |
| Filtered semantic (module/status/doc_type) | 3 | 3 | 3 | Equal |
| Aggregation, open set | 3 | 2 (hand) | 1 | **phi4 missed BUG-2039 in A01** even though it was in context; qwen listed all 3. A02 fails for both (see 4.2) |
| Unanswerable (no such field) | 5 | 4 | 4 | Both abstain correctly. Neither says "BUG-1009 is still Open" when asked when it was fixed (U02) |
| False premise | 3 | **1** | **3** | **The real gap** (see 3.1) |
| Retrieval-limited (S04) | 1 | 0 | 0 | Same context for both (see 4.3) |

## 3. Weaknesses of qwen3:4b-instruct relative to phi4:14b

### 3.1 It does not correct false premises (the main gap)
- **F01:** "Why was BUG-1025 fixed so quickly?" BUG-1025 is **Won't Fix**, and its context header says `status=Won't Fix`.
  - qwen: "The retrieved documents do not address this question."
  - phi4: "…it is marked with a status of 'Won't Fix', indicating that no resolution or fix was applied."
- **F04:** "What does TC-0142 say about the 10-minute cooldown?" TC-0142 says **5 minutes**.
  - qwen: "…do not address this question."
  - phi4: "TC-0142 specifies a 5-minute cooldown… There is no mention of a 10-minute cooldown."

**Pattern:** when the question asserts something the document contradicts, qwen falls back to the generic abstain sentence instead of stating the documented value. The answer is not wrong, but it is unhelpful and misses a correction that a QA user needs. It handled F03 (a duplicate vs related premise) correctly, likely because Rule 3 tells it to report recorded relationships verbatim.

### 3.2 It misreads status when filtering a list (A02)
"Which **open** bugs involve users who cannot sign in…?" With the same 5 documents in context:
- qwen listed BUG-2024 and BUG-1002, and **claimed BUG-2051 is "not open"**. That is wrong: its status is Open.
- phi4 listed BUG-2024 and BUG-2051 correctly.

### 3.3 It is less precise in open-ended lists (A03)
qwen padded "bulk export crashing on large exports" with three unrelated synthetic "times out over 17/39 rows" bugs. phi4 listed only BUG-1007 and BUG-1017. Both still named the fix.

### Where qwen was better
- **A01:** qwen listed all 3 Safari 17 DST bugs; phi4 dropped BUG-2039.
- Answers are shorter and arrive about 5.7× faster.

## 4. Weaknesses shared by both models (not a model problem)

These would not be fixed by switching to phi4 or by fine-tuning.

### 4.1 The guardrail rejects short or messy questions (S01–S03)
- "dup notif bug??", "checkout 504" and "lockout after 3 tries what should happen" were rejected as out of domain before any LLM call.
- S03 even retrieved TC-0142 first.
- This is the known short-fragment limitation of the 0.75 cosine threshold (PROJECT_LOG W5 / `id_resolution.py`).

### 4.2 The top-5 context caps list answers (A02)
There are 4 Open "cannot sign in" bugs, but retrieval put 2 Resolved/Closed ones in the top 5. So BUG-2043 and BUG-2108 never reached either model.

### 4.3 Messy phrasing misses the key document (S04)
"export broke >5000 rows fixed??" retrieved BUG-1007 but not BUG-1017 (the fix), so both models said no resolution is stated. This is the first measured retrieval miss that a query-rewrite step could fix. It is new evidence against decision D14 ("no query-rewrite node").

### 4.4 Status is not used when abstaining (U02)
"When was BUG-1009 fixed?" Both say "do not state a resolution" without adding that its status is Open.

## 5. What to do about it (cheapest first)

| Fix | Targets | Effort | Notes |
|---|---|---|---|
| Prompt rule: *if the question assumes a fact the documents contradict (status, number, relationship), say so and give the documented value* | 3.1, 4.4 | ~1 h + eval | A positive worked example only (Phase 6 showed negative examples backfire). Re-run both holdout and frozen 20 on qwen. |
| Deterministic status filter: when the question says "open/closed/resolved bugs" and no status filter was passed, apply it before retrieval | 3.2, 4.2 | ~half day | Same pattern as the count route; removes the model's chance to misread status. |
| Query rewrite only for short/messy questions (≤ 6 words or no verb) | 4.1, 4.3 | ~1 day | Now justified by S01–S04; use the small model itself to expand the question, then retrieve. |
| Larger context for list questions (top 8 instead of 5) | 4.2 | small | Costs ~+400 prompt tokens ≈ +10 s on qwen. |
| Fine-tuning (Step 6) | 3.1–3.3 | days | **Not recommended yet.** The gap is 1/36 and is a prompt-addressable behaviour. Revisit only if the prompt rule fails. |

## 6. Caveats
- One run per model at temperature 0.1. Earlier repeat runs on the frozen 20 were identical, but this set was not repeated.
- n = 36 is small. A 1-question gap is within noise. The **false-premise pattern (2/2 for phi4, 0/2 for qwen)** is the only consistent qualitative difference.
- phi4 ran on a 16 GB machine under memory pressure. That affects its speed, not its answers.

## 7. Attempted fix: prompt Rule 8 (one bounded attempt, reverted)

- **Change:** an agentic-only system prompt (so `/generate` stayed byte-identical) that added Rule 8 ("if the question assumes something the documents contradict, correct it"), with a positive example. It also stripped a ~230-word developer note that sits inside the `SYSTEM_PROMPT` string.
- **Accept criteria, set before running:** F01 and F04 pass, and no new failures on the holdout or the frozen 20.
- **Result** (`eval/runs/holdout_2026-10-01_qwen3-4b-instruct_rule8/`, `eval/runs/2026-10-01c_qwen3-4b-instruct_rule8/`):
  - Holdout: **0 questions changed**. F01, F04 and U02 still answer "The retrieved documents do not address this question."
  - Frozen 20: **15 PASS / 1 FAIL** (was 16/0). On the BUG-1024 duplicate question it listed only BUG-1001 and dropped BUG-1002.
- **Decision: reverted.** The false-premise gap (3.1) stays a **documented known limitation of qwen3:4b-instruct**. Two options if it ever matters:
  - Use phi4:14b on hardware with enough RAM.
  - Revisit with fine-tuning (NEXT_STEPS_PLAN Step 6), which now has a concrete, measured target.
- **Separate finding kept for the record:** the developer note inside the `SYSTEM_PROMPT` string ("--- Session finding: garbled question text …") is sent to the model on every call by both endpoints. It is left in place because `/generate` is frozen, and removing it alone was not measured.
