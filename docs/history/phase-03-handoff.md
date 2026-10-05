# Phase 3 Handoff — BM25 Keyword Search Foundation

## 1. Completion Status: **Complete — verified against the real corpus**

Code was written and unit-tested in a sandbox with no Docker/Postgres/
OpenSearch (same limitation Phase 1 hit). It was then run for real by
the project owner on Windows/Docker Desktop, against the actual
ingested 28-document corpus (25 bug reports, 3 test cases). All six
acceptance checks in `verify_phase3.sh` passed with exact expected
values — 28 total docs, 25/3 doc_type split, 3/1/1/2 error-code counts,
1 exact hit on `BUG-1013`, and the dangling `TC-0209` reference
correctly present via `mentioned_tc_ids`. Real output is captured in
`phase3_query_results_TEMPLATE.md` (now filled in, not a template).

Two real bugs surfaced during this real run that the sandbox's
synthetic testing could not have caught — see Section 6.

---

## 2. What Was Implemented

- OpenSearch index mapping (`app/search/mapping.py`) for a `qa_documents`
  index: `standard` analyzer for full text, `keyword` fields with a
  lowercase normalizer for exact-match metadata, `number_of_replicas: 0`
  (single-node dev cluster).
- Index-time entity extraction (`app/search/indexer.py`): reuses
  `ingest.py`'s `TC_ID_RE`/`BUG_ID_RE` patterns to pull `mentioned_bug_ids`
  and `mentioned_tc_ids` out of `cleaned_text` at index build time, plus a
  Postgres join to attach `error_codes` per document. These become
  dedicated `keyword` fields — IDs are NOT trusted to survive standard
  analyzer tokenization (see Section 8).
- Full-rebuild-with-alias-swap indexing workflow (`rebuild_index()` in
  `indexer.py`, CLI wrapper `scripts/build_index.py`): builds a new
  versioned index, verifies the doc count against Postgres before
  touching the alias, then atomically flips `qa_documents` to point at
  it, keeping the previous index for rollback.
- BM25 query builder (`app/search/queries.py`): `multi_match` over
  `title^2` + `cleaned_text` for relevance, `filter` clauses for
  `doc_type`/`module`/`status` (unscored), and a boosted exact-`term`
  clause on ID/error-code fields — restricted to single-token queries
  after a dry-run test showed multi-word natural-language queries were
  generating four always-false term clauses per search (found and fixed
  in this session, see Section 6).
- `GET /search` FastAPI endpoint (`app/routers/search.py`) — not yet
  wired into `app/main.py`; requires one manual `include_router` line
  (instructions in the file itself, so this diff doesn't silently
  overwrite whatever else `main.py` has picked up since Phase 2).

---

## 3. Files Created (all under a working directory, not yet merged into your repo)

| Path | Purpose |
|---|---|
| `app/search/mapping.py` | Index settings + field mapping, with the analyzer/normalizer reasoning inline |
| `app/search/indexer.py` | Postgres row → OpenSearch doc shaping; full-rebuild + alias-swap logic |
| `app/search/queries.py` | BM25 query construction, filters, exact-ID boosting |
| `app/search/service.py` | Thin OpenSearch client wrapper + result shaping |
| `app/routers/search.py` | `GET /search` endpoint (needs manual `include_router` addition to `main.py`) |
| `scripts/build_index.py` | CLI: rebuild the index from live Postgres |
| `verify_phase3.sh` | Exact `curl` commands + acceptance criteria — **run this first** |
| `phase3_query_results_TEMPLATE.md` | Fill-in template for the 5 required queries — do not fabricate the contents |
| `config_additions.py`, `requirements_phase3_additions.txt`, `env_phase3_additions.txt` | Small diffs to apply to `app/config.py`, `requirements.txt`, `.env.example` — not full-file overwrites |

