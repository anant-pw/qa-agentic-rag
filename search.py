"""
Already applied to app/main.py (verified during Phase 4 readiness --
the Phase 3 handoff's "not yet wired" note was stale; the router was
already included). Left here unmodified as the source of truth for
this router's routes.
"""

from fastapi import APIRouter, Query

from app.config import settings
from app.search.service import get_client, search, hybrid_search

router = APIRouter()


@router.get("/search")
def search_documents(
    q: str | None = Query(None, description="Free-text or exact ID/error-code query"),
    doc_type: str | None = Query(None, description="Filter: 'bug_report' or 'test_case'"),
    module: str | None = Query(None, description="Filter: exact module name"),
    status: str | None = Query(None, description="Filter: exact status (bug reports only)"),
    size: int = Query(10, ge=1, le=100),
):
    """Phase 3, UNCHANGED. BM25-only. Frozen deliberately -- see
    app/search/service.py's search() docstring."""
    client = get_client(settings.opensearch_host, settings.opensearch_port)
    results = search(
        client,
        alias=settings.opensearch_index_alias,
        q=q,
        doc_type=doc_type,
        module=module,
        status=status,
        size=size,
    )
    return {"query": q, "doc_type": doc_type, "module": module, "status": status, "results": results}


@router.get("/search/hybrid")
def search_hybrid(
    q: str = Query(..., description="Query text -- required, unlike /search, since there's no meaningful k-NN leg with no text to embed"),
    doc_type: str | None = Query(None, description="Filter: 'bug_report' or 'test_case'"),
    module: str | None = Query(None, description="Filter: exact module name"),
    status: str | None = Query(None, description="Filter: exact status (bug reports only)"),
    size: int = Query(10, ge=1, le=100),
):
    """Phase 4: BM25 + k-NN fused via RRF. New route, separate from
    /search -- see app/search/service.py's hybrid_search() docstring
    for why this isn't a modification of the Phase 3 endpoint. Response
    includes bm25_rank/score, vector_rank/score, and rrf_score per
    result so a retrieval failure is diagnosable by which leg (or
    neither) contributed a given document."""
    client = get_client(settings.opensearch_host, settings.opensearch_port)
    results = hybrid_search(
        client,
        alias=settings.opensearch_index_alias,
        q=q,
        ollama_host=settings.ollama_host,
        ollama_port=settings.ollama_port,
        doc_type=doc_type,
        module=module,
        status=status,
        size=size,
    )
    return {"query": q, "doc_type": doc_type, "module": module, "status": status, "results": results}
