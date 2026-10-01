"""
Deterministic error-code aggregation for /generate/agentic: "Which bug
reports are associated with ERR_403_FORBIDDEN?" answered from the
error_codes / document_error_codes tables, zero LLM calls.

WHY (measured 2026-10-01, docs/PERF_RUN_2026-10-01.md): llama3.1:8b had all
five ERR_403_FORBIDDEN documents in its context and still listed only three;
phi4:14b listed all five but took 1,387s. The association is already a
normalized table written at ingest time (ingest.py's ERR_CODE_RE), so the
complete list is a lookup, not a generation task -- same reasoning as
deterministic.py's count shortcut.

"Do any of them look related to each other?" -- answered ONLY from recorded
document_references rows among the matched documents, phrased with the same
REFERENCE_TYPE_PHRASING the LLM context uses. Sharing an error code is not
evidence of a shared root cause (the eval seed's own note says so), so the
answer says that explicitly instead of guessing similarity from prose.

Trigger (narrow, same asymmetric-cost policy as field_lookup.py): exactly
one ERR_* code, a "which/list/show ... bugs/documents/tickets" ask, no
reasoning words (why, cause, fix, ...). Everything else -> existing path.

Gated by settings.deterministic_error_code_lookup.
"""

import re

from app.generation.context import REFERENCE_TYPE_PHRASING
from app.search.queries import EMBEDDED_ID_PATTERNS

# Reuse queries.py's ERR_ pattern rather than adding another copy.
_ERR_PATTERN = next(p for p in EMBEDDED_ID_PATTERNS if p.pattern.startswith(r"\bERR_"))

_AGGREGATION_ASK = re.compile(
    r"\b(which|what|list|show|find|all)\b.*\b(bugs?|bug reports?|tickets?|documents?|docs|"
    r"test cases?|issues?|reports?)\b"
)
_EXCLUDE = re.compile(
    r"\b(why|how come|caus\w*|fix\w*|resol\w*|workaround|explain\w*|summar\w*|"
    r"should|recommend\w*|suggest\w*|steps?|reproduc\w*|how many|count)\b"
)

DOCS_SQL = """
    SELECT d.id, d.external_id, d.title, d.doc_type, d.module, d.status
    FROM documents d
    JOIN document_error_codes de ON de.document_id = d.id
    JOIN error_codes e ON e.id = de.error_code_id
    WHERE upper(e.code) = %s
    ORDER BY d.external_id
"""

REFS_AMONG_SQL = """
    SELECT ds.external_id, r.reference_type, dt.external_id
    FROM document_references r
    JOIN documents ds ON ds.id = r.source_document_id
    JOIN documents dt ON dt.id = r.target_document_id
    WHERE r.source_document_id = ANY(%s) AND r.target_document_id = ANY(%s)
    ORDER BY ds.external_id, dt.external_id
"""


def error_code_question(question: str) -> str | None:
    """The single ERR_* code (uppercase) this question aggregates over, or
    None if it should go to the existing path."""
    q = (question or "")
    codes = {m.group(0).upper() for m in _ERR_PATTERN.finditer(q)}
    if len(codes) != 1:
        return None
    lowered = q.lower()
    if _EXCLUDE.search(lowered) or not _AGGREGATION_ASK.search(lowered):
        return None
    return codes.pop()


def _passes(row: dict, doc_type, module, status) -> bool:
    for want, have in ((doc_type, row["doc_type"]), (module, row["module"]), (status, row["status"])):
        if want and (have or "").lower() != want.lower():
            return False
    return True


def answer_error_code_question(conn, code: str, doc_type=None, module=None, status=None):
    """Returns (answer_text, sources). Respects the caller's explicit filters
    the same way the rest of the endpoint does."""
    with conn.cursor() as cur:
        cur.execute(DOCS_SQL, (code,))
        cols = [c.name for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    rows = [r for r in rows if _passes(r, doc_type, module, status)]

    if not rows:
        return f"No document in this corpus is associated with {code}.", []

    with conn.cursor() as cur:
        ids = [r["id"] for r in rows]
        cur.execute(REFS_AMONG_SQL, (ids, ids))
        refs = cur.fetchall()

    lines = [f"{len(rows)} document(s) are associated with {code}:"]
    for r in rows:
        lines.append(f"- {r['external_id']}: {r['title']} (module={r['module']}, status={r['status']})")

    if refs:
        lines.append("")
        lines.append("Recorded relationships among these documents:")
        for src, ref_type, tgt in refs:
            phrase = REFERENCE_TYPE_PHRASING.get(ref_type, f"has a {ref_type} relationship with")
            lines.append(f"- {src} {phrase} {tgt}")
        lines.append("")
        lines.append(
            "No other relationships are recorded between them; sharing an error "
            "code alone does not establish a common root cause."
        )
    else:
        lines.append("")
        lines.append(
            "No relationships are recorded between these documents; sharing an "
            "error code alone does not establish a common root cause."
        )
    lines.append(f"(Source: {', '.join(r['external_id'] for r in rows)})")

    sources = [
        {
            "external_id": r["external_id"],
            "title": r["title"],
            "doc_type": r["doc_type"],
            "rrf_score": None,
            "vector_score": None,
            "parent_document_id": r["id"],
        }
        for r in rows
    ]
    return "\n".join(lines), sources
