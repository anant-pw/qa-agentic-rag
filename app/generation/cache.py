"""
Redis response cache for /generate.

Design decisions from the Phase 6 readiness report (this chat):

1. CACHE KEY = hash of (question, doc_type, module, status, current
   concrete OpenSearch index name). NOT the retrieval results themselves,
   NOT a timestamp. The four request fields are exactly what
   GenerateRequest already accepts -- caching anything finer-grained than
   that would cache per-retrieval-state instead of per-request, which is
   not what a response cache is for.

2. Including the current concrete index name (not the alias -- see
   app/search/indexer.py's rebuild_index(), the alias is stable but the
   concrete index behind it changes on every rebuild) is deliberately
   how TTL is handled: rather than a wall-clock expiry (which risks
   serving a stale answer for however long the TTL says, even after a
   prompt fix or corpus correction), the key changes automatically the
   moment `python -m scripts.build_index` runs. A rebuild silently
   invalidates every old entry by construction -- no explicit flush step,
   no clock to get wrong.

3. Caching is OFF during eval runs, ON for normal use, per the real
   tension the readiness report named directly: Phase 5 spent real
   effort finding and fixing generation non-determinism (temperature,
   missing document_references grounding). A cache that silently serves
   the same generation for every eval re-run would mask a future
   regression in that exact behavior instead of revealing it. This is
   why GenerateRequest gets a `no_cache` field (see app/routers/generate.py)
   rather than a single global on/off switch -- a global flag is one
   forgotten toggle away from an eval run silently scoring cached
   responses.

4. Graceful degradation if Redis is unreachable: every function here
   catches connection errors and returns None (cache miss) on read, or
   silently no-ops on write. A local Redis outage should degrade
   /generate to "always regenerate," never take the endpoint down --
   this mirrors the same posture app/observability/logger.py takes for
   log-write failures.
"""

import hashlib
import json
import os

import redis
from opensearchpy import OpenSearch

_client: "redis.Redis | None" = None


def get_redis_client(host: str, port: int) -> "redis.Redis":
    """Lazily builds and reuses a single Redis client. Not wrapped in a
    try/except here on purpose -- connection errors surface at the point
    of an actual GET/SET call (see get_cached / set_cached below), not at
    client construction, since redis-py's client is lazy and doesn't
    actually open a socket until the first command."""
    global _client
    if _client is None:
        _client = redis.Redis(host=host, port=port, decode_responses=True, socket_connect_timeout=2, socket_timeout=2)
    return _client


def get_current_index_name(os_client: OpenSearch, alias: str) -> str:
    """Resolves the alias to its current concrete index name, e.g.
    'qa_documents' -> 'qa_documents_v1735689600'. Falls back to the alias
    name itself if resolution fails (e.g. alias doesn't exist yet, fresh
    environment) -- a cache keyed on a fallback string still behaves
    correctly (it just won't auto-invalidate on the next rebuild until
    the alias exists), it just loses the auto-invalidation property
    described in the module docstring until then."""
    try:
        if os_client.indices.exists_alias(name=alias):
            concrete = list(os_client.indices.get_alias(name=alias).keys())
            if concrete:
                return sorted(concrete)[0]
    except Exception:
        pass
    return alias


def build_cache_key(
    question: str,
    doc_type: str | None,
    module: str | None,
    status: str | None,
    index_name: str,
    model_name: str,
) -> str:
    payload = json.dumps(
        {"q": question, "doc_type": doc_type, "module": module, "status": status,
         "index": index_name, "model": model_name},
        sort_keys=True,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"generate:{digest}"


def get_cached(client: "redis.Redis", key: str) -> dict | None:
    """Returns the cached {'answer': str, 'sources': [...]} dict, or None
    on a genuine miss OR on any Redis error (indistinguishable to the
    caller by design -- both mean 'regenerate')."""
    try:
        raw = client.get(key)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as e:
        print(f"[cache] WARNING: Redis GET failed, treating as cache miss: {e}")
        return None


def set_cached(client: "redis.Redis", key: str, value: dict) -> None:
    """Best-effort write. Silently no-ops on failure -- see module
    docstring point 4. No TTL is set (point 2: invalidation is via key
    change on rebuild, not expiry) -- entries live until the index is
    rebuilt or Redis's own eviction policy reclaims them."""
    try:
        client.set(key, json.dumps(value))
    except Exception as e:
        print(f"[cache] WARNING: Redis SET failed, response not cached: {e}")
