"""
Bounded experiment: module-partitioned, pre-encoded contexts ("module KV
cache"). Diagnostic only -- does not touch the app.

What yesterday's CAG experiment measured (docs/CAG_EXPERIMENT_2026-10-01.md):
  - llama-server slot save/restore is nearly free (1.5 GB restored in 1.0 s);
  - reading context is the cost: ~25 s per question for ~700 retrieved tokens;
  - decode stays fast only with a SMALL context (~6 tok/s at ~2k tokens,
    ~1 tok/s at 19k), which is why whole-corpus CAG failed.

This probe keeps contexts small and still skips the per-question read:
  offline  one context per MODULE (system prompt + every document of that
           module, compacted as in cag_probe.py) is prefilled once and its
           KV state saved to disk -- 10 files;
  online   normal retrieval picks the module (explicit filter, else the
           module of the named IDs, else the module of the top hybrid hit);
           that slot file is restored and only the question is prefilled.
If the named IDs span two modules the question is "not covered" and would
use the normal RAG path in a real integration; it is reported, not hidden.

Server (the llama-server bundled with Ollama, same GGUF blob Ollama uses):
    llama-server.exe -m <blob> --port 8091 -c 8192 -np 1 -fa on \
        --slot-save-path <dir> --no-webui

    python -m diagnostics.module_cache_probe --build     # prime + save 10 slots
    python -m diagnostics.module_cache_probe --run       # holdout LLM-path + 9 seed questions

Accept criteria (set before the run): holdout >= 31/36 with near-duplicate
7/7; seed 9/9; median first token <= 5 s; median total <= 20 s.
"""

import argparse
import json
import os
import re
import time
from collections import defaultdict

import httpx

from app.generation.context import REFERENCE_TYPE_PHRASING
from app.search.id_resolution import extract_document_ids
from diagnostics.cag_probe import LLM_SEED_TYPES, SERVER, SYNTH, ask, messages_for, pg_connect
from eval.run_holdout import grade

APP = os.environ.get("EVAL_BASE_URL", "http://127.0.0.1:8000")
LABEL = os.environ.get("MODULE_CACHE_LABEL", "qwen3-4b-instruct")
# "module": one context per module (first run: 29/36 -- up to 30 near-identical
# docs per context cost the 4B model precision: wrong ID cited, doc overlooked).
# "family": each module split into its original documents and its synthetic
# family, so a context holds ~5-15 documents, close to what top-5 RAG shows.
PARTITION = os.environ.get("MODULE_CACHE_PARTITION", "module")
OUT_DIR = os.path.join("eval", "runs", f"module_cache_2026-10-04_{LABEL}" + ("" if PARTITION == "module" else f"_{PARTITION}"))


def key_of(external_id: str, module: str) -> str:
    if PARTITION == "module":
        return module
    return f"{module}/{'synthetic' if SYNTH.match(external_id) else 'original'}"


def slot_file(module: str) -> str:
    return f"{PARTITION}_{LABEL}_{re.sub(r'[^A-Za-z0-9]', '_', module)}.bin"


