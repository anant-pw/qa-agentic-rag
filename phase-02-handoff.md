# Phase 2 Handoff — QA Domain Ingestion

## 1. Completion Status: **Complete**

Ingestion pipeline verified end-to-end against real data and a live Postgres
instance — not just a script that printed success. Every claim below was
checked with an actual command and actual output, not assumed. One known,
non-blocking fragility remains (Section 9).

---

## 2. What Was Implemented

- Postgres schema for the QA corpus: class-table inheritance (`documents`
  parent + `bug_reports`/`test_cases` children), a generalized
  `document_references` table for cross-doc links, and a normalized
  `error_codes` table.
- `ingest.py`: parses `bug_reports.csv` and `test_cases/*.md`, cleans embedded
  HTML, extracts error codes and TC-ID/BUG-ID cross-references via regex,
  loads everything into Postgres, and classifies bug-to-bug references as
  `duplicate_of` or `related_to` based on explicit language in the text.
- `generate_eval_seed.py`: derives eval questions from the live
  `document_references` graph after ingestion, rather than hand-authoring
  them — rewritten mid-phase to cover *all* duplicate/related/error-code
  groups instead of one example per category, plus two genuinely
  data-driven single-document questions (no placeholders).
- `eval/eval_seed.json`: 12 real questions, generated from and verified
  against the live database.

---

## 3. Files Created or Changed

| Path | Purpose | Notes |
|---|---|---|
| `schema.sql` | QA schema | Applied to live Postgres, verified via `\dt` |
| `ingest.py` | Parse + load + reference resolution | See Section 9 for the 3 real bugs found and fixed while getting this running |
| `generate_eval_seed.py` | Derives eval_seed.json from live DB | Rewritten once (see Section 9) after first version silently truncated to 1 example per category and left 2 template placeholders unfilled |
| `requirements.txt` | Pinned deps | Added `scikit-learn==1.5.2` (was missing; `ingest.py` needs it for TF-IDF fallback duplicate detection) |
| `docker-compose.yml` | Infra orchestration | `postgres` host port changed `5432→5433` (Section 9) |
| `.env.example` | Env template | `POSTGRES_PORT` updated to `5433` to match |
| `eval/eval_seed.json` | Fixed eval seed set | 12 questions, verified field-by-field against a Python simulation of the DB query logic, not just trusted on row count |

---

## 4. Data Actually Ingested

- **25 bug reports**, **3 test cases** (`TC-0142`, `TC-0301`, `TC-0302`).
  The original project brief mentioned 10 test case docs — only 3 were ever
  provided. **Unresolved**: either more files exist and weren't uploaded, or
  the brief's number was aspirational. Not something this phase can resolve
  on its own.
- **4 unique error codes** across 7 bug reports: `ERR_401_UNAUTH` (3 bugs),
  `ERR_504_TIMEOUT` (1), `ERR_500_INTERNAL` (1), `ERR_403_FORBIDDEN` (2).
- **8 `document_references` rows total**: 3 `test_case_citation` (2 resolved,
  1 genuinely dangling — `BUG-1013` cites `TC-0209`, which does not exist in
  the uploaded corpus, correctly recorded rather than silently dropped),
  4 `duplicate_of`, 1 `related_to`.

---

## 5. Design Decisions and Why

- **Class-table inheritance, not one wide table** — bug reports and test
  cases don't share a field shape (`steps_to_reproduce` vs.
  `preconditions`/`steps`/`expected_result`); a single table would mean
  permanently-null columns on every row.
- **Explicit `BUG-XXXX` text mentions are the primary duplicate signal, not
  TF-IDF similarity.** Verified against the real 25-bug corpus: TF-IDF's
  top-ranked pair (BUG-1006↔BUG-1021, 0.289 similarity) was a false
  positive — same module, unrelated issues. The real duplicate
  BUG-1006↔BUG-1011 scored only 0.143, below any usable threshold. TF-IDF
  + Ollama LLM-confirm is stubbed in as a fallback discovery pass
  (`ollama_confirm_fn=None`, never wired up) for corpora where reporters
  don't cross-reference explicitly — **not needed for this dataset**, since
  explicit-mention regex caught 100% of the real duplicates.
- **`duplicate_of` vs. `related_to` distinction is deliberate**, not
  incidental: `BUG-1017` cites `BUG-1007` as a resolved follow-up ticket
  (different relationship than two independently-filed reports of the same
  bug). Conflating the two would be a real correctness bug in an eval
  system meant to test whether retrieval understands *why* documents relate,
  not just *that* they do.
- **OpenSearch indexing is correctly out of scope for this phase** per the
  original brief — that's Phase 3.

---

## 6. Bugs Found and Fixed While Verifying (all confirmed, not just claimed)

1. **`requirements.txt` missing `scikit-learn`** — `ingest.py` imports it;
   would have failed on first run regardless of DB connectivity.
2. **Hardcoded reference-repo credentials** (`arxiv_curator`/`postgres`/
   `postgres`) left over in both `ingest.py` and `generate_eval_seed.py`'s
   `__main__` blocks — didn't match the actual `rag_db`/`rag_user`/
   `rag_password` in use. Fixed in both files; confirmed no other instances
   via a full-repo grep.
