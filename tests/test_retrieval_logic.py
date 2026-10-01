from app.search.queries import build_filters, build_search_query, reciprocal_rank_fusion


def _ids(results):
    return [r["chunk_id"] for r in results]


def test_rrf_rewards_documents_found_by_both_legs():
    bm25 = [{"chunk_id": "a"}, {"chunk_id": "b"}, {"chunk_id": "c"}]
    vector = [{"chunk_id": "c"}, {"chunk_id": "a"}]
    fused = reciprocal_rank_fusion(bm25, vector, k=60)
    assert _ids(fused) == ["a", "c", "b"]
    a = fused[0]
    assert (a["bm25_rank"], a["vector_rank"]) == (1, 2)
    assert a["rrf_score"] == 1 / 61 + 1 / 62


def test_rrf_single_leg_document_keeps_none_for_missing_leg():
    fused = reciprocal_rank_fusion([{"chunk_id": "x", "score": 3.0}], [], k=60)
    assert fused[0]["vector_rank"] is None
    assert fused[0]["bm25_score"] == 3.0
    assert fused[0]["rrf_score"] == 1 / 61


def test_filters_are_lowercased_and_only_for_given_fields():
    assert build_filters("bug_report", None, "Open") == [
        {"term": {"doc_type": "bug_report"}},
        {"term": {"status": "open"}},
    ]
    assert build_filters(None, None, None) == []


def test_empty_query_is_a_filtered_listing():
    body = build_search_query(q="", module="Login", size=7)
    assert body["size"] == 7
    assert body["query"]["bool"]["must"] == [{"match_all": {}}]
    assert body["query"]["bool"]["filter"] == [{"term": {"module": "login"}}]


def test_single_token_query_gets_exact_id_boost():
    should = build_search_query(q="BUG-1013")["query"]["bool"]["should"]
    boosts = [c for c in should if "term" in c]
    assert boosts and all(list(c["term"].values())[0]["value"] == "bug-1013" for c in boosts)


def test_embedded_ids_only_boosted_when_opted_in():
    q = "Which bugs mention ERR_401_UNAUTH and BUG-1015?"
    plain = build_search_query(q=q)["query"]["bool"]["should"]
    assert not [c for c in plain if "term" in c]  # /search stays Phase-3 identical

    boosted = build_search_query(q=q, extract_embedded_ids=True)["query"]["bool"]["should"]
    values = {list(c["term"].values())[0]["value"] for c in boosted if "term" in c}
    assert values == {"err_401_unauth", "bug-1015"}
