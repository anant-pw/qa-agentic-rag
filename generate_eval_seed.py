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
}


def fetch_reference_examples(conn, reference_type, limit=3):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT d1.external_id, d1.title, d2.external_id, d2.title, r.confidence
            FROM document_references r
            JOIN documents d1 ON d1.id = r.source_document_id
            LEFT JOIN documents d2 ON d2.id = r.target_document_id
            WHERE r.reference_type = %s AND r.target_document_id IS NOT NULL
            ORDER BY r.confidence DESC NULLS LAST
            LIMIT %s
            """,
            (reference_type, limit),
        )
        return cur.fetchall()


def fetch_error_code_example(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ec.code, array_agg(d.external_id ORDER BY d.external_id)
            FROM document_error_codes dec
            JOIN error_codes ec ON ec.id = dec.error_code_id
            JOIN documents d ON d.id = dec.document_id
            GROUP BY ec.code
            HAVING count(*) > 1
            ORDER BY count(*) DESC
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

    dup_examples = fetch_reference_examples(conn, "duplicate_of")
    if dup_examples:
        src_id, src_title, tgt_id, tgt_title, conf = dup_examples[0]
        seed.append({
            "question": QUERY_TEMPLATES["duplicate_of"].format(source_id=src_id, source_title=src_title),
            "requires_docs": [src_id, tgt_id],
            "type": "duplicate_resolution",
            "notes": "Fails if system treats the two reports as unrelated and only returns one; correct answer names both and the shared module.",
        })

    err = fetch_error_code_example(conn)
    if err:
        code, doc_ids = err
        seed.append({
            "question": f"Which bug reports are associated with {code}, and do any of them look related to each other?",
            "requires_docs": list(doc_ids),
            "type": "error_code_aggregation",
            "notes": "Multi-document retrieval keyed on a normalized field, not free-text similarity.",
        })

    mod_status = fetch_module_status_example(conn)
    if mod_status:
        module, status, cnt = mod_status
        seed.append({
            "question": f"How many {status or 'unspecified-status'} bugs are open in the {module} module?",
            "requires_docs": "filter:module+status",
            "type": "structured_filter",
            "notes": "Tests metadata-filtered retrieval rather than semantic search.",
        })

    # Remaining slots (single-doc factual, steps-to-reproduce lookup, test
    # case preconditions lookup, etc.) should be filled the same way --
    # query documents/test_cases directly for one concrete example each --
    # once this runs against your real data. Stubbing structure only:
    seed.append({
        "question": "TEMPLATE: What are the steps to reproduce <bug external_id>?",
        "requires_docs": "<single bug_report id>",
        "type": "single_doc_factual",
        "notes": "Fill from an actual row after running ingest.py.",
    })
    seed.append({
        "question": "TEMPLATE: What are the preconditions for <test case external_id>?",
        "requires_docs": "<single test_case id>",
        "type": "single_doc_factual",
        "notes": "Fill from an actual row after running ingest.py.",
    })

    return seed


if __name__ == "__main__":
    conn = psycopg2.connect(dbname="arxiv_curator", user="postgres",
                             password="postgres", host="localhost")
    seed = build_eval_seed(conn)
    with open("eval_seed.json", "w") as f:
        json.dump(seed, f, indent=2)
    print(f"Wrote {len(seed)} eval seed questions to eval_seed.json")
