# Phase 4 Handoff — QA-Aware Chunking, Embeddings, and Hybrid Search

## 1. Completion Status: **Complete — verified against the real corpus, including a mid-phase Phase 3 defect fix**

Code was written without a live Postgres/OpenSearch/Ollama available in
the authoring sandbox (same limitation every prior phase hit). It was
then run for real by the project owner on Windows/Docker Desktop
against the actual 28-document corpus. All Phase 4 acceptance checks
passed, including a real BM25-vs-hybrid eval comparison — not asserted,
computed from live endpoint output twice (before and after a bug fix
described in Section 6).

One real defect was found using Phase 4's own eval infrastructure,
traced to root cause in Phase 3's `queries.py`, and fixed within this
phase (not deferred) with the person's explicit approval, since it was
out of Phase 4's original file scope. See Section 6 — this is the most
consequential finding in this phase and should not be treated as a
footnote.

---

## 2. What Was Implemented

- **Ollama embedding client** (`app/search/embeddings.py`): `nomic-embed-text`
  (768-dim, F16, embedding-only capability — confirmed via `ollama show`,
  not assumed) via Ollama's native `/api/embed` endpoint. Two separate
  functions, `embed_document()` and `embed_query()`, applying
  `search_document: ` / `search_query: ` prefixes respectively — this
  model's training format requires the distinction, and splitting the
  function (rather than a single function with an `is_query` flag) makes
  it structurally harder to pass the wrong one at a call site.
- **Chunk-aware OpenSearch mapping** (`app/search/mapping.py`): added
  `chunk_vector` (`knn_vector`, dim 768, `hnsw`/`cosinesimil`),
  `chunk_id`, `parent_document_id`, and `index.knn: true`. All existing
  Phase 3 fields (`title`, `cleaned_text`, `error_codes`,
  `mentioned_bug_ids`, `mentioned_tc_ids`, etc.) are unchanged.
- **Chunking strategy: whole-document = single chunk**, for every
  currently-ingested document. Measured directly against the real
  corpus before deciding this — bug reports run 30-80 words, test cases
  85-125 words, both well under any reasonable chunk-size threshold.
  `chunk_id`/`parent_document_id` schema is in place so a future
  longer document type (e.g. an SRS/spec doc) can be split into real
  multi-chunk documents without a schema change — that splitting logic
  itself was NOT built, since no document requiring it exists yet.
- **Extended indexing** (`app/search/indexer.py`): `FETCH_DOCUMENTS_SQL`
  now joins `bug_reports`/`test_cases` to pull structured per-type
  fields for embedding input (`build_embedding_input()`) — bug reports
  embed `title + description + steps_to_reproduce`; test cases embed
  `title + preconditions + steps + expected_result`. Deliberately NOT
  the flattened `cleaned_text` blob Phase 3 uses for BM25, since field
  order/composition matters to the embedding model in a way it doesn't
  to BM25. `rebuild_index()` now takes optional `ollama_host`/
  `ollama_port`; if provided, embedding failures abort the whole
  rebuild (all-or-nothing, consistent with the existing doc-count
  mismatch behavior) rather than producing a silently partial index.
- **`--no-embed` flag** (`scripts/build_index.py`): rebuilds BM25-only,
  no Ollama call at all — used specifically to prove Phase 3 behavior
  survives Phase 4's changes independent of embeddings being available.
- **Hybrid query construction** (`app/search/queries.py`):
  `build_knn_query()` (filtered k-NN using OpenSearch's native
  `knn.filter`, not a post-hoc bool-wrap, so filtering doesn't starve
  the ANN candidate pool), `build_filters()` (shared filter-clause
  builder so BM25 and k-NN legs can never silently apply filters
  inconsistently), `reciprocal_rank_fusion()` (k=60, the standard RRF
  paper constant — fuses on rank, not raw score, since BM25 and cosine
  similarity are on incompatible scales).
- **`hybrid_search()`** (`app/search/service.py`): runs BM25 and k-NN
  independently with identical filters, fuses via RRF, returns full
  diagnostics per result (`bm25_rank`, `bm25_score`, `vector_rank`,
  `vector_score`, `rrf_score`, `chunk_id`, `parent_document_id`).
  `search()` (Phase 3, BM25-only) is unchanged — same function, same
  response shape.
- **New route** `GET /search/hybrid` (`app/routers/search.py`), separate
  from the frozen `GET /search`. `q` is required (unlike `/search`,
  which tolerates a filter-only listing) — there's no meaningful k-NN
  leg with no text to embed.
- **`eval/run_eval.py`**: reusable script computing Recall@3/5/10 and
  MRR against either endpoint from `eval/eval_seed.json`, plus a
  `--check-err-401-recall-5` regression assertion (see Section 6).
