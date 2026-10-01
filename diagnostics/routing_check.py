"""
REAL-STACK routing check for the ID route -- no LLM call, so a full run takes
seconds. Calls the real guardrail_and_route() node against your live
OpenSearch + Ollama (embedding only) and asserts the ROUTING outcome:
decision, path, and which document is first in the context.

Why this and not the eval harness: the routing decision is what changed.
Running 20+ questions through phi4:14b at 100-480 s each would measure the
model, and eval/run_eval_generation.py's check_out_of_domain FAILS any answer
that cites a BUG-/TC- ID -- which the unknown-ID message legitimately does.

This is NOT a replacement for the frozen 20-question baseline. After this
passes, the baseline must still be re-run
(eval/run_eval_generation.py --endpoint-path /generate/agentic) and must still
read 16 PASS / 0 FAIL / 4 MANUAL_REQUIRED.

Run:
    docker cp diagnostics/routing_check.py rag-fastapi:/app/diagnostics/routing_check.py
    docker exec -it rag-fastapi python -m diagnostics.routing_check
Exit code is non-zero if any row FAILs.

Row kinds:
  ID-*        the new behaviour
  CTRL-*      must behave exactly as before this change
  LIMITATION  documented, deliberately unfixed -- expected outcome is the OLD one
  TRADEOFF    documented, accepted consequence of the new behaviour
The Ollama version is printed because the 0.75 threshold (used by CTRL rows)
was calibrated on 0.34.0.
"""

import sys
import time

import httpx

from app.config import settings
from app.routers.generate_agentic import guardrail_and_route

# Single-ID plain field asks ("steps to reproduce BUG-1023") route to the
# no-LLM field lookup when settings.deterministic_field_lookup is on (see
# app/generation/field_lookup.py); otherwise to the semantic path as before.
FL = "field_lookup" if settings.deterministic_field_lookup else "semantic"
# Single-ERR_* "which bugs ..." asks -> no-LLM error-code route when on.
EC = "error_code" if settings.deterministic_error_code_lookup else "semantic"

# (label, question, filters, expected)   expected keys: decision, path,
# first (external_id that must be top_hits[0]), absent (must NOT be in
# top_hits), no_docs (top_hits must be empty)
CASES = [
    ("ID-phrased", "What are the steps to reproduce BUG-1023?", {},
     {"decision": FL, "path": FL, "first": "BUG-1023"}),
    ("ID-buried-on-A", "What are the steps to reproduce BUG-2015?", {},
     {"decision": FL, "path": FL, "first": "BUG-2015"}),
    ("ID-testcase", "What are the preconditions for TC-0301?", {},
     {"decision": FL, "path": FL, "first": "TC-0301"}),
    ("ID-bare-lower", "bug-1011", {},
     {"decision": "semantic", "path": "semantic", "first": "BUG-1011"}),
    ("ID-known-fail", "where is the bug steps in bug-1011", {},
     {"decision": FL, "path": FL, "first": "BUG-1011"}),
    ("ID-typo-dash", "What are the steps to reproduce BUG\u20111003?", {},
     {"decision": FL, "path": FL, "first": "BUG-1003"}),
    ("ID-unknown", "What are the steps to reproduce BUG-9999?", {},
     {"decision": "reject", "path": "reject_unknown_id", "no_docs": True}),
    ("ID-unknown-bare", "BUG-9999", {},
     {"decision": "reject", "path": "reject_unknown_id", "no_docs": True}),
    ("ID-partial", "Is BUG-1024 a duplicate of BUG-9999?", {},
     {"decision": "semantic", "path": "semantic", "first": "BUG-1024"}),
    ("ID-plus-count", "How many bugs mention BUG-1001?", {},
     {"decision": "semantic", "path": "semantic", "first": "BUG-1001"}),
    ("ID-vs-filter", "What are the steps to reproduce BUG-1001?", {"module": "Payments"},
     {"decision": "semantic", "absent": "BUG-1001"}),
    ("TRADEOFF", "What is the capital of France? BUG-1011", {},
     {"decision": "semantic", "path": "semantic", "first": "BUG-1011"}),
    ("CTRL-ood", "What is the capital of France?", {},
     {"decision": "reject", "path": "reject"}),
    ("CTRL-ood", "How do I bake a chocolate cake?", {},
     {"decision": "reject", "path": "reject"}),
    ("CTRL-count", "How many documents are there in total?", {},
     {"decision": "deterministic", "path": "deterministic"}),
    ("CTRL-count-filt", "How many bugs are currently Open in the Login module?",
     {"module": "Login", "status": "Open"},
     {"decision": "deterministic", "path": "deterministic"}),
    ("CTRL-title", "What are the steps to reproduce BUG-1003 (Checkout page times out on slow connections)?", {},
     {"decision": FL, "first": "BUG-1003"}),
    ("ERR-agg", "Which bug reports mention ERR_401_UNAUTH?", {},
     {"decision": EC, "path": EC}),
    ("ERR-agg-seed", "Which bug reports are associated with ERR_403_FORBIDDEN, and do any of them look related to each other?", {},
     {"decision": EC, "path": EC}),
    ("CTRL-errcode-why", "Why does ERR_500_INTERNAL happen?", {},
     {"decision": "semantic", "path": "semantic"}),
    ("REF-expand", "What is the expected result of the test case referenced in bug report BUG-1001 (Login fails after 3 attempts even with correct password)?", {},
     {"decision": "semantic", "path": "semantic", "first": "BUG-1001",
      **({"contains": "TC-0142"} if settings.reference_expansion else {})}),
    ("LIMITATION", "test cases", {},
     {"decision": "reject", "path": "reject"}),
]


