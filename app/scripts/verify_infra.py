"""
Phase 1 deep verification — goes beyond /health's connectivity checks.

/health proves each service accepts a connection. This proves:
  - Postgres can actually create a table and round-trip a row
  - OpenSearch can create an index, index a doc, and query it back
  - Ollama can generate real output, and how long that takes on this hardware

Run inside the fastapi container:
    docker exec -it rag-fastapi python -m app.scripts.verify_infra

Test fixtures (table, index) are left in place by default so this can be
re-run later as a smoke test. Pass --cleanup to tear them down instead.
"""

import sys
import time
import argparse

import psycopg2
import httpx

from app.config import settings


def verify_postgres() -> bool:
    print("\n[postgres] connecting...")
    try:
        conn = psycopg2.connect(
            host=settings.postgres_host,
            port=settings.postgres_port,
            user=settings.postgres_user,
            password=settings.postgres_password,
            dbname=settings.postgres_db,
            connect_timeout=5,
        )
        conn.autocommit = True
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS phase1_verify (
                id SERIAL PRIMARY KEY,
                note TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)
        print("[postgres] table created/confirmed")

        cur.execute(
            "INSERT INTO phase1_verify (note) VALUES (%s) RETURNING id;",
            ("phase1 verification run",),
        )
        row_id = cur.fetchone()[0]
        print(f"[postgres] inserted row id={row_id}")

        cur.execute("SELECT note FROM phase1_verify WHERE id = %s;", (row_id,))
        result = cur.fetchone()[0]
        assert result == "phase1 verification run"
        print(f"[postgres] read back: '{result}'")

        cur.close()
        conn.close()
        print("[postgres] PASS")
        return True
    except Exception as e:
        print(f"[postgres] FAIL: {e}")
        return False


def verify_opensearch() -> bool:
    print("\n[opensearch] connecting...")
    base_url = f"http://{settings.opensearch_host}:{settings.opensearch_port}"
    index_name = "phase1-verify"
    try:
        # Create index (ignore if it already exists)
        resp = httpx.put(f"{base_url}/{index_name}", timeout=10)
        if resp.status_code not in (200, 400):  # 400 = already exists
            resp.raise_for_status()
        print(f"[opensearch] index '{index_name}' created/confirmed")

        # Index a document
        doc = {"note": "phase1 verification run", "source": "verify_infra.py"}
        resp = httpx.post(f"{base_url}/{index_name}/_doc?refresh=true", json=doc, timeout=10)
        resp.raise_for_status()
        doc_id = resp.json()["_id"]
        print(f"[opensearch] indexed doc id={doc_id}")

        # Query it back (OpenSearch's _search takes a GET with a body, which
        # httpx.get() disallows by design — use request() to force it through)
        resp = httpx.request(
            "GET",
            f"{base_url}/{index_name}/_search",
            json={"query": {"match": {"note": "verification"}}},
            timeout=10,
        )
        resp.raise_for_status()
        hits = resp.json()["hits"]["total"]["value"]
        assert hits >= 1
        print(f"[opensearch] search returned {hits} hit(s)")

        print("[opensearch] PASS")
        return True
    except Exception as e:
        print(f"[opensearch] FAIL: {e}")
        return False


def verify_ollama(model: str = "phi4-mini:latest") -> bool:
    base_url = f"http://{settings.ollama_host}:{settings.ollama_port}"

    # Force the model out of memory first so "cold" is a real cold start,
    # not whatever state Ollama happened to be in from a previous run.
    print(f"\n[ollama] unloading model to force a cold start...")
    try:
        httpx.post(
            f"{base_url}/api/generate",
            json={"model": model, "prompt": "", "keep_alive": 0},
            timeout=15,
        )
    except Exception as e:
        print(f"[ollama] warning: unload request failed ({e}), cold timing may be inaccurate")

    def _timed_generate(label: str) -> float | None:
        try:
            start = time.time()
            resp = httpx.post(
                f"{base_url}/api/generate",
                json={"model": model, "prompt": "Reply with exactly one word: hello", "stream": False},
                timeout=120,
            )
            elapsed = time.time() - start
            resp.raise_for_status()
            output = resp.json().get("response", "").strip()
            print(f"[ollama] {label}: '{output}' in {elapsed:.2f}s")
            return elapsed
        except Exception as e:
            print(f"[ollama] {label} FAIL: {e}")
            return None

    cold = _timed_generate("cold")
    warm = _timed_generate("warm")

    if cold is None or warm is None:
        print("[ollama] FAIL")
        return False

    print(f"[ollama] cold={cold:.2f}s  warm={warm:.2f}s  "
          f"(gap={cold - warm:.2f}s — this is your real cost of an idle model)")
    if cold > 15:
        print("[ollama] WARNING: cold start >15s — first query in any new session "
              "will feel slow regardless of how fast steady-state is")

    print("[ollama] PASS")
    return True


def cleanup():
    print("\n[cleanup] removing test fixtures...")
    try:
        conn = psycopg2.connect(
            host=settings.postgres_host,
            port=settings.postgres_port,
            user=settings.postgres_user,
            password=settings.postgres_password,
            dbname=settings.postgres_db,
            connect_timeout=5,
        )
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("DROP TABLE IF EXISTS phase1_verify;")
        cur.close()
        conn.close()
        print("[cleanup] postgres table dropped")
    except Exception as e:
        print(f"[cleanup] postgres cleanup failed: {e}")

    try:
        base_url = f"http://{settings.opensearch_host}:{settings.opensearch_port}"
        httpx.delete(f"{base_url}/phase1-verify", timeout=10)
        print("[cleanup] opensearch index dropped")
    except Exception as e:
        print(f"[cleanup] opensearch cleanup failed: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cleanup", action="store_true",
                         help="Tear down test fixtures after verification instead of leaving them")
    parser.add_argument("--model", default="phi4-mini:latest",
                         help="Ollama model to test generation with")
    args = parser.parse_args()

    results = {
        "postgres": verify_postgres(),
        "opensearch": verify_opensearch(),
        "ollama": verify_ollama(args.model),
    }

    if args.cleanup:
        cleanup()

    print("\n" + "=" * 40)
    print("SUMMARY")
    for service, passed in results.items():
        print(f"  {service}: {'PASS' if passed else 'FAIL'}")
    print("=" * 40)

    sys.exit(0 if all(results.values()) else 1)
