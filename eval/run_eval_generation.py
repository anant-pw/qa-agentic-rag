"""
Phase 6 Tier A: deterministic generation-quality scoring against /generate.

WHY DETERMINISTIC, NOT LLM-AS-JUDGE (Phase 6 readiness report, this repo's
chat history): a judge model checking whether the generator hallucinated
has no more access to ground truth than the generator did -- a wrong judge
verdict is a second unverified opinion stacked on the first, not an
independent check. This project already has real ground truth for most
question types -- document_references and error_codes, populated
deterministically by ingest.py since Phase 2 -- so this script queries
THAT directly, the same tables app/generation/context.py's
fetch_references() reads from in production. Eval and generation are
checked against one source of truth, not two independently-fallible ones.

WHAT THIS DOES NOT COVER (see eval/manual_eval_log.py for Tier B):
single_doc_factual questions and general groundedness/abstention
correctness have no structured field to check against -- only whether the
answer correctly claims nothing exists, or correctly restates free text.
Those question types are marked "MANUAL_REQUIRED" here, not silently
skipped, and not scored true/false by guesswork.

Always calls /generate with no_cache=true -- an eval run must see real,
uncached generation on every call, per the Phase 6 readiness report's
caching-vs-determinism resolution (app/generation/cache.py's module
docstring). Running this script with caching accidentally on would score
Redis, not Ollama.

Run:
    python -m eval.run_eval_generation
    python -m eval.run_eval_generation --base-url http://localhost:8000
"""

import argparse
import json
import os
import re
import time

import httpx
import psycopg2

EVAL_BASE_URL = os.environ.get("EVAL_BASE_URL", "http://localhost:8000")
EVAL_REQUEST_TIMEOUT = float(os.environ.get("EVAL_REQUEST_TIMEOUT", "6000"))


def _pg_connect():
    return psycopg2.connect(
        dbname=os.environ.get("POSTGRES_DB", "rag_db"),
        user=os.environ.get("POSTGRES_USER", "rag_user"),
        password=os.environ.get("POSTGRES_PASSWORD", "rag_password"),
        host=os.environ.get("POSTGRES_HOST", "127.0.0.1"),  # host-side convention,
        # matches ingest.py / generate_eval_seed.py / scripts/build_index.py
        port=os.environ.get("POSTGRES_PORT", "5433"),
    )


def call_generate(base_url: str, endpoint_path: str, question: str, requires_docs, doc_type=None, module=None, status=None) -> tuple[str, list[dict]]:
    """POSTs to /generate with no_cache=true, splits the streamed response
    on the ---SOURCES--- marker (app/routers/generate.py's fixed contract),
    returns (answer_text, sources_list).

    doc_type/module/status are Phase 6 bug-fix additions -- the original
    version of this function only ever sent {question, no_cache}, which
    meant structured_filter questions ran through /generate with NO
    metadata filter applied at all: "How many bugs are Open in Login" was
    answered from whatever 5 documents hybrid search happened to retrieve
    for that raw sentence, not from an actual module=Login/status=Open
    filtered set. Confirmed as the root cause of both structured_filter
    FAILs in the first real run, not a generation defect -- see
    PHASE6_VERIFY.md / the corresponding readiness-review conversation."""
    payload = {"question": question, "no_cache": True}
    if doc_type:
        payload["doc_type"] = doc_type
    if module:
        payload["module"] = module
    if status:
        payload["status"] = status
    resp = httpx.post(f"{base_url}{endpoint_path}", json=payload, timeout=EVAL_REQUEST_TIMEOUT)
    resp.raise_for_status()
    full = resp.text
    if "---SOURCES---" in full:
        answer, _, sources_block = full.partition("---SOURCES---")
        try:
            sources = json.loads(sources_block.strip())
        except json.JSONDecodeError:
            sources = []
    else:
        answer, sources = full, []
    return answer.strip(), sources