- **`verify_phase4.sh`**: mirrors `verify_phase3.sh`'s
  structure — includes a step 0 that reruns the *entire* Phase 3
  verification via `--no-embed`, before any Phase-4-specific check.

---

## 3. Files Created or Changed

| Path | Status | Purpose |
|---|---|---|
| `app/search/embeddings.py` | **New** | Ollama embedding client, document/query prefix split |
| `app/search/mapping.py` | Changed | Added `chunk_vector`/`chunk_id`/`parent_document_id`, `index.knn: true` |
| `app/search/indexer.py` | Changed | Structured embedding input, chunk fields, all-or-nothing embedding failure handling |
| `app/search/queries.py` | Changed | Added `build_knn_query`, `build_filters`, `reciprocal_rank_fusion`, `extract_embedded_ids` param (see Section 6) |
| `app/search/service.py` | Changed | Added `hybrid_search()`; `search()` untouched |
| `app/routers/search.py` | Changed | Added `GET /search/hybrid` |
| `app/main.py` | Changed (cleanup only) | Removed a dead, never-wired duplicate `/search` router found during the readiness check — zero behavior change, confirmed by reading the file before and after |
| `scripts/build_index.py` | Changed | `--ollama-host`/`--ollama-port`/`--no-embed` flags |
| `eval/run_eval.py` | **New** | Recall@k/MRR scoring against a live endpoint, plus the ERR_401 regression check |
| `verify_phase4.sh` | **New** | Full Phase 4 verification sequence, corrected mid-phase for a `python3`-vs-`python` Windows bug (Section 6) |
| `requirements.txt` | Changed | Pinned `opensearch-py==2.7.1` (was unpinned) |

**Not changed:** `schema.sql`, `ingest.py`, `docker-compose.yml`, `.env.example`, `app/config.py`.

---

## 4. Design Decisions and Why

- **Whole-document = single chunk.** Measured, not assumed: every
  ingested document (30-165 words) is smaller than any reasonable
  chunk-size floor. Building real section-aware splitting logic against
  documents that don't need it would have been speculative engineering.
  The `chunk_id`/`parent_document_id` schema exists so this doesn't
  require a reindex-format change later, but the actual splitting logic
  is explicitly deferred until a document that needs it exists.
- **`nomic-embed-text` over a heavier alternative** (e.g.
  `mxbai-embed-large`): smaller (274MB vs. ~670MB), already pulled,
  no retrieval-quality argument at this corpus size justifies the
  larger model's RAM/download cost on a resource-constrained machine.
- **Structured embedding input, not `cleaned_text`.** Field
  composition/order carries semantic weight for an embedding model in a
  way it doesn't for BM25's bag-of-words scoring — embedding
  `title + description + steps_to_reproduce` (bug reports) or
  `title + preconditions + steps + expected_result` (test cases)
  separately from the BM25 text field is a deliberate divergence, not
  an oversight.
- **IDs/error codes stay lexical, not embedded.** `ERR_401_UNAUTH` and
  `BUG-1013` are opaque tokens with no reliable semantic neighborhood in
  embedding space — same argument Phase 3's `mapping.py` already made
  for BM25's analyzer choice, extended to embeddings.
- **New `/search/hybrid` route, not a modification of `/search`.**
  Guarantees Phase 3's frozen behavior structurally rather than by
  discipline while editing shared code — confirmed in Section 7 by
  exact output comparison against `phase3_query_results_TEMPLATE.md`.
- **RRF, k=60, unmodified from the original paper's constant.** No
  corpus-specific evidence justified a different value; fusing on rank
  rather than raw score sidesteps BM25/cosine being on incompatible
  scales without needing a score-normalization step neither leg
  performs.
- **Full diagnostics on every hybrid result** (`bm25_rank`,
  `vector_rank`, `rrf_score`, etc.) — a hybrid result that can't be
  attributed to a specific ranker is a result that can't be debugged,
  which defeats this project's stated eval-first purpose.

---

## 5. What Was NOT Done (deliberately out of scope, per the phase brief)

RAG generation, Ollama chat completion, streaming, source-attribution
prose, Redis, Langfuse, LangGraph. All correctly Phase 5+ territory.
Real multi-chunk splitting logic (no document currently needs it — see
Section 4). Batched embedding calls (28 documents embed in well under a
second one-at-a-time; not worth the complexity yet, same "revisit if
corpus grows" posture Phase 3 already established for full-rebuild
indexing).

---

## 6. Bugs Found and Fixed (real, from actual runs against the live corpus)

