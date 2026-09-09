"""
Structured-field and cross-reference lookup for Phase 5 generation.

Why this exists at all: OpenSearch's `_source` (see app/search/indexer.py's
build_os_document()) only ever stores the flattened `cleaned_text` blob for
free-text/BM25 matching. The subtype-specific fields -- `description` /
`steps_to_reproduce` for bug reports, `preconditions` / `steps` /
`expected_result` for test cases -- are read from Postgres at index-build
time ONLY to construct the embedding input (build_embedding_input()) and are
never written into the OpenSearch document itself. Confirmed by reading
indexer.py directly, not assumed.

That means hybrid_search()'s results give you enough to retrieve and cite a
document, but not enough to show the LLM doc_type-aware structure -- for
that, Phase 5 needs a second, small Postgres lookup keyed on
`parent_document_id`. Deliberately one query for the whole retrieved batch
(WHERE d.id = ANY(...)), not one query per document -- at 5 documents this
barely matters, but there's no reason to write it the slow way.

--- Why fetch_references() was added after initial Phase 5 verification ---

Bug-to-bug relationships (duplicate_of vs related_to) are recorded as
RESOLVED, DETERMINISTIC classifications in document_references -- Phase 2's
resolve_explicit_bug_mentions() already ran a keyword check against each
bug's free text and wrote the answer. But the free text that motivated that
classification is often genuinely ambiguous on its own ("Might be same root
cause as BUG-1001, filed separately" -- a human reading only this sentence
could defend calling it either "related" or "duplicate"). The original
version of context.py only fetched structured fields, not the reference
graph -- which meant the LLM was being asked to re-derive, from ambiguous
prose, a classification the ingestion pipeline had already resolved
deterministically. Confirmed as a real failure mode during Phase 5
verification: the same BUG-1002 duplicate-classification question, asked
four times, produced four different answers, because the model had nothing
but hedged natural language to go on. fetch_references() closes that gap by
handing the model the resolved reference_type directly, so Rule 3 in
prompt.py can tell it to report a recorded relationship verbatim instead of
re-inferring one.

This does NOT touch schema.sql, ingest.py, or app/search/indexer.py -- both
functions below are new, additive read paths against tables that already
exist.
"""

STRUCTURED_FIELDS_SQL = """
    SELECT
        d.id,
        br.description         AS description,
        br.steps_to_reproduce  AS steps_to_reproduce,
        tc.preconditions       AS preconditions,
        tc.steps               AS steps,
        tc.expected_result     AS expected_result
    FROM documents d
    LEFT JOIN bug_reports br ON br.document_id = d.id
    LEFT JOIN test_cases  tc ON tc.document_id  = d.id
    WHERE d.id = ANY(%s)
"""

REFERENCES_SQL = """
    SELECT
        r.source_document_id,
        r.target_document_id,
        ds.external_id AS source_external_id,
        dt.external_id AS target_external_id,
        r.target_external_id AS target_external_id_raw,
        r.reference_type
    FROM document_references r
    JOIN documents ds ON ds.id = r.source_document_id
    LEFT JOIN documents dt ON dt.id = r.target_document_id
    WHERE r.source_document_id = ANY(%s) OR r.target_document_id = ANY(%s)
"""


def fetch_structured_fields(conn, parent_document_ids: list[int]) -> dict[int, dict]:
    """Returns {document_id: {field: value, ...}} for the given IDs.
    A document_id with no matching bug_reports/test_cases row (shouldn't
    happen given the schema's class-table-inheritance FK constraints, but
    not assumed) simply won't be a key in the returned dict -- callers
    should use .get(id, {}) and treat missing fields as absent, not error."""
    if not parent_document_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(STRUCTURED_FIELDS_SQL, (parent_document_ids,))
        cols = [c.name for c in cur.description]
        return {row[0]: dict(zip(cols, row)) for row in cur.fetchall()}


REFERENCE_TYPE_PHRASING = {
    # reference_type values already contain "_of"/"_to" (see schema.sql's
    # CHECK constraint) -- naively concatenating them into "is recorded as
    # duplicate_of of BUG-1001" produces a doubled "of". Mapped to clean
    # verb phrasing instead. related_to's phrasing explicitly repeats
    # "not a duplicate of" inline -- redundant with Rule 3's wording in
    # prompt.py, but cheap insurance: the distinction is stated directly
    # in the fact the model is asked to report verbatim, not only in an
    # instruction the model has to remember to apply.
    "duplicate_of": "is a duplicate of",
    "related_to": "is related to (NOT a duplicate of)",
    "test_case_citation": "cites",
}


def fetch_references(conn, parent_document_ids: list[int]) -> dict[int, list[str]]:
    """Returns {document_id: [formatted reference line, ...]} for the given
    IDs, covering BOTH directions -- a retrieved document that is the
    SOURCE of a reference (e.g. BUG-1002 -> duplicate_of -> BUG-1001) and
    one that is the TARGET of a reference from some other document (e.g.
    TC-0142 being cited by BUG-1001) both get a line, phrased identically
    regardless of direction so the model doesn't have to work out
    directionality itself -- it only has to report the line.

    A dangling reference (target_document_id IS NULL -- e.g. BUG-1013 citing
    TC-0209, which was never ingested; see phase-02-handoff.md Section 4)
    is still surfaced, using the raw target_external_id text with a
    "(not found in corpus)" note -- this is a real, correct state
    (Phase 2 deliberately records dangling citations rather than dropping
    them), not something to hide from the LLM.

    Only queries references touching the CURRENTLY RETRIEVED documents --
    does not expand the corpus by pulling in referenced-but-not-retrieved
    documents. If a recorded reference points at a document outside the
    retrieved set, the LLM sees that the reference exists (by external_id)
    but won't have that document's own content -- an honest reflection of
    what was actually retrieved, not a silent gap."""
    if not parent_document_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(REFERENCES_SQL, (parent_document_ids, parent_document_ids))
        rows = cur.fetchall()

    by_doc: dict[int, list[str]] = {doc_id: [] for doc_id in parent_document_ids}
    for source_id, target_id, source_ext, target_ext, target_ext_raw, ref_type in rows:
        target_label = target_ext or f"{target_ext_raw} (not found in corpus)"
        phrase = REFERENCE_TYPE_PHRASING.get(ref_type, f"has a {ref_type} relationship with")
        line = f"{source_ext} {phrase} {target_label}"
        if source_id in by_doc:
            by_doc[source_id].append(line)
        if target_id is not None and target_id in by_doc and target_id != source_id:
            by_doc[target_id].append(line)
    return by_doc