def _parse_module_status(question_text: str) -> tuple[str | None, str | None]:
    """Shared by call_generate (to actually filter the request) and
    check_structured_filter (to build the DB ground-truth query) so the
    two can't silently drift out of parsing agreement with each other."""
    module_m = re.search(r"in the (\w+) module", question_text)
    status_m = re.search(r"currently (\S+) in", question_text)
    module = module_m.group(1) if module_m else None
    status = status_m.group(1) if status_m else None
    return module, status


def _normalize_dashes(text: str) -> str:
    """Replace Unicode dash/hyphen variants with plain ASCII '-' before any
    ID substring comparison.

    Real bug, not theoretical: gpt-oss models (confirmed on both the Groq
    and local Ollama versions during this project's model comparison) will
    render document IDs as e.g. "BUG‑1013" using U+2011 NON-BREAKING HYPHEN
    instead of U+002D HYPHEN-MINUS. A literal "BUG-1013" in ids in answer
    check silently fails against that -- the ID IS in the answer, just not
    byte-for-byte identical to what ingest.py/generate_eval_seed.py store.
    Confirmed by comparing a full-sweep run on gpt-oss:20b (10 of 13
    ID-substring checks failed, including duplicate_resolution/
    error_code_aggregation cases spot-checked and known correct via
    diagnostics/groq_probe.py minutes earlier) against the same model
    family's Groq output, which visibly used U+2011 in an earlier
    dangling-citation answer in this project's own investigation.
    """
    variants = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"  # hyphen, non-breaking
    # hyphen, figure dash, en dash, em dash, horizontal bar, minus sign
    for ch in variants:
        text = text.replace(ch, "-")
    return text


def _contains_all_ids(text: str, ids: list[str]) -> bool:
    text = _normalize_dashes(text)
    return all(doc_id in text for doc_id in ids)


def check_duplicate_resolution(question: dict, answer: str, conn) -> tuple[bool, str]:
    ids = question["requires_docs"]
    if not _contains_all_ids(answer, ids):
        return False, f"missing one or more required doc IDs {ids} in answer"
    # Must actually call it a duplicate, not hedge into related/unrelated.
    if not re.search(r"\bduplicate\b", answer, re.IGNORECASE):
        return False, "answer does not use the word 'duplicate'"
    return True, "contains required IDs and 'duplicate' language"


def check_related_resolution(question: dict, answer: str, conn) -> tuple[bool, str]:
    ids = question["requires_docs"]
    if not _contains_all_ids(answer, ids):
        return False, f"missing one or more required doc IDs {ids} in answer"
    # Must NOT make an unqualified duplicate claim. "not a duplicate" is fine;
    # bare "is a duplicate of" without a negation nearby is the failure mode
    # Phase 5 originally found (fetch_references() was added because of it).
    unqualified_duplicate = re.search(
        r"(?<!not a )(?<!not\s)duplicate of", answer, re.IGNORECASE
    )
    if unqualified_duplicate and "not a duplicate" not in answer.lower() and "not duplicate" not in answer.lower():
        return False, "answer appears to call this a duplicate without qualification (related_to should not)"
    return True, "contains required IDs, no unqualified duplicate claim"


def check_cross_reference(question: dict, answer: str, conn) -> tuple[bool, str]:
    ids = question["requires_docs"]
    if not _contains_all_ids(answer, ids):
        return False, f"missing one or more required doc IDs {ids} in answer"
    return True, "contains required source and target IDs"


