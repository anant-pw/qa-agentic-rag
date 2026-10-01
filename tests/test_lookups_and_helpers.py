from conftest import FakeConn

from app.generation.cache import build_cache_key
from app.generation.context import fetch_reference_targets, fetch_references
from app.generation.error_code_lookup import answer_error_code_question
from app.generation.field_lookup import answer_field_question
from app.generation.llm import ollama_timings
from app.generation.prompt import format_doc_context
from eval.run_holdout import grade

DOC_COLS = ["id", "external_id", "title", "doc_type", "module", "status"]


# --- error-code lookup -------------------------------------------------------

def test_error_code_answer_lists_all_docs_and_only_recorded_relations():
    conn = FakeConn(
        (DOC_COLS, [
            (1, "BUG-1001", "Login fails", "bug_report", "Login", "Open"),
            (15, "BUG-1015", "Session expires", "bug_report", "Checkout", "In Progress"),
            (24, "BUG-1024", "Login still broken", "bug_report", "Login", "Reopened"),
        ]),
        (["src", "type", "tgt"], [("BUG-1024", "duplicate_of", "BUG-1001")]),
    )
    answer, sources = answer_error_code_question(conn, "ERR_401_UNAUTH")
    assert answer.startswith("3 document(s) are associated with ERR_401_UNAUTH")
    assert "BUG-1024 is a duplicate of BUG-1001" in answer
    assert "does not establish a common root cause" in answer
    assert "(Source: BUG-1001, BUG-1015, BUG-1024)" in answer
    assert [s["external_id"] for s in sources] == ["BUG-1001", "BUG-1015", "BUG-1024"]


def test_error_code_answer_applies_filters_and_handles_no_match():
    conn = FakeConn((DOC_COLS, [(1, "BUG-1001", "t", "bug_report", "Login", "Open")]))
    answer, sources = answer_error_code_question(conn, "ERR_401_UNAUTH", module="Payments")
    assert answer == "No document in this corpus is associated with ERR_401_UNAUTH."
    assert sources == []


# --- field lookup -------------------------------------------------------------

def test_field_answer_is_verbatim_and_marks_missing_fields():
    cols = ["external_id", "title", "doc_type", "module", "status", "description",
            "steps_to_reproduce", "preconditions", "steps", "expected_result"]
    conn = FakeConn((cols, [("BUG-1003", "Checkout times out", "bug_report", "Checkout", "In Progress",
                             "desc", "1. Throttle 2. Submit", None, None, None)]))
    # requested_fields() always returns fields in field_lookup._FIELDS order
    answer, _ = answer_field_question(conn, 3, ["steps_to_reproduce", "description"])
    assert answer.splitlines() == [
        "BUG-1003 (Checkout times out)",
        "Steps to reproduce: 1. Throttle 2. Submit",
        "Description: desc",
        "(Source: BUG-1003)",
    ]


def test_field_answer_returns_none_when_row_missing():
    assert answer_field_question(FakeConn((["external_id"], [])), 999, ["status"]) is None


# --- reference expansion / formatting ---------------------------------------

def test_reference_targets_prefer_cited_test_case_and_respect_limit():
    conn = FakeConn((["src", "tgt", "type"], [
        ("BUG-1002", "BUG-1001", "duplicate_of"),
        ("BUG-1002", "TC-0142", "test_case_citation"),
        ("BUG-1002", "BUG-1007", "related_to"),
    ]))
    assert fetch_reference_targets(conn, ["bug-1002"], limit=2) == ["TC-0142", "BUG-1001"]


def test_references_are_phrased_for_both_directions_and_dangling_targets():
    conn = FakeConn((["s", "t", "se", "te", "raw", "type"], [
        (2, 1, "BUG-1002", "BUG-1001", "BUG-1001", "duplicate_of"),
        (13, None, "BUG-1013", None, "TC-0209", "test_case_citation"),
    ]))
    refs = fetch_references(conn, [1, 13])
    assert refs[1] == ["BUG-1002 is a duplicate of BUG-1001"]
    assert refs[13] == ["BUG-1013 cites TC-0209 (not found in corpus)"]


def test_format_doc_context_bug_report_block():
    hit = {"external_id": "BUG-1", "title": "T", "doc_type": "bug_report", "module": "Login", "status": "Open"}
    block = format_doc_context(hit, {"description": "D"}, ["BUG-1 cites TC-1"])
    assert block.splitlines() == [
        "[BUG-1] T (doc_type=bug_report, module=Login, status=Open)",
        "Description: D",
        "Steps to reproduce: (none recorded)",
        "Recorded cross-references:",
        "- BUG-1 cites TC-1",
    ]


# --- small helpers ------------------------------------------------------------

def test_ollama_timings_converts_nanoseconds():
    t = ollama_timings({"load_duration": 2_000_000_000, "prompt_eval_count": 2099,
                        "prompt_eval_duration": 5_003_000_000, "eval_count": 66})
    assert t["ollama_load_s"] == 2.0
    assert t["ollama_prompt_eval_s"] == 5.003
    assert t["ollama_prompt_tokens"] == 2099
    assert t["ollama_eval_s"] is None
    assert ollama_timings(None) == {}


def test_cache_key_changes_with_model_and_index():
    base = build_cache_key("q", None, "Login", None, "idx_v1", "m1")
    assert base.startswith("generate:")
    assert base == build_cache_key("q", None, "Login", None, "idx_v1", "m1")
    assert base != build_cache_key("q", None, "Login", None, "idx_v2", "m1")
    assert base != build_cache_key("q", None, "Login", None, "idx_v1", "m2")


def test_holdout_grader_rules():
    ids = ["BUG-2024", "BUG-2043", "BUG-2051"]
    item = {"must_include_all": ["BUG-1001"], "must_not_include": ["BUG-2095"], "min_of": {"ids": ids, "n": 2}}
    assert grade(item, "BUG‑1001, BUG-2024 and BUG-2043") == []   # unicode dash normalised
    assert grade(item, "BUG-1001, BUG-2024, BUG-2095") == [
        f"only 1/2 of {ids} (['BUG-2024'])", "contains forbidden 'BUG-2095'"]
    assert grade({"expect": "abstain"}, "The retrieved documents do not address this question.") == []
    assert grade({"expect": "reject_or_abstain"}, "This question doesn't appear to be about the QA corpus") == []
