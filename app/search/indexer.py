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
"""

import re
import time
from typing import Optional

from opensearchpy import OpenSearch, helpers

from app.search.mapping import INDEX_BODY

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
        COALESCE(
            array_agg(ec.code) FILTER (WHERE ec.code IS NOT NULL),
            '{}'
        ) AS error_codes
    FROM documents d
    LEFT JOIN document_error_codes dec ON dec.document_id = d.id
    LEFT JOIN error_codes ec ON ec.id = dec.error_code_id
    GROUP BY d.id
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


def build_os_document(row: dict) -> dict:
    """Postgres row -> OpenSearch document body.

    Exact-match ID fields are extracted here, not trusted to the
    analyzer -- see mapping.py's module docstring for why. Extraction
    scans `cleaned_text`, which already contains title + body for bug
    reports and the full markdown for test cases, so a self-reference
    (a document mentioning its own ID in its title) is included. That's
    harmless for filtering/boosting purposes and not worth excluding.
    """
    text = row.get("cleaned_text") or ""
    date_created = row.get("date_created")
    return {
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
    }


def rebuild_index(
    pg_conn,
    os_client: OpenSearch,
    alias: str = "qa_documents",
    keep_previous: int = 1,
) -> dict:
    """Full rebuild into a new versioned index, verify count, then
    atomically flip the alias. Returns a summary dict for logging/tests.

    Raises RuntimeError (does NOT swap the alias) if the OpenSearch doc
    count after indexing doesn't match the Postgres row count -- a
    partial index should never become the live one.
    """
    rows = fetch_documents_from_postgres(pg_conn)
    pg_count = len(rows)

    new_index = f"{alias}_v{int(time.time())}"
    os_client.indices.create(index=new_index, body=INDEX_BODY)

    actions = (
        {"_index": new_index, "_id": row["id"], "_source": build_os_document(row)}
        for row in rows
    )
    success, errors = helpers.bulk(os_client, actions, raise_on_error=False)
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
