"""
DIAGNOSTIC ONLY -- not part of the production system, not wired into
/generate, not added to docker-compose.yml or requirements.txt as a new
runtime dependency (uses httpx, already required).

Purpose: isolate ONE variable -- is phi4-mini's repeated hedging on
resolution/fix questions (Phase 6's Rule 2/2b investigation) a genuine
model-capacity ceiling, or something about this project's prompt/context
construction that a larger model would also fail on? Three prompt
variants already failed to change phi4-mini's behavior on BUG-1017 --
before concluding "model capacity," this runs the SAME retrieval, the
SAME context blocks, the SAME system prompt through a much larger model
(Groq-hosted openai/gpt-oss-120b) and prints both answers side by side.

Deliberately does NOT touch retrieval or embeddings -- hybrid_search()
still calls native Ollama for the query embedding, exactly as production
does. Only the final generation call is swapped. If retrieval/context
construction were the actual problem, both models would fail identically;
if it's phi4-mini's capacity, only the small model should fail.

This is local-first in spirit even though Groq is cloud: it's a one-time
diagnostic run, not a standing dependency the system needs to function.
If Groq's answer is materially better, that's evidence for the Phase 6
handoff's "known limitation" writeup -- it does NOT mean swapping the
production LLM to Groq, which would break the project's actual
local-first requirement and its explicit non-negotiable stated in
production-agentic-rag-project-brief.md. Keep this script, delete the
temptation to wire it in permanently.

Requires GROQ_API_KEY in the environment (get one at console.groq.com --
Groq has a permanent free tier, no card required as of this writing).
Not added to .env.example -- this is a manual, occasional diagnostic, not
part of the stack's normal configuration surface.

Run from the project root (needs `app` on sys.path, same reason
scripts/build_index.py is invoked with -m, not by path):
    GROQ_API_KEY=... python -m diagnostics.groq_probe --question \
        "What was the resolution or fix for BUG-1017 (Admin bulk export - resolved, follow-up to BUG-1007)?"
"""

import argparse
import os

import httpx
import psycopg2
from opensearchpy import OpenSearch

from app.search.service import get_client, hybrid_search
from app.generation.context import fetch_structured_fields, fetch_references
from app.generation.prompt import build_messages, format_doc_context, SYSTEM_PROMPT
from app.generation.llm import stream_chat

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "openai/gpt-oss-120b"  # current Groq general-purpose/reasoning
# recommendation as of this writing -- llama-3.3-70b-versatile is being
# deprecated on Groq, do not silently fall back to it if this breaks;
# check https://console.groq.com/docs/deprecations for the current list
# before assuming the model id below still exists.


def build_real_context(question: str, pg_host, pg_port, pg_db, pg_user, pg_password,
                        os_host, os_port, ollama_host, ollama_port, alias) -> tuple[list[dict], list[str]]:
    """Reuses the EXACT production functions (app.search.service,
    app.generation.context, app.generation.prompt) to reproduce what
    app/routers/generate.py would build for this question -- same
    retrieval, same doc_type-aware context blocks, same system prompt.
    Only the final model call is swapped by the caller."""
    os_client = get_client(os_host, os_port)
    hits = hybrid_search(
        os_client, alias=alias, q=question,
        ollama_host=ollama_host, ollama_port=ollama_port, size=10,
    )
    top_hits = hits[:5]  # matches generate.py's CONTEXT_TOP_N

    pg_conn = psycopg2.connect(host=pg_host, port=pg_port, dbname=pg_db, user=pg_user, password=pg_password)
    try:
        doc_ids = [h["parent_document_id"] for h in top_hits]
        structured_by_id = fetch_structured_fields(pg_conn, doc_ids)
        references_by_id = fetch_references(pg_conn, doc_ids)
    finally:
        pg_conn.close()

    context_blocks = [
        format_doc_context(
            h,
            structured_by_id.get(h["parent_document_id"], {}),
            references_by_id.get(h["parent_document_id"], []),
        )
        for h in top_hits
    ]
    return context_blocks, [h["external_id"] for h in top_hits]


def call_groq(messages: list[dict]) -> str:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY not set in environment")
    resp = httpx.post(
        GROQ_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": GROQ_MODEL, "messages": messages, "temperature": 0.1},
        # temperature 0.1 to match this project's own Ollama pinning
        # convention (Phase 5 decision, see project memory) -- keeps the
        # comparison about model capacity, not about one side being more
        # random than the other.
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def call_ollama(messages: list[dict], ollama_host: str, ollama_port: int, model: str = "phi4-mini:latest") -> str:
    return "".join(stream_chat(messages, ollama_host, ollama_port, model=model))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--question", required=True)
    parser.add_argument("--pg-host", default=os.environ.get("POSTGRES_HOST", "127.0.0.1"))
    parser.add_argument("--pg-port", default=os.environ.get("POSTGRES_PORT", "5433"))
    parser.add_argument("--pg-db", default=os.environ.get("POSTGRES_DB", "rag_db"))
    parser.add_argument("--pg-user", default=os.environ.get("POSTGRES_USER", "rag_user"))
    parser.add_argument("--pg-password", default=os.environ.get("POSTGRES_PASSWORD", "rag_password"))
    parser.add_argument("--os-host", default=os.environ.get("OPENSEARCH_HOST", "localhost"))
    parser.add_argument("--os-port", type=int, default=int(os.environ.get("OPENSEARCH_PORT", 9200)))
    parser.add_argument("--ollama-host", default=os.environ.get("OLLAMA_HOST", "localhost"))
    parser.add_argument("--ollama-port", type=int, default=int(os.environ.get("OLLAMA_PORT", 11434)))
    parser.add_argument("--ollama-model", default=os.environ.get("OLLAMA_CHAT_MODEL", "phi4-mini:latest"),
                         help="Try e.g. gpt-oss:20b, llama3.1:8b, or phi4:14b to compare against the "
                              "default -- see KNOWN_LIMITATIONS_generation.md for why this matters.")
    parser.add_argument("--alias", default=os.environ.get("OPENSEARCH_INDEX_ALIAS", "qa_documents"))
    args = parser.parse_args()

    context_blocks, doc_ids = build_real_context(
        args.question, args.pg_host, args.pg_port, args.pg_db, args.pg_user, args.pg_password,
        args.os_host, args.os_port, args.ollama_host, args.ollama_port, args.alias,
    )
    messages = build_messages(args.question, context_blocks)

    print(f"Retrieved (top 5): {doc_ids}\n")
    print("=" * 60)
    print(f"OLLAMA ({args.ollama_model}) answer:")
    print("=" * 60)
    print(call_ollama(messages, args.ollama_host, args.ollama_port, args.ollama_model))

    print("\n" + "=" * 60)
    print(f"GROQ ({GROQ_MODEL}) answer, SAME messages/system prompt:")
    print("=" * 60)
    print(call_groq(messages))