1. **Phase 3 exact-ID boost defect, found via Phase 4's eval data, fixed
   with explicit approval mid-phase.** `build_search_query()`'s
   exact-term boost only fired when the ENTIRE query was a single
   token. Eval baseline showed the `error_code_aggregation` question
   for `ERR_401_UNAUTH` completely missing `BUG-1015` from the top 10
   — root-caused to this gating logic: the question is a full sentence
   containing `ERR_401_UNAUTH`, so the boost never triggered, and BM25
   fell back to bare-word `multi_match` scoring that buried the
   correct document under unrelated ones sharing incidental vocabulary
   ("bug," "reports," "related"). **Fix**: `extract_embedded_ids: bool`
   parameter on `build_search_query()`, default `False`. When `True`,
   scans the query for `BUG-\d{4}` / `TC-\d{4}` / `ERR_[A-Z0-9_]+`
   patterns regardless of overall query length and adds boosted term
   clauses for whatever is found. `/search` does NOT pass this flag —
   confirmed via direct function-level testing that
   `extract_embedded_ids=False` produces an identical query shape to
   pre-fix Phase 3 (1 `should` clause vs. 5 when `True`). Only
   `hybrid_search()`'s BM25 leg passes `True`. Verified end-to-end
   against the live corpus: `BUG-1015`'s `bm25_rank` moved from 12 to
   3 on the exact failing question, and the eval regression check
   (`--check-err-401-recall-5`) went from failing to passing.
   **Impact on eval numbers**: Recall@5 improved from 92.4% to 100.0%
   after this fix, at the cost of a 1.5-point Recall@3 dip (83.3% →
   81.8%) — expected, since the fix changes ranking for every
   multi-word query containing an embedded ID, not just the one it was
   found on.
2. **Container running stale code, twice.** `Dockerfile` copies `app/`
   at build time; there is no source volume mount in
   `docker-compose.yml`. Editing Python files on the host does nothing
   to the running `rag-fastapi` container until
   `docker compose up -d --build fastapi` is run. Hit once for the
   initial `/search/hybrid` 404, hit again implicitly by anyone who
   forgets this after future edits — flagged here explicitly because
   it's a real gap, not fixed in this phase (no volume mount was added;
   that's a deliberate scope decision left for whoever wants
   hot-reload during active development, not needed for this project's
   phase-by-phase cadence).
3. **`verify_phase4.sh` used `python3`, hit the exact Windows/Git-Bash
   bug Phase 3's handoff already documented and fixed once** (`python3`
   resolves to the Windows Store stub, not the active venv). This was
   a preventable regression — the fact pattern was already in
   `phase-03-handoff.md` Section 6 before this script was written.
   Fixed by switching to bare `python`, matching `verify_phase3.sh`'s
   established convention.
4. **Git Bash MSYS path-conversion mangled `--endpoint /search`** into
   a Windows filesystem path (`C:/Program Files/Git/search`) before
   Python ever saw the argument — an MSYS2 quirk (auto-rewriting any
   leading-slash CLI argument), not an argparse or script bug. Not
   fixed in code (nothing in `eval/run_eval.py` can detect or prevent
   this from the Python side); documented as a required invocation
   pattern instead: `MSYS_NO_PATHCONV=1 python -m eval.run_eval
   --endpoint /search`.

---

## 7. What Was Verified (real commands, real output)

- `python -m scripts.build_index --no-embed` → `bash verify_phase3.sh`:
  all 6 Phase 3 acceptance checks passed against the Phase-4-modified
  codebase, proving the shared indexer/mapping changes didn't break
  BM25-only rebuilds.
- `python -m scripts.build_index` (with embeddings): index mapping
  confirmed `index.knn: true`, `chunk_vector` present as `knn_vector`
  dim 768 with `hnsw`/`cosinesimil`; document count with a non-null
  `chunk_vector` confirmed at 28/28 — no document silently skipped.
- Direct k-NN query against the live index returned real, ranked
  results (`BUG-1001`, `TC-0142`, `BUG-1002` for a login/password query)
  — not an error, not a placeholder.
- `GET /search?q=ERR_401_UNAUTH` output compared directly against
  `phase3_query_results_TEMPLATE.md` query 1 — identical results and
  scores, confirming zero behavior drift on the frozen endpoint.
- `GET /search/hybrid` confirmed returning full diagnostics
  (`bm25_rank`, `bm25_score`, `vector_rank`, `vector_score`,
  `rrf_score`, `chunk_id`, `parent_document_id`) on every result.
- Full eval comparison run twice (before and after the Section 6 fix),
  against the live 28-document corpus, via `eval/run_eval.py`:

| Metric | BM25 baseline | Hybrid (post-fix) |
|---|---|---|
| Recall@3 | 66.7% | 81.8% |
| Recall@5 | 78.8% | 100.0% |
| Recall@10 | 92.4% | 100.0% |
| MRR | 0.939 | 1.000 |

