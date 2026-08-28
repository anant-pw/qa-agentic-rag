"""
Postgres -> OpenSearch indexing.

Strategy: FULL REBUILD + ALIAS SWAP, not incremental upsert. Why:

- `documents` has no `updated_at` column (confirmed against schema.sql --
  only `ingested_at`, which is a creation timestamp, not a last-modified
  one). There is no cheap way to ask Postgres "what changed since the
  last index run" without adding a column and a trigger, which is out
  of scope for a 28-row corpus.
- This matches the architectural principle already established in
  Phase 1: OpenSearch is a DERIVED, REBUILDABLE index, Postgres is the
  transactional source of truth. A full rebuild is the correct
  operational posture for a derived index at this scale, not a
  shortcut -- doing incremental sync on a store defined as "always
  reconstructable from source of truth" is solving a problem this
  architecture doesn't have yet.
- Alias indirection (`qa_documents` -> `qa_documents_v<timestamp>`)
  means the rebuild is atomic from a reader's point of view: queries
  against the alias never see a half-populated index, and a bad
  rebuild can be rolled back by pointing the alias at the previous
  concrete index, which we keep around for exactly that reason.

Deferred, explicitly, not forgotten: true incremental reindexing
(requires an `updated_at` column + a WHERE-changed-since query) and
delete propagation (requires either a soft-delete flag on `documents`
or a diff between Postgres IDs and OpenSearch IDs on each run). Neither
exists yet. Full rebuild sidesteps both by construction -- revisit if
the corpus grows past "rebuild in a few seconds" territory.

--- Phase 4 addition ---

Every document currently ingested is short enough (30-165 words,
measured directly against the real CSV/markdown -- see Phase 4
readiness report) to be exactly one chunk. `build_os_document()` below
therefore emits ONE OpenSearch document per Postgres row, same as
Phase 3, but now carries a `chunk_id` / `parent_document_id` pair and a
`chunk_vector` embedding, so the schema shape doesn't have to change
the day a longer document (e.g. a future SRS/spec doc) needs real
sub-document splitting -- at that point, this function is where a
`for chunk in split(text): yield {...}` loop would go, one Postgres row
producing multiple OpenSearch documents instead of one.

Embedding input is built from the SUBTYPE-specific structured fields
(description/steps_to_reproduce for bug reports; preconditions/steps/
expected_result for test cases), not the flattened `cleaned_text` blob
-- see embeddings.py and the Phase 4 readiness report for why field
order matters to the embedding model. This requires joining
bug_reports/test_cases into the fetch query below, which Phase 3 never
needed since BM25 only ever read `cleaned_text`.
"""

import re
import time

from opensearchpy import OpenSearch, helpers

from app.search.mapping import INDEX_BODY
from app.search.embeddings import embed_document

# Same patterns ingest.py uses for TC_ID_RE / BUG_ID_RE. Duplicated here
# rather than imported because indexer.py and ingest.py are meant to be
# runnable independently (indexer.py only needs Postgres + OpenSearch
# creds, not the CSV/markdown parsing dependencies). If these regexes
# drift from ingest.py's, that's a real bug to catch in code review --
# flagging the duplication explicitly rather than hiding it.
TC_ID_RE = re.compile(r"\bTC-\d{4}\b")
BUG_ID_RE = re.compile(r"\bBUG-\d{4}\b")

FETCH_DOCUMENTS_SQL = """
    SELECT
        d.id,
        d.doc_type,
        d.external_id,
        d.title,
        d.module,
        d.status,
        d.date_created,
        d.cleaned_text,
        d.ingested_at,
        br.description        AS bug_description,
        br.steps_to_reproduce AS bug_steps_to_reproduce,
        tc.preconditions       AS tc_preconditions,
        tc.steps               AS tc_steps,
        tc.expected_result     AS tc_expected_result,
        COALESCE(
            array_agg(ec.code) FILTER (WHERE ec.code IS NOT NULL),
            '{}'
        ) AS error_codes
    FROM documents d
    LEFT JOIN document_error_codes dec ON dec.document_id = d.id
    LEFT JOIN error_codes ec ON ec.id = dec.error_code_id
    LEFT JOIN bug_reports br ON br.document_id = d.id
    LEFT JOIN test_cases tc ON tc.document_id = d.id
    GROUP BY d.id, br.description, br.steps_to_reproduce,
             tc.preconditions, tc.steps, tc.expected_result
    ORDER BY d.id;
"""


def fetch_documents_from_postgres(conn) -> list[dict]:
    """One row per document, error codes pre-aggregated via LEFT JOIN so
    documents with zero error codes still come back (as an empty array),
    not silently dropped by an INNER JOIN."""
    with conn.cursor() as cur:
        cur.execute(FETCH_DOCUMENTS_SQL)
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def build_embedding_input(row: dict) -> str:
    """Structured, per-doc-type text for the embedding model -- NOT
    `cleaned_text`. Field order/composition differs by doc_type because
    a flattened blob loses the semantic role each field plays (a
    bug's `steps_to_reproduce` and a test case's `steps` are different
    things that happen to share a column name)."""
    if row["doc_type"] == "bug_report":
        parts = [
            row.get("title") or "",
            row.get("bug_description") or "",
            row.get("bug_steps_to_reproduce") or "",
        ]
    else:  # test_case
        parts = [
            row.get("title") or "",
            row.get("tc_preconditions") or "",
            row.get("tc_steps") or "",
            row.get("tc_expected_result") or "",
        ]
    return "\n".join(p for p in parts if p)


