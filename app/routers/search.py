"""
Add to app/main.py:

    from app.routers.search import router as search_router
    app.include_router(search_router)

Not merged into main.py directly here, to avoid silently overwriting
whatever else main.py has picked up since the Phase 2 handoff was
written -- you apply this one-line addition to the real file yourself.
"""

from fastapi import APIRouter, Query

from app.config import settings
from app.search.service import get_client, search

router = APIRouter()


@router.get("/search")
def search_documents(
    q: str | None = Query(None, description="Free-text or exact ID/error-code query"),
    doc_type: str | None = Query(None, description="Filter: 'bug_report' or 'test_case'"),
    module: str | None = Query(None, description="Filter: exact module name"),
    status: str | None = Query(None, description="Filter: exact status (bug reports only)"),
    size: int = Query(10, ge=1, le=100),
):
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
