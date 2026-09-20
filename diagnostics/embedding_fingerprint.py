"""
DIAGNOSTIC ONLY -- separates WHERE cross-machine vector-score drift comes
from. Retrieval-layer read-only: embeds a few fixed strings, reads a few
stored vectors, reads Postgres. Changes nothing.

Context: machine A and B run byte-identical embeddings.py / indexer.py /
mapping.py / queries.py, the same nomic-embed-text model ID, and index the
same text (BM25 scores identical on every probed row) -- yet raw k-NN top-1
scores differ by 0.001-0.05. Code cannot explain that. The remaining
candidates, which this script tells apart:

  Q  query-side runtime drift -- same string, different query vector
     (Ollama version / compute backend). Compare section 1 across machines.
  D  index-side drift -- each machine embedded its own documents at index
     time, possibly under a different Ollama build. Compare "stored" lines
     in section 2 across machines.
  S  stale index -- a machine's index was built under an Ollama behaviour
     that no longer matches what it runs today. Shows up as
     cosine(stored, fresh) < ~0.999 ON THE SAME MACHINE in section 2.

How to read it (paste both machines' output):
  - Section 1 dims/sha equal on A and B  -> queries are numerically the same.
  - Section 2 "fresh" equal on A and B but "stored" differ -> indexes were
    built under different conditions; rebuild one from the other's Ollama.
  - Small differences only in the 5th-6th decimal = float noise, not the
    cause of 0.01-0.05 score gaps. Differences at the 2nd-3rd decimal are.
The sha1 is over vectors rounded to 3 decimals: a coarse "same/different"
flag only -- compare the printed leading dimensions, not just the hash.

Run WITHOUT rebuilding (keeps the running container's code intact):
    docker cp diagnostics/embedding_fingerprint.py rag-fastapi:/app/diagnostics/embedding_fingerprint.py
    docker exec -it rag-fastapi python -m diagnostics.embedding_fingerprint
"""

import hashlib
import math

import httpx
import psycopg2

from app.config import settings
from app.search.embeddings import embed_document, embed_query
from app.search.indexer import FETCH_DOCUMENTS_SQL, build_embedding_input
from app.search.service import get_client

QUERY_STRINGS = ["test cases", "What are the steps to reproduce BUG-1003?"]
DOC_IDS = ["BUG-1011", "BUG-1003", "TC-0301"]
NDIMS = 8


def _norm(v):
    return math.sqrt(sum(x * x for x in v))


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b)) / (_norm(a) * _norm(b))


def _sha(v):
    return hashlib.sha1(",".join(f"{x:.3f}" for x in v).encode()).hexdigest()[:10]


def _show(label, v):
    lead = " ".join(f"{x:+.6f}" for x in v[:NDIMS])
    print(f"  {label:7s} dims={len(v)} norm={_norm(v):.6f} sha3dp={_sha(v)}  first{NDIMS}: {lead}")


def main() -> None:
    base = f"http://{settings.ollama_host}:{settings.ollama_port}"
    try:
        version = httpx.get(f"{base}/api/version", timeout=10).json()
    except Exception as e:  # diagnostic: report, don't crash
        version = f"unavailable ({e})"
    print(f"ollama /api/version : {version}")
    print(f"embed model         : {settings.ollama_embedding_model}")

    print("\n== 1. QUERY side (embed_query, fixed strings) ==")
    for q in QUERY_STRINGS:
        print(f" {q!r}")
        _show("query", embed_query(q, settings.ollama_host, settings.ollama_port))

    print("\n== 2. DOCUMENT side: stored vs freshly re-embedded ==")
    conn = psycopg2.connect(
        host=settings.postgres_host, port=settings.postgres_port,
        user=settings.postgres_user, password=settings.postgres_password,
        dbname=settings.postgres_db, connect_timeout=settings.postgres_connect_timeout,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(FETCH_DOCUMENTS_SQL)
            cols = [c.name for c in cur.description]
            rows = {r[cols.index("external_id")]: dict(zip(cols, r)) for r in cur.fetchall()}
    finally:
        conn.close()

    os_client = get_client(settings.opensearch_host, settings.opensearch_port)
    for ext_id in DOC_IDS:
        print(f" {ext_id}")
        row = rows.get(ext_id)
        if row is None:
            print("  NOT in Postgres")
            continue
        resp = os_client.search(
            index=settings.opensearch_index_alias,
            body={"size": 1, "_source": ["chunk_vector", "chunk_id"],
                  "query": {"term": {"chunk_id": f"{ext_id}::chunk_000"}}},
        )["hits"]["hits"]
        if not resp:
            print("  NOT in OpenSearch (chunk_id not found)")
            continue
        stored = resp[0]["_source"]["chunk_vector"]
        fresh = embed_document(build_embedding_input(row), settings.ollama_host, settings.ollama_port)
        _show("stored", stored)
        _show("fresh", fresh)
        print(f"  cosine(stored, fresh) = {_cos(stored, fresh):.6f}")


if __name__ == "__main__":
    main()
