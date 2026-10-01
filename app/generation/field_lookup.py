"""
Deterministic field lookup for /generate/agentic: "What are the steps to
reproduce BUG-1003?" answered straight from Postgres, zero LLM calls.

WHY (measured 2026-10-01, logs/generation.jsonl ollama_* fields): a single
semantic answer for exactly this question cost 101-493s on phi4:14b --
373s of it reading a 2,099-token prompt -- to reproduce a field that
already sits verbatim in bug_reports.steps_to_reproduce. Same reasoning as
deterministic.py's count shortcut: when the answer IS a stored value, the
LLM adds latency and a chance to paraphrase it wrong, nothing else.

SCOPE IS DELIBERATELY NARROW -- it only fires when ALL of these hold, and
every other question falls through to the existing semantic path unchanged:
  1. exactly ONE BUG-/TC- ID in the question, and it resolved + passed the
     caller's filters (the router's existing id_route == "resolved");
  2. the question asks for at least one field this module knows, AND every
     field it asks for belongs to that document's type ("expected result of
     the test case referenced in BUG-1001" asks a test-case field about a
     bug -> falls through: it needs the reference graph, not a lookup);
  3. no word signalling reasoning/relationships (why, duplicate, related,
     fix, compare, ...) -- those need the LLM + reference context;
  4. the question is short (<= MAX_WORDS) -- long questions tend to carry
     extra asks a field dump would silently ignore.
Words inside parentheses (conventionally a quoted title, as in the eval
seed) are ignored for 2. and 3.

Wrong-route cost is asymmetric on purpose: a missed match costs one normal
LLM answer (today's behaviour); a false match returns the wrong field.
So the patterns err toward missing. Extend them only when a real question
misses, same policy deterministic.py documents for its count patterns.

Gated by settings.deterministic_field_lookup.
"""

import re

MAX_WORDS = 30

# field -> (label, owning doc_type or None for both, regex over lowercased question)
_FIELDS = {
    "steps_to_reproduce": (
        "Steps to reproduce", "bug_report",
        r"\b(steps?|reproduce|reproduction|repro)\b",
    ),
    "description": ("Description", "bug_report", r"\b(description|describe)\b"),
    "preconditions": ("Preconditions", "test_case", r"\bpre-?conditions?\b"),
    "steps": ("Steps", "test_case", r"\bsteps?\b"),
    "expected_result": (
        "Expected result", "test_case",
        r"\bexpected (result|outcome|behaviou?r)s?\b",
    ),
    "status": ("Status", None, r"\bstatus\b"),
    "module": ("Module", None, r"\b(module|component)\b"),
}

_EXCLUDE = re.compile(
    r"\b(why|how come|cause|caus\w*|duplicat\w*|relat\w*|similar|compar\w*|"
    r"differ\w*|fix\w*|resol\w*|workaround|referenc\w*|cit\w*|link\w*|"
    r"connected|summar\w*|explain\w*|should|recommend\w*|suggest\w*|"
    r"improv\w*|impact|affect\w*)\b"
)

FIELD_SQL = """
    SELECT d.external_id, d.title, d.doc_type, d.module, d.status,
           br.description, br.steps_to_reproduce,
           tc.preconditions, tc.steps, tc.expected_result
    FROM documents d
    LEFT JOIN bug_reports br ON br.document_id = d.id
    LEFT JOIN test_cases  tc ON tc.document_id  = d.id
    WHERE d.id = %s
"""


def requested_fields(question: str, doc_type: str) -> list[str] | None:
    """Fields to return, in _FIELDS order, or None if this question must go
    to the LLM. doc_type is the resolved document's type."""
    q = (question or "").lower()
    if len(q.split()) > MAX_WORDS:
        return None
    # Match intent against the question WITHOUT parenthetical text: titles
    # are conventionally quoted in parentheses ("BUG-1003 (Checkout page
    # times out on slow connections)") and their words are not the ask.
    q = re.sub(r"\([^)]*\)", " ", q)
    if _EXCLUDE.search(q):
        return None
    fields = []
    for name, (_, owner, pattern) in _FIELDS.items():
        if not re.search(pattern, q):
            continue
        if owner is not None and owner != doc_type:
            # "steps" matches both types' patterns; only the other type's
            # EXCLUSIVE words (preconditions, expected result, reproduce,
            # description) should force a fall-through.
            if name == "steps" or (name == "steps_to_reproduce" and not re.search(r"\b(reproduce|reproduction|repro)\b", q)):
                continue
            return None
        fields.append(name)
    return fields or None


def answer_field_question(conn, parent_document_id: int, fields: list[str]) -> tuple[str, dict] | None:
    """Returns (answer_text, row) or None if the document row is missing
    (index/DB drift -- let the LLM path handle it rather than guess)."""
    with conn.cursor() as cur:
        cur.execute(FIELD_SQL, (parent_document_id,))
        row = cur.fetchone()
        if row is None:
            return None
        rec = dict(zip([c.name for c in cur.description], row))

    lines = [f"{rec['external_id']} ({rec['title']})"]
    for name in fields:
        label = _FIELDS[name][0]
        value = rec.get(name)
        lines.append(f"{label}: {value.strip() if isinstance(value, str) and value.strip() else '(none recorded)'}")
    lines.append(f"(Source: {rec['external_id']})")
    return "\n".join(lines), rec
