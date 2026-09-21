"""
OFFLINE self-test for the ID route in app/routers/generate_agentic.py.
Needs NO OpenSearch, Ollama, Postgres or Redis. Runs the REAL
guardrail_and_route() node, the REAL id_resolution helpers and the REAL
resolve_document_ids(); only two things are faked:

  * the OpenSearch client (an in-memory `terms` lookup over a tiny FIXTURE
    corpus), and
  * hybrid_search() (returns hand-built fused hits, in RRF order).

The fixture uses BUG-/TC-looking IDs and made-up modules/statuses purely to
exercise routing logic. It is NOT your data and proves nothing about real
retrieval quality -- that is what diagnostics/routing_check.py (real stack)
is for. This file proves the DECISION LOGIC: what the node does given what
retrieval returned.

Run (inside the container, where the env vars Settings needs already exist):
    docker cp diagnostics/id_resolution_selftest.py rag-fastapi:/app/diagnostics/id_resolution_selftest.py
    docker exec -it rag-fastapi python -m diagnostics.id_resolution_selftest
Exit code is non-zero if any check fails.
"""

import sys

import app.routers.generate_agentic as gar
from app.search.id_resolution import MAX_IDS, extract_document_ids

# ---------------------------------------------------------------- fixtures
_DOCS = {}


def _doc(ext, title, doc_type, module, status):
    _DOCS[ext.lower()] = {
        "chunk_id": f"{ext}::chunk_000",
        "document_id": len(_DOCS) + 1,
        "parent_document_id": len(_DOCS) + 1,
        "external_id": ext,
        "title": title,
        "doc_type": doc_type,
        "module": module,
        "status": status,
    }


_doc("BUG-1001", "Fixture login bug", "bug_report", "Login", "Open")
_doc("BUG-1003", "Fixture checkout bug", "bug_report", "Checkout", "Open")
_doc("BUG-1011", "Fixture hub bug", "bug_report", "Notifications", "Open")
_doc("BUG-1017", "Fixture export bug", "bug_report", "Admin", "Resolved")
_doc("BUG-1023", "Fixture payment bug", "bug_report", "Payments", "Open")
_doc("BUG-1024", "Fixture login dup", "bug_report", "Login", "Open")
_doc("BUG-2015", "Fixture other bug", "bug_report", "Payments", "Open")
_doc("TC-0301", "Fixture tokenization case", "test_case", "Search", "Approved")
_doc("TC-0302", "Fixture SKU case", "test_case", "Search", "Approved")
_doc("TC-2013", "Fixture generic test case", "test_case", "Search", "Approved")


class FakeClient:
    """Supports exactly the query resolve_document_ids() sends."""

    def search(self, index, body):
        wanted = body["query"]["terms"]["external_id"]
        hits = [{"_source": _DOCS[w], "_score": 1.0} for w in wanted if w in _DOCS]
        return {"hits": {"hits": hits}}


def _fused(ext, rrf, vec, b_rank=None, v_rank=None):
    """A fused hit as hybrid_search() returns it (RRF order is the caller's)."""
    d = _DOCS[ext.lower()]
    return {
        "chunk_id": d["chunk_id"], "document_id": d["document_id"],
        "parent_document_id": d["parent_document_id"], "external_id": d["external_id"],
        "title": d["title"], "doc_type": d["doc_type"], "module": d["module"],
        "status": d["status"], "score": 1.0,
        "bm25_rank": b_rank, "bm25_score": None, "vector_rank": v_rank,
        "vector_score": vec, "rrf_score": rrf,
    }


STUB = {"hits": [], "calls": 0}


def _fake_hybrid(client, alias, q, ollama_host, ollama_port, doc_type=None, module=None,
                 status=None, size=10, rrf_k=60):
    STUB["calls"] += 1
    return list(STUB["hits"])


gar.get_client = lambda host, port: FakeClient()
gar.hybrid_search = _fake_hybrid

# ------------------------------------------------------------------ harness
RESULTS = []


