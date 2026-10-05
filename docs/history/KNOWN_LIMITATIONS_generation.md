# Known Limitation: phi4-mini hedges on resolution/citation questions
regardless of prompt wording — confirmed via cross-model diagnostic

## Symptom

Two Tier-A deterministic checks in `eval/run_eval_generation.py` fail
consistently against live `/generate` output:

- `resolution_in_narrative` (BUG-1017): asks for the fix. BUG-1017's
  `description` field genuinely contains one, in prose, not under a
  labeled field (schema.sql has no resolution column). phi4-mini answers
  "The retrieved documents do not state a resolution for BUG-1017"
  every time, despite BUG-1017 being correctly retrieved and its full
  Description text being present in the prompt context.
- `dangling_reference_citation` (BUG-1013 → TC-0209): asks what the
  cited test case's expected result is. TC-0209 was never ingested
  (`document_references.target_document_id IS NULL` — a deliberate
  Phase 2 design decision, not a bug). phi4-mini answers with the
  *identical* resolution-abstention sentence, even though the question
  has nothing to do with a resolution.

## Investigation (in order, each ruling out one variable)

1. **Context availability** — confirmed NOT the cause. `format_doc_context()`
   includes the full, unfiltered `Description`/`Steps` text for every
   retrieved bug report; no truncation, no keyword filtering. Verified by
   reading the function directly, not assumed.
2. **Prompt wording precision** — confirmed NOT the cause. Three system
   prompt variants were tried against the live system, in order:
   (a) original Rule 2 (single abstention rule covering all cases),
   (b) Rule 2 rescoped to require reading Description text before
   abstaining + new Rule 2b splitting out the citation case, (c) same as
   (b) plus a concrete worked few-shot example matching the BUG-1017
   scenario almost exactly. All three produced the identical failure on
   both questions, unchanged.
3. **Model capacity** — confirmed AS the cause. `diagnostics/groq_probe.py`
   reconstructs the exact same retrieval, context blocks, and system
   prompt (variant c) that production `/generate` builds, and sends it to
   Groq-hosted `openai/gpt-oss-120b` instead of local `phi4-mini:latest`,
   with temperature pinned to 0.1 on both sides to control for
   determinism. Same input, different model:
   - BUG-1017: Groq correctly extracted and paraphrased the actual fix
     ("changed the export query to stream data using cursor-based
     pagination instead of loading the entire dataset into memory...
     verified with a 12,000-row export") from unlabeled Description
     prose. phi4-mini still hedged.
   - BUG-1013: Groq correctly distinguished the citation case from the
     resolution case per Rule 2b. phi4-mini still applied Rule 2's
     sentence to a Rule 2b situation. (Caveat: Groq's phrasing here is
     close to the worked example's own wording, so this result leans
     more on instruction-following than the BUG-1017 result, which was
     genuine free-text extraction — treat BUG-1017 as the stronger of
     the two data points.)

## Conclusion

[Certain] This is a real ceiling in phi4-mini's ability to extract an
implicit fact from unlabeled free text and discriminate between two
similarly-worded but semantically different instructions, not a defect
in this project's retrieval, context construction, or prompt design.
Confirmed by holding retrieval/context/prompt constant and varying only
the generation model.

## Decision

Not fixed in Phase 6. Rule 2/2b changes are KEPT (more correct
regardless of this specific case, and may still help other phrasings).
The production system continues to use Ollama/phi4-mini per the
project's local-first requirement — this diagnostic does not argue for
switching the default model, only for documenting where its limits are.
`diagnostics/groq_probe.py` is retained as a reusable tool for any future
case where "is this a prompt bug or a model-capacity ceiling" needs a
real answer instead of another guess.

## If revisited later

A larger local model (if hardware allows) or a hybrid approach (small
model for retrieval-adjacent tasks, escalate specific question types to
a cloud call) would be the two legitimate paths — both are real
architecture decisions requiring their own trade-off discussion, not
something to back into from this diagnostic alone.
