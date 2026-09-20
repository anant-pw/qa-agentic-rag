"""
DIAGNOSTIC ONLY -- not wired into any endpoint. Retrieval + embedding only:
no chat model call, so a full run takes seconds, not the 100-480s a
/generate/agentic free-text question costs.

Place at: diagnostics/guardrail_shape_probe.py
Run (same pattern as rrf_threshold_probe.py; Dockerfile COPYs diagnostics/):
    docker compose up -d --build fastapi
    docker exec -it rag-fastapi python -m diagnostics.guardrail_shape_probe

WHY THIS EXISTS
Two in-domain, answerable questions false-reject at the guardrail:
    "test cases"                                (no filter, ~175ms)
    "where is the bug steps in bug-1011"        (lowercase ID, awkward phrasing)
Neither has been root-caused. This probe separates three competing
hypotheses instead of assuming one:

  H1 "used != max": guardrail_and_route() takes the vector_score of the
     FIRST hit *in RRF order* that has one -- not the highest vector_score.
     RRF order is driven by both legs, so a BM25-favoured chunk can sit
     above the true nearest-neighbour and lend the guardrail its (lower)
     score. Signature: max_vec >= 0.75 but used < 0.75.

  H2 "cosine is the wrong signal for this query SHAPE": even the TRUE
     nearest-neighbour scores < 0.75, because the query is short/generic
     or is mostly an identifier. build_embedding_input() embeds title +
     description + steps only -- NOT external_id/module/status/doc_type --
     so the vector leg cannot see "bug-1011" at all, by design.
     Signature: raw_knn_top1 < 0.75 while the ID's document is BM25 rank 1.

  H3 "lowercase matters": mapping.py normalizes IDs to lowercase and
     queries.py lowercases them, so this should NOT matter. Included as a
     one-line falsification check (compare the bug-1011 / BUG-1011 rows).

CONTROL ROWS: the threshold-0.75 calibration set (OOD + known-passing
in-domain) is included so every number is read against the same run.
Nothing here proposes a fix; it prints numbers. Decide from the numbers.
"""

import re

from app.config import settings
from app.generation.deterministic import is_countable_question
from app.generation.guardrail import is_in_domain
from app.search.embeddings import embed_query
from app.search.queries import EMBEDDED_ID_PATTERNS, build_knn_query
from app.search.service import get_client, hybrid_search

# (label, question) -- grouped by which hypothesis the group discriminates.
PROBES = [
    # Known failures
    ("FAIL-known", "test cases"),
    ("FAIL-known", "where is the bug steps in bug-1011"),
    # H3: case only (everything else identical)
    ("H3-case", "where is the bug steps in BUG-1011"),
    # H2: strip phrasing awkwardness, keep the ID
    ("H2-id", "bug-1011"),
    ("H2-id", "BUG-1011"),
    ("H2-id", "steps to reproduce bug-1011"),
    ("H2-id", "What are the steps to reproduce BUG-1011?"),
    # Same ID, eval-style (title in parentheses) -- title text from eval_seed.json
    ("H2-id+title", "What are the steps to reproduce BUG-1011 (Duplicate notification bug)?"),
    # H1/H2: short generic in-domain phrases
    ("short-generic", "bug reports"),
    ("short-generic", "bugs"),
    ("short-generic", "test case"),
    ("short-generic", "login bugs"),
    # Controls: known-passing in-domain (verbatim from eval_seed.json)
    ("CTRL-in", "What are the steps to reproduce BUG-1003 (Checkout page times out on slow connections)?"),
    ("CTRL-in", "What are the preconditions for TC-0301 (Search tokenization consistency for hyphenated terms)?"),
    # Controls: OOD (verbatim from diagnostics/rrf_threshold_probe.py)
    ("CTRL-ood", "What is the capital of France?"),
    ("CTRL-ood", "How do I bake a chocolate cake?"),
    ("CTRL-ood", "What's the weather like today?"),
]