- `--check-err-401-recall-5` regression check: failed before the
  Section 6 fix (`BUG-1015` missing from top-5), passed after
  (confirmed present at rank 3).

**Not verified:**
- Behavior under any condition other than local Windows + Docker
  Desktop + Git Bash — consistent with every prior phase's caveat.
- Whether `--no-embed`'s BM25-only rebuild survives a scenario where
  Ollama was never pulled at all on a fresh machine (tested here only
  with Ollama already available but deliberately not called).
- Real-world latency of `hybrid_search()` under concurrent requests —
  each call does one live embedding round-trip to Ollama; fine at
  interactive, single-user testing volume, unmeasured under load.

---

## 8. Known Limitations (confirmed against real query results)

- **The Section 6 fix is corpus-pattern-specific, not a general
  solution to embedded-entity extraction.** It catches `BUG-XXXX`,
  `TC-XXXX`, `ERR_XXXX`-shaped substrings specifically — the same
  regex family `ingest.py` and `indexer.py` already use, now
  triplicated across three files. If the ID format ever changes, all
  three need updating. This was already flagged as a drift risk in
  `phase-03-handoff.md`; Phase 4 made it worse by copying the pattern a
  third time rather than centralizing it. Worth a shared-constants
  module in a future cleanup pass, not fixed here.
- **`hybrid_search()` fetches exactly `size` results per leg before
  fusion**, not a wider candidate pool. Not a problem at 28 documents
  (confirmed: the failing case was recoverable within a `size=15`
  fetch without needing a wider pool) but is the first thing to widen
  if the corpus grows and a document ranks outside `size` on one leg
  while ranking well on the other.
- **RRF's k=60 is unvalidated for this corpus size specifically** —
  it's the standard default, not a tuned value. The one real regression
  observed (Recall@3 dropping 1.5 points post-fix) was not
  root-caused to `k` specifically and no evidence currently points at
  `k` as the cause; recorded here as an open question, not a diagnosed
  problem.
- **No source volume mount** — every code change requires
  `docker compose up -d --build fastapi` before it's live. Real
  friction during active development, not fixed in this phase (see
  Section 6, bug 2).
- **10-vs-3 test case discrepancy** (from Phase 2, still unresolved):
  unchanged by Phase 4. Whatever `ingest.py` loaded is what gets
  chunked/embedded/indexed; nothing in Phase 4's design assumes a fixed
  count.
- **Batched embedding calls not implemented** — one Ollama round-trip
  per document during a full rebuild. Negligible at 28 documents;
  revisit if rebuild time stops being "a few seconds."

---

## 9. Conflicts With Phase 1 / Phase 2 / Phase 3

**None structural**, and one explicit, approved exception:

- The Section 6 fix touches `app/search/queries.py`, a Phase 3 file,
  outside Phase 4's original stated scope ("preserve the working Phase
  3 BM25 behavior and endpoint"). This was flagged explicitly before
  being made, approved by the project owner, and implemented as an
  additive, opt-in parameter specifically so `/search`'s behavior
  remains unchanged — the constraint was honored in spirit (Phase 3's
  endpoint behavior is byte-identical, confirmed) even though a Phase
  3 file was edited. Recorded here so this isn't discovered later as
  an unexplained divergence from "Phase 4 doesn't touch Phase 3 files."
- `app/config.py`'s Postgres/OpenSearch/Ollama connection conventions
  (unchanged from Phase 1) continue to apply; `scripts/build_index.py`
  continues the host-side `127.0.0.1:5433` / `localhost` convention
  Phase 3 established, not the Docker-network hostnames `app/main.py`
  uses internally.

---

## 10. Files Phase 5 Should Inspect First

1. `app/search/service.py` — `hybrid_search()` is very likely what
   Phase 5's RAG endpoint retrieves from before generation; its
   response shape (chunk-level results with `parent_document_id` for
   citation) is what a grounded-answer prompt needs to cite sources
   correctly. `search()` (BM25-only) remains available if Phase 5 ever
   needs a non-hybrid retrieval path.
2. `app/search/embeddings.py` — establishes the Ollama HTTP-call
   pattern (`httpx` direct, not a wrapper library) Phase 5 should
   follow for its own Ollama chat-completion calls, for consistency.
3. `eval/eval_seed.json` and `eval/run_eval.py` — Phase 5 needs its own
   answer-quality eval, but retrieval correctness is already measured
   here; don't re-litigate retrieval quality in Phase 5's eval, extend
   this one's infrastructure for generation quality instead.
4. This document — where anything here conflicts with the live repo,
   the repo is correct, not this summary.
