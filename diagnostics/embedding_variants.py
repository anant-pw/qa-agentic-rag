"""
DIAGNOSTIC ONLY. Tests one specific hypothesis from the fingerprint run:

  Machine A (Ollama 0.34.0) and B (Ollama 0.18.2) embed the plain lowercase
  string "test cases" to within ~1e-5 (float noise) but embed
  "What are the steps to reproduce BUG-1003?" and long documents
  differently at the 2nd decimal. If the gap comes from text handling
  (case, punctuation, digits) rather than hardware, then on ONE of the
  machines the variants below will NOT collapse onto their base string.

Read it per machine: cos(variant, base) == 1.000000 means that machine
treats the variant as the same text (e.g. an uncased tokenizer path).
Compare the two machines' columns. No fix is implied by this script.

Run WITHOUT rebuilding:
    docker cp diagnostics/embedding_variants.py rag-fastapi:/app/diagnostics/embedding_variants.py
    docker exec -it rag-fastapi python -m diagnostics.embedding_variants
"""

import math

import httpx

from app.config import settings
from app.search.embeddings import embed_query

# (base, variant, what it isolates)
PAIRS = [
    ("test cases", "Test Cases", "case, plain words"),
    ("test cases", "TEST CASES", "case, plain words (all caps)"),
    ("test cases", "test cases?", "trailing punctuation"),
    ("bug-1011", "BUG-1011", "case, ID with hyphen+digits"),
    ("bug 1011", "bug-1011", "hyphen vs space"),
    ("steps to reproduce bug-1003", "What are the steps to reproduce BUG-1003?", "phrasing + case + punctuation"),
]


def _cos(a, b):
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def main() -> None:
    base_url = f"http://{settings.ollama_host}:{settings.ollama_port}"
    try:
        print("ollama /api/version :", httpx.get(f"{base_url}/api/version", timeout=10).json())
    except Exception as e:
        print("ollama /api/version : unavailable", e)
    for base, variant, note in PAIRS:
        vb = embed_query(base, settings.ollama_host, settings.ollama_port)
        vv = embed_query(variant, settings.ollama_host, settings.ollama_port)
        print(f"cos={_cos(vb, vv):.6f}  {base!r} vs {variant!r}   [{note}]")
        print(f"    base    first4: {' '.join(f'{x:+.6f}' for x in vb[:4])}")
        print(f"    variant first4: {' '.join(f'{x:+.6f}' for x in vv[:4])}")


if __name__ == "__main__":
    main()
