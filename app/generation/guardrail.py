"""
Guardrail: reject out-of-domain questions before paying for retrieval
context assembly or a generation call.

Two other signals were tried first and both failed real tests (see
diagnostics/rrf_threshold_probe.py, three live runs against the actual
stack):

  rrf_score  -- out-of-domain questions scored 0.0313-0.0328, in-domain
                0.0325-0.0328. No usable gap. Root cause: RRF fuses on
                RANK, and in a 28-document corpus something always ranks
                #1 regardless of true relevance -- rank-based fusion
                throws away exactly the magnitude information a
                reject/accept decision needs.

  bm25_score -- separated cleanly on vocab-rich in-domain questions
                (50.3-54.5) vs out-of-domain (0.5-4.8), but FALSE-
                REJECTED real paraphrased in-domain questions
                (3.444-8.997 -- one scored below the highest fake
                question's 4.810). Same paraphrase weakness Phase 3
                already documented for BM25, now confirmed to break a
                guardrail built on it.

  vector_score (chosen) -- in-domain, vocab-rich: 0.8141-0.8222.
                in-domain, paraphrased: 0.7890-0.8322. out-of-domain:
                0.6526-0.7063. Clean separation, no overlap, across all
                8 real test questions including the hardest case
                (casual paraphrase with zero shared vocabulary).

Threshold set at 0.75 -- the midpoint of the closest real pair (0.7063
out-of-domain vs 0.7890 in-domain-paraphrased), giving roughly equal
(~0.04) margin on both sides. This is 8 data points, not a large sample
-- exposed as a config value (not hardcoded) specifically so it can be
retuned without a code change if real usage surfaces a false
accept/reject that this test set didn't happen to cover.
"""

from app.config import settings


def is_in_domain(top_vector_score: float | None, has_explicit_filter: bool = False) -> bool:
    """top_vector_score is hits[0]["vector_score"] from hybrid_search()'s
    top result -- the same live function /generate already calls, so
    this reuses work already being done rather than triggering a
    second retrieval pass.

    None (hybrid_search returned zero hits at all) is always
    out-of-domain, filter or no filter: if nothing matched even loosely
    -- including a bogus module/status value matching no real document --
    there's nothing to ground an answer in regardless.

    has_explicit_filter (real fix, Round 4): when the caller has already
    supplied doc_type/module/status, TRUST that filter as the domain
    signal instead of the vector_score threshold. Real finding, from
    diagnostics/rrf_threshold_probe.py against the live stack: templated
    structured-filter questions ("How many bugs are currently Open in
    the Payments module?") score systematically lower on vector_score
    than free-text semantic questions, even when genuinely in-domain --
    Login: 0.7502 (technically above 0.75, but by 0.0002 -- luck, not
    margin), Payments: 0.7207 (a real, confirmed false reject in
    production, structured_filter FAIL that wasn't there in the Phase 6
    baseline). bm25_score doesn't rescue this either -- both scored
    0.69-3.47, indistinguishable from real out-of-domain garbage
    (0.55-4.81). Neither signal reliably separates this question SHAPE
    from noise, because a generic "how many X are open" question shares
    almost no content vocabulary or close embedding-space meaning with
    any single bug's specific description, regardless of which real
    module it names.

    ACCEPTED, DOCUMENTED TRADE-OFF -- not hidden: a genuinely nonsense
    question ("write me a poem") paired with a real filter value
    (module="Login") would NOT be caught by this guardrail; it falls
    through the filter-trust path even though the question text itself
    is nonsense. Not fixed here. Accepted because (a) doc_type/module/
    status are structured API parameters in this system's real usage,
    not typically attacker-controlled free text, and (b) neither
    bm25_score nor vector_score actually distinguished that case from a
    legitimate one in real testing -- there's no signal in hand that
    would catch it without a different, unbuilt mechanism (checking
    question text against the filter's real content). Flagged for the
    Phase 7 handoff as a known limitation, not silently accepted.
    """
    if top_vector_score is None:
        return False
    if has_explicit_filter:
        return True
    return top_vector_score >= settings.vector_score_guardrail_threshold