def run(question, hits, **filters):
    STUB["hits"], STUB["calls"] = hits, 0
    state = {"question": question, "doc_type": None, "module": None, "status": None, **filters}
    return gar.guardrail_and_route(state)


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))


def ids(out):
    return [h["external_id"] for h in out["top_hits"]]


# A machine-A-like ranking: hub documents fill positions 1-5, target is 6th
# (the real BUG-2015 situation measured on machine A).
HUB_THEN_TARGET = [
    _fused("BUG-1011", 0.0323, 0.7399, 3, 1), _fused("BUG-1017", 0.0320, 0.7037, 2, 3),
    _fused("BUG-1024", 0.0318, 0.7232, 4, 2), _fused("BUG-1001", 0.0300, 0.70, 6, 4),
    _fused("TC-0302", 0.0290, 0.69, 7, 5), _fused("BUG-2015", 0.0245, 0.6933, 1, 66),
]

# 1. resolved ID, target buried at position 6 (outside context_top_n=5)
o = run("What are the steps to reproduce BUG-2015?", HUB_THEN_TARGET)
check("1 resolved: buried target pinned into top_hits[0]", ids(o)[0] == "BUG-2015", ids(o))
check("1 resolved: decision semantic, path semantic, id_route resolved",
      o["decision"] == "semantic" and o["retrieval_span"]["path"] == "semantic"
      and o["retrieval_span"]["id_route"] == "resolved")
check("1 resolved: no duplicate documents in top_hits", len(ids(o)) == len(set(ids(o))), ids(o))
check("1 resolved: fused hit keeps its REAL scores (not replaced by None)",
      o["top_hits"][0]["vector_score"] == 0.6933 and o["top_hits"][0]["rrf_score"] == 0.0245)
check("1 resolved: guardrail_vector_score logged from RRF order BEFORE pinning (0.7399)",
      o["guardrail_vector_score"] == 0.7399, o["guardrail_vector_score"])

# 2. resolved ID absent from hits entirely -> injected from the lookup
o = run("What are the preconditions for TC-0301?", [_fused("BUG-1011", 0.0323, 0.72, 1, 1)])
check("2 injected: document not in fused hits is still pinned first", ids(o)[0] == "TC-0301", ids(o))
check("2 injected: scores are None, not fabricated",
      o["top_hits"][0]["vector_score"] is None and o["top_hits"][0]["rrf_score"] is None)

# 3. unknown ID -> reject, NO retrieval call, accurate message
o = run("What are the steps to reproduce BUG-9999?", HUB_THEN_TARGET)
check("3 unknown: decision reject / path reject_unknown_id",
      o["decision"] == "reject" and o["retrieval_span"]["path"] == "reject_unknown_id")
check("3 unknown: hybrid_search was NOT called", STUB["calls"] == 0, STUB["calls"])
check("3 unknown: message names the ID and is not the generic out-of-domain text",
      "BUG-9999" in o["answer"] and "doesn't appear to be about" not in o["answer"], o["answer"])
check("3 unknown: sources empty, top_hits empty", o["sources"] == [] and o["top_hits"] == [])
o2 = run("bug-9999", HUB_THEN_TARGET)
check("3b unknown bare lowercase id also rejected", o2["retrieval_span"]["path"] == "reject_unknown_id")

# 4. partial: one exists, one does not -> resolved, only the existing one pinned
o = run("Is BUG-1024 a duplicate of BUG-9999?", HUB_THEN_TARGET)
check("4 partial: semantic, existing ID pinned first",
      o["decision"] == "semantic" and ids(o)[0] == "BUG-1024", ids(o))
check("4 partial: resolved_ids lists only the existing one",
      o["retrieval_span"]["resolved_ids"] == ["bug-1024"], o["retrieval_span"]["resolved_ids"])

# 5. ID + count phrasing must NOT take the deterministic shortcut
o = run("How many bugs mention BUG-1001?", HUB_THEN_TARGET)
check("5 id+count: routed semantic, not deterministic",
      o["decision"] == "semantic" and ids(o)[0] == "BUG-1001", (o["decision"], ids(o)))

