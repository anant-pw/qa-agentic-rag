"""
Query construction for the qa_documents index.

Why exact lexical matching matters for THIS corpus, concretely:

- Bug/TC IDs (BUG-1013, TC-0142, TC-0209): these are identifiers, not
  concepts. Vector search would happily return "similar" bug reports
  for a query like "BUG-1013" because embeddings don't know an ID is
  supposed to be an exact key -- they'd match on surrounding language
  instead. BM25 with a term-query boost on `external_id` /
  `mentioned_bug_ids` / `mentioned_tc_ids` does the right thing: exact
  ID in, exact document(s) out, full stop. This is the textbook case
  for keeping keyword search even after Phase 4 adds embeddings.

- Error codes (ERR_401_UNAUTH, ERR_504_TIMEOUT, ...): same problem,
  worse, because these look like meaningless tokens to a general text
  embedding model (subword pieces of "ERR", "401", "UNAUTH" carry no
  real semantic content on their own), but they're exact, high-value
  filter keys in this domain (see generate_eval_seed.py's
  error_code_aggregation eval questions, which are precisely this).

- Status / module / version strings: low-cardinality, exact-value
  fields where "similar" is meaningless -- a bug is either status=Open
  or it isn't. These belong in a `filter` clause (not scored, cached,
  cheap), not a `should` clause.

- Natural-language QA questions ("why does login keep failing after
  password reset") are the one case BM25 is NOT the ideal tool for --
  it will only find documents sharing literal vocabulary with the
  query, not paraphrases. That's the honest limitation to demonstrate
  in query 4 below, and it's exactly the gap Phase 4's embeddings are
  scoped to close. BM25 isn't wrong here, it's incomplete -- worth
  showing, not hiding.
"""

TEXT_FIELDS = ["title^2", "cleaned_text"]
EXACT_ID_FIELDS = ["external_id", "mentioned_bug_ids", "mentioned_tc_ids", "error_codes"]


def build_search_query(
    q: str | None = None,
    doc_type: str | None = None,
    module: str | None = None,
    status: str | None = None,
    size: int = 10,
) -> dict:
    """Bool query: filter clauses for exact metadata (unscored, cheap),
    should clauses for relevance (BM25 text match + exact-ID term
    boost). If `q` is empty, this degrades to a pure filtered listing
    (match_all inside the scored portion)."""
    filters = []
    if doc_type:
        filters.append({"term": {"doc_type": doc_type.lower()}})
    if module:
        filters.append({"term": {"module": module.lower()}})
    if status:
        filters.append({"term": {"status": status.lower()}})

    if not q:
        return {
            "query": {"bool": {"must": [{"match_all": {}}], "filter": filters}},
            "size": size,
        }

    should = [
        {
            "multi_match": {
                "query": q,
                "fields": TEXT_FIELDS,
                "type": "best_fields",
            }
        }
    ]
    # Boosted exact-match: if the query text IS an ID/code, this term
    # clause dominates the score regardless of how the analyzer would
    # have tokenized it in the text fields. Restricted to single-token
    # queries -- a multi-word natural-language query can never term-match
    # a keyword field, so adding these clauses for it is pure overhead
    # (found during dry-run testing against synthetic queries, not
    # theoretical: a 6-word sentence was generating 4 always-false term
    # clauses per search).
    q_stripped = q.strip()
    if " " not in q_stripped:
        q_lower = q_stripped.lower()
        for field in EXACT_ID_FIELDS:
            should.append({"term": {field: {"value": q_lower, "boost": 5.0}}})

    return {
        "query": {
            "bool": {
                "should": should,
                "minimum_should_match": 1,
                "filter": filters,
            }
        },
        "size": size,
    }
