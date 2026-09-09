"""
Generates the fixed eval seed set for Phase 3/4 (retrieval) and Phase 6
(generation quality) by querying document_references / documents directly
-- questions are grounded in what was actually resolved or actually
absent, not guessed at.

Emits a mix of:
  - single-document lookups (module/status/error-code filters)
  - a bug -> cited test case question (test_case_citation edges)
  - a bug -> duplicate/related sibling question (duplicate_of edges)
  - an error-code cross-cutting question
  - a dangling test-case-citation question (Phase 6 addition -- see
    fetch_dangling_citation() below)
  - a resolution-in-narrative question (Phase 6 addition -- see
    fetch_resolved_narrative_bug() below)
  - a second, tie-free structured_filter question (Phase 6 addition)

Phase 6 additions exist because phase-05-handoff.md Section 10 flagged
BUG-1013's dangling citation of the never-ingested TC-0209 as designed
but never exercised end-to-end through /generate, and because BUG-1017
(status=Resolved, fix narrative embedded in its `description` field --
confirmed by reading data/bug_reports.csv directly, not assumed) is a
real case the original Phase 5 system prompt's Rule 2 ("say so if no
resolution is documented") was never tested against -- Rule 2's premise
was "no bug has a resolution field," which is true of the schema but not
true of every bug's free text.

Run after ingest.py. Writes eval_seed.json.
"""

import json
import os
import psycopg2

QUERY_TEMPLATES = {
    "test_case_citation": (
        "What is the expected result of the test case referenced in "
        "bug report {source_id} ({source_title})?"
    ),
    "duplicate_of": (
        "Is bug report {source_id} ({source_title}) a duplicate of another "
        "open ticket, and if so which one and what module does it affect?"
    ),
    "related_to": (
        "Is bug report {source_id} ({source_title}) connected to another "
        "ticket -- and if so, is it a duplicate or something else, like a "
        "follow-up or resolution?"
    ),
    "dangling_citation": (
        "What is the expected result of the test case referenced in "
        "bug report {source_id} ({source_title})?"
    ),
    "resolution_in_narrative": (
        "What was the resolution or fix for {source_id} ({source_title})?"
    ),
}


def fetch_reference_examples(conn, reference_type, limit=10):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d1.external_id, d1.title, d2.external_id, d2.title, r.confidence
            FROM document_references r
            JOIN documents d1 ON d1.id = r.source_document_id
            LEFT JOIN documents d2 ON d2.id = r.target_document_id
            WHERE r.reference_type = %s AND r.target_document_id IS NOT NULL
            ORDER BY d1.external_id, d2.external_id
            LIMIT %s
            """,
            (reference_type, limit),
        )
        return cur.fetchall()


def fetch_error_code_groups(conn):
    """All error codes shared by 2+ documents, not just the top one --
    each is a genuine multi-doc aggregation question, not a single sample."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ec.code, array_agg(d.external_id ORDER BY d.external_id)
            FROM document_error_codes dec
            JOIN error_codes ec ON ec.id = dec.error_code_id
            JOIN documents d ON d.id = dec.document_id
            GROUP BY ec.code
            HAVING count(*) > 1
            ORDER BY ec.code
            """
        )
        return cur.fetchall()


def fetch_standalone_bug(conn):
    """A bug report with no outgoing or incoming document_references --
    deliberately picks a document with zero relational complexity, so this
    question tests plain single-doc retrieval and nothing else."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.external_id, d.title, br.steps_to_reproduce
            FROM documents d
            JOIN bug_reports br ON br.document_id = d.id
            WHERE d.doc_type = 'bug_report'
              AND d.id NOT IN (SELECT source_document_id FROM document_references)
              AND d.id NOT IN (
                  SELECT target_document_id FROM document_references
                  WHERE target_document_id IS NOT NULL
              )
            ORDER BY d.external_id
            LIMIT 1
            """
        )
        return cur.fetchone()


def fetch_standalone_bug_second(conn, exclude_external_id):
    """A second standalone bug report (no in/out document_references),
    distinct from whatever fetch_standalone_bug() already picked --
    real corpus: with BUG-1003 excluded, this lands on BUG-1004 by the
    same ORDER BY external_id convention fetch_standalone_bug() uses, but
    the Phase 6 readiness report specifically identified BUG-1023 (a
    HIGH-severity double-charge report) as a more useful second sample --
    its own text uses editorializing language ("HIGH severity -- real
    money impact") that a grounded system should report as-is without
    escalating it further, a different stress case than a plain
    low-drama standalone bug would be. Picked explicitly by external_id
    rather than by convention for that reason -- not a purely mechanical
    second row."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.external_id, d.title, br.steps_to_reproduce
            FROM documents d
            JOIN bug_reports br ON br.document_id = d.id
            WHERE d.doc_type = 'bug_report'
              AND d.external_id != %s
              AND d.id NOT IN (SELECT source_document_id FROM document_references)
              AND d.id NOT IN (
                  SELECT target_document_id FROM document_references
                  WHERE target_document_id IS NOT NULL
              )
            ORDER BY (d.external_id != 'BUG-1023'), d.external_id
            LIMIT 1
            """,
            (exclude_external_id,),
        )
        return cur.fetchone()


def fetch_standalone_test_case(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.external_id, d.title, tc.preconditions
            FROM documents d
            JOIN test_cases tc ON tc.document_id = d.id
            WHERE d.doc_type = 'test_case'
              AND d.id NOT IN (
                  SELECT target_document_id FROM document_references
                  WHERE target_document_id IS NOT NULL
              )
            ORDER BY d.external_id
            LIMIT 1
            """
        )
        return cur.fetchone()


