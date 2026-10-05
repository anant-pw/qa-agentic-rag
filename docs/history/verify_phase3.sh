#!/usr/bin/env bash
# Phase 3 verification commands. Run these LOCALLY against your real
# stack (docker compose up -d, ingest.py already run, then
# python scripts/build_index.py). None of this has been executed in
# the sandbox that produced this file -- paste real output back into
# the Phase 3 chat/handoff, do not assume it matches what's shown here.
set -e

echo "=== 1. Index + alias exist ==="
curl -s http://localhost:9200/_cat/aliases/qa_documents?v
curl -s http://localhost:9200/_cat/indices/qa_documents*?v

echo ""
echo "=== 2. Doc count matches Postgres (expect 28: 25 bug_report + 3 test_case) ==="
curl -s http://localhost:9200/qa_documents/_count | python -m json.tool

echo ""
echo "=== 3. doc_type counts (expect 25 / 3) ==="
curl -s -X GET "http://localhost:9200/qa_documents/_count" \
  -H 'Content-Type: application/json' \
  -d '{"query": {"term": {"doc_type": "bug_report"}}}' | python -m json.tool
curl -s -X GET "http://localhost:9200/qa_documents/_count" \
  -H 'Content-Type: application/json' \
  -d '{"query": {"term": {"doc_type": "test_case"}}}' | python -m json.tool

echo ""
echo "=== 4. error_codes counts (expect ERR_401_UNAUTH=3, ERR_504_TIMEOUT=1, ERR_500_INTERNAL=1, ERR_403_FORBIDDEN=2, per phase-02-handoff.md Section 4) ==="
for code in ERR_401_UNAUTH ERR_504_TIMEOUT ERR_500_INTERNAL ERR_403_FORBIDDEN; do
  echo "-- $code --"
  curl -s -X GET "http://localhost:9200/qa_documents/_count" \
    -H 'Content-Type: application/json' \
    -d "{\"query\": {\"term\": {\"error_codes\": \"$code\"}}}" | python -c "import sys,json; print(json.load(sys.stdin)['count'])"
done

echo ""
echo "=== 5. Exact external_id lookup returns exactly 1 hit ==="
curl -s -X GET "http://localhost:9200/qa_documents/_search" \
  -H 'Content-Type: application/json' \
  -d '{"query": {"term": {"external_id": "bug-1013"}}}' | python -m json.tool

echo ""
echo "=== 6. Dangling reference sanity check: BUG-1013 should have mentioned_tc_ids containing TC-0209 ==="
curl -s -X GET "http://localhost:9200/qa_documents/_search" \
  -H 'Content-Type: application/json' \
  -d '{"query": {"term": {"external_id": "bug-1013"}}, "_source": ["external_id", "mentioned_tc_ids"]}' | python -m json.tool

echo ""
echo "=== ACCEPTANCE CRITERIA ==="
echo "[ ] Step 1: alias resolves to exactly one concrete index"
echo "[ ] Step 2: count == 28"
echo "[ ] Step 3: bug_report count == 25, test_case count == 3"
echo "[ ] Step 4: counts match 3/1/1/2 exactly"
echo "[ ] Step 5: exactly 1 hit, external_id == BUG-1013"
echo "[ ] Step 6: mentioned_tc_ids includes TC-0209 (proves dangling-ref extraction works end to end)"
echo "If any check fails, do not proceed to the 5 test queries below -- fix the index first."
