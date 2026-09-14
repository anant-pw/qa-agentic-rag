"""
System prompt and message construction for Phase 5 generation.

Grounding is the primary constraint, not an afterthought -- see the Phase 5
readiness report for the reasoning behind each rule's wording. Two failure
modes are kept explicitly separate (Rule 2 vs Rule 4) because they're
genuinely different: "the field exists in the corpus's schema but is empty
for this document" (e.g. no bug report has a resolution field at all --
confirmed against schema.sql, this isn't hypothetical) versus "retrieval
didn't surface anything relevant to this question." Collapsing them into one
"if unsure, say so" instruction is exactly what eval_seed.json's
duplicate_resolution / related_resolution question types are designed to
catch a model getting wrong.

Doc-type-aware context (not raw cleaned_text) is built here per document --
see format_doc_context(). This is why context.py's Postgres lookup exists:
the structured fields aren't in OpenSearch's _source at all.

--- Phase 6 finding: Rule 2 was firing as a generic fallback, not scoped
to what it says it's for ---

The first live run of eval/run_eval_generation.py (Phase 6) surfaced a
real bug, not an eval-script artifact: phi4-mini used Rule 2's exact
abstention sentence ("The retrieved documents do not state a resolution
for BUG-XXXX") for TWO questions that were not asking about a resolution
at all -- a test-case-citation lookup (BUG-1013/TC-0209) and a
resolution-in-narrative lookup (BUG-1017, whose Description field
genuinely contains the fix in prose: "Fix deployed using cursor-based
pagination... Verified with 12000-row export"). Confirmed via real
/generate output, not inferred: both retrieved the right document and
both still produced the identical fallback sentence. Root cause,
[Likely] not [Certain] without more probing: the model can't reliably
tell "no fix mentioned anywhere" from "fix mentioned but not under a
labeled heading," since format_doc_context() never emits a "Resolution:"
line (this corpus has no such field -- confirmed against schema.sql).
Rule 2 is now scoped explicitly to require reading Description/Steps
text before concluding nothing is there, and a new Rule 2b handles the
citation-lookup case separately so the same sentence stops being reused
for two different situations.

--- Phase 6 finding #2: a NEGATIVE few-shot example backfired on a small
model, made the over-triggering WORSE, not better ---

The first fix (Rule 2/2b rewrite + a worked example) was tested via
diagnostics/groq_probe.py against a much larger model (Groq gpt-oss-120b)
and confirmed correct on both target cases -- but a subsequent full
eval/run_eval_generation.py sweep against the actual production model
(phi4-mini) showed real regressions on PREVIOUSLY PASSING questions:
cross_reference_bug_to_test_case (BUG-1001/BUG-1002, pure test-case
lookups, nothing to do with resolution) and duplicate_resolution
(BUG-1011, a relationship question) both started returning the exact
Rule 2 abstention sentence, verbatim, for questions Rule 2 has nothing to
do with. Root cause, [Likely]: the worked example's negative framing --
"...it does NOT say 'the retrieved documents do not state a
resolution'..." -- put the literal target string directly in the prompt.
A small model doing shallow surface pattern-matching can anchor on a
salient phrase regardless of the negation wrapped around it, making the
sentence MORE likely to surface as a generic fallback, not less. The
example was rewritten to be purely positive (shows only the correct
output, never reproduces the wrong sentence anywhere in the prompt) --
general lesson for prompting small local models: negative few-shot
examples are a real risk, not just a style choice, and should be tested
against the actual deployed model, not just a larger stand-in.

--- Phase 6 finding #3: structured_filter (counting) questions fail via
Rule 4, not Rule 2 -- a genuine aggregation-reasoning gap ---

Confirmed via the same live sweep: for "how many bugs are Open in the
Login module," retrieval correctly returned exactly the matching
documents (all 4 for Login/Open, all 3 for Payments/Open -- the earlier
eval-script bug that never sent module/status filters to /generate was
already fixed by this point), but the model answered "The retrieved
documents do not address this question" -- Rule 4's sentence -- instead
of counting what it was given. This is a distinct failure mode from
findings #1/#2: not an over-triggered abstention, but a failure to
recognize that tallying provided documents IS answering a "how many"
question. New Rule 6 added to name this task explicitly.

--- Rule 3 rewrite: report recorded references, don't re-infer them ---

The original Rule 3 ("only state a relationship if explicitly present in
the text") assumed the retrieved TEXT was the only source of truth for
duplicate/related/citation classifications. It isn't -- Phase 2's
resolve_explicit_bug_mentions() already resolved these classifications
deterministically into document_references, and free text alone is often
genuinely ambiguous (e.g. "Might be same root cause as BUG-1001, filed
separately" defensibly reads as either "related" or "duplicate" to a human,
let alone a model). Verified as a real failure mode, not theoretical: the
same duplicate-classification question asked four times against unchanged
retrieval produced four different answers. Rule 3 now tells the model a
"Recorded cross-references" section (built by
context.fetch_references() + format_doc_context() below) is the resolved
answer and should be reported verbatim, with free-text inference as a
fallback only when no recorded reference exists.
"""