def _fmt(x, nd=4):
    return "None" if x is None else f"{x:.{nd}f}"


def _embedded_ids(q: str) -> list[str]:
    found = set()
    for pat in EMBEDDED_ID_PATTERNS:
        found.update(m.group(0).lower() for m in pat.finditer(q))
    return sorted(found)


def probe_one(os_client, label: str, q: str) -> None:
    # 1) Production-faithful: exactly what guardrail_and_route() sees
    #    (same function, same size=retrieval_size, same truncation to top-N
    #    of the FUSED list).
    hits = hybrid_search(
        os_client,
        alias=settings.opensearch_index_alias,
        q=q,
        ollama_host=settings.ollama_host,
        ollama_port=settings.ollama_port,
        size=settings.retrieval_size,
    )
    used = next((h for h in hits if h["vector_score"] is not None), None)
    used_score = used["vector_score"] if used else None
    with_vec = [h for h in hits if h["vector_score"] is not None]
    max_hit = max(with_vec, key=lambda h: h["vector_score"]) if with_vec else None

    # Verdict computed with the router's REAL inputs (no filters here).
    verdict = is_in_domain(
        used_score,
        has_explicit_filter=(
            settings.deterministic_count_routing and is_countable_question(q)
        ),
    )

    # 2) Ground truth for the vector leg, bypassing hybrid_search()'s
    #    fused[:size] truncation: raw unfiltered k-NN top-N.
    vec = embed_query(q, settings.ollama_host, settings.ollama_port)
    raw = os_client.search(
        index=settings.opensearch_index_alias,
        body=build_knn_query(vec, size=settings.retrieval_size),
    )["hits"]["hits"]
    raw_top = raw[0] if raw else None

    bm25_top = next((h for h in hits if h["bm25_rank"] == 1), None)

    print(f"\n[{label}] {q!r}")
    print(f"  verdict(current rule)  : {'ACCEPT' if verdict else 'REJECT'}"
          f"   threshold={settings.vector_score_guardrail_threshold}")
    print(f"  used   (first-by-RRF w/ vector): {_fmt(used_score)}  doc={used['external_id'] if used else None}")
    print(f"  max    (best vector in fused top-{settings.retrieval_size}): "
          f"{_fmt(max_hit['vector_score'] if max_hit else None)}  doc={max_hit['external_id'] if max_hit else None}")
    print(f"  raw kNN top1 (untruncated)     : "
          f"{_fmt(raw_top['_score'] if raw_top else None)}  doc={raw_top['_source']['external_id'] if raw_top else None}")
    print(f"  BM25 rank1                     : "
          f"{bm25_top['external_id'] if bm25_top else None}  score={_fmt(bm25_top['bm25_score'], 3) if bm25_top else 'None'}")
    print("  fused top-3 (rrf order)        : " + " | ".join(
        f"{h['external_id']} rrf={h['rrf_score']:.4f} bm25r={h['bm25_rank']} vecr={h['vector_rank']} vec={_fmt(h['vector_score'])}"
        for h in hits[:3]
    ))

    for id_ in _embedded_ids(q):
        target = [h for h in hits if h["external_id"].lower() == id_]
        if not target:
            print(f"  ID {id_}: NOT in fused top-{settings.retrieval_size}")
        for h in target:
            print(f"  ID {id_}: rrf_pos={hits.index(h) + 1} bm25_rank={h['bm25_rank']} "
                  f"vector_rank={h['vector_rank']} vector_score={_fmt(h['vector_score'])}")


def main() -> None:
    os_client = get_client(settings.opensearch_host, settings.opensearch_port)
    print(f"retrieval_size={settings.retrieval_size} "
          f"deterministic_count_routing={settings.deterministic_count_routing} "
          f"threshold={settings.vector_score_guardrail_threshold} "
          f"embed_model={settings.ollama_embedding_model}")
    for label, q in PROBES:
        probe_one(os_client, label, q)


if __name__ == "__main__":
    main()