**Not created:** any change to `docker-compose.yml` (OpenSearch service already exists from Phase 1, unchanged), any change to `schema.sql` (Phase 3 reads Postgres, doesn't modify it).

---

## 4. Design Decisions and Why

- **Full rebuild + alias swap, not incremental sync.** `documents` has no
  `updated_at` column — confirmed by reading `schema.sql`, not assumed.
  Incremental reindexing needs a way to ask "what changed," which doesn't
  exist yet. A 28-row corpus rebuilds in well under a second; doing
  incremental sync on a store explicitly designed as "derived and
  rebuildable" (Phase 1 principle) would be solving a problem this
  architecture doesn't have at this scale. **Explicitly deferred, not
  fixed**: real incremental reindexing needs (a) an `updated_at` column
  + trigger or application-level bump, and (b) either a soft-delete flag
  or an ID-diff pass to handle deletions. Revisit if/when full rebuild
  stops being "a few seconds."
- **Exact IDs extracted at index time into dedicated `keyword` fields**,
  not left to the text analyzer. `"BUG-1013"` under a standard analyzer
  tokenizes to `["bug", "1013"]` — a free-text search for the ID would
  actually match on "bug" AND "1013" appearing anywhere, which is a real
  false-positive risk, not a hypothetical one (e.g., a document
  mentioning an unrelated "1013" count near the word "bug"). Trade-off:
  the extraction regex (copied from `ingest.py`, `\bBUG-\d{4}\b` /
  `\bTC-\d{4}\b`) only catches exactly this format — a 5-digit ID or a
  different prefix format falls back to text-only recall silently. This
  regex is now duplicated in two files (`ingest.py`, `indexer.py`); if
  the ID format ever changes, both need updating — flagged as a real
  maintenance liability, not hidden.
- **`standard` analyzer, not `english`.** Stemming trades recall for
  precision loss on a small technical corpus where exact vocabulary
  (module names, status values) carries most of the signal. Reversible —
  if Phase 3 real-query testing (Section 7) shows natural-language
  queries missing obvious matches specifically because of un-stemmed
  vocabulary mismatch, this is the first thing to change.
- **Exact-ID term boosting restricted to single-token queries.** Found
  during unit testing in this session (not theoretical): the original
  version added `term` clauses on `external_id`/`mentioned_bug_ids`/
  `mentioned_tc_ids`/`error_codes` for every query, including full
  natural-language sentences, which can never match a keyword field.
  That's dead weight on every multi-word query. Fixed by gating the
  boost clauses on `" " not in query`.

---

## 5. What Was NOT Done (deliberately out of scope, per your instructions)

Embeddings, vector search, chunking, hybrid retrieval, RRF fusion, LLM
generation, Redis, Langfuse, LangGraph. All correctly Phase 4+ territory.

---

## 6. Bugs Found and Fixed While Building This (real, from actual test runs in this session)

1. Exact-ID `should` clauses were unconditionally added to every query,
   including multi-word natural-language ones, where they can never
   match. Caught by printing the actual query DSL for a synthetic
   6-word query and noticing 4 always-false clauses. Fixed by gating on
   single-token queries (Section 4, Section 8).

That's the only bug the sandbox's synthetic-data testing could catch on
its own. Two more surfaced once the project owner actually ran this
against the real stack on Windows — exactly the kind of thing unit
tests against fake data can't find:

2. `verify_phase3.sh` used `python3` for `json.tool` piping, which
   resolves on Mac/Linux but hits the Windows Store's Python stub on
   Windows/Git Bash, not the active venv's interpreter. Fixed by
   switching to `python` (bare) throughout the script. Environment
   assumption baked into the original script without being stated —
   should have been flagged as Mac/Linux-authored up front.
3. `scripts/build_index.py` run as `python scripts/build_index.py`
   failed with `ModuleNotFoundError: No module named 'app'` — running a
   script directly by path doesn't put the project root on `sys.path`,
   so the `from app.search.indexer import ...` import couldn't resolve.
   Fixed by adding `scripts/__init__.py` and invoking it as
   `python -m scripts.build_index` from the project root instead, which
   does put the project root on the path. This is a Python import-system
   behavior, not an OS-specific issue — would have hit on Mac/Linux too.

Both are now real, confirmed-fixed items — not theoretical caveats.

---

## 7. Verification: What You Need To Run

```bash
# 1. Apply the small diffs
#    - app/config.py: add opensearch_index_alias: str = "qa_documents"
#    - requirements.txt: add opensearch-py==2.7.1
#    - .env.example: add OPENSEARCH_INDEX_ALIAS=qa_documents
#    - app/main.py: add the two lines from app/routers/search.py's docstring

pip install -r requirements.txt --break-system-packages   # or your existing install method

# 2. Confirm data is actually loaded (Phase 2 prerequisite)
docker compose up -d
python ingest.py

# 3. Build the index
python scripts/build_index.py

# 4. Run all acceptance checks
bash verify_phase3.sh

# 5. Only if step 4 fully passes: run the 5 required queries via
#    http://localhost:8000/search?... and fill in
#    phase3_query_results_TEMPLATE.md with REAL output
```

**Acceptance criteria** (also embedded in `verify_phase3.sh`):
1. Alias `qa_documents` resolves to exactly one concrete index.
2. Document count == 28 (25 bug_report + 3 test_case).
3. `doc_type` filter counts == 25 / 3.
4. `error_codes` counts == 3 / 1 / 1 / 2 for `ERR_401_UNAUTH` /
   `ERR_504_TIMEOUT` / `ERR_500_INTERNAL` / `ERR_403_FORBIDDEN`.
5. Exact query for `external_id=bug-1013` returns exactly 1 hit.
6. That hit's `mentioned_tc_ids` includes `TC-0209` — proves the
   dangling-reference case is correctly extracted end to end, not just
   present in Postgres.

If any of these fail, the index is wrong — do not proceed to the 5
required queries or to Phase 4 until they pass.

---

## 8. Known Limitations (confirmed against real query results, not predicted)

- **Free-text ID queries pull in unrelated documents via bare-word
  matching.** `q=BUG-1013` correctly ranks BUG-1013 first (score 14.8,
  ~2.6x the next result), but the tail of the result set includes
  BUG-1011, BUG-1024, BUG-1017 — none topically related to BUG-1013's
  payment-form issue. Root cause: the standard analyzer tokenizes
  "BUG-1013" into `["bug", "1013"]`, and the `multi_match` clause then
  matches any document containing the bare word "bug" (which appears in
  most bug-report titles/text as a side effect of cross-references like
  "see BUG-1001"). The exact-term boost on `external_id` saves the top
  result, but doesn't suppress the noisy tail. Not fixed in this phase —
  would need either `minimum_should_match` tuning or dropping the
  `multi_match` clause entirely for single-token ID-shaped queries,
  which is a real design choice worth making deliberately in a later
  pass, not silently.
- **Dangling-reference handling confirmed working end-to-end**, not just
  in Postgres: `q=TC-0209` correctly returns zero documents with that
  `external_id` and correctly surfaces BUG-1013 via `mentioned_tc_ids`
  as the citing document. This was the single highest-value test in the
  5-query set and it passed cleanly.
- **10-vs-3 test case discrepancy** (from Phase 2, still unresolved):
  Phase 3 indexes whatever `ingest.py` actually loaded — currently 3
  test cases. If more test case files surface later, re-run
  `ingest.py` then `scripts/build_index.py`; nothing in Phase 3's design
  assumes a fixed count.
- **ID-extraction regex duplicated** in `ingest.py` and
  `app/search/indexer.py` — a real drift risk, not fixed in this phase.
- **BM25's honest weakness on paraphrase** (query 4 in the template) is
  expected to show up as a real limitation, not a bug — that's the
  argument for Phase 4, and should be documented with real numbers once
  run, not asserted here.
- **No auth/TLS on the OpenSearch client** — consistent with Phase 1's
  local-dev-only security posture (`plugins.security.disabled=true`).
  Same caveat Phase 1 already gave: never reuse this pattern
  internet-facing.

---

## 9. Conflicts With Phase 1 / Phase 2

None structural. `opensearch_host`/`opensearch_port` reused unchanged
from `app/config.py`. The Postgres connection convention in
`scripts/build_index.py` matches `ingest.py`'s (host-side
`127.0.0.1:5433`, not the `postgres` Docker-network hostname), for the
same reason: it's meant to be run from the host, not inside a container.

---

## 10. Files Phase 4 Should Inspect First

1. `app/search/mapping.py` — the index shape Phase 4's embeddings/hybrid
   search will need to add vector fields alongside, not replace.
2. `app/search/queries.py` — BM25 query shape that RRF fusion in Phase 4
   will need to run in parallel with a k-NN query.
3. `phase3_query_results_TEMPLATE.md` (once filled in with real output)
   — the concrete evidence for where BM25 actually falls short on
   natural-language queries, which is exactly what Phase 4 exists to fix.
4. `eval/eval_seed.json` — re-run these 12 questions through `/search`
   as a baseline before Phase 4 adds embeddings, so there's a real
   before/after comparison instead of an assumed one.
5. This document — where anything here conflicts with the live repo
   once Phase 3 is actually run, the repo (and the filled-in query
   template) is correct, not this file.
