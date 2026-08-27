"""
Generates the fixed eval seed set for Phase 3/4 by querying the
document_references table populated during ingestion -- questions
are grounded in what was actually resolved, not guessed at.

Emits a mix of:
  - single-document lookups (module/status/error-code filters)
  - a bug -> cited test case question (test_case_citation edges)
  - a bug -> duplicate/related sibling question (duplicate_of edges)
  - an error-code cross-cutting question

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
    if mod_status:
        module, status, cnt = mod_status
        status_label = status or "unspecified-status"
        seed.append({
            "question": f"How many bugs are currently {status_label} in the {module} module?",
            "requires_docs": "filter:module+status",
            "type": "structured_filter",
            "notes": "Tests metadata-filtered retrieval rather than semantic search.",
        })

    standalone_bug = fetch_standalone_bug(conn)
    if standalone_bug:
        ext_id, title, steps = standalone_bug
        seed.append({
            "question": f"What are the steps to reproduce {ext_id} ({title})?",
            "requires_docs": [ext_id],
            "type": "single_doc_factual",
            "notes": "Deliberately picked a bug report with no cross-references, to isolate plain single-document retrieval from relational retrieval.",
        })

    standalone_tc = fetch_standalone_test_case(conn)
    if standalone_tc:
        ext_id, title, preconditions = standalone_tc
        seed.append({
            "question": f"What are the preconditions for {ext_id} ({title})?",
            "requires_docs": [ext_id],
            "type": "single_doc_factual",
            "notes": "Picked a test case not explicitly cited by any bug report ID -- worth separately checking whether the system links it to a thematically similar bug anyway, since that would be inference beyond what the reference graph supports.",
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
