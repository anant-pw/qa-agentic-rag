"""
DIAGNOSTIC ONLY -- round 2 of diagnostics/guardrail_shape_probe.py.
Retrieval + embedding only, no chat model call.

Place at: diagnostics/guardrail_shape_probe_r2.py  (needs round 1 in the
same folder -- it imports probe_one from it)
Run:
    docker compose up -d --build fastapi
    docker exec -it rag-fastapi python -m diagnostics.guardrail_shape_probe_r2

WHY ROUND 2
Round 1 (machine A) showed the "ID resolves to the right document but the
vector score rejects it" pattern -- but only for BUG-1011, which is also a
generic "hub" document (vector rank 1 for the bare queries "bug reports" and
"bugs"). One document cannot support a general rule. This round tests:

  ID-*        other real IDs (bare and phrased, NO title in parentheses).
              Repo-derived IDs: BUG-1003, BUG-1023, TC-0301, TC-0302
              (eval_seed.json), BUG-2015 (seen in round-1 hits),
              ERR_401_UNAUTH (eval_seed.json).
  ID-missing  IDs that do not exist. What should the system say?
  ADV         off-topic text + a real ID: documents the accepted trade-off
              of any ID-trust rule, measured rather than assumed.
  CAL-ood     the two calibration OOD questions round 1 skipped (verbatim
              from rrf_threshold_probe.py).
  NEAR-ood    AUTHOR-WRITTEN (not from the repo) domain-flavoured questions
              that the corpus cannot answer. These are what actually bound
              any threshold change: if they score ~0.70-0.75, lowering the
              threshold is unsafe regardless of the 3 clean OOD rows.
"""

from app.config import settings
from app.search.service import get_client

from diagnostics.guardrail_shape_probe import probe_one

PROBES_R2 = [
    # Do other IDs behave like BUG-1011, or was that hubness?
    ("ID-bare", "BUG-1003"),
    ("ID-phrased", "What are the steps to reproduce BUG-1003?"),
    ("ID-phrased", "What are the steps to reproduce BUG-1023?"),
    ("ID-bare", "TC-0301"),
    ("ID-phrased", "What are the preconditions for TC-0301?"),
    ("ID-phrased", "What are the preconditions for TC-0302?"),
    ("ID-phrased", "What are the steps to reproduce BUG-2015?"),
    ("ID-phrased", "Which bug reports mention ERR_401_UNAUTH?"),
    # Nonexistent IDs
    ("ID-missing", "BUG-9999"),
    ("ID-missing", "What are the steps to reproduce BUG-9999?"),
    # Off-topic + real ID
    ("ADV", "What is the capital of France? BUG-1011"),
    # Remaining calibration OOD (verbatim, rrf_threshold_probe.py)
    ("CAL-ood", "Write me a short poem about the ocean."),
    ("CAL-ood", "Explain quantum entanglement in simple terms."),
    # Near-domain OOD (author-written)
    ("NEAR-ood", "What is a test case?"),
    ("NEAR-ood", "How do I write a good bug report?"),
    ("NEAR-ood", "How do I get rid of bugs in my garden?"),
]


def main() -> None:
    os_client = get_client(settings.opensearch_host, settings.opensearch_port)
    print(f"retrieval_size={settings.retrieval_size} "
          f"deterministic_count_routing={settings.deterministic_count_routing} "
          f"threshold={settings.vector_score_guardrail_threshold} "
          f"embed_model={settings.ollama_embedding_model}")
    for label, q in PROBES_R2:
        probe_one(os_client, label, q)


if __name__ == "__main__":
    main()
