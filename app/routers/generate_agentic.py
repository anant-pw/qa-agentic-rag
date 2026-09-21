"""
Phase 7: POST /generate/agentic -- a NEW endpoint, sitting alongside the
untouched /generate. app/routers/generate.py is not imported, modified,
or called by this file. Its contract (POST /generate, StreamingResponse,
---SOURCES--- marker) is preserved exactly, per the original Phase 7
brief.

This endpoint wires together three pieces that were each built and
tested separately before being connected here:

  app/generation/guardrail.py   -- reject, using vector_score (NOT
                                    rrf_score, NOT bm25_score -- both
                                    tried first, both failed real tests,
                                    see guardrail.py's own docstring for
                                    the numbers).
  app/generation/deterministic.py -- count/list shortcut, zero LLM calls,
                                    gated by settings.deterministic_count_routing.
  hybrid_search() + stream_chat() -- the EXACT same Phase 4/5 functions
                                    /generate already calls. No retrieval
                                    or generation logic is duplicated or
                                    reimplemented here.

ID ROUTE (added after the guardrail shape probes -- see
app/search/id_resolution.py's docstring for the measurements): a question
naming a BUG-/TC- document ID is decided by whether that ID EXISTS, not by
the cosine score. Exists and passes the active filters -> that document is
pinned to the front of the hits, the cosine gate and the deterministic count
shortcut are skipped, decision "semantic". Named IDs that exist nowhere ->
decision "reject" with an accurate "no document with ID ..." message and NO
retrieval call at all. No ID (or IDs excluded by explicit filters) -> the
pre-existing path, unchanged.

WIRE CONTRACT: deliberately matches /generate's shape (text/plain,
answer text, then "\n\n---SOURCES---\n", then a JSON source list) so
eval/run_eval_generation.py can point at this endpoint with only a URL
change -- no parsing logic changes needed to compare this against the
Phase 6 baseline on the same eval set.

REAL, FLAGGED SIMPLIFICATION (v1, not hidden): unlike /generate, tokens
are NOT streamed to the client as they're generated. stream_chat() is
still called and still streams internally, but this endpoint accumulates
the full answer inside the LangGraph node before returning it as one
chunk -- the same delivery-cadence trade-off Phase 6 already established
precedent for on cache hits (see generate.py's cached_stream()), just
applied to every request here instead of only cache hits. Reason: wiring
true token-by-token streaming through a compiled LangGraph graph is a
separate, real piece of work (StreamingResponse would need to consume
graph.stream() events, not graph.invoke()'s final state) and doing it
before the graph's actual decision-making logic is verified correct
would be solving two problems at once. Revisit once the routing itself
is confirmed right.

RETRIEVAL RUNS ONCE, in the guardrail node, at settings.retrieval_size --
not once for the guardrail decision and again for generation. The
semantic-path node reuses the SAME hits the guardrail already computed
(state["top_hits"]), for the same reason Phase 2's cross-reference
lookup batches one query instead of one-per-document: no reason to pay
for a second retrieval pass when the first one already has what's needed.

CACHING: reuses app/generation/cache.py's build_cache_key() UNMODIFIED --
same key shape (question + filters + index + model) as /generate uses.
This is deliberate, not an oversight: caching ONLY ever applies to the
final semantic-path generation output, exactly like /generate. The
guardrail's reject decision and the deterministic path's answer are
NEVER cached -- both are already free (no LLM call), so caching them
would add a second cache surface for zero latency benefit, and could
mask a guardrail-threshold change behind a stale cached rejection. This
was flagged as an open design question in the Phase 7 readiness report;
answered here by NOT caching either of them.
"""

import json
import time
from typing import TypedDict

import psycopg2
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from langgraph.graph import StateGraph, END

from app.config import settings
from app.search.service import get_client, hybrid_search, resolve_document_ids
from app.search.id_resolution import (
    extract_document_ids,
    classify_id_question,
    pin_resolved_hits,
    passes_filters,
    unknown_id_message,
)
from app.generation.context import fetch_structured_fields, fetch_references
from app.generation.prompt import build_messages, format_doc_context
from app.generation.llm import stream_chat, GenerationError
from app.generation.guardrail import is_in_domain
from app.generation.deterministic import is_countable_question, answer_countable_question
from app.generation.cache import (
    get_redis_client,
    get_current_index_name,
    build_cache_key,
    get_cached,
    set_cached,
)
from app.observability.logger import log_generation_event, timed_span

router = APIRouter()


class AgenticGenerateRequest(BaseModel):
    question: str
    doc_type: str | None = None
    module: str | None = None
    status: str | None = None
    no_cache: bool = False