def build_module_contexts(conn) -> dict[str, str]:
    """{module: compacted context of every document in that module}. Same
    compaction rules as cag_probe.build_corpus()."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT d.external_id, d.title, d.doc_type, d.module, d.status,
                   br.description, br.steps_to_reproduce, tc.preconditions, tc.steps, tc.expected_result
            FROM documents d
            LEFT JOIN bug_reports br ON br.document_id = d.id
            LEFT JOIN test_cases tc ON tc.document_id = d.id
            ORDER BY d.module, d.doc_type, d.external_id
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

    by_module: dict[str, list[str]] = defaultdict(list)
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
        by_module[key_of(ext, module)].append("\n".join(lines))
    return {m: "\n\n".join(blocks) for m, blocks in by_module.items()}


def id_modules(conn, ids: list[str]) -> set[str]:
    """Context keys (see key_of) of the documents named in the question."""
    if not ids:
        return set()
    with conn.cursor() as cur:
        cur.execute("SELECT external_id, module FROM documents WHERE lower(external_id) = ANY(%s)", (ids,))
        return {key_of(ext, module) for ext, module in cur.fetchall()}


def route(conn, client: httpx.Client, question: str, filters: dict | None) -> tuple[str | None, str, float]:
    """(module or None if not covered, how it was decided, seconds spent)."""
    t = time.perf_counter()
    if PARTITION == "module" and filters and filters.get("module"):
        return filters["module"], "explicit filter", time.perf_counter() - t
    mods = id_modules(conn, extract_document_ids(question))
    if len(mods) == 1:
        return mods.pop(), "named ID", time.perf_counter() - t
    if len(mods) > 1:
        return None, f"IDs span modules {sorted(mods)}", time.perf_counter() - t
    params = {"q": question, "size": 5, **{k: v for k, v in (filters or {}).items() if v}}
    hits = client.get(f"{APP}/search/hybrid", params=params).json()
    hits = hits.get("results", hits) if isinstance(hits, dict) else hits
    if not hits:
        return None, "no hits", time.perf_counter() - t
    how = "top hybrid hit" + (" within filter" if filters else "")
    return key_of(hits[0]["external_id"], hits[0]["module"]), how, time.perf_counter() - t


def filter_note(filters: dict | None) -> dict | None:
    """The module is already implied by the chosen context; pass the rest."""
    rest = {k: v for k, v in (filters or {}).items() if k != "module" and v}
    return rest or None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--build", action="store_true")
    p.add_argument("--run", action="store_true")
    p.add_argument("--limit", type=int, default=0, help="only the first N holdout questions (and no seed)")
    args = p.parse_args()

    conn = pg_connect()
    contexts = build_module_contexts(conn)
    client = httpx.Client(timeout=3600)
    os.makedirs(OUT_DIR, exist_ok=True)

    if args.build:
        build = []
        for module, ctx in contexts.items():
            n_tok = len(client.post(f"{SERVER}/tokenize", json={"content": ctx}).json()["tokens"])
            prime = ask(client, messages_for(ctx, "Reply with the single word OK."), max_tokens=2)
            t = time.perf_counter()
            saved = client.post(f"{SERVER}/slots/0?action=save", json={"filename": slot_file(module)}).json()
            row = {"module": module, "context_tokens": n_tok, "prime_s": prime["total_s"], "prefilled": prime["prompt_n"],
                   "reused": prime["cache_n"], "save_s": round(time.perf_counter() - t, 2),
                   "file_mb": round(saved.get("n_written", 0) / 1e6, 1)}
            build.append(row)
            print(row, flush=True)
        json.dump(build, open(os.path.join(OUT_DIR, "build.json"), "w"), indent=2)
        print(f"total prime {sum(r['prime_s'] for r in build):.0f}s, disk {sum(r['file_mb'] for r in build):.0f} MB")

    if not args.run:
        return

    def answer(question: str, filters: dict | None) -> dict:
        module, how, route_s = route(conn, client, question, filters)
        if module is None:
            return {"covered": False, "route": how, "answer": ""}
        t = time.perf_counter()
        client.post(f"{SERVER}/slots/0?action=restore", json={"filename": slot_file(module)}).raise_for_status()
        restore_s = time.perf_counter() - t
        res = ask(client, messages_for(contexts[module], question, filter_note(filters)))
        overhead = route_s + restore_s
        res.update(covered=True, module=module, route=how, route_s=round(route_s, 2), restore_s=round(restore_s, 2),
                   e2e_first_token_s=round(res["first_token_s"] + overhead, 2), e2e_total_s=round(res["total_s"] + overhead, 2))
        return res

    holdout = {i["id"]: i for i in json.load(open("eval/holdout.json", encoding="utf-8"))}
    llm_ids = [r["id"] for r in json.load(open(
        "eval/runs/holdout_2026-10-01_phi4-14b/holdout_results.json", encoding="utf-8"))["results"]]
    if args.limit:
        llm_ids = llm_ids[:args.limit]
    results, seed_results = [], []

    def dump():
        json.dump({"label": LABEL, "holdout": results, "seed": seed_results},
                  open(os.path.join(OUT_DIR, "results.json"), "w", encoding="utf-8"), indent=2, ensure_ascii=False)

    for n, hid in enumerate(llm_ids, 1):
        item = holdout[hid]
        res = answer(item["question"], item.get("filters"))
        res.update(id=hid, category=item["category"])
        if res["covered"]:
            res["problems"] = grade(item, res["answer"])
            res["result"] = "PASS" if not res["problems"] else "FAIL"
            print(f"[holdout {n}/{len(llm_ids)}] {hid} {res['result']} module={res['module']} ({res['route']}) "
                  f"ttft={res['e2e_first_token_s']}s total={res['e2e_total_s']}s restore={res['restore_s']}s "
                  f"prefill={res['prompt_n']} {res['problems'] or ''}", flush=True)
        else:
            res["result"] = "NOT_COVERED"
            print(f"[holdout {n}/{len(llm_ids)}] {hid} NOT_COVERED ({res['route']})", flush=True)
        results.append(res)
        dump()

    if not args.limit:
        from eval.run_eval_generation import CHECKERS
        seed = [q for q in json.load(open("eval/eval_seed.json", encoding="utf-8")) if q["type"] in LLM_SEED_TYPES]
        for n, q in enumerate(seed, 1):
            res = answer(q["question"], None)
            res.update(question=q["question"], type=q["type"])
            if res["covered"]:
                ok, detail = CHECKERS[q["type"]](q, res["answer"], conn)
                res.update(result="PASS" if ok else "FAIL", detail=detail)
                print(f"[seed {n}/{len(seed)}] {q['type']} {res['result']} module={res['module']} "
                      f"ttft={res['e2e_first_token_s']}s total={res['e2e_total_s']}s", flush=True)
            else:
                res["result"] = "NOT_COVERED"
                print(f"[seed {n}/{len(seed)}] {q['type']} NOT_COVERED ({res['route']})", flush=True)
            seed_results.append(res)
            dump()

    cov = [r for r in results + seed_results if r["covered"]]
    ttft = sorted(r["e2e_first_token_s"] for r in cov)
    tot = sorted(r["e2e_total_s"] for r in cov)
    hp = sum(r["result"] == "PASS" for r in results)
    nd = sum(r["result"] == "PASS" for r in results if r["category"] == "near_duplicate_discrimination")
    print(f"\nholdout PASS {hp}/{len(results)} (not covered: {sum(r['result']=='NOT_COVERED' for r in results)})"
          f" | near-dup {nd} | seed PASS {sum(r['result']=='PASS' for r in seed_results)}/{len(seed_results)}"
          f" | median e2e first token {ttft[len(ttft)//2]}s | median e2e total {tot[len(tot)//2]}s"
          f" | max total {tot[-1]}s")


if __name__ == "__main__":
    main()
