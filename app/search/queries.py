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

--- Phase 4 addition: embedded-ID extraction for multi-word queries ---

Phase 3's exact-ID boost only fired when the ENTIRE query was a single
token -- correct for a bare "BUG-1013" query, but it meant a full
sentence that happens to CONTAIN an ID or error code (e.g. "Which bug
reports are associated with ERR_401_UNAUTH...") got no boost at all,
even though the ID is right there in the text. Confirmed as a real,
not theoretical, problem: Phase 4's eval baseline showed BUG-1015
ranked bm25_rank=12 on the ERR_401_UNAUTH aggregation question --
multi_match's bare-word overlap scoring buried a document whose only
real relevance signal was the error code embedded in the question.

Fix: `extract_embedded_ids=True` scans the query text for BUG-XXXX /
TC-XXXX / ERR_XXXX-shaped substrings (same pattern family ingest.py and
indexer.py already use) REGARDLESS of overall query length, and adds
boosted term clauses for whatever it finds, in addition to (not instead
of) the existing single-token-whole-query path.

This defaults to `False` and is NOT applied by /search (Phase 3's
frozen endpoint) -- only hybrid_search()'s BM25 leg passes `True`. Two
reasons: (1) preserving byte-identical /search output was an explicit
Phase 4 constraint, and this flag would change ranking for any
multi-word query containing an embedded ID; (2) this was found and
fixed IN Phase 4, using Phase 4's eval infrastructure -- attributing it
to Phase 3's shipped endpoint after the fact would misrepresent when
and how it was actually caught.
"""

import re

TEXT_FIELDS = ["title^2", "cleaned_text"]
EXACT_ID_FIELDS = ["external_id", "mentioned_bug_ids", "mentioned_tc_ids", "error_codes"]

# Same ID shapes ingest.py (BUG_ID_RE/TC_ID_RE) and indexer.py already
# extract at ingest/index time -- duplicated here for the same reason
# indexer.py duplicates them rather than importing from ingest.py: this
# module should stay runnable without ingest.py's CSV/markdown parsing
# dependencies. A third copy of the same drift risk already flagged in
# indexer.py and phase-03-handoff.md -- not new, not hidden.
EMBEDDED_ID_PATTERNS = [
    re.compile(r"\bBUG-\d{4}\b", re.IGNORECASE),
    re.compile(r"\bTC-\d{4}\b", re.IGNORECASE),
    re.compile(r"\bERR_[A-Z0-9_]+\b", re.IGNORECASE),
]


def build_search_query(
    q: str | None = None,
    doc_type: str | None = None,
    module: str | None = None,
    status: str | None = None,
    size: int = 10,
    extract_embedded_ids: bool = False,
) -> dict:
    """Bool query: filter clauses for exact metadata (unscored, cheap),
    should clauses for relevance (BM25 text match + exact-ID term
    boost). If `q` is empty, this degrades to a pure filtered listing
    (match_all inside the scored portion).

    `extract_embedded_ids`: OFF by default -- this is what keeps
    /search's output byte-identical to Phase 3. See module docstring's
    Phase 4 addition section for why this exists and why it's opt-in,
    not the new default."""
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
    elif extract_embedded_ids:
        # Multi-word query, but it may still CONTAIN an ID/error code
        # worth boosting -- see module docstring. Deliberately an
        # `elif`, not a separate `if`: if the whole query were a single
        # token, the branch above already added the exact boost; no
        # need to also run the substring scan on it.
        found_ids = set()
        for pattern in EMBEDDED_ID_PATTERNS:
            found_ids.update(m.group(0).lower() for m in pattern.finditer(q_stripped))
        for id_value in found_ids:
            for field in EXACT_ID_FIELDS:
                should.append({"term": {field: {"value": id_value, "boost": 5.0}}})

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


def build_filters(doc_type: str | None, module: str | None, status: str | None) -> list[dict]:
    """Shared filter-clause construction so BM25 and k-NN legs of hybrid
    search apply identical filters. Pulled out of build_search_query()
    rather than duplicated -- Phase 4's failure mode to avoid is filters
    only being applied to one retrieval leg by accident."""
    filters = []
    if doc_type:
        filters.append({"term": {"doc_type": doc_type.lower()}})
    if module:
        filters.append({"term": {"module": module.lower()}})
    if status:
        filters.append({"term": {"status": status.lower()}})
    return filters


def build_knn_query(
    vector: list[float],
    doc_type: str | None = None,
    module: str | None = None,
    status: str | None = None,
    size: int = 10,
) -> dict:
    """k-NN query against `chunk_vector`. Filters use the same
    build_filters() as BM25 so a hybrid search never applies a filter
    to one leg and not the other.

    --- Phase 5 fix: post-filter, not native knn.filter ---

    OpenSearch's k-NN plugin supports a filtered k-NN form (`knn.filter`)
    that makes the ANN search itself filter-aware -- this was the
    original design here, on paper the more correct pattern regardless
    of corpus size (avoids under-returning when a filter is selective
    and the ANN candidate pool is small). It was never actually exercised
    against a live index until Phase 5's `/generate` endpoint became the
    first caller to combine doc_type+module+status filters with the k-NN
    leg. That surfaced a real, confirmed error:

        opensearchpy.exceptions.RequestError: (400,
        'search_phase_execution_exception',
        'failed to create query: Engine [NMSLIB] does not support filters')

    `mapping.py` configures `chunk_vector` with method "hnsw" / engine
    "nmslib" (see mapping.py's module docstring) -- native `knn.filter`
    requires the lucene or faiss engine. Switching engines is a mapping
    change requiring a full reindex, out of scope for fixing an unfiltered
    query mid-Phase-5. Fixed instead by post-filtering: a `bool` query
    with the k-NN clause in `must` and metadata filters in `filter`, so
    the ANN search runs unfiltered and the filter is applied to its
    results afterward.

    Trade-off, stated not hidden: this is NOT filter-aware at ANN
    candidate-generation time -- a sufficiently selective filter combined
    with a small `k` could in principle return fewer than `size` results,
    if the unfiltered top-k doesn't contain enough filter-matching
    documents. Not a concern at 28 documents (the whole corpus fits
    inside one k-NN candidate pool); the first thing to revisit if the
    corpus grows and filtered hybrid search starts under-returning.

    Unfiltered queries (no doc_type/module/status given) are unaffected --
    same query shape as before this fix, confirmed by the `if not filters`
    early return below."""
    filters = build_filters(doc_type, module, status)
    knn_clause = {"vector": vector, "k": size}

    if not filters:
        return {
            "size": size,
            "query": {"knn": {"chunk_vector": knn_clause}},
        }

    return {
        "size": size,
        "query": {
            "bool": {
                "filter": filters,
                "must": [{"knn": {"chunk_vector": knn_clause}}],
            }
        },
    }


def reciprocal_rank_fusion(
    bm25_results: list[dict],
    vector_results: list[dict],
    k: int = 60,
) -> list[dict]:
    """Fuses two ranked result lists on RANK, not raw score -- BM25
    scores are unbounded and corpus/query-dependent, cosine similarity
    is bounded [-1, 1]; averaging or summing them directly is not
    mathematically meaningful without a normalization step neither
    leg currently performs. RRF sidesteps this by construction.

    k=60 is the constant from the original RRF paper (Cormack et al.,
    2009) and the de facto default across production hybrid-search
    implementations. Not tuned for this 28-document corpus specifically
    -- there's no principled alternative value without evidence, and
    picking one without evidence is exactly the kind of unjustified
    default this project avoids. Revisit only if real query testing
    shows a specific, documented failure mode traceable to k.

    Each input list is expected in rank order (best first), items keyed
    by `chunk_id`. Returns fused results sorted by RRF score descending,
    each carrying both legs' rank/score for diagnostics -- a hybrid
    result you can't attribute to BM25 vs. vector is a result you can't
    debug, which defeats the point of an eval-first project.
    """
    bm25_rank = {r["chunk_id"]: (i + 1, r.get("score")) for i, r in enumerate(bm25_results)}
    vector_rank = {r["chunk_id"]: (i + 1, r.get("score")) for i, r in enumerate(vector_results)}

    by_chunk = {}
    for r in bm25_results:
        by_chunk[r["chunk_id"]] = r
    for r in vector_results:
        by_chunk.setdefault(r["chunk_id"], r)

    fused = []
    for chunk_id, base in by_chunk.items():
        b_rank, b_score = bm25_rank.get(chunk_id, (None, None))
        v_rank, v_score = vector_rank.get(chunk_id, (None, None))
        rrf_score = 0.0
        if b_rank is not None:
            rrf_score += 1.0 / (k + b_rank)
        if v_rank is not None:
            rrf_score += 1.0 / (k + v_rank)
        fused.append({
            **base,
            "bm25_rank": b_rank,
            "bm25_score": b_score,
            "vector_rank": v_rank,
            "vector_score": v_score,
            "rrf_score": rrf_score,
        })

    fused.sort(key=lambda r: r["rrf_score"], reverse=True)
    return fused