# 6. ID exists but the caller's filter excludes it -> old path, no pinning
o = run("What are the steps to reproduce BUG-1001?", HUB_THEN_TARGET, module="Payments")
check("6 filtered-out: id_route none, BUG-1001 NOT force-pinned",
      o["retrieval_span"].get("id_route") == "none" and ids(o)[0] != "BUG-1001", ids(o))
check("6 filtered-out: old filter-trust path still accepts (semantic)", o["decision"] == "semantic")
o = run("What are the steps to reproduce BUG-1001?", HUB_THEN_TARGET, module="login")
check("6b filter match is case-insensitive -> pinned", o["retrieval_span"].get("id_route") == "resolved"
      and ids(o)[0] == "BUG-1001")

# 7-9. no-ID questions: behaviour must be unchanged
below = [_fused("TC-2013", 0.0325, 0.7480, 2, 1)]
o = run("test cases", below)
check("7 no-ID below threshold: still rejected on the OLD path (known limitation)",
      o["decision"] == "reject" and o["retrieval_span"]["path"] == "reject"
      and "id_route" not in o["retrieval_span"])
o = run("How many documents are there in total?", below)
check("8 no-ID countable: still deterministic", o["decision"] == "deterministic", o["decision"])
o = run("Explain the login lockout problem", [_fused("BUG-1024", 0.0328, 0.8179, 1, 1)])
check("9 no-ID above threshold: still semantic", o["decision"] == "semantic")
o = run("Which bug reports mention ERR_401_UNAUTH?", [_fused("BUG-1024", 0.0328, 0.8179, 1, 1)])
check("9b ERR_ code question: no document IDs, untouched old path",
      o["decision"] == "semantic" and "id_route" not in o["retrieval_span"])

# 10. typographic dash (U+2011) as pasted from Jira/Word
o = run("What are the steps to reproduce BUG\u20111003?", HUB_THEN_TARGET + [_fused("BUG-1003", 0.02, 0.6, 9, 40)])
check("10 typographic dash resolves", o["retrieval_span"].get("id_route") == "resolved" and ids(o)[0] == "BUG-1003")

# 11. documented trade-off: off-topic text + real ID is accepted
o = run("What is the capital of France? BUG-1011", HUB_THEN_TARGET)
check("11 ADV (documented trade-off): off-topic + real ID is now accepted",
      o["decision"] == "semantic" and ids(o)[0] == "BUG-1011")

# 12. pin order follows question order
o = run("Compare TC-0301 with BUG-1011", HUB_THEN_TARGET)
check("12 pin order = question order", ids(o)[:2] == ["TC-0301", "BUG-1011"], ids(o))

# 13. extractor unit checks
check("13 extract: case/dedupe/order", extract_document_ids("tc-0301 and BUG-1011, TC-0301 again")
      == ["tc-0301", "bug-1011"])
check("13 extract: 3- and 5-digit IDs ignored", extract_document_ids("BUG-101 and BUG-12345") == [])
many = " ".join(f"BUG-{1000 + i}" for i in range(25))
check(f"13 extract: capped at MAX_IDS={MAX_IDS}", len(extract_document_ids(many)) == MAX_IDS)

# 14. whole compiled graph on the unknown-ID route (no LLM, no Redis)
final = gar.compiled_agentic_graph.invoke({"question": "What are the steps to reproduce BUG-9999?",
                                            "doc_type": None, "module": None, "status": None})
check("14 graph: unknown-ID reject ends at END with the message and no sources",
      final["decision"] == "reject" and "BUG-9999" in final["answer"] and final["sources"] == [])

# ------------------------------------------------------------------- report
failed = [r for r in RESULTS if not r[1]]
for name, ok, detail in RESULTS:
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok or not detail else f"   -> {detail}"))
print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
sys.exit(1 if failed else 0)