def build_os_document(
    row: dict,
    ollama_host: str | None = None,
    ollama_port: int | None = None,
    chunk_index: int = 0,
) -> dict:
    """Postgres row -> OpenSearch document body.

    Exact-match ID fields are extracted here, not trusted to the
    analyzer -- see mapping.py's module docstring for why. Extraction
    scans `cleaned_text`, which already contains title + body for bug
    reports and the full markdown for test cases, so a self-reference
    (a document mentioning its own ID in its title) is included. That's
    harmless for filtering/boosting purposes and not worth excluding.

    Phase 4: if `ollama_host`/`ollama_port` are provided, also computes
    `chunk_vector` via embed_document() and sets `chunk_id` /
    `parent_document_id`. If omitted, chunk_vector is left unset --
    lets build_os_document() still be called (e.g. in tests) without a
    live Ollama, at the cost of that document being BM25-only in the
    resulting index. rebuild_index() always passes them in real use.
    """
    text = row.get("cleaned_text") or ""
    date_created = row.get("date_created")
    doc = {
        "document_id": row["id"],
        "doc_type": row["doc_type"],
        "external_id": row["external_id"],
        "title": row["title"],
        "module": row.get("module"),
        "status": row.get("status"),
        "date_created": date_created.isoformat() if date_created else None,
        "cleaned_text": text,
        "error_codes": row.get("error_codes") or [],
        "mentioned_bug_ids": sorted(set(BUG_ID_RE.findall(text))),
        "mentioned_tc_ids": sorted(set(TC_ID_RE.findall(text))),
        "ingested_at": row["ingested_at"].isoformat() if row.get("ingested_at") else None,
        "chunk_id": f"{row['external_id']}::chunk_{chunk_index:03d}",
        "parent_document_id": row["id"],
    }
    if ollama_host and ollama_port:
        embedding_input = build_embedding_input(row)
        doc["chunk_vector"] = embed_document(embedding_input, ollama_host, ollama_port)
    return doc


def rebuild_index(
    pg_conn,
    os_client: OpenSearch,
    alias: str = "qa_documents",
    keep_previous: int = 1,
    ollama_host: str | None = None,
    ollama_port: int | None = None,
) -> dict:
    """Full rebuild into a new versioned index, verify count, then
    atomically flip the alias. Returns a summary dict for logging/tests.

    Raises RuntimeError (does NOT swap the alias) if the OpenSearch doc
    count after indexing doesn't match the Postgres row count -- a
    partial index should never become the live one. Same all-or-nothing
    posture applies to embedding failures (Phase 4): if `ollama_host`/
    `ollama_port` are given and any document's embedding call fails
    (EmbeddingError, propagated as a bulk-index error below since it's
    raised while building the action generator), the whole rebuild
    aborts and the new index is deleted -- a hybrid index missing
    vectors on some documents would silently degrade to BM25-only for
    those documents with no visible signal that anything was wrong,
    which is worse than a loud failure here.
    """
    rows = fetch_documents_from_postgres(pg_conn)
    pg_count = len(rows)

    new_index = f"{alias}_v{int(time.time())}"
    os_client.indices.create(index=new_index, body=INDEX_BODY)

    def _actions():
        for row in rows:
            doc = build_os_document(row, ollama_host=ollama_host, ollama_port=ollama_port)
            yield {"_index": new_index, "_id": doc["chunk_id"], "_source": doc}

    try:
        success, errors = helpers.bulk(os_client, _actions(), raise_on_error=False)
    except Exception as e:
        # Embedding failures (EmbeddingError) surface here, mid-generator,
        # before opensearchpy gets a chance to collect them as a normal
        # per-doc bulk error -- treat identically to a bulk-index error:
        # abort, clean up the partial index, don't touch the alias.
        os_client.indices.delete(index=new_index, ignore=[404])
        raise RuntimeError(f"Rebuild aborted during embedding/indexing: {e}") from e

    if errors:
        os_client.indices.delete(index=new_index)
        raise RuntimeError(f"Bulk indexing had {len(errors)} error(s): {errors[:3]}")

    os_client.indices.refresh(index=new_index)
    os_count = os_client.count(index=new_index)["count"]
    if os_count != pg_count:
        os_client.indices.delete(index=new_index)
        raise RuntimeError(
            f"Doc count mismatch after indexing: Postgres={pg_count}, "
            f"OpenSearch={os_count}. Alias NOT swapped; new index deleted."
        )

    # Atomic alias swap: find whatever the alias currently points at
    # (may be nothing, on first run), then remove+add in one call so
    # readers never see the alias unresolved.
    old_indices = []
    if os_client.indices.exists_alias(name=alias):
        old_indices = list(os_client.indices.get_alias(name=alias).keys())

    actions = [{"add": {"index": new_index, "alias": alias}}]
    for old in old_indices:
        actions.append({"remove": {"index": old, "alias": alias}})
    os_client.indices.update_aliases(body={"actions": actions})

    # Rollback safety net: keep the most recent `keep_previous` old
    # indices, delete the rest.
    for old in old_indices[: max(0, len(old_indices) - keep_previous)]:
        os_client.indices.delete(index=old)

    return {
        "new_index": new_index,
        "doc_count": os_count,
        "alias": alias,
        "retired_indices": old_indices,
    }
