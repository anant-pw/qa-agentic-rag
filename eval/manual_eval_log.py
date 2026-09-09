"""
Phase 6 Tier B: append-only manual grading log.

WHY THIS EXISTS AS ACTUAL TOOLING, NOT JUST A CONVENTION: the Phase 6
readiness report found that PHASE5_VERIFY.md -- the thing phase-05-handoff.md
cites as "11 real runs" of manual verification -- is not itself a record.
It's a verification SCRIPT (instructions + expected shapes), and the actual
verbatim answers from those 11 runs exist only as a prose summary in the
handoff document, written after the fact, in a different chat. That's not
a reusable artifact; it's a memory of one. This module is the fix: every
manual grading pass appends one real, structured record to a local
append-only file, at the time the answer is actually read, not
reconstructed afterward from notes.

Covers question types Tier A (run_eval_generation.py) cannot check
deterministically: single_doc_factual (no structured ground truth to
diff against -- the answer is free text derived from free text) and any
question where the real concern is abstention/groundedness correctness
(does the model correctly say "not documented" per Rule 2, correctly
refuse per Rule 4, or state something not actually present in what was
retrieved) rather than a fact with one correct value.

Usage:
    python -m eval.manual_eval_log add \\
        --question "What are the steps to reproduce BUG-1003 ...?" \\
        --answer "<paste the actual /generate output here>" \\
        --verdict pass \\
        --note "Matches steps_to_reproduce verbatim, no invented steps."

    python -m eval.manual_eval_log list
    python -m eval.manual_eval_log summary
"""

import argparse
import json
import os
from datetime import datetime, timezone

LOG_PATH = os.environ.get("MANUAL_EVAL_LOG_PATH", os.path.join("eval", "manual_eval_log.jsonl"))


def add_entry(question: str, answer: str, verdict: str, note: str = "") -> dict:
    if verdict not in ("pass", "fail"):
        raise ValueError(f"verdict must be 'pass' or 'fail', got {verdict!r}")
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "question": question,
        "answer": answer,
        "verdict": verdict,
        "note": note,
    }
    os.makedirs(os.path.dirname(LOG_PATH) or ".", exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def read_entries() -> list[dict]:
    if not os.path.exists(LOG_PATH):
        return []
    with open(LOG_PATH, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def summarize() -> dict:
    entries = read_entries()
    passed = sum(1 for e in entries if e["verdict"] == "pass")
    failed = sum(1 for e in entries if e["verdict"] == "fail")
    return {"total": len(entries), "pass": passed, "fail": failed}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="Append one manually-graded entry")
    p_add.add_argument("--question", required=True)
    p_add.add_argument("--answer", required=True)
    p_add.add_argument("--verdict", required=True, choices=["pass", "fail"])
    p_add.add_argument("--note", default="")

    sub.add_parser("list", help="Print every logged entry")
    sub.add_parser("summary", help="Print pass/fail counts")

    args = parser.parse_args()

    if args.command == "add":
        entry = add_entry(args.question, args.answer, args.verdict, args.note)
        print(f"Logged: [{entry['verdict'].upper()}] {entry['question'][:60]}")
    elif args.command == "list":
        for e in read_entries():
            print(f"[{e['verdict'].upper():4}] {e['timestamp']}  {e['question'][:60]}")
            if e["note"]:
                print(f"       note: {e['note']}")
    elif args.command == "summary":
        s = summarize()
        print(f"Manual eval log: {s['pass']} pass / {s['fail']} fail (of {s['total']} total entries)")
