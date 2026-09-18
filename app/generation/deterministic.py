"""
Deterministic short-circuit for countable/listable questions -- Phase 7
building block, not yet called from any endpoint.

Rule 6 in app/generation/prompt.py already asks phi4:14b to count/list
retrieved documents correctly, and it already passes today (both
structured_filter questions PASS in Phase 6's eval, confirmed in
eval/run_eval_generation_results.json). This module is an ALTERNATIVE
path, not a replacement forced on anyone -- gated by
settings.deterministic_count_routing (see app/config.py addition below),
default False, so the existing prompt-based behavior keeps working
unchanged until this is deliberately turned on and A/B'd against it.

Real correctness trap avoided here, on purpose: /generate's
context_top_n (5) truncates retrieval results to what actually gets
shown to the LLM. Counting against that slice would silently undercount
any time more than 5 documents match a filter. This module does NOT
reuse hybrid_search()'s top-N context slice -- it re-queries search()
(Phase 3, BM25-only, filter-only capable, unmodified) at a size larger
than the entire corpus, so the count is exact against the filters
actually requested, not against whatever fit in a generation prompt.

Where this plugs in: the future Phase 7 router/guardrail node calls
is_countable_question() on the incoming question; if True AND
deterministic_count_routing is on, it calls answer_countable_question()
instead of running retrieval+generation at all. Not wired here because
that node doesn't exist yet -- this file has zero import-time
dependency on anything Phase-7-specific, so it can be added to the repo
now without touching /generate or any other live endpoint.
"""

import re

from app.search.service import search, count_documents


# Corpus was 28 real documents as of Phase 2's ingest (25 bug_report + 3
# test_case) -- confirmed in phase-02-handoff.md Section 4, unchanged
# through Phase 6. Set well above that so a filtered COUNT never
# silently truncates. Revisit if the corpus genuinely grows past this.
MAX_LISTED_IDS = 100

_COUNT_PATTERNS = [
    r"\bhow many (bugs?|bug reports?|test cases?|documents?)\b",
    r"\bcount of (bugs?|bug reports?|test cases?|documents?)\b",
]
_LIST_PATTERNS = [
    r"\blist all\b",
    r"\blist every\b",
    r"\bshow all\b",
    r"\bwhich (bugs|documents|test cases) are\b",
]


def is_countable_question(question: str) -> bool:
    """Heuristic keyword match, not NLU -- will miss rephrasings outside
    this list. That's an accepted trade-off of an opt-in, reversible
    shortcut: it's meant to be tested against real questions and
    extended only when a real miss is observed, not pre-guessed against
    phrasings that don't exist yet in eval_seed.json or real usage.
    """
    q = question.lower()
    return any(re.search(p, q) for p in _COUNT_PATTERNS + _LIST_PATTERNS)


def answer_countable_question(client, alias, doc_type, module, status):
    n = count_documents(client, alias=alias, doc_type=doc_type, module=module, status=status)
    if n == 0:
        return "No documents match the given filters.", []

    hits = search(client, alias=alias, doc_type=doc_type, module=module, status=status,
                  size=min(n, MAX_LISTED_IDS))
    ids = [h["external_id"] for h in hits]

    if n > len(ids):
        answer = f"{n} document(s) match. Showing {len(ids)}: {', '.join(ids)} ({n - len(ids)} more not shown)."
    else:
        answer = f"{n} document(s) match: {', '.join(ids)}."

    sources = [{"external_id": h["external_id"], "title": h["title"], "doc_type": h["doc_type"]} for h in hits]
    return answer, sources