def _get_pg_connection():
    # Duplicated from generate.py's private _get_pg_connection() rather
    # than imported -- it's 8 lines and underscore-private on purpose in
    # that file. Real, small duplication; flagged rather than hidden.
    # Worth moving to a shared app/db.py if a third caller ever needs it.
    return psycopg2.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=settings.postgres_password,
        dbname=settings.postgres_db,
        connect_timeout=settings.postgres_connect_timeout,
    )


class AgenticState(TypedDict, total=False):
    question: str
    doc_type: str | None
    module: str | None
    status: str | None
    decision: str  # "reject" | "deterministic" | "semantic"
    hits: list[dict]
    top_hits: list[dict]
    guardrail_vector_score: float | None
    answer: str
    sources: list[dict]
    retrieval_span: dict
    generation_span: dict
    error: str | None


def guardrail_and_route(state: AgenticState) -> dict:
    """The only node that touches OpenSearch/Ollama for retrieval. Runs
    hybrid_search() ONCE; every downstream node reuses its output."""
    os_client = get_client(settings.opensearch_host, settings.opensearch_port)
    doc_type, module, status = state.get("doc_type"), state.get("module"), state.get("status")

    with timed_span() as retrieval_span:
        # ID route, step 1: which BUG-/TC- IDs does the question name, and
        # which exist? Separate exact-match lookup -- NOT inferred from
        # hybrid_search()'s truncated hits (see resolve_document_ids()).
        question_ids = extract_document_ids(state["question"])
        existing = (
            resolve_document_ids(os_client, settings.opensearch_index_alias, question_ids)
            if question_ids else {}
        )
        pinnable = {
            k: v for k, v in existing.items() if passes_filters(v, doc_type, module, status)
        }
        id_class = classify_id_question(question_ids, existing, pinnable)

        if id_class == "unknown":
            # Named IDs exist nowhere: no retrieval, no embedding call.
            hits = []
            top_vector_score = None
        else:
            hits = hybrid_search(
                os_client,
                alias=settings.opensearch_index_alias,
                q=state["question"],
                ollama_host=settings.ollama_host,
                ollama_port=settings.ollama_port,
                doc_type=doc_type,
                module=module,
                status=status,
                size=settings.retrieval_size,
            )
            # Guardrail score is taken from RRF order BEFORE any pinning, so
            # it keeps its original meaning for logs/eval comparisons.
            top_vector_score = next((h["vector_score"] for h in hits if h["vector_score"] is not None), None)
            if id_class == "resolved":
                hits = pin_resolved_hits(hits, pinnable, question_ids)

    top_hits = hits[: settings.context_top_n]

    retrieval_span["doc_ids"] = [h["external_id"] for h in top_hits]
    retrieval_span["vector_scores"] = [h["vector_score"] for h in top_hits]
    retrieval_span["guardrail_vector_score"] = top_vector_score
    if question_ids:
        retrieval_span["question_ids"] = question_ids
        retrieval_span["resolved_ids"] = [i for i in question_ids if i in existing]
        retrieval_span["id_route"] = id_class

    base = {
        "hits": hits,
        "top_hits": top_hits,
        "guardrail_vector_score": top_vector_score,
    }

    if id_class == "unknown":
        retrieval_span["path"] = "reject_unknown_id"
        return {
            **base,
            "decision": "reject",
            "retrieval_span": retrieval_span,
            "answer": unknown_id_message(question_ids),
            "sources": [],
        }

    if id_class == "resolved":
        # Existence of the named document is the domain signal; the cosine
        # gate and the count shortcut are both skipped (see module docstring).
        retrieval_span["path"] = "semantic"
        return {**base, "decision": "semantic", "retrieval_span": retrieval_span}

    if not is_in_domain(
        top_vector_score,
        has_explicit_filter=bool(doc_type or module or status)
        or (settings.deterministic_count_routing and is_countable_question(state["question"])),
    ):
        retrieval_span["path"] = "reject"
        return {
            **base,
            "decision": "reject",
            "retrieval_span": retrieval_span,
            "answer": (
                "This question doesn't appear to be about the QA corpus "
                "(bug reports and test cases) this system covers, so no "
                "answer was generated."
            ),
            "sources": [],
        }

    if settings.deterministic_count_routing and is_countable_question(state["question"]):
        retrieval_span["path"] = "deterministic"
        return {**base, "decision": "deterministic", "retrieval_span": retrieval_span}

    retrieval_span["path"] = "semantic"
    return {**base, "decision": "semantic", "retrieval_span": retrieval_span}