SYSTEM_PROMPT = """You are a QA knowledge assistant answering questions about a corpus of bug \
reports and test cases. You will be given retrieved documents and a question.

Rules, in order of priority:

1. Answer using ONLY the information in the retrieved documents provided in \
the user message. Do not use outside knowledge about software bugs, \
testing, or this domain.

2. If the question asks SPECIFICALLY about a resolution, root cause, or \
fix: first check whether the retrieved document's Description or Steps \
text already describes what was done, even though no line is ever \
labeled "Resolution" in this corpus (bug reports here have no such \
field -- a fix may appear as ordinary prose inside Description, e.g. \
"Fix deployed using cursor-based pagination..."). Read that text before \
concluding nothing is there. Only say "The retrieved documents do not \
state a resolution for BUG-XXXX" (substituting the real ID, never a \
placeholder like "[bug ID]") if you have read the full Description/Steps \
and genuinely find no fix, root cause, or resolution described anywhere \
in it. Do not infer, guess, or generate a plausible-sounding fix. Do NOT \
use this sentence for any question that is not itself asking about a \
resolution, root cause, or fix -- for a question about a cited test \
case's expected result, a duplicate/related classification, or anything \
else, this sentence does not apply; see Rules 2b, 3, and 4 instead.

2b. If the question asks about a test case cited by a bug report, and a \
"Recorded cross-references" line shows that citation, but the cited test \
case's own content was not retrieved (or the citation is marked as not \
present in the corpus): say so explicitly, naming both IDs, e.g. \
"BUG-1013 cites TC-0209, but TC-0209 is not present in the retrieved \
corpus." Do not use Rule 2's resolution-abstention sentence for this \
case -- a missing cited test case is not the same situation as a missing \
resolution, and conflating them produces a misleading answer. Do not \
invent or guess at what the missing test case might say.

3. Some retrieved documents include a "Recorded cross-references" line. \
This states the ACTUAL relationship type (duplicate_of, related_to, or \
test_case_citation) between two documents, already determined by this \
system -- it is not your job to re-derive it. If a recorded \
cross-reference exists for the documents relevant to the question, report \
that relationship exactly as recorded (duplicate_of means duplicate; \
related_to means related but NOT a duplicate -- these are different \
answers, do not blur them). Do not reinterpret, hedge, or override a \
recorded relationship based on your own reading of the documents' \
free-text descriptions. Only infer a relationship from descriptive text \
when NO recorded cross-reference is present for the documents in \
question, and say plainly that you are inferring, not citing a recorded \
relationship, when you do.

4. If none of the retrieved documents answer the question at all, say: \
"The retrieved documents do not address this question." Do not speculate \
about whether a better answer might exist elsewhere in the corpus that \
wasn't retrieved.

5. When you state a fact, name the specific document ID(s) it came from, \
using exactly this format at the end of your answer: (Source: BUG-XXXX) \
or (Source: TC-XXXX) -- substituting the real ID, comma-separated if \
citing more than one, e.g. "(Source: BUG-1001, BUG-1002)". Use this \
exact citation format every time, whether the answer is a single fact, \
a list, or a count.

6. If the question asks for a count ("how many...") or asks you to list \
all documents matching some condition (module, status, error code): \
count or list only the retrieved documents provided to you that match, \
and state the number as a digit. Computing a count or list from the \
documents you were given IS answering the question -- do not say the \
retrieved documents "do not address this question" just because no \
single document states a total by itself.

Example of correctly extracting a fix from unlabeled prose (Rule 2): a \
retrieved document's Description reads "Root cause was a stale session \
token. Fix deployed using cursor-based pagination on 2026-01-15, \
verified with a 12,000-row export." If asked for this document's \
resolution, the correct answer is: "The fix was to deploy cursor-based \
pagination to address a stale session token issue, verified with a \
12,000-row export. (Source: BUG-XXXX)" -- a resolution IS present in \
Description text even without a labeled Resolution field.

Example of correct citation format on a multi-item answer (Rule 5): if \
two test cases both qualify, the correct citation is: "1. TC-0301 \
requires both 'wifi' and 'wi-fi' spellings in the catalog. 2. TC-0302 \
requires a unique SKU with similar competing descriptions. (Source: \
TC-0301, TC-0302)" -- one citation block at the end, in the exact \
(Source: ...) format, not a separate label per item."""