def check_dangling_reference(question: dict, answer: str, conn) -> tuple[bool, str]:
    source_id = question["requires_docs"][0]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.target_external_id
            FROM document_references r
            JOIN documents d ON d.id = r.source_document_id
            WHERE d.external_id = %s AND r.target_document_id IS NULL
            LIMIT 1
            """,
            (source_id,),
        )
        row = cur.fetchone()
    if not row:
        return False, f"no dangling reference found in DB for {source_id} -- eval question is stale"
    target_raw = row[0]
    normalized_answer = _normalize_dashes(answer)
    if target_raw not in normalized_answer:
        return False, f"answer does not mention the cited-but-missing ID {target_raw} at all"
    not_found_phrases = ["not found", "not in the corpus", "does not exist", "not present",
                          "no record", "was not ingested", "not documented"]
    if not any(p in answer.lower() for p in not_found_phrases):
        return False, f"mentions {target_raw} but doesn't state it's missing from the corpus -- possible fabrication, needs manual read"
    return True, f"mentions {target_raw} and states it is not in the corpus"


def check_resolution_in_narrative(question: dict, answer: str, conn) -> tuple[bool, str]:
    bug_id = question["requires_docs"][0]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT br.description FROM documents d
            JOIN bug_reports br ON br.document_id = d.id
            WHERE d.external_id = %s
            """,
            (bug_id,),
        )
        row = cur.fetchone()
    if not row:
        return False, f"{bug_id} not found in DB -- eval question is stale"
    description = row[0] or ""
    abstention_phrase = f"do not state a resolution for {bug_id}"
    if abstention_phrase.lower() in answer.lower() or "does not state a resolution" in answer.lower():
        return False, f"answer wrongly abstains (Rule 2) even though {bug_id}'s description contains fix narrative"
    # Weak positive signal: answer shares meaningful vocabulary with the
    # real description rather than being generically non-committal. Not a
    # strict substring match (LLM will paraphrase) -- flag for manual read
    # if this weak check fails, don't hard-fail on it alone.
    desc_words = set(w.lower() for w in re.findall(r"[a-zA-Z]{5,}", description))
    answer_words = set(w.lower() for w in re.findall(r"[a-zA-Z]{5,}", answer))
    overlap = desc_words & answer_words
    if len(overlap) < 2:
        return False, f"low vocabulary overlap with actual description ({len(overlap)} shared words) -- likely generic non-answer, needs manual read"
    return True, f"no wrongful abstention, {len(overlap)} shared content words with real description"


def check_error_code_aggregation(question: dict, answer: str, conn) -> tuple[bool, str]:
    ids = question["requires_docs"]
    normalized_answer = _normalize_dashes(answer)
    missing = [i for i in ids if i not in normalized_answer]
    if missing:
        return False, f"missing doc IDs {missing} in answer"
    return True, "contains all IDs in the error-code group"


def check_structured_filter(question: dict, answer: str, conn) -> tuple[bool, str]:
    module, status = _parse_module_status(question["question"])
    if not module or not status:
        return False, "could not parse module/status from question text -- check question wording"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM documents WHERE doc_type = 'bug_report' AND module = %s AND status = %s",
            (module, status),
        )
        (real_count,) = cur.fetchone()
    if str(real_count) not in answer:
        return False, f"expected count {real_count} not found in answer text"
    return True, f"answer contains the correct live count ({real_count})"

def check_out_of_domain(question: dict, answer: str, conn) -> tuple[bool, str]:
        """PASS means the answer did not cite a real document ID and reads
        like a rejection, not a fabricated answer. This check is only
        meaningful against an endpoint that HAS a guardrail. Run against
        plain /generate, this is EXPECTED to FAIL -- /generate has no
        mechanism to refuse an out-of-scope question at all. That's not a
        bug in this checker; it's the documented reason the guardrail was
        built in the first place. A FAIL here on /generate is not alarming.
        A FAIL here on /generate/agentic is.
        """
        id_pattern = re.compile(r"\b(BUG|TC)-\d{4}\b")
        if id_pattern.search(_normalize_dashes(answer)):
            return False, "answer cites a real document ID for a question that should have been rejected"
        reject_phrases = [
            "doesn't appear to be about", "does not appear to be about",
            "out of scope", "cannot answer", "no answer was generated",
        ]
        if not any(p in answer.lower() for p in reject_phrases):
            return False, "answer does not read like a rejection -- check manually, may be a generic non-answer rather than a real reject"
        return True, "correctly rejected, no document ID cited"




