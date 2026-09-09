# Phase 6 Verification — commands to run for real, output not yet captured

Nothing below has been executed against live infra in this chat -- same
discipline as every prior phase's handoff. Run these locally and paste
real output back before Phase 6 is called done.

## 0. Apply file changes
- `app/config.py` — added `redis_host`/`redis_port`
- `app/routers/generate.py` — cache + logging wired in, streaming contract preserved (cache-hit delivers as one chunk, not token-by-token — see module docstring)
- `app/generation/cache.py`, `app/observability/logger.py` — new
- `generate_eval_seed.py` — 5 new question-generating functions
- `eval/eval_seed.json` — extended to 17 questions (PREDICTED via CSV simulation, same method Phase 2 used before its live-DB run — re-generate for real, see step 2)
- `eval/run_eval_generation.py`, `eval/manual_eval_log.py` — new
- `docker-compose.yml` — added `redis` service, `REDIS_HOST`/`REDIS_PORT` on `fastapi`
- `requirements.txt` — added `redis` (also fixed a pre-existing missing-newline bug that had silently concatenated `opensearch-py==2.7.1` and a would-be `redis` line into one bad requirement)

```bash
pip install -r requirements.txt --break-system-packages
docker compose up -d   # brings up the new redis container too
```

## 1. Redis reachable
```bash
docker exec -it rag-redis redis-cli ping
# expect: PONG
```

## 2. Regenerate eval_seed.json for real (don't trust the predicted file as final)
```bash
python generate_eval_seed.py
python -c "import json; d=json.load(open('eval/eval_seed.json')); print(len(d)); [print(q['type']) for q in d]"
# expect: 17, with dangling_reference_citation and resolution_in_narrative
# present exactly once each, and two structured_filter / four
# single_doc_factual entries. If the count or types differ from the
# predicted file, the live DB is correct — update accordingly, don't
# force it to match this document.
```

## 3. Tier A deterministic generation eval
```bash
python -m eval.run_eval_generation
```
Expect a PASS/FAIL/MANUAL_REQUIRED line per question. `single_doc_factual`
questions will show `MANUAL_REQUIRED` — that's correct, not a bug, use
step 5. Everything else should resolve to a real PASS or FAIL against
live Postgres + live `/generate` output — no fabricated numbers belong
in the handoff until this has actually run.

Two questions specifically worth reading closely the first time, since
they're the two gaps this phase exists to close:
- `dangling_reference_citation` (BUG-1013/TC-0209) — does the model say
  TC-0209 is cited but not in the corpus, or does it invent content for it?
- `resolution_in_narrative` (BUG-1017) — does the model correctly report
  the fix from `description`, or does it wrongly emit Rule 2's abstention
  sentence because there's no separate `Resolution:` field?

## 4. Cache behavior
```bash
# First call — expect a normal token-by-token stream, several seconds
time curl -s -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{"question": "What are the steps to reproduce BUG-1003?"}'

# Second, identical call — expect near-instant, single-chunk delivery
time curl -s -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{"question": "What are the steps to reproduce BUG-1003?"}'

# no_cache override — expect a full regeneration again, not the cached hit
time curl -s -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{"question": "What are the steps to reproduce BUG-1003?", "no_cache": true}'
```
Acceptance: call 2 is dramatically faster than call 1 and call 3, and
call 3's latency is back in line with call 1's (proving `no_cache`
actually bypasses the cache rather than just relabeling a hit).

## 5. Cache busts on index rebuild
```bash
python scripts/build_index.py   # new concrete index name behind the alias
# repeat the "second, identical call" from step 4 — expect a FULL
# regeneration again (cache key changed because the index name changed),
# not a stale hit against the pre-rebuild answer.
```

## 6. Redis unreachable → graceful degradation
```bash
docker compose stop redis
curl -s -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{"question": "What are the steps to reproduce BUG-1003?"}'
# expect: /generate still returns a normal streamed answer (regenerates
# every time, cache silently no-ops) -- NOT a 500, NOT a hang.
docker compose start redis
```

## 7. Structured logging
```bash
cat logs/generation.jsonl | python -m json.tool  # or `jq .` if installed
```
Expect one JSON line per `/generate` call made in steps 3-6, each with
`retrieval` (doc_ids, scores, latency_s) and `generation` (latency_s,
token_count) spans, and `cache_hit: true` on the step-4 cache hit call.

## 8. Manual (Tier B) log, smoke test
```bash
python -m eval.manual_eval_log add \
  --question "What are the steps to reproduce BUG-1003?" \
  --answer "<paste real answer from step 4's first call>" \
  --verdict pass \
  --note "Matches steps_to_reproduce verbatim."
python -m eval.manual_eval_log summary
```

## 9. Contract preservation
```bash
curl -s "http://localhost:8000/search?q=BUG-1013"
curl -s "http://localhost:8000/search/hybrid?q=login+failures"
```
Both must return byte-identical shapes to Phase 3/4 — Phase 6 touches
neither file.

## Acceptance criteria
- [ ] `redis-cli ping` → PONG
- [ ] Live `eval_seed.json` regeneration produces 17 questions matching the types predicted above (or a documented, understood diff)
- [ ] `run_eval_generation.py` runs to completion with real PASS/FAIL against live `/generate` — dangling-reference and resolution-in-narrative checks specifically reviewed, not just glanced at
- [ ] Cache hit is measurably faster than a miss; `no_cache: true` reliably bypasses it
- [ ] An index rebuild busts the cache (no stale post-rebuild answers served)
- [ ] Redis stopped → `/generate` still works, degrades to always-regenerate
- [ ] `logs/generation.jsonl` has one real line per call with both spans populated
- [ ] `/search` and `/search/hybrid` unchanged
