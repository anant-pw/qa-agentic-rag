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

Run from inside the fastapi container (needs the `opensearch` hostname to
resolve, same reason verify_infra.py and groq_probe.py are run this way):
    docker compose up -d --build fastapi
    docker exec -it rag-fastapi python -m diagnostics.rrf_threshold_probe
"""

from app.search.service import get_client, hybrid_search
from app.config import settings

# Deliberately nothing-to-do-with-QA-or-bugs, varied phrasing/length, so a
# single lucky/unlucky question doesn't decide the threshold alone.
PROBE_QUESTIONS = [
    "What's the weather like today?",
    "Write me a short poem about the ocean.",
    "What is the capital of France?",
    "How do I bake a chocolate cake?",
    "Explain quantum entanglement in simple terms.",
]


def main():
    os_client = get_client(settings.opensearch_host, settings.opensearch_port)
    print(f"{'question':55s} top_rrf   top_doc")
    print("-" * 90)
    for q in PROBE_QUESTIONS:
        hits = hybrid_search(
            os_client,
            alias=settings.opensearch_index_alias,
            q=q,
            ollama_host=settings.ollama_host,
            ollama_port=settings.ollama_port,
            size=5,
        )
        top_score = hits[0]["rrf_score"] if hits else None
        top_doc = hits[0]["external_id"] if hits else None
        score_str = f"{top_score:.4f}" if top_score is not None else "NO HITS"
        print(f"{q!r:55s} {score_str:9s} {top_doc}")


if __name__ == "__main__":
    main()
