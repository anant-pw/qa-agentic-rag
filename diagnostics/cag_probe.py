"""
Bounded experiment: cache-augmented generation (CAG) + context compaction
(docs/NEXT_STEPS_PLAN.md Step 9, ideas 1 + 5). Diagnostic only -- does not
touch the app.

Idea: the corpus is small enough to put ENTIRELY into the model's context
once. With the system prompt + whole corpus as a fixed prefix, the server's
prompt cache means each question only prefills its own few tokens, so time to
first token should drop from ~27 s (RAG: system prefix cached, ~700 context
tokens prefilled per question) to a few seconds.

Compaction (idea 5), measured on the corpus before writing this:
  - all 120 synthetic bugs share one templated steps text ("Open the X page
    on Y. Reproduce the condition described above. Observe the incorrect
    behavior.") -> dropped;
  - 104/120 synthetic descriptions are the title minus its "Module: " prefix
    -> dropped when identical, kept otherwise;
  - original docs and all test cases keep every field.
Recorded cross-references are included, grouped per source document.

Runs against a separately started llama-server (the one bundled with Ollama),
using the same qwen3:4b-instruct GGUF blob Ollama uses:

    llama-server.exe -m <blob> --port 8091 -c 32768 -np 1 -fa on \
        -ctk q8_0 -ctv q8_0 --slot-save-path <dir> --no-webui

Then:
    python -m diagnostics.cag_probe --stats            # corpus size only
    python -m diagnostics.cag_probe --run              # prime, save slot, ask

Accept criteria (set before the run, docs/CAG_EXPERIMENT_2026-10-01.md):
holdout LLM-path >= 31/36 with near-duplicate 7/7; no new FAIL on the 9
frozen-seed LLM questions; median first token <= 10 s; median total <= 48 s.
"""

import argparse
import json
import os
import re
import time
from collections import defaultdict

import httpx
import psycopg2

from app.generation.context import REFERENCE_TYPE_PHRASING
from app.generation.prompt import SYSTEM_PROMPT
from eval.run_holdout import grade

SERVER = os.environ.get("CAG_SERVER", "http://127.0.0.1:8091")
OUT_DIR = os.path.join("eval", "runs", "cag_2026-10-01")
SYNTH = re.compile(r"^(BUG|TC)-2\d{3}$")
LLM_SEED_TYPES = {"cross_reference_bug_to_test_case", "duplicate_resolution", "related_resolution",
                  "dangling_reference_citation", "resolution_in_narrative"}


def _env(key: str, default: str) -> str:
    if key in os.environ:
        return os.environ[key]
    try:
        with open(".env", encoding="utf-8") as f:
            for line in f:
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return default


def pg_connect():
    return psycopg2.connect(
        host="127.0.0.1", port=int(_env("POSTGRES_PORT", "5433")),
        user=_env("POSTGRES_USER", "rag_user"), password=_env("POSTGRES_PASSWORD", "rag_password"),
        dbname=_env("POSTGRES_DB", "rag_db"),
    )


