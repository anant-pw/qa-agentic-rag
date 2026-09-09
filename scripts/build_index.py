"""
Builds (or rebuilds) the qa_documents OpenSearch index from Postgres.

Run from the host (not inside a container), matching ingest.py's and
generate_eval_seed.py's convention (Section 9 of phase-02-handoff.md):
Postgres reached via 127.0.0.1:5433 (the remapped host-side port,
Windows port-conflict workaround), NOT `postgres:5432` (that hostname
only resolves inside the Docker network, from the fastapi container).

Usage:
    python scripts/build_index.py
    python scripts/build_index.py --alias qa_documents --keep-previous 1

Prerequisite: run `python ingest.py` first. This script only reads
`documents` / `document_error_codes` / `error_codes` -- it does not
parse CSV/markdown itself, and an empty `documents` table will index
successfully as an empty (0-doc) index without error, which is a
"technically works, definitely wrong" footgun worth knowing about
before you run this on a fresh, unseeded database.

Phase 4: also requires a reachable Ollama with `nomic-embed-text`
pulled (see app/search/embeddings.py). Defaults to
`host.docker.internal:11434` to match this project's established
native-Ollama convention (app/config.py), but this script runs from
the HOST (same convention as ingest.py -- see --pg-host defaulting to
127.0.0.1:5433, not the `postgres` Docker-network hostname), so the
default Ollama target is overridden to `localhost:11434` below --
`host.docker.internal` only resolves from inside a container.
Use --no-embed to rebuild BM25-only (e.g. to confirm Phase 3 behavior
is unaffected without needing Ollama up at all).
"""

import argparse
import os

import psycopg2
from opensearchpy import OpenSearch


from app.search.indexer import rebuild_index


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--alias", default="qa_documents")
    parser.add_argument("--keep-previous", type=int, default=1)
    parser.add_argument("--pg-host", default=os.environ.get("POSTGRES_HOST", "127.0.0.1"))
    parser.add_argument("--pg-port", default=os.environ.get("POSTGRES_PORT", "5433"))
    parser.add_argument("--pg-db", default=os.environ.get("POSTGRES_DB", "rag_db"))
    parser.add_argument("--pg-user", default=os.environ.get("POSTGRES_USER", "rag_user"))
    parser.add_argument("--pg-password", default=os.environ.get("POSTGRES_PASSWORD", "rag_password"))
    parser.add_argument("--os-host", default=os.environ.get("OPENSEARCH_HOST", "localhost"))
    parser.add_argument("--os-port", type=int, default=int(os.environ.get("OPENSEARCH_PORT", 9200)))
    parser.add_argument("--ollama-host", default=os.environ.get("OLLAMA_HOST_LOCAL", "localhost"))
    parser.add_argument("--ollama-port", type=int, default=int(os.environ.get("OLLAMA_PORT", 11434)))
    parser.add_argument("--no-embed", action="store_true",
                         help="Rebuild BM25-only, skip embedding calls entirely "
                              "(no chunk_vector on any document). Use to confirm "
                              "Phase 3 behavior is unaffected without needing Ollama up.")
    args = parser.parse_args()

    pg_conn = psycopg2.connect(
        host=args.pg_host, port=args.pg_port, dbname=args.pg_db,
        user=args.pg_user, password=args.pg_password,
    )
    os_client = OpenSearch(
        hosts=[{"host": args.os_host, "port": args.os_port}],
        use_ssl=False, verify_certs=False,
    )

    ollama_host = None if args.no_embed else args.ollama_host
    ollama_port = None if args.no_embed else args.ollama_port

    try:
        summary = rebuild_index(
            pg_conn, os_client, alias=args.alias, keep_previous=args.keep_previous,
            ollama_host=ollama_host, ollama_port=ollama_port,
        )
        print(f"Indexed {summary['doc_count']} documents into {summary['new_index']}")
        print(f"Alias '{summary['alias']}' now points at {summary['new_index']}")
        if args.no_embed:
            print("Rebuilt WITHOUT embeddings (--no-embed) -- chunk_vector unset on all documents.")
        if summary["retired_indices"]:
            print(f"Retired (or kept for rollback): {summary['retired_indices']}")
    finally:
        pg_conn.close()


if __name__ == "__main__":
    main()
