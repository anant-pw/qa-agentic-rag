"""
Embedding client for the QA corpus, backed by Ollama's native embedding
endpoint (`nomic-embed-text`, confirmed pulled and working against this
project's Ollama install as of Phase 4 readiness -- 768-dim, F16,
embedding-only capability, no chat/completion capability on this model).

Why two functions instead of one `embed(text)` with a flag:

nomic-embed-text is trained with task-instruction prefixes baked into its
input format: text meant to be INDEXED needs "search_document: " prepended,
text used as a QUERY needs "search_query: " prepended. This isn't cosmetic
-- omitting or swapping the prefix measurably degrades retrieval quality
for this model family, because the model was trained to treat those two
prefixes as distinct tasks, not just distinct strings. A single function
with an `is_query: bool` parameter makes it trivially easy to pass the
wrong value at a call site and get a silent quality regression with no
error. Two named functions make the mistake structurally harder: the
caller has to explicitly reach for the wrong one.

Why Ollama's HTTP API directly, not the `ollama` Python package: this
project's existing code (verify_infra.py, app/main.py's check_ollama)
already talks to Ollama via raw `httpx` calls against
`http://{ollama_host}:{ollama_port}`, not a wrapper library. Matching that
convention keeps one HTTP client pattern in the codebase instead of two.
"""

import httpx

EMBEDDING_MODEL = "nomic-embed-text"
EMBEDDING_DIMENSION = 768  # confirmed via `ollama show nomic-embed-text`; a
# mismatch between this constant and the model actually installed will
# surface as an OpenSearch bulk-index error (vector dimension mismatch),
# not a silent failure -- see indexer.py's rebuild_index() count check,
# which will catch a corrupted partial index the same way it catches any
# other bulk-index error.

DOCUMENT_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "


class EmbeddingError(Exception):
    """Raised when Ollama's embedding endpoint fails or returns a
    vector of the wrong dimension. Distinct from a generic httpx
    exception so callers (indexer.py, the hybrid search route) can
    catch this specifically and decide whether to abort a rebuild or
    degrade to BM25-only, rather than crashing on an unrelated network
    error."""


def _embed(text: str, host: str, port: int, timeout: float = 30.0) -> list[float]:
    url = f"http://{host}:{port}/api/embed"
    try:
        resp = httpx.post(
            url,
            json={"model": EMBEDDING_MODEL, "input": text},
            timeout=timeout,
        )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise EmbeddingError(f"Ollama embedding request failed: {e}") from e

    body = resp.json()
    # /api/embed returns {"embeddings": [[...]]} -- a list of vectors,
    # one per input. We send one input at a time (see module docstring
    # trade-off note below on batching), so we take the first.
    embeddings = body.get("embeddings")
    if not embeddings:
        raise EmbeddingError(f"Ollama returned no embeddings for input: {body}")

    vector = embeddings[0]
    if len(vector) != EMBEDDING_DIMENSION:
        raise EmbeddingError(
            f"Expected {EMBEDDING_DIMENSION}-dim vector from {EMBEDDING_MODEL}, "
            f"got {len(vector)}. Model on this host may not match EMBEDDING_MODEL/"
            f"EMBEDDING_DIMENSION constants -- run `ollama show {EMBEDDING_MODEL}` "
            f"to confirm what's actually installed."
        )
    return vector


def embed_document(text: str, host: str, port: int) -> list[float]:
    """Embed text that will be INDEXED (a chunk's content). Prefixes
    with 'search_document: ' per nomic-embed-text's training format."""
    return _embed(DOCUMENT_PREFIX + text, host, port)


def embed_query(text: str, host: str, port: int) -> list[float]:
    """Embed text that is a QUERY (a search request). Prefixes with
    'search_query: ' -- deliberately a different prefix than
    embed_document, per the module docstring."""
    return _embed(QUERY_PREFIX + text, host, port)


# NOT done in this phase, flagged rather than silently absent: batching
# multiple inputs into a single /api/embed call (Ollama's endpoint accepts
# a list for `input`). At 28 documents, one embedding call per document
# during a full index rebuild is well under a second of added latency --
# not worth the added complexity of batching + partial-failure handling
# yet. Revisit if the corpus grows past "rebuild in a few seconds."