def evaluate(out: dict, exp: dict) -> list[str]:
    problems = []
    span = out.get("retrieval_span", {})
    ids = [h["external_id"] for h in out.get("top_hits", [])]
    if "decision" in exp and out.get("decision") != exp["decision"]:
        problems.append(f"decision={out.get('decision')!r} (want {exp['decision']!r})")
    if "path" in exp and span.get("path") != exp["path"]:
        problems.append(f"path={span.get('path')!r} (want {exp['path']!r})")
    if "first" in exp and (not ids or ids[0] != exp["first"]):
        problems.append(f"top_hits[0]={ids[0] if ids else None!r} (want {exp['first']!r})")
    if "contains" in exp and exp["contains"] not in ids:
        problems.append(f"{exp['contains']} missing from top_hits {ids}")
    if "absent" in exp and exp["absent"] in ids:
        problems.append(f"{exp['absent']} present in top_hits {ids}")
    if exp.get("no_docs") and ids:
        problems.append(f"top_hits not empty: {ids}")
    return problems


def main() -> int:
    try:
        ver = httpx.get(f"http://{settings.ollama_host}:{settings.ollama_port}/api/version", timeout=10).json()
    except Exception as e:
        ver = f"unavailable ({e})"
    print(f"ollama {ver}  threshold={settings.vector_score_guardrail_threshold}  "
          f"routing={settings.deterministic_count_routing}  field_lookup={settings.deterministic_field_lookup}  error_code={settings.deterministic_error_code_lookup}  ref_expansion={settings.reference_expansion}  context_top_n={settings.context_top_n}\n")

    failed = 0
    for label, question, filters, exp in CASES:
        state = {"question": question, "doc_type": None, "module": None, "status": None, **filters}
        t0 = time.time()
        try:
            out = guardrail_and_route(state)
        except Exception as e:  # report and continue; a crash is a FAIL
            print(f"FAIL  [{label}] {question!r}  EXCEPTION {type(e).__name__}: {e}")
            failed += 1
            continue
        ms = int((time.time() - t0) * 1000)
        problems = evaluate(out, exp)
        ids = [h["external_id"] for h in out["top_hits"]]
        span = out["retrieval_span"]
        status = "PASS" if not problems else "FAIL"
        failed += bool(problems)
        print(f"{status}  [{label}] {question!r} {filters or ''}\n"
              f"      -> decision={out['decision']} path={span.get('path')} "
              f"id_route={span.get('id_route', '-')} guardrail_vec={span.get('guardrail_vector_score')} "
              f"top={ids[:5]} refs={span.get('referenced_ids', '-')}  {ms} ms")
        for p in problems:
            print(f"      !! {p}")
    print(f"\n{len(CASES) - failed}/{len(CASES)} rows passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
