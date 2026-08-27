import psycopg2
import httpx
from fastapi import FastAPI, APIRouter, Query
from fastapi.responses import JSONResponse
from app.routers.search import router as search_router
from app.config import settings
from app.search.service import get_client, search

app = FastAPI(title="RAG Infra - Phase 1")
 
router = APIRouter()

app.include_router(search_router)
 
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





def check_postgres() -> dict:
    try:
        conn = psycopg2.connect(
            host=settings.postgres_host,
            port=settings.postgres_port,
            user=settings.postgres_user,
            password=settings.postgres_password,
            dbname=settings.postgres_db,
            connect_timeout=3,
        )
        conn.close()
        return {"status": "healthy"}
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}


def check_opensearch() -> dict:
    try:
        url = f"http://{settings.opensearch_host}:{settings.opensearch_port}/_cluster/health"
        resp = httpx.get(url, timeout=3)
        resp.raise_for_status()
        cluster_status = resp.json().get("status")
        # OpenSearch reports green/yellow/red; single-node dev setups are
        # typically yellow (no replica shards), which is fine, not degraded.
        return {"status": "healthy", "cluster_status": cluster_status}
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}


def check_ollama() -> dict:
    try:
        url = f"http://{settings.ollama_host}:{settings.ollama_port}/api/tags"
        resp = httpx.get(url, timeout=3)
        resp.raise_for_status()
        models = [m["name"] for m in resp.json().get("models", [])]
        return {"status": "healthy", "models_loaded": models}
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}


@app.get("/")
def root():
    return {"service": "rag-infra", "phase": 1}


@app.get("/health")
def health():
    checks = {
        "postgres": check_postgres(),
        "opensearch": check_opensearch(),
        "ollama": check_ollama(),
    }
    all_healthy = all(c["status"] == "healthy" for c in checks.values())
    payload = {
        "status": "healthy" if all_healthy else "unhealthy",
        "services": checks,
    }
    return JSONResponse(status_code=200 if all_healthy else 503, content=payload)
