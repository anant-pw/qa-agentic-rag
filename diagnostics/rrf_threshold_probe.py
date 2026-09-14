"""
DIAGNOSTIC ONLY -- not wired into /generate or /search/hybrid, not part of
the production request path.

Purpose: measure real RRF top-scores for questions that have no business
matching this corpus, so the guardrail/grade threshold is picked from
actual measured data instead of guessed. The 17-question eval set only
has questions the system is SUPPOSED to answer -- its top scores
(0.0325-0.0328, all real, already on record in
eval/run_eval_generation_results.json) tell you what a correct hit looks
like. They tell you nothing about what an out-of-domain question's score
looks like, because none exist in that set. This script fills that gap.

Deliberately reuses hybrid_search() unmodified -- same function /generate
calls, so the numbers this prints are the real numbers the guardrail
would actually see, not a simulation of them.

--- Round 2, after Round 1's real finding ---

Round 1 measured rrf_score only and found it useless: out-of-domain
questions scored 0.0313-0.0328, indistinguishable from real in-corpus
hits (0.0325-0.0328). Root cause: RRF fuses on RANK, and in a
28-document corpus, k-NN always returns its 5 nearest neighbors and
BM25 almost always ranks something first, REGARDLESS of true relevance
-- rank-1 looks identical whether the match is strong or garbage. RRF
was the right choice for combining two legs' rankings; it is the wrong
signal for "does this question belong here at all," because that
question needs match STRENGTH, which RRF discards by construction.

This round instead prints bm25_score and vector_score -- the raw,
pre-fusion numbers reciprocal_rank_fusion() already computes and keeps
on every hit (see app/search/queries.py), just never surfaced past that
point. Includes 2 real in-domain questions alongside the 5 out-of-domain
ones so both sides of the comparison come from the same live run, not
mixed with old cached numbers from a different metric.

Run from inside the fastapi container (needs the `opensearch` hostname to
resolve, same reason verify_infra.py and groq_probe.py are run this way):
    docker compose up -d --build fastapi
    docker exec -it rag-fastapi python -m diagnostics.rrf_threshold_probe
"""

from app.search.service import get_client, hybrid_search
from app.config import settings

# Out-of-domain, deliberately nothing-to-do-with-QA-or-bugs.
OUT_OF_DOMAIN_QUESTIONS = [
    "What's the weather like today?",
    "Write me a short poem about the ocean.",
    "What is the capital of France?",
    "How do I bake a chocolate cake?",
    "Explain quantum entanglement in simple terms.",
]

# In-domain, real eval_seed.json questions -- for a same-run comparison.
IN_DOMAIN_QUESTIONS = [
    "What is the expected result of the test case referenced in bug report BUG-1001 (Login fails after 3 attempts even with correct password)?",
    "What are the steps to reproduce BUG-1003 (Checkout page times out on slow connections)?",
]

# Round 4: real regression found in production -- /generate/agentic
# false-rejected "How many bugs are currently Open in the Payments
# module?" (module=Payments, status=Open forwarded, same as the eval
# script does), while the sibling Login question passed. Both are
# genuinely in-domain, generic/templated structured_filter questions --
# neither was in the original 8-question calibration set. This probes
# both, WITH the same filters applied, to get the real vector_score each
# one actually produces.
FILTERED_STRUCTURED_QUESTIONS = [
    ("How many bugs are currently Open in the Login module?", "Login", "Open"),
    ("How many bugs are currently Open in the Payments module?", "Payments", "Open"),
]

# Round 3: same real bugs, deliberately reworded with NO exact IDs and NO
# corpus jargon -- tests two things in one run: (1) does the proposed
# bm25<10 guardrail threshold false-reject a legitimately in-domain but
# plainly-worded question, and (2) does the ACTUAL retrieval (top_doc,
# not just the score) still surface the right document under paraphrase
# -- i.e. is there any real evidence yet that a query-rewrite/retry node
# is needed. expected_doc is what a correct retrieval should return.
PARAPHRASED_IN_DOMAIN = [
    ("Someone can't sign in even though their password is right, what's going on?", "BUG-1001"),
    ("Why does the checkout process freeze when someone has a bad internet connection?", "BUG-1003"),
    ("Is there a problem where retrying a failed charge ends up billing twice?", "BUG-1023"),
]


def probe(os_client, label: str, questions: list[str]) -> None:
    print(f"\n-- {label} --")
    for q in questions:
        hits = hybrid_search(
            os_client,
            alias=settings.opensearch_index_alias,
            q=q,
            ollama_host=settings.ollama_host,
            ollama_port=settings.ollama_port,
            size=5,
        )
        if not hits:
            print(f"{q!r:70s} NO HITS")
            continue
        top = hits[0]
        bm25 = f"{top['bm25_score']:.3f}" if top.get("bm25_score") is not None else "None"
        vec = f"{top['vector_score']:.4f}" if top.get("vector_score") is not None else "None"
        print(f"{q!r:70s} bm25={bm25:8s} vector={vec:8s} doc={top['external_id']}")


def probe_paraphrased(os_client, label: str, pairs: list[tuple[str, str]]) -> None:
    print(f"\n-- {label} --")
    for q, expected in pairs:
        hits = hybrid_search(
            os_client,
            alias=settings.opensearch_index_alias,
            q=q,
            ollama_host=settings.ollama_host,
            ollama_port=settings.ollama_port,
            size=5,
        )
        if not hits:
            print(f"{q!r:70s} NO HITS -- expected {expected}")
            continue
        top = hits[0]
        bm25 = f"{top['bm25_score']:.3f}" if top.get("bm25_score") is not None else "None"
        vec = f"{top['vector_score']:.4f}" if top.get("vector_score") is not None else "None"
        correct = "OK" if top["external_id"] == expected else f"MISS (expected {expected})"
        print(f"{q!r:70s} bm25={bm25:8s} vector={vec:8s} doc={top['external_id']:10s} {correct}")


def probe_filtered(os_client, label: str, triples: list[tuple[str, str, str]]) -> None:
    print(f"\n-- {label} --")
    for q, module, status in triples:
        hits = hybrid_search(
            os_client,
            alias=settings.opensearch_index_alias,
            q=q,
            ollama_host=settings.ollama_host,
            ollama_port=settings.ollama_port,
            module=module,
            status=status,
            size=5,
        )
        if not hits:
            print(f"{q!r:60s} module={module:10s} NO HITS")
            continue
        top = hits[0]
        bm25 = f"{top['bm25_score']:.3f}" if top.get("bm25_score") is not None else "None"
        vec = f"{top['vector_score']:.4f}" if top.get("vector_score") is not None else "None"
        print(f"{q!r:60s} module={module:10s} bm25={bm25:8s} vector={vec:8s} doc={top['external_id']}")


def main():
    os_client = get_client(settings.opensearch_host, settings.opensearch_port)
    probe(os_client, "IN-DOMAIN, vocab-rich (should score high)", IN_DOMAIN_QUESTIONS)
    probe(os_client, "OUT-OF-DOMAIN (should score low)", OUT_OF_DOMAIN_QUESTIONS)
    probe_paraphrased(os_client, "IN-DOMAIN, PARAPHRASED (real stress test)", PARAPHRASED_IN_DOMAIN)
    probe_filtered(os_client, "IN-DOMAIN, FILTERED/STRUCTURED (regression repro)", FILTERED_STRUCTURED_QUESTIONS)


if __name__ == "__main__":
    main()