def format_doc_context(hit: dict, structured: dict, references: list[str] | None = None) -> str:
    """One retrieved document's context block, doc_type-aware.

    `hit`: a single result dict from hybrid_search() -- needs external_id,
    title, doc_type, module, status.
    `structured`: the matching row from fetch_structured_fields(), or {} if
    none was found (treated as all-fields-absent, not an error).
    `references`: formatted lines from fetch_references(), or None/[] if
    this document has no recorded cross-references -- the section is
    omitted entirely in that case, not printed as an empty stub, so the
    model isn't misled into thinking "no references" was itself a recorded
    fact rather than an absence.
    """
    doc_type = hit.get("doc_type")
    header = (
        f"[{hit.get('external_id')}] {hit.get('title')} "
        f"(doc_type={doc_type}, module={hit.get('module')}, status={hit.get('status')})"
    )

    if doc_type == "bug_report":
        body = (
            f"Description: {structured.get('description') or '(none recorded)'}\n"
            f"Steps to reproduce: {structured.get('steps_to_reproduce') or '(none recorded)'}"
        )
        # Deliberately no "Resolution:" line -- schema.sql's bug_reports
        # table has no resolution/fix column at all. Omitting the line
        # rather than printing "Resolution: (none recorded)" avoids
        # implying resolution tracking exists and just wasn't filled in.
    elif doc_type == "test_case":
        body = (
            f"Preconditions: {structured.get('preconditions') or '(none recorded)'}\n"
            f"Steps: {structured.get('steps') or '(none recorded)'}\n"
            f"Expected result: {structured.get('expected_result') or '(none recorded)'}"
        )
    else:
        # Defensive fallback only -- doc_type is a CHECK-constrained column
        # in schema.sql (bug_report | test_case), so this branch should be
        # unreachable against real data. Not silently dropping the document
        # if it somehow happens.
        body = "(unrecognized doc_type -- no structured fields available)"

    block = f"{header}\n{body}"

    if references:
        ref_lines = "\n".join(f"- {r}" for r in references)
        block += f"\nRecorded cross-references:\n{ref_lines}"

    return block


def build_messages(question: str, context_blocks: list[str]) -> list[dict]:
    """Builds the messages list for Ollama's /api/chat. Context blocks go in
    the user message (not the system prompt) so the system prompt stays
    static across requests -- only the retrieved-document content and the
    question actually vary per call."""
    if context_blocks:
        context = "\n\n".join(context_blocks)
    else:
        # Empty retrieval is a real, reachable state (e.g. an overly narrow
        # doc_type/module/status filter combined with a question that
        # matches nothing) -- Rule 4 is what's supposed to fire here.
        context = "(no documents were retrieved for this question)"

    user_content = f"Retrieved documents:\n\n{context}\n\nQuestion: {question}"

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
