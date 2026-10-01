import pytest

from app.generation.deterministic import is_countable_question
from app.generation.error_code_lookup import error_code_question
from app.generation.field_lookup import requested_fields
from app.generation.guardrail import is_in_domain
from app.search.id_resolution import (
    classify_id_question,
    extract_document_ids,
    passes_filters,
    pin_resolved_hits,
    unknown_id_message,
)


# --- guardrail ---------------------------------------------------------------

@pytest.mark.parametrize("score,has_filter,expected", [
    (None, False, False),   # nothing retrieved -> always out of domain
    (None, True, False),    # ...even with an explicit filter
    (0.80, False, True),
    (0.74, False, False),   # below the 0.75 calibration
    (0.60, True, True),     # explicit filter is trusted as the domain signal
])
def test_is_in_domain(score, has_filter, expected):
    assert is_in_domain(score, has_explicit_filter=has_filter) is expected


# --- count/list route --------------------------------------------------------

@pytest.mark.parametrize("question,expected", [
    ("How many bugs are currently Open in the Login module?", True),
    ("list all test cases", True),
    ("Which bugs are open in Payments?", True),
    ("How much does checkout cost?", False),   # "how much" removed as an off-topic bypass
    ("What are the steps to reproduce BUG-1003?", False),
])
def test_is_countable_question(question, expected):
    assert is_countable_question(question) is expected


# --- ID resolution -----------------------------------------------------------

def test_extract_document_ids_normalises_case_dashes_and_order():
    q = "Is bug‑1024 a duplicate of BUG-1001 or bug-1024? see TC-0142"
    assert extract_document_ids(q) == ["bug-1024", "bug-1001", "tc-0142"]


def test_extract_document_ids_ignores_error_codes():
    assert extract_document_ids("Which bugs mention ERR_401_UNAUTH?") == []


@pytest.mark.parametrize("ids,existing,pinnable,expected", [
    ([], {}, {}, "none"),
    (["bug-9999"], {}, {}, "unknown"),
    (["bug-1001"], {"bug-1001": {}}, {"bug-1001": {}}, "resolved"),
    (["bug-1001"], {"bug-1001": {}}, {}, "none"),  # exists but excluded by filters
])
def test_classify_id_question(ids, existing, pinnable, expected):
    assert classify_id_question(ids, existing, pinnable) == expected


def test_passes_filters_is_case_insensitive_and_ignores_unset():
    hit = {"doc_type": "bug_report", "module": "Login", "status": "Open"}
    assert passes_filters(hit, None, "login", "OPEN")
    assert not passes_filters(hit, None, "Payments", None)


def test_pin_resolved_hits_moves_named_docs_first_without_duplicates():
    hits = [{"external_id": f"BUG-{n}", "chunk_id": f"c{n}"} for n in (1, 2, 3)]
    pinnable = {"bug-3": {"external_id": "BUG-3", "chunk_id": "c3"}}
    assert [h["chunk_id"] for h in pin_resolved_hits(hits, pinnable, ["bug-3"])] == ["c3", "c1", "c2"]


def test_unknown_id_message_names_the_ids():
    assert "BUG-9999" in unknown_id_message(["bug-9999"])


# --- field-lookup route ------------------------------------------------------

@pytest.mark.parametrize("question,doc_type,expected", [
    ("What are the steps to reproduce BUG-1003 (Checkout page times out on slow connections)?",
     "bug_report", ["steps_to_reproduce"]),
    ("where is the bug steps in bug-1011", "bug_report", ["steps_to_reproduce"]),
    ("What are the preconditions and expected result for TC-0301?", "test_case",
     ["preconditions", "expected_result"]),
    ("What are the steps for TC-0301?", "test_case", ["steps"]),
    ("What is the status of BUG-1003?", "bug_report", ["status"]),
    # must fall through to the LLM:
    ("bug-1011", "bug_report", None),
    ("What is the expected result for BUG-1001?", "bug_report", None),        # other type's field
    ("How do I reproduce TC-0301?", "test_case", None),
    ("Why does BUG-1003 happen?", "bug_report", None),                         # reasoning word
    ("Is BUG-1011 (Duplicate notification bug) a duplicate?", "bug_report", None),
    ("Is BUG-1017 connected to another ticket?", "bug_report", None),
])
def test_requested_fields(question, doc_type, expected):
    assert requested_fields(question, doc_type) == expected


# --- error-code route --------------------------------------------------------

@pytest.mark.parametrize("question,expected", [
    ("Which bug reports are associated with ERR_403_FORBIDDEN, and do any of them look related?",
     "ERR_403_FORBIDDEN"),
    ("list all bugs with err_504_timeout", "ERR_504_TIMEOUT"),
    ("Why does ERR_500_INTERNAL happen?", None),
    ("How many bugs have ERR_401_UNAUTH?", None),      # count wording stays on the count path
    ("What is ERR_409_CONFLICT?", None),               # no "which bugs" ask
    ("Which bugs have ERR_401_UNAUTH or ERR_403_FORBIDDEN?", None),  # two codes
])
def test_error_code_question(question, expected):
    assert error_code_question(question) == expected