def fetch_standalone_test_case_second(conn, exclude_external_id):
    """The second standalone test case, if one exists -- real corpus has
    exactly two (TC-0301, TC-0302); with TC-0301 excluded (already used
    by fetch_standalone_test_case()), this returns TC-0302. Returns None
    if there isn't a second one, which callers must handle -- not every
    corpus state guarantees two standalone test cases exist."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.external_id, d.title, tc.preconditions
            FROM documents d
            JOIN test_cases tc ON tc.document_id = d.id
            WHERE d.doc_type = 'test_case'
              AND d.external_id != %s
              AND d.id NOT IN (
                  SELECT target_document_id FROM document_references
                  WHERE target_document_id IS NOT NULL
              )
            ORDER BY d.external_id
            LIMIT 1
            """,
            (exclude_external_id,),
        )
        return cur.fetchone()


def fetch_module_status_example(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT module, status, count(*)
            FROM documents WHERE doc_type = 'bug_report' AND module IS NOT NULL
            GROUP BY module, status ORDER BY count(*) DESC LIMIT 1
            """
        )
        return cur.fetchone()


def fetch_module_status_second_example(conn, exclude_module):
    """A SECOND structured_filter question, deliberately chosen to NOT be
    tied for first place -- phase-02-handoff.md Section 8 flagged that the
    original single structured_filter question (Login/Open vs Search/Open,
    tied at 4 each) has no ORDER BY tiebreaker and isn't guaranteed to
    reproduce on regeneration. Rather than add a tiebreaker to the first
    query (which would silently change which of the two tied questions
    gets asked, invalidating any eval history already recorded against the
    old one), this adds a second, independent question picked from
    whatever module/status pair is NOT tied for the top spot -- confirmed
    against the real corpus this lands on Payments/Open=3, unambiguous."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT module, status, count(*) AS cnt
            FROM documents
            WHERE doc_type = 'bug_report' AND module IS NOT NULL AND module != %s
            GROUP BY module, status
            ORDER BY cnt DESC, module ASC, status ASC
            LIMIT 1
            """,
            (exclude_module,),
        )
        return cur.fetchone()


def fetch_dangling_citation(conn):
    """A bug report that cites a test case NEVER ingested -- Phase 2
    deliberately records this as a document_references row with
    target_document_id IS NULL rather than dropping it (schema.sql's
    comment on document_references explains why). Real corpus: BUG-1013
    cites TC-0209. This question type exists specifically to check that
    /generate correctly reports "cited but not found in the corpus"
    rather than either (a) silently omitting the citation or (b)
    fabricating TC-0209's contents -- neither of which the existing
    cross_reference_bug_to_test_case questions can catch, since both of
    those resolve to a real, ingested test case."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ds.external_id, ds.title, r.target_external_id
            FROM document_references r
            JOIN documents ds ON ds.id = r.source_document_id
            WHERE r.reference_type = 'test_case_citation'
              AND r.target_document_id IS NULL
            ORDER BY ds.external_id
            LIMIT 1
            """
        )
        return cur.fetchone()


def fetch_resolved_narrative_bug(conn):
    """A bug report whose FIX is described in prose inside `description`
    (not a dedicated resolution column -- schema.sql has none), status
    typically 'Resolved'. Real corpus: BUG-1017 ("Fix deployed using
    cursor-based pagination... Verified with 12000-row export"). Confirmed
    by direct text search, not a keyword guess dressed up as a query --
    this looks for status='Resolved' bugs, which is exactly what makes
    this deterministic rather than a hand-picked example. Exists to test
    the model does NOT emit Rule 2's abstention sentence for a bug that
    genuinely does have documented fix information, just not in a
    separate field."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.external_id, d.title, br.description
            FROM documents d
            JOIN bug_reports br ON br.document_id = d.id
            WHERE d.doc_type = 'bug_report' AND d.status = 'Resolved'
            ORDER BY d.external_id
            LIMIT 1
            """
        )
        return cur.fetchone()


def build_eval_seed(conn):
    seed = []

    for src_id, src_title, tgt_id, tgt_title, conf in fetch_reference_examples(conn, "test_case_citation"):
        seed.append({
            "question": QUERY_TEMPLATES["test_case_citation"].format(source_id=src_id, source_title=src_title),
            "requires_docs": [src_id, tgt_id],
            "type": "cross_reference_bug_to_test_case",
            "notes": "Fails if system answers from the bug report alone without pulling the cited test case's expected_result.",
        })

    for src_id, src_title, tgt_id, tgt_title, conf in fetch_reference_examples(conn, "duplicate_of"):
        seed.append({
            "question": QUERY_TEMPLATES["duplicate_of"].format(source_id=src_id, source_title=src_title),
            "requires_docs": [src_id, tgt_id],
            "type": "duplicate_resolution",
            "notes": "Fails if system treats the two reports as unrelated and only returns one; correct answer names both and the shared module.",
        })

    for src_id, src_title, tgt_id, tgt_title, conf in fetch_reference_examples(conn, "related_to"):
        seed.append({
            "question": QUERY_TEMPLATES["related_to"].format(source_id=src_id, source_title=src_title),
            "requires_docs": [src_id, tgt_id],
            "type": "related_resolution",
            "notes": "Distinguishes 'related_to' from 'duplicate_of' -- correct answer should NOT call this a duplicate if it's a follow-up/resolution ticket instead.",
        })

    for code, doc_ids in fetch_error_code_groups(conn):
        seed.append({
            "question": f"Which bug reports are associated with {code}, and do any of them look related to each other?",
            "requires_docs": list(doc_ids),
            "type": "error_code_aggregation",
            "notes": "Multi-document retrieval keyed on a normalized field, not free-text similarity. Same error code does not necessarily mean same root cause.",
        })

    mod_status = fetch_module_status_example(conn)
    first_module = None
    if mod_status:
        module, status, cnt = mod_status
        first_module = module
        status_label = status or "unspecified-status"
        seed.append({
            "question": f"How many bugs are currently {status_label} in the {module} module?",
            "requires_docs": "filter:module+status",
            "type": "structured_filter",
            "notes": "Tests metadata-filtered retrieval rather than semantic search. NOTE: this question is tied with at least one other module/status pair at equal count -- see phase-02-handoff.md Section 8. Not fixed here deliberately, so eval history against this exact question stays valid; see the second structured_filter question below for a tie-free case.",
        })

    mod_status_2 = fetch_module_status_second_example(conn, exclude_module=first_module) if first_module else None
    if mod_status_2:
        module2, status2, cnt2 = mod_status_2
        status_label2 = status2 or "unspecified-status"
        seed.append({
            "question": f"How many bugs are currently {status_label2} in the {module2} module?",
            "requires_docs": "filter:module+status",
            "type": "structured_filter",
            "notes": "Phase 6 addition: a second structured_filter question chosen specifically to NOT be tied for its count, so it reproduces deterministically on regeneration even though the first structured_filter question above still isn't fixed.",
        })

    standalone_bug = fetch_standalone_bug(conn)
    first_standalone_id = None
    if standalone_bug:
        ext_id, title, steps = standalone_bug
        first_standalone_id = ext_id
        seed.append({
            "question": f"What are the steps to reproduce {ext_id} ({title})?",
            "requires_docs": [ext_id],
            "type": "single_doc_factual",
            "notes": "Deliberately picked a bug report with no cross-references, to isolate plain single-document retrieval from relational retrieval.",
        })

    standalone_bug_2 = fetch_standalone_bug_second(conn, exclude_external_id=first_standalone_id) if first_standalone_id else None
    if standalone_bug_2:
        ext_id2, title2, steps2 = standalone_bug_2
        seed.append({
            "question": f"What are the steps to reproduce {ext_id2} ({title2})?",
            "requires_docs": [ext_id2],
            "type": "single_doc_factual",
            "notes": "Phase 6 addition: a second standalone-bug single_doc_factual question, picked to include a report with editorializing severity language in its own text, testing whether the system reports content as-is without amplifying it.",
        })

    standalone_tc = fetch_standalone_test_case(conn)
    first_standalone_tc_id = None
    if standalone_tc:
        ext_id, title, preconditions = standalone_tc
        first_standalone_tc_id = ext_id
        seed.append({
            "question": f"What are the preconditions for {ext_id} ({title})?",
            "requires_docs": [ext_id],
            "type": "single_doc_factual",
            "notes": "Picked a test case not explicitly cited by any bug report ID -- worth separately checking whether the system links it to a thematically similar bug anyway, since that would be inference beyond what the reference graph supports.",
        })

    standalone_tc_2 = fetch_standalone_test_case_second(conn, exclude_external_id=first_standalone_tc_id) if first_standalone_tc_id else None
    if standalone_tc_2:
        ext_id2, title2, preconditions2 = standalone_tc_2
        seed.append({
            "question": f"What are the preconditions for {ext_id2} ({title2})?",
            "requires_docs": [ext_id2],
            "type": "single_doc_factual",
            "notes": "Phase 6 addition: the second (and, in the current corpus, last) standalone test case.",
        })

    dangling = fetch_dangling_citation(conn)
    if dangling:
        src_id, src_title, target_raw = dangling
        seed.append({
            "question": QUERY_TEMPLATES["dangling_citation"].format(source_id=src_id, source_title=src_title),
            "requires_docs": [src_id],
            "type": "dangling_reference_citation",
            "notes": f"Phase 6 addition. {src_id} cites {target_raw}, which was never ingested (document_references.target_document_id IS NULL). Correct answer states the citation exists but {target_raw} is not present in the corpus -- fails if the system either omits the citation entirely or fabricates {target_raw}'s contents. This is the specific case phase-05-handoff.md flagged as designed but never tested end-to-end through /generate.",
        })

    resolved_narrative = fetch_resolved_narrative_bug(conn)
    if resolved_narrative:
        rn_id, rn_title, rn_description = resolved_narrative
        seed.append({
            "question": QUERY_TEMPLATES["resolution_in_narrative"].format(source_id=rn_id, source_title=rn_title),
            "requires_docs": [rn_id],
            "type": "resolution_in_narrative",
            "notes": f"Phase 6 addition. {rn_id} has no dedicated resolution field (schema.sql has none), but its `description` contains the actual fix in prose. Fails if the system wrongly emits Rule 2's abstention sentence ('the retrieved documents do not state a resolution') for a bug that does document one, just not in a separate column.",
        })

    return seed


if __name__ == "__main__":
    conn = psycopg2.connect(
        dbname=os.environ.get("POSTGRES_DB", "rag_db"),
        user=os.environ.get("POSTGRES_USER", "rag_user"),
        password=os.environ.get("POSTGRES_PASSWORD", "rag_password"),
        host=os.environ.get("POSTGRES_HOST", "127.0.0.1"),  # see ingest.py: "localhost"
        # can resolve to ::1 on Windows and hit a different Postgres instance
        port=os.environ.get("POSTGRES_PORT", "5433"),
    )
    seed = build_eval_seed(conn)
    with open("eval/eval_seed.json", "w") as f:
        json.dump(seed, f, indent=2)
    print(f"Wrote {len(seed)} eval seed questions to eval/eval_seed.json")