CHECKERS = {
    "duplicate_resolution": check_duplicate_resolution,
    "related_resolution": check_related_resolution,
    "cross_reference_bug_to_test_case": check_cross_reference,
    "dangling_reference_citation": check_dangling_reference,
    "resolution_in_narrative": check_resolution_in_narrative,
    "error_code_aggregation": check_error_code_aggregation,
    "structured_filter": check_structured_filter,
    "out_of_domain": check_out_of_domain,

    # single_doc_factual deliberately absent -- no structured ground truth
    # to check against. See eval/manual_eval_log.py.
}


def run(base_url: str, seed_path: str, endpoint_path: str = "/generate"):
    with open(seed_path) as f:
        questions = json.load(f)

    conn = _pg_connect()
    results = []
    try:
        total_questions = len(questions)
        for question_number, q in enumerate(questions, start=1):
            question_started = time.perf_counter()
            print(
                f"[{question_number}/{total_questions}] START ({q['type']}): "
                f"{q['question']}",
                flush=True,
            )
            qtype = q["type"]
            checker = CHECKERS.get(qtype)
            if checker is None:
                # Phase 6 fix: this branch used to `continue` before ever
                # calling /generate, so results.json had no "answer" key
                # for MANUAL_REQUIRED rows -- the manual grading tier was
                # unusable for its actual purpose, since there was nothing
                # to read. Now fetches the real answer/sources like every
                # other question type; only the deterministic check is
                # skipped, not the generation call itself.
                answer, sources = call_generate(base_url, endpoint_path, q["question"], q["requires_docs"])
                results.append({
                    "question": q["question"], "type": qtype,
                    "result": "MANUAL_REQUIRED",
                    "detail": "no deterministic ground truth for this question type -- use eval/manual_eval_log.py",
                    "answer": answer,
                    "sources": sources,
                })
                print(
                    f"[{question_number}/{total_questions}] DONE "
                    f"MANUAL_REQUIRED in {time.perf_counter() - question_started:.1f}s",
                    flush=True,
                )
                continue
            if qtype == "structured_filter":
                module, status = _parse_module_status(q["question"])
                answer, sources = call_generate(base_url, endpoint_path, q["question"], q["requires_docs"], module=module, status=status)
            else:
                answer, sources = call_generate(base_url, endpoint_path, q["question"], q["requires_docs"])
            passed, detail = checker(q, answer, conn)
            results.append({
                "question": q["question"], "type": qtype,
                "result": "PASS" if passed else "FAIL",
                "detail": detail,
                "answer": answer,
                "sources": sources,
            })
            print(
                f"[{question_number}/{total_questions}] DONE "
                f"{'PASS' if passed else 'FAIL'} in "
                f"{time.perf_counter() - question_started:.1f}s",
                flush=True,
            )
    finally:
        conn.close()

    passed_n = sum(1 for r in results if r["result"] == "PASS")
    failed_n = sum(1 for r in results if r["result"] == "FAIL")
    manual_n = sum(1 for r in results if r["result"] == "MANUAL_REQUIRED")

    print(f"\n{'='*60}\nTIER A DETERMINISTIC RESULTS: {passed_n} PASS / {failed_n} FAIL / {manual_n} MANUAL_REQUIRED (of {len(results)})\n{'='*60}")
    for r in results:
        print(f"[{r['result']:14}] ({r['type']}) {r['question'][:70]}")
        print(f"                 -> {r['detail']}")

    out_path = os.path.join(os.path.dirname(seed_path), "run_eval_generation_results.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nFull results (including raw answers/sources) written to {out_path}")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=EVAL_BASE_URL)
    parser.add_argument("--seed-path", default="eval/eval_seed.json")
    parser.add_argument("--endpoint-path", default="/generate")
    args = parser.parse_args()
    run(args.base_url, args.seed_path, args.endpoint_path)