def build_corpus(conn) -> str:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT d.external_id, d.title, d.doc_type, d.module, d.status,
                   br.description, br.steps_to_reproduce, tc.preconditions, tc.steps, tc.expected_result
            FROM documents d
            LEFT JOIN bug_reports br ON br.document_id = d.id
            LEFT JOIN test_cases tc ON tc.document_id = d.id
            ORDER BY d.doc_type, d.external_id
        """)
        docs = cur.fetchall()
        cur.execute("""
            SELECT ds.external_id, r.reference_type, COALESCE(dt.external_id, r.target_external_id || ' (not found in corpus)')
            FROM document_references r
            JOIN documents ds ON ds.id = r.source_document_id
            LEFT JOIN documents dt ON dt.id = r.target_document_id
            ORDER BY 1, 2, 3
        """)
        refs = defaultdict(lambda: defaultdict(list))
        for src, rtype, tgt in cur.fetchall():
            refs[src][rtype].append(tgt)

    blocks = []
    for ext, title, doc_type, module, status, desc, repro, pre, steps, expected in docs:
        lines = [f"[{ext}] {title} (doc_type={doc_type}, module={module}, status={status})"]
        synthetic = bool(SYNTH.match(ext))
        if doc_type == "bug_report":
            title_body = re.sub(r"^[A-Za-z]+: ", "", title)
            if desc and not (synthetic and desc.strip().rstrip(".") == title_body):
                lines.append(f"Description: {desc.strip()}")
            if repro and not synthetic:
                lines.append(f"Steps to reproduce: {repro.strip()}")
        else:
            lines.append(f"Preconditions: {(pre or '(none recorded)').strip()}")
            lines.append(f"Steps: {(steps or '(none recorded)').strip()}")
            lines.append(f"Expected result: {(expected or '(none recorded)').strip()}")
        for rtype, targets in refs.get(ext, {}).items():
            lines.append(f"Recorded cross-reference: {ext} {REFERENCE_TYPE_PHRASING.get(rtype, rtype)} {', '.join(targets)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def messages_for(corpus: str, question: str, filters: dict | None = None) -> list[dict]:
    note = ""
    if filters:
        note = "\n(Only consider documents with " + ", ".join(f"{k}={v}" for k, v in filters.items()) + ".)"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Retrieved documents:\n\n{corpus}\n\nQuestion: {question}{note}"},
    ]


def ask(client: httpx.Client, msgs: list[dict], max_tokens: int = 400) -> dict:
    t0 = time.perf_counter()
    first = None
    parts, timings = [], {}
    body = {"messages": msgs, "stream": True, "temperature": 0.1, "max_tokens": max_tokens,
            "cache_prompt": True, "id_slot": 0, "timings_per_token": False}
    with client.stream("POST", f"{SERVER}/v1/chat/completions", json=body) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line.startswith("data: ") or line.strip() == "data: [DONE]":
                continue
            chunk = json.loads(line[6:])
            if chunk.get("timings"):
                timings = chunk["timings"]
            for choice in chunk.get("choices", []):
                piece = (choice.get("delta") or {}).get("content") or ""
                if piece:
                    if first is None:
                        first = time.perf_counter() - t0
                    parts.append(piece)
    return {"answer": "".join(parts), "first_token_s": round(first or 0, 2),
            "total_s": round(time.perf_counter() - t0, 2),
            "prompt_n": timings.get("prompt_n"), "prompt_ms": timings.get("prompt_ms"),
            "predicted_n": timings.get("predicted_n"), "predicted_ms": timings.get("predicted_ms"),
            "cache_n": timings.get("cache_n")}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--stats", action="store_true")
    p.add_argument("--run", action="store_true")
    args = p.parse_args()

    conn = pg_connect()
    corpus = build_corpus(conn)
    client = httpx.Client(timeout=3600)
    toks = client.post(f"{SERVER}/tokenize", json={"content": corpus}).json()["tokens"]
    sys_toks = client.post(f"{SERVER}/tokenize", json={"content": SYSTEM_PROMPT}).json()["tokens"]
    print(f"corpus: {len(corpus):,} chars, {len(toks):,} tokens; system prompt {len(sys_toks):,} tokens")
    if not args.run:
        return

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "corpus.txt"), "w", encoding="utf-8") as f:
        f.write(corpus)
    run = {"corpus_tokens": len(toks), "system_tokens": len(sys_toks)}

    prime = ask(client, messages_for(corpus, "Reply with the single word OK."), max_tokens=2)
    run["prime"] = prime
    print(f"prime: {prime}")
    t = time.perf_counter()
    saved = client.post(f"{SERVER}/slots/0?action=save", json={"filename": "cag_qwen3_4b_instruct.bin"}).json()
    run["slot_save"] = {"wall_s": round(time.perf_counter() - t, 2), "response": saved}
    print(f"slot save: {run['slot_save']}")

    holdout = {i["id"]: i for i in json.load(open("eval/holdout.json", encoding="utf-8"))}
    llm_ids = [r["id"] for r in json.load(open(
        "eval/runs/holdout_2026-10-01_phi4-14b/holdout_results.json", encoding="utf-8"))["results"]]
    results = []
    for n, hid in enumerate(llm_ids, 1):
        item = holdout[hid]
        res = ask(client, messages_for(corpus, item["question"], item.get("filters")))
        res.update(id=hid, category=item["category"], problems=grade(item, res["answer"]))
        res["result"] = "PASS" if not res["problems"] else "FAIL"
        results.append(res)
        print(f"[holdout {n}/{len(llm_ids)}] {hid} {res['result']} ttft={res['first_token_s']}s "
              f"total={res['total_s']}s prefill={res['prompt_n']} cached={res['cache_n']} {res['problems'] or ''}", flush=True)
        json.dump({**run, "holdout": results}, open(os.path.join(OUT_DIR, "results.json"), "w", encoding="utf-8"),
                  indent=2, ensure_ascii=False)

    from eval.run_eval_generation import CHECKERS
    seed = [q for q in json.load(open("eval/eval_seed.json", encoding="utf-8")) if q["type"] in LLM_SEED_TYPES]
    seed_results = []
    for n, q in enumerate(seed, 1):
        res = ask(client, messages_for(corpus, q["question"]))
        ok, detail = CHECKERS[q["type"]](q, res["answer"], conn)
        res.update(question=q["question"], type=q["type"], result="PASS" if ok else "FAIL", detail=detail)
        seed_results.append(res)
        print(f"[seed {n}/{len(seed)}] {q['type']} {res['result']} ttft={res['first_token_s']}s total={res['total_s']}s", flush=True)
        json.dump({**run, "holdout": results, "seed": seed_results},
                  open(os.path.join(OUT_DIR, "results.json"), "w", encoding="utf-8"), indent=2, ensure_ascii=False)

    ttft = sorted(r["first_token_s"] for r in results + seed_results)
    tot = sorted(r["total_s"] for r in results + seed_results)
    print(f"\nholdout LLM-path: {sum(r['result']=='PASS' for r in results)}/{len(results)}"
          f" | near-dup: {sum(r['result']=='PASS' for r in results if r['category']=='near_duplicate_discrimination')}/7"
          f" | seed LLM: {sum(r['result']=='PASS' for r in seed_results)}/{len(seed_results)}"
          f" | median ttft {ttft[len(ttft)//2]}s | median total {tot[len(tot)//2]}s")


if __name__ == "__main__":
    main()