def run_deterministic(state: AgenticState) -> dict:
    """No LLM call. See deterministic.py for why this re-queries search()
    instead of counting state['top_hits'] (top-N truncation would
    silently undercount)."""
    os_client = get_client(settings.opensearch_host, settings.opensearch_port)
    answer, sources = answer_countable_question(
        os_client,
        alias=settings.opensearch_index_alias,
        doc_type=state.get("doc_type"),
        module=state.get("module"),
        status=state.get("status"),
    )
    return {
        "answer": answer,
        "sources": sources,
        "generation_span": {"path": "deterministic", "llm_calls": 0},
    }


def run_semantic_generate(state: AgenticState) -> dict:
    """Same context-assembly and generation logic as /generate --
    fetch_structured_fields, fetch_references, build_messages,
    stream_chat -- called identically, just accumulated into one string
    instead of yielded token-by-token (see module docstring)."""
    top_hits = state["top_hits"]

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
    messages = build_messages(state["question"], context_blocks)

    sources = [
        {
            "external_id": h["external_id"],
            "title": h["title"],
            "doc_type": h["doc_type"],
            "rrf_score": h["rrf_score"],
            "vector_score": h["vector_score"],
            "parent_document_id": h["parent_document_id"],
        }
        for h in top_hits
    ]

    accumulated: list[str] = []
    gen_start = time.time()
    generation_failed = False
    try:
        for content, _ in stream_chat(
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
    except GenerationError as e:
        generation_failed = True
        print(f"[generation] ERROR: Ollama chat request failed: {e}")
        accumulated.append(
            "\n\n[This question took too long to answer, or the local model "
            "is temporarily unreachable. Try a shorter or more specific "
            "question, or try again in a moment.]"
        )

    gen_span = {
        "latency_s": round(time.time() - gen_start, 4),
        "token_count": len("".join(accumulated).split()),
        "model": settings.ollama_chat_model,
        "path": "semantic",
    }

    return {
        "answer": "".join(accumulated),
        "sources": [] if generation_failed else sources,
        "generation_span": gen_span,
        "error": "generation_error" if generation_failed else None,
    }


def _route(state: AgenticState) -> str:
    return state["decision"]


_graph = StateGraph(AgenticState)
_graph.add_node("guardrail_and_route", guardrail_and_route)
_graph.add_node("deterministic", run_deterministic)
_graph.add_node("semantic_generate", run_semantic_generate)
_graph.set_entry_point("guardrail_and_route")
_graph.add_conditional_edges(
    "guardrail_and_route",
    _route,
    {"reject": END, "deterministic": "deterministic", "semantic": "semantic_generate"},
)
_graph.add_edge("deterministic", END)
_graph.add_edge("semantic_generate", END)
compiled_agentic_graph = _graph.compile()


@router.post("/generate/agentic")
def generate_agentic(req: AgenticGenerateRequest):
    os_client = get_client(settings.opensearch_host, settings.opensearch_port)
    redis_client = get_redis_client(settings.redis_host, settings.redis_port)
    index_name = get_current_index_name(os_client, settings.opensearch_index_alias)
    cache_key = build_cache_key(
        req.question, req.doc_type, req.module, req.status, index_name, settings.ollama_chat_model
    )

    if not req.no_cache:
        cached = get_cached(redis_client, cache_key)
        if cached is not None:
            log_generation_event(
                question=req.question,
                doc_type=req.doc_type,
                module=req.module,
                status=req.status,
                retrieval_span={"cache_hit": True, "path": "cache"},
                generation_span={"cache_hit": True, "model": settings.ollama_chat_model},
                cache_hit=True,
            )

            def cached_stream():
                yield cached["answer"]
                yield "\n\n---SOURCES---\n"
                yield json.dumps(cached["sources"], indent=2)

            return StreamingResponse(cached_stream(), media_type="text/plain")

    initial_state: AgenticState = {
        "question": req.question,
        "doc_type": req.doc_type,
        "module": req.module,
        "status": req.status,
    }
    final_state = compiled_agentic_graph.invoke(initial_state)

    answer = final_state.get("answer", "")
    sources = final_state.get("sources", [])
    decision = final_state.get("decision", "unknown")
    error = final_state.get("error")

    # Cache ONLY the semantic path's real generation output -- see module
    # docstring for why reject/deterministic are never cached.
    if decision == "semantic" and not req.no_cache and not error:
        set_cached(redis_client, cache_key, {"answer": answer, "sources": sources})

    log_generation_event(
        question=req.question,
        doc_type=req.doc_type,
        module=req.module,
        status=req.status,
        retrieval_span=final_state.get("retrieval_span", {}),
        generation_span=final_state.get("generation_span", {"path": decision, "llm_calls": 0}),
        cache_hit=False,
        error=error,
    )

    def event_stream():
        yield answer
        yield "\n\n---SOURCES---\n"
        yield json.dumps(sources, indent=2)

    return StreamingResponse(event_stream(), media_type="text/plain")
