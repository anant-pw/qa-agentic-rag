"""
Structured JSON-line request logging for /generate.

Phase 6 readiness report (this chat) rejected self-hosted Langfuse
explicitly: Langfuse v3+ self-hosted is a 6-container stack (web, worker,
ClickHouse, Redis/Valkey, MinIO, its own Postgres) with an officially
documented 16GB RAM recommendation -- verified via web search during the
readiness review, not assumed. That directly contradicts this project's
own local-first constraint, which Phase 1 already treats as real (OpenSearch
is capped at 512MB heap on purpose -- see docker-compose.yml). Langfuse
Cloud was equally rejected: it trades one violation (too heavy) for
another (a hosted SaaS dependency the project brief never authorized).

This module is the replacement: one JSON line per /generate call, written
to a local file. It gives the same retrieval -> generation trace shape
Langfuse would have (a "span" for what was retrieved, a "span" for what
was generated) without a second multi-container stack, and it's directly
queryable with jq/pandas -- which is how this project has been debugging
things by hand since Phase 2 anyway (grep-and-count over live output).

Not a tracing framework. No nesting, no UI, no sampling. If real
distributed tracing becomes necessary later (e.g. Phase 7's LangGraph
adds multiple LLM calls per request and single-line-per-request stops
being enough to debug a failure), that's a deliberate Phase 7+ decision
to make with then-current requirements in hand, not something to
over-build here on spec.
"""

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone

LOG_DIR = os.environ.get("GENERATION_LOG_DIR", "logs")
LOG_PATH = os.path.join(LOG_DIR, "generation.jsonl")


def _ensure_log_dir() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)


@contextmanager
def timed_span():
    """Context manager yielding a dict the caller fills in; records
    wall-clock elapsed seconds into it under 'latency_s' on exit,
    regardless of whether the block raised."""
    span = {}
    start = time.time()
    try:
        yield span
    finally:
        span["latency_s"] = round(time.time() - start, 4)


def log_generation_event(
    question: str,
    doc_type: str | None,
    module: str | None,
    status: str | None,
    retrieval_span: dict,
    generation_span: dict,
    cache_hit: bool,
    error: str | None = None,
) -> None:
    """Writes one JSON line. retrieval_span is expected to carry at least
    {'doc_ids': [...], 'scores': [...], 'latency_s': float} and
    generation_span at least {'latency_s': float, 'token_count': int}
    (token_count is a rough len()-based estimate, not a real tokenizer
    count -- flagged, not silently presented as exact).

    Never raises: a broken log write should not take down /generate.
    Mirrors the graceful-degradation posture app/generation/cache.py
    uses for a Redis outage -- observability and caching are both
    additive, and neither should become a new way for the core endpoint
    to fail.
    """
    try:
        _ensure_log_dir()
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "question": question,
            "filters": {"doc_type": doc_type, "module": module, "status": status},
            "cache_hit": cache_hit,
            "retrieval": retrieval_span,
            "generation": generation_span,
            "error": error,
        }
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        # Deliberately swallowed -- see docstring. A print is enough for
        # local dev visibility that logging itself broke, without raising
        # into the request path.
        print(f"[observability] WARNING: failed to write log line: {e}")