3. **Windows IPv6 `localhost` resolution bug** — `host="localhost"` was
   resolving to `::1` and connecting to an unrelated process, not Docker's
   published port. Root-caused via a 4-step elimination (env var check →
   local socket → in-container TCP loopback → disposable container on the
   compose network) before touching any code, to avoid guessing. Fixed by
   using `127.0.0.1` explicitly instead of `localhost`.
4. **Real port conflict, not a code bug**: a native Windows `postgres.exe`
   service (confirmed via `tasklist`, PID identified) was already bound to
   `:5432`, silently intercepting connections meant for Docker's
   `com.docker.backend.exe` proxy (also confirmed via `tasklist`). Fixed by
   remapping the **host-side** port only (`docker-compose.yml`:
   `5433:5432`) — the container-internal port and the FastAPI service's
   internal `POSTGRES_PORT` (used for container-to-container traffic on the
   Docker network) were correctly left at `5432`, since that conflict never
   existed for them.
5. **`generate_eval_seed.py` classification undercounted real relationships**
   — first version took only `[0]` of the duplicate/related examples and
   `LIMIT 1` on error-code groups, plus 2 unfilled `"TEMPLATE: ..."` string
   literals that were never replaced with real queries. Rewritten to loop
   over all reference rows and all multi-doc error-code groups, and to
   query real standalone documents (no cross-references) for the two
   single-doc-factual questions instead of leaving placeholders.
6. **`RELATED_KEYWORDS` field-coverage bug**: `BUG-1024`'s duplicate
   language ("Same as BUG-1001.") was in its `steps_to_reproduce` field,
   which the classifier wasn't scanning (only `title` + `description`).
   Silently misclassified as `related_to` instead of `duplicate_of` until
   caught by comparing generator output against manually-verified expected
   counts. Fixed by including `steps_to_reproduce` in the scanned text.

---

## 7. What Was Verified (real commands, real output — not assumed)

- `schema.sql` applied to live Postgres; confirmed via `\dt` showing all six
  tables.
- `python ingest.py` completed successfully; row counts confirmed via direct
  SQL: `documents` grouped by `doc_type` (25 bug_report, 3 test_case),
  `document_references` grouped by `reference_type` (3/4/1 as predicted),
  the `TC-0209` dangling reference confirmed present with a `NULL` target,
  `error_codes` count confirmed at 4 (not 7, a number I initially got wrong
  by conflating "bugs mentioning a code" with "unique codes" — corrected
  once actually checked).
- `generate_eval_seed.py` rewrite was simulated in pure Python against the
  real CSV/markdown **before** being handed over, predicting the exact
  question set (12 questions, specific document IDs including which
  standalone documents would be picked). The live-DB run then matched that
  prediction field-for-field, confirmed by direct comparison of the actual
  output file against the simulation — not just a matching count.

---

## 8. What Remains / Deferred (not blocking Phase 3)

- **Non-blocking fragility**: the `structured_filter` question's
  module/status pick (`Login`/`Open`) is tied with `Search`/`Open` at equal
  counts (4 each) in the real data. The SQL has no tiebreaker, so re-running
  the generator later is not guaranteed to reproduce the same question.
  Fine for now; add `, module` as a secondary `ORDER BY` if deterministic
  reproducibility of the eval set matters before Phase 4.
- **Deferred, not forgotten**: Ollama LLM-confirm step for duplicate
  detection remains unwired (`ollama_confirm_fn=None`). Not needed for this
  25-row corpus; revisit if/when the corpus grows past what explicit-mention
  regex reliably catches.
- **Unresolved discrepancy**: 3 test case files exist vs. 10 mentioned in
  the original brief. Needs a decision from the project owner, not a code
  fix.
- **Deliberately out of scope**: OpenSearch indexing, embeddings, chunking —
  all correctly Phase 3/4 territory per the original brief.

---

## 9. Conflicts With Phase 1 / Original Brief

- **None structural.** The Phase 1 `psycopg2-binary` (sync) decision was
  carried forward without revisiting, as planned. `app/config.py`'s
  Postgres host defaults (`postgres` hostname, used inside the Docker
  network by the FastAPI container) are correctly different from
  `ingest.py`/`generate_eval_seed.py`'s host defaults (`127.0.0.1` on port
  `5433`, used from the Windows host running these as standalone scripts,
  not containerized). Two different connection conventions living in the
  same repo for a real reason — worth a short README note for a future
  reader, not a bug.
- **The 10-vs-3 test case count** is the one genuine open discrepancy
  between the original brief and what actually exists — flagged above,
  carried forward rather than silently resolved either way.

---

## 10. Files Phase 3 Should Inspect First

1. `schema.sql` — source of truth for what's actually queryable in
   OpenSearch once indexing is added (Phase 3's core task).
2. `eval/eval_seed.json` — the 12 verified questions; Phase 3's BM25 search
   should be checked against at least the `error_code_aggregation` and
   `structured_filter` questions, since those test exact-match/filtered
   retrieval specifically, which is BM25's strength over embeddings.
3. `ingest.py` — confirms exact field names and cleaning behavior feeding
   whatever gets indexed into OpenSearch.
4. This document — where anything here conflicts with the live repo, the
   repo is correct; this is a summary, not a replacement for reading the
   files directly.