from opensearchpy import OpenSearch

from app.search.queries import build_search_query, build_knn_query, reciprocal_rank_fusion
from app.search.embeddings import embed_query


def get_client(host: str, port: int) -> OpenSearch:
    # Matches Phase 1's health-check pattern (app/main.py): plain HTTP,
    # no auth, no TLS -- consistent with `plugins.security.disabled=true`
    # in docker-compose.yml for local dev. Do not reuse this client
    # config against anything internet-facing.
    return OpenSearch(hosts=[{"host": host, "port": port}], use_ssl=False, verify_certs=False)


def _shape_hit(hit: dict) -> dict:
    """Shared result shaping for both BM25 and k-NN legs -- keeps
    `chunk_id` in the result, which reciprocal_rank_fusion() needs as
    its join key. Phase 3's `search()` intentionally omits chunk_id
    from its response shape below (unchanged, see note there); this
    internal helper is only used by hybrid_search()."""
    src = hit["_source"]
    return {
        "chunk_id": src.get("chunk_id"),
        "document_id": src["document_id"],
        "parent_document_id": src.get("parent_document_id", src["document_id"]),
        "external_id": src["external_id"],
        "title": src["title"],
        "doc_type": src["doc_type"],
        "module": src["module"],
        "status": src["status"],
        "score": hit["_score"],
    }


def search(
    client: OpenSearch,
    alias: str,
    q: str | None = None,
    doc_type: str | None = None,
    module: str | None = None,
    status: str | None = None,
    size: int = 10,
) -> list[dict]:
    """UNCHANGED from Phase 3, deliberately -- this is the frozen
    BM25-only path (GET /search). Response shape (document_id,
    external_id, title, doc_type, module, status, score) is exactly
    what Phase 3 shipped; Phase 4's hybrid_search() below is a
    separate function with its own response shape, not a modification
    of this one, so nothing calling /search sees any change."""
    body = build_search_query(q=q, doc_type=doc_type, module=module, status=status, size=size)
    resp = client.search(index=alias, body=body)
    return [
        {
            "document_id": hit["_source"]["document_id"],
            "external_id": hit["_source"]["external_id"],
            "title": hit["_source"]["title"],
            "doc_type": hit["_source"]["doc_type"],
            "module": hit["_source"]["module"],
            "status": hit["_source"]["status"],
            "score": hit["_score"],
        }
        for hit in resp["hits"]["hits"]
    ]


def hybrid_search(
    client: OpenSearch,
    alias: str,
    q: str,
    ollama_host: str,
    ollama_port: int,
    doc_type: str | None = None,
    module: str | None = None,
    status: str | None = None,
    size: int = 10,
    rrf_k: int = 60,
) -> list[dict]:
    """Phase 4: runs BM25 and k-NN retrieval independently against the
    same filters, fuses via RRF. `q` is required (unlike search(), which
    tolerates a filter-only listing) -- there's no meaningful k-NN query
    with no query text to embed.

    Each leg is requested at `size` (not some larger candidate pool) --
    fine at 28 documents; if the corpus grows, widening each leg's
    candidate size before fusion (e.g. fetch top-30, fuse, return top-10)
    would be the first knob to turn to avoid RRF being starved of
    candidates one leg ranked lower than `size` but the other ranked
    highly.
    """
    filters_body = build_search_query(
        q=q, doc_type=doc_type, module=module, status=status, size=size,
        extract_embedded_ids=True,  # fixed Phase 3 defect -- see queries.py's
        # module docstring "Phase 4 addition" section. /search (above) does
        # NOT pass this, so its output is unaffected.
    )
    bm25_resp = client.search(index=alias, body=filters_body)
    bm25_results = [_shape_hit(h) for h in bm25_resp["hits"]["hits"]]

    query_vector = embed_query(q, ollama_host, ollama_port)
    knn_body = build_knn_query(query_vector, doc_type=doc_type, module=module, status=status, size=size)
    vector_resp = client.search(index=alias, body=knn_body)
    vector_results = [_shape_hit(h) for h in vector_resp["hits"]["hits"]]

    fused = reciprocal_rank_fusion(bm25_results, vector_results, k=rrf_k)
    return fused[:size]
