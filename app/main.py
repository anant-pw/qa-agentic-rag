import psycopg2
import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from app.routers.search import router as search_router
from app.routers.generate import router as generate_router
from fastapi.responses import FileResponse
from app.config import settings
from app.routers.generate_agentic import router as generate_agentic_router

app = FastAPI(title="RAG Infra - Phase 1")

app.include_router(search_router)
app.include_router(generate_router)  # Phase 5: POST /generate -- see
# app/routers/generate.py. Does not modify search_router's routes or
# behavior; GET /search and GET /search/hybrid are unchanged.
# Removed (Phase 4 cleanup): a second, inert `/search` APIRouter used to
# live here, duplicating app/routers/search.py's endpoint but never
# passed to include_router(). It had no effect on runtime behavior --
# app.include_router(search_router) above was already live -- but it
# made main.py look like there were two competing /search
# implementations. Deleted as dead code, not a behavior change; the
# Phase 3 handoff's claim that the router "isn't yet wired in" was
# already stale before this cleanup (confirmed by reading this file
# during the Phase 4 readiness check).
app.include_router(generate_agentic_router)  # Phase 7: POST /generate/agentic


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

@app.get("/ui")
def ui():
    return FileResponse("app/static/index.html")
