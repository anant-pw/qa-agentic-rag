#!/usr/bin/env bash
# Phase 4 verification commands. Run these LOCALLY against your real
# stack. None of this has been executed against a live system in the
# sandbox that produced this file -- paste real output back before
# treating Phase 4 as verified, same discipline as verify_phase3.sh.
#
# Prerequisites, in order:
#   docker compose up -d
#   python ingest.py                    (if not already run)
#   ollama pull nomic-embed-text         (confirmed already done)
#   python -m scripts.build_index        (rebuilds WITH embeddings -- this
#                                          is the default; use --no-embed
#                                          only for step 1 below)
set -e

echo "=== 0. Confirm Phase 3 is UNCHANGED (rebuild BM25-only, no Ollama needed) ==="
echo "Run this once to confirm the --no-embed path still produces a working"
echo "Phase-3-equivalent index, independent of anything Phase 4 added:"
echo "  python -m scripts.build_index --no-embed"
echo "  bash verify_phase3.sh"
echo "All 6 Phase 3 acceptance checks must still pass. If they don't,"
echo "Phase 4 broke something in the shared indexer/mapping path -- stop"
echo "and fix before proceeding, do not layer more changes on top."
echo ""
echo "Then rebuild WITH embeddings for the checks below:"
echo "  python -m scripts.build_index"
echo ""

echo "=== 1. index.knn is set and chunk_vector field exists ==="
curl -s http://localhost:9200/qa_documents/_mapping | python -m json.tool
curl -s http://localhost:9200/qa_documents/_settings | python -m json.tool

echo ""
echo "=== 2. Every document has a non-null chunk_vector (28/28 expected) ==="
curl -s -X GET "http://localhost:9200/qa_documents/_count" \
  -H 'Content-Type: application/json' \
  -d '{"query": {"exists": {"field": "chunk_vector"}}}' | python -m json.tool

echo ""
echo "=== 3. k-NN query returns results (sanity check, not a relevance judgment) ==="
echo "This requires a real 768-dim query vector -- generate one via Python,"
echo "not a hand-typed placeholder:"
python - <<'EOF'
import json
import httpx
from app.search.embeddings import embed_query

vec = embed_query("login keeps failing after password reset", "localhost", 11434)
body = {"size": 3, "query": {"knn": {"chunk_vector": {"vector": vec, "k": 3}}}}
resp = httpx.post("http://localhost:9200/qa_documents/_search", json=body, timeout=10)
resp.raise_for_status()
hits = resp.json()["hits"]["hits"]
print(f"k-NN returned {len(hits)} hit(s):")
for h in hits:
    print(f"  {h['_source']['external_id']}  score={h['_score']:.4f}")
EOF

echo ""
echo "=== 4. /search (Phase 3) still returns identical shape/behavior ==="
curl -s "http://localhost:8000/search?q=ERR_401_UNAUTH" | python -m json.tool
echo "Compare against phase3_query_results_TEMPLATE.md query 1 -- results and"
echo "scores should match exactly (same BM25 query path, untouched)."

echo ""
echo "=== 5. /search/hybrid returns BM25+vector+RRF diagnostics per result ==="
curl -s "http://localhost:8000/search/hybrid?q=login%20keeps%20failing%20after%20password%20reset" | python -m json.tool
echo "Each result must include: bm25_rank, bm25_score, vector_rank,"
echo "vector_score, rrf_score, chunk_id, parent_document_id."

echo ""
echo "=== 6. Full eval comparison: Phase 3 baseline vs Phase 4 hybrid ==="
echo "--- BM25 baseline (/search) ---"
python -m eval.run_eval --endpoint /search
echo ""
echo "--- Hybrid (/search/hybrid), with the ERR_401_UNAUTH regression check ---"
python -m eval.run_eval --endpoint /search/hybrid --check-err-401-recall-5

echo ""
echo "=== ACCEPTANCE CRITERIA ==="
echo "[ ] Step 0: --no-embed rebuild still passes all 6 Phase 3 checks unmodified"
echo "[ ] Step 1: index settings show index.knn=true; mapping shows chunk_vector as knn_vector, dim 768"
echo "[ ] Step 2: chunk_vector count == 28 (every document embedded, none silently skipped)"
echo "[ ] Step 3: k-NN query returns 3 hits with real scores (not an error)"
echo "[ ] Step 4: /search output matches phase3_query_results_TEMPLATE.md exactly -- no behavior drift"
echo "[ ] Step 5: /search/hybrid returns full diagnostic fields on every result"
echo "[ ] Step 6: eval.run_eval --check-err-401-recall-5 exits 0 (the one question that completely"
echo "    failed on the BM25 baseline must be fully recovered at k<=5, not just improved on average)"
echo "If any check fails, do not treat Phase 4 as verified -- paste the real"
echo "failing output back rather than proceeding to a handoff document."
