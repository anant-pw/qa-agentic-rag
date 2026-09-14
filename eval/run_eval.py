"""
Runs eval/eval_seed.json against a running /search or /search/hybrid
endpoint and reports Recall@3/5/10 and MRR.

Why these metrics and not others, for THIS corpus specifically (28
documents, gold sets of size 1-3 per question): Recall@10 is close to
meaningless at this scale (retrieving most of the corpus trivially
satisfies it), so Recall@3 is the number that actually discriminates.
MRR captures "was the first relevant document ranked early," which
matters most for the single-gold-document questions. Precision@k is
skipped as a headline metric -- with gold sets this small against
k=5/10, precision is dominated by corpus size, not retrieval quality.

`structured_filter` questions are excluded from ranked-retrieval
scoring (their `requires_docs` is a string, not a doc-ID list -- they
test filtered-count correctness, not ranking, and need separate manual
verification against the live corpus, not a hardcoded expected count
here, since re-running generate_eval_seed.py could change which
module/status pair "wins" the tie noted in phase-02-handoff.md Section 8).

Usage:
    python -m eval.run_eval --endpoint /search
    python -m eval.run_eval --endpoint /search/hybrid
    python -m eval.run_eval --endpoint /search/hybrid --check-err-401-recall-5
"""

import argparse
import json

import httpx


def load_seed(path: str = "eval/eval_seed.json") -> list[dict]:
    with open(path) as f:
        return json.load(f)


def run(base_url: str, endpoint: str, seed: list[dict], size: int = 10) -> list[dict]:
    rows = []
    for q in seed:
        if q["type"] in ("structured_filter", "out_of_domain"):
            continue
        resp = httpx.get(f"{base_url}{endpoint}", params={"q": q["question"], "size": size}, timeout=3000)
        resp.raise_for_status()
        hits = [r["external_id"].upper() for r in resp.json()["results"]]
        gold = set(q["requires_docs"])
        rows.append({"type": q["type"], "question": q["question"], "gold": gold, "hits": hits})
    return rows


def recall_at_k(rows: list[dict], k: int) -> float:
    scores = []
    for r in rows:
        top_k = set(r["hits"][:k])
        found = len(r["gold"] & top_k)
        scores.append(found / len(r["gold"]))
    return sum(scores) / len(scores)


def mrr(rows: list[dict]) -> float:
    scores = []
    for r in rows:
        rr = 0.0
        for rank, hit in enumerate(r["hits"], start=1):
            if hit in r["gold"]:
                rr = 1.0 / rank
                break
        scores.append(rr)
    return sum(scores) / len(scores)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--endpoint", default="/search", choices=["/search", "/search/hybrid"])
    parser.add_argument("--seed", default="eval/eval_seed.json")
    parser.add_argument(
        "--check-err-401-recall-5",
        action="store_true",
        help="Regression check: asserts the ERR_401_UNAUTH aggregation "
             "question's full gold set (BUG-1001, BUG-1024, BUG-1015) is "
             "present at k<=5. This question had a COMPLETE MISS on the "
             "Phase 3 BM25 baseline (BUG-1015 absent from top-10) -- exit "
             "code is nonzero if this regresses, since an averaged "
             "recall/MRR improvement can hide this one question still "
             "failing.",
    )
    args = parser.parse_args()

    seed = load_seed(args.seed)
    rows = run(args.base_url, args.endpoint, seed, size=10)

    print(f"Endpoint: {args.endpoint}")
    print(f"Questions scored (ranked-retrieval, structured_filter and out_of_domain excluded): {len(rows)}")
    print(f"Recall@3:  {recall_at_k(rows, 3):.3f}")
    print(f"Recall@5:  {recall_at_k(rows, 5):.3f}")
    print(f"Recall@10: {recall_at_k(rows, 10):.3f}")
    print(f"MRR:       {mrr(rows):.3f}")
    print()
    for r in rows:
        top5 = set(r["hits"][:5])
        missing = r["gold"] - top5
        flag = "  <-- MISSING FROM TOP 5" if missing else ""
        print(f"[{r['type']}] gold={sorted(r['gold'])} hits@5={r['hits'][:5]}{flag}")

    if args.check_err_401_recall_5:
        target = next(
            (r for r in rows if r["type"] == "error_code_aggregation"
             and r["gold"] == {"BUG-1001", "BUG-1024", "BUG-1015"}),
            None,
        )
        if target is None:
            raise SystemExit(
                "REGRESSION CHECK FAILED: could not find the ERR_401_UNAUTH "
                "aggregation question in eval_seed.json -- has the seed set "
                "changed shape since this check was written?"
            )
        missing = target["gold"] - set(target["hits"][:5])
        if missing:
            raise SystemExit(
                f"REGRESSION CHECK FAILED: ERR_401_UNAUTH question still "
                f"missing {missing} from top-5. This was a complete miss on "
                f"the Phase 3 baseline; Phase 4 hybrid retrieval was "
                f"expected to fix multi-document aggregation recall "
                f"specifically. Do not treat an improved average as "
                f"sufficient evidence this case was fixed."
            )
        print("\nREGRESSION CHECK PASSED: ERR_401_UNAUTH full gold set present at k<=5.")


if __name__ == "__main__":
    main()
