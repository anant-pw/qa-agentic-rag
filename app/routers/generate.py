"""
Phase 5: grounded, streaming RAG generation endpoint.
Phase 6: adds a Redis response cache and structured request logging
around the same retrieval-then-generation flow -- see app/generation/cache.py
and app/observability/logger.py for the design rationale (Langfuse
rejected as violating local-first, LLM-as-judge rejected entirely). Both
additions are opt-out/degrade-gracefully, and neither changes what
/generate returns on a cache miss.

POST /generate, not GET -- unlike /search and /search/hybrid's short
filter-style query params, `question` here is free-text and can be long or
contain characters awkward in a URL, so a JSON body is the right shape.

Retrieval-then-generation, not interleaved: hybrid_search() runs to
completion before the StreamingResponse is constructed. See the Phase 5
readiness report for why overlapping retrieval with generation isn't
worth the complexity here.

RETRIEVAL_SIZE is 10, not CONTEXT_TOP_N's 5 -- hybrid_search() passes
`size` to BOTH legs BEFORE RRF fusion, so requesting 5 directly would fuse
over a narrower per-leg pool than Phase 4's validated Recall@5 eval used
(eval/run_eval.py hard-codes size=10 and slices top 5). Retrieve at 10,
truncate to 5, to replay the same fusion behavior already validated.

CACHE HIT IS A REAL, DELIBERATE DEVIATION FROM PHASE 5's byte-for-byte
streaming shape: on a hit, the full cached answer is yielded as one chunk
instead of token-by-token, followed by the same ---SOURCES--- block. The
HTTP contract (POST /generate, StreamingResponse, text/plain,
---SOURCES--- marker, JSON source list) is unchanged -- a client reading
to completion sees identical bytes either way. What changes is delivery
cadence. Flagged here and in the Phase 6 handoff, not silently introduced.

Preserves GET /search and GET /search/hybrid exactly -- this file adds a
new route, it does not modify app/search/service.py, app/search/queries.py,
or app/routers/search.py.
"""

import json
import time

import psycopg2
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.config import settings
from app.search.service import get_client, hybrid_search
from app.generation.context import fetch_structured_fields, fetch_references
from app.generation.prompt import build_messages, format_doc_context
from app.generation.llm import stream_chat, GenerationError
from app.generation.cache import (
    get_redis_client,
    get_current_index_name,
    build_cache_key,
    get_cached,
    set_cached,
)
from app.observability.logger import log_generation_event, timed_span

router = APIRouter()

class GenerateRequest(BaseModel):
    question: str
    doc_type: str | None = None
    module: str | None = None
    status: str | None = None
    # Phase 6 addition. Per-request opt-out, not a global config flag --
    # a global cache toggle is one forgotten flip away from an eval run
    # silently scoring cached (non-representative) generation output.
    # eval/run_eval_generation.py always sends no_cache=true.
    no_cache: bool = False


def _get_pg_connection():
    return psycopg2.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=settings.postgres_password,
        dbname=settings.postgres_db,
        connect_timeout=settings.postgres_connect_timeout,
    )


@router.post("/generate")
def generate(req: GenerateRequest):
    os_client = get_client(settings.opensearch_host, settings.opensearch_port)

    redis_client = get_redis_client(settings.redis_host, settings.redis_port)
    index_name = get_current_index_name(os_client, settings.opensearch_index_alias)
    cache_key = build_cache_key(req.question, req.doc_type, req.module, req.status, index_name, settings.ollama_chat_model)

    if not req.no_cache:
        cached = get_cached(redis_client, cache_key)
        if cached is not None:
            log_generation_event(
                question=req.question,
                doc_type=req.doc_type,
                module=req.module,
                status=req.status,
                retrieval_span={"cache_hit": True},
                generation_span={"cache_hit": True, "model": settings.ollama_chat_model},
                cache_hit=True,
            )

            def cached_stream():
                yield cached["answer"]
                yield "\n\n---SOURCES---\n"
                yield json.dumps(cached["sources"], indent=2)

            return StreamingResponse(cached_stream(), media_type="text/plain")

    with timed_span() as retrieval_span:
        hits = hybrid_search(
            os_client,
            alias=settings.opensearch_index_alias,
            q=req.question,
            ollama_host=settings.ollama_host,
            ollama_port=settings.ollama_port,
            doc_type=req.doc_type,
            module=req.module,
            status=req.status,
            size=settings.retrieval_size,
        )
        top_hits = hits[:settings.context_top_n]
        retrieval_span["doc_ids"] = [h["external_id"] for h in top_hits]
        retrieval_span["scores"] = [h["rrf_score"] for h in top_hits]

    pg_conn = _get_pg_connection()
    try:
        doc_ids = [h["parent_document_id"] for h in top_hits]
        structured_by_id = fetch_structured_fields(pg_conn, doc_ids)
        references_by_id = fetch_references(pg_conn, doc_ids)
    finally:
        pg_conn.close()

    context_blocks = [
        format_doc_context(
            h,
            structured_by_id.get(h["parent_document_id"], {}),
            references_by_id.get(h["parent_document_id"], []),
        )
        for h in top_hits
    ]
    messages = build_messages(req.question, context_blocks)

    sources = [
        {
            "external_id": h["external_id"],
            "title": h["title"],
            "doc_type": h["doc_type"],
            "rrf_score": h["rrf_score"],
            "parent_document_id": h["parent_document_id"],
        }
        for h in top_hits
    ]

    def event_stream():
        generation_failed = False
        accumulated = []
        gen_span = {}
        gen_start = time.time()
        try:
            for content, maybe_done_chunk in stream_chat(
                messages,
                settings.ollama_host,
                settings.ollama_port,
                model=settings.ollama_chat_model,
                timeout=settings.ollama_chat_timeout,
                temperature=settings.ollama_temperature,
                think=settings.ollama_think,
                keep_alive=settings.ollama_keep_alive,
            ):
                accumulated.append(content)
                yield content
        except GenerationError as e:
            generation_failed = True
            print(f"[generation] ERROR: Ollama chat request failed: {e}")
            yield (
                "\n\n[This question took too long to answer, or the local model "
                "is temporarily unreachable. Try a shorter or more specific "
                "question, or try again in a moment.]"
            )

        gen_span["latency_s"] = round(time.time() - gen_start, 4)
        gen_span["token_count"] = len("".join(accumulated).split())
        gen_span["model"] = settings.ollama_chat_model  # Phase 6 fix: logs had no
        # record of which model actually generated a response, which directly
        # caused real confusion diagnosing a gpt-oss:20b vs phi4-mini mixup --
        # a sweep's results and latencies were ambiguous about which model
        # produced them. Now every line is self-describing.

        if not generation_failed:
            yield "\n\n---SOURCES---\n"
            yield json.dumps(sources, indent=2)

            if not req.no_cache:
                full_answer = "".join(accumulated)
                set_cached(redis_client, cache_key, {"answer": full_answer, "sources": sources})

        log_generation_event(
            question=req.question,
            doc_type=req.doc_type,
            module=req.module,
            status=req.status,
            retrieval_span=retrieval_span,
            generation_span=gen_span,
            cache_hit=False,
            error=None if not generation_failed else "generation_error",
        )

    return StreamingResponse(event_stream(), media_type="text/plain")
