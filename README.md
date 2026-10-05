# qa-agentic-rag

A local-first question-answering system over QA artefacts (bug reports and test cases). It runs entirely on a 16 GB laptop, with no cloud APIs. It combines hybrid search, deterministic routing, and a small local LLM that only answers from retrieved documents and cites them.

**What "agentic" means here.** It is a router, not an autonomous agent. A LangGraph graph inspects each question once and sends it down exactly one of five paths: reject, count, field lookup, error-code lookup, or search-then-LLM. There are no tool calls chosen by the model, no loops and no multi-step planning. The name comes from the project's Phase 7 ("agentic layer"); "routed RAG" would describe it more precisely.

**How it was built.** This is a personal project built with AI coding assistance (Claude), which is visible in the commit history. The design decisions, the accept criteria for each experiment, and the review of results were directed by me; much of the code and documentation was written with the assistant.

```
question ─► guardrail + router (LangGraph) ─┬─ unknown ID ............ reject (no search, no LLM)
                                            ├─ off-topic ............. reject (cosine < 0.75)
                                            ├─ "how many / list all" . count from OpenSearch   ~0.2 s
                                            ├─ "steps for BUG-1003" .. field from Postgres     ~0.1 s
                                            ├─ "which bugs have ERR_X" error-code table        ~0.1 s
                                            └─ everything else ....... hybrid search (BM25 + k-NN, RRF)
                                                                       + referenced docs
                                                                       → LLM, streamed, cited  ~45 s
```

## Measured results

All figures are on the 173-document corpus with `qwen3:4b-instruct` on a Core Ultra 7 155U laptop (Machine B) with 16 GB RAM, CPU-only inference.

| Eval | Result |
|---|---|
| Frozen 20-question seed (`eval/eval_seed.json`) | 16 PASS / 0 FAIL / 4 manual (verified correct), in 2 identical runs |
| Hand-written holdout (`eval/holdout.json`, 41 questions) | 33 / 41 correct after hand review (grader: 32); on the 36 LLM-path questions `phi4:14b` scores 32 / 36 vs qwen 31 / 36, at ~5.7× the latency |
| LLM answer latency | median ~44–48 s, first token ~27 s (streamed) |
| Deterministic routes | 11 of the 20 seed questions answered in ≤ 0.3 s with no LLM call |
| Unit tests | 52 (`pytest`, run in CI) |

Details: `docs/PERF_RUN_2026-10-01.md`, `docs/HOLDOUT_EVAL_2026-10-01.md`, `docs/NEXT_STEPS_PLAN.md`, and the per-phase handoffs in `docs/history/` (start with `phase-08-handoff.md`).

## Stack

- **Docker Compose:**
  - Postgres 16: the store of record, with class-table inheritance (`schema.sql`).
  - OpenSearch 2.15: BM25 plus k-NN (nmslib HNSW), with an alias swap on rebuild.
  - Redis: the answer cache.
  - FastAPI: the API and a demo UI.
- **Ollama, run natively on the host:**
  - `nomic-embed-text` for embeddings.
  - `qwen3:4b-instruct` for answers.

  Ollama runs natively because Docker Desktop on Windows/macOS cannot use the host's acceleration.

## Setup

Prerequisites: Docker Desktop, Python 3.12+, and [Ollama](https://ollama.com).

```bash
# 1. Models (host)
ollama pull nomic-embed-text
ollama pull qwen3:4b-instruct
#    Ollama must listen on all interfaces so the container can reach it:
#    OLLAMA_HOST=0.0.0.0 ollama serve

# 2. Config
cp .env.example .env            # set POSTGRES_PASSWORD and OPENSEARCH_INITIAL_ADMIN_PASSWORD

# 3. Services
docker compose up -d --build
curl http://127.0.0.1:8000/health    # all three services "healthy"

# 4. Data (host Python; Postgres is on host port 5433)
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
python ingest.py --reset                         # applies schema.sql, loads data/
python -m scripts.build_index                    # embeds + indexes, swaps the alias

# 5. Check
docker exec rag-fastapi python -m diagnostics.routing_check     # expect 22/22
# browse to http://127.0.0.1:8000/ui
```

**Verification status of these steps.** Verified end to end from a fresh `git clone` on 2026-10-03:
- 52 tests pass in a new venv.
- The Docker image builds from the clone.
- `ingest.py --reset` reloads a database identical to the previous one (same counts, same content hash over all 173 documents).
- `build_index` takes ~6.5 min (embedding 173 docs).
- `routing_check` 22/22; frozen-20 eval 16/0/4/0.
- GitHub Actions `unit-tests` passes.

Step 1 (installing Ollama and pulling the models) was done earlier on this machine, not repeated.

**Windows notes** (each one has caused a real failure on this project):
- Use `127.0.0.1`, not `localhost`. On Windows, `localhost` can resolve to IPv6 and reach a different Postgres.
- Use bare `python`, not `python3`.
- In Git Bash, prefix commands with leading-slash arguments with `MSYS_NO_PATHCONV=1`.

## API

| Endpoint | Purpose |
|---|---|
| `POST /generate/agentic` | Main endpoint: routing plus a grounded, streamed answer. Body: `{"question", "doc_type"?, "module"?, "status"?, "no_cache"?}`. Response: the answer text, then `---SOURCES---`, then a JSON list of sources. |
| `POST /generate` | Phase 5/6 baseline (always the LLM), kept frozen for comparison |
| `GET /search`, `GET /search/hybrid` | BM25-only search, and hybrid search with per-leg ranks and scores |
| `GET /health` | Connectivity of Postgres, OpenSearch and Ollama |
| `GET /ui` | Demo page |

## Changing behaviour

Every setting lives in **three places**: `.env.example`, `app/config.py`, and the `fastapi` `environment:` block of `docker-compose.yml`. Compose uses an explicit whitelist, so a setting missing from it is silently ignored. After any change to source code or `.env`, run `docker compose up -d fastapi`, adding `--build` if source changed.

| Setting | Default | What it does |
|---|---|---|
| `OLLAMA_CHAT_MODEL` | `qwen3:4b-instruct` | `phi4:14b` scores slightly higher but needs ~10 GB RAM and is ~5.7× slower on this hardware |
| `DETERMINISTIC_COUNT_ROUTING` | `True` | Count/list questions answered without the LLM |
| `DETERMINISTIC_FIELD_LOOKUP` | `true` | Single-ID field questions answered from Postgres |
| `DETERMINISTIC_ERROR_CODE_LOOKUP` | `true` | "Which bugs have ERR_X" answered from the error-code table |
| `REFERENCE_EXPANSION` | `true` | A named ID's cited test case and duplicates are added to the context |
| `AGENTIC_TOKEN_STREAMING` | `true` | Stream LLM tokens as they are generated |
| `VECTOR_SCORE_GUARDRAIL_THRESHOLD` | `0.75` | Off-topic cut-off. Calibrated for nmslib and this corpus; re-probe with `diagnostics/rrf_threshold_probe.py` if either changes |

## Testing

```bash
pytest -q                                                    # unit tier, no services needed (CI runs this)
docker exec rag-fastapi python -m diagnostics.routing_check  # live routing, no LLM, seconds
python -m eval.run_holdout --label <name>                    # 41-question holdout, ~35 min on qwen
MSYS_NO_PATHCONV=1 python -m eval.run_eval_generation --endpoint-path /generate/agentic \
    --seed-path eval/runs/<run>/eval_seed.json               # frozen 20; copy the seed first so results land in that folder
```

## Measured experiments (not part of the app)

Speed ideas tested with pass criteria set in advance. The app's behaviour is unchanged by all of them.

| Idea | Outcome | Report |
|---|---|---|
| Pre-read document groups: save the model's state per small group and restore it per question | **Passed as a diagnostic**: first token ~3 s (was ~27 s), median answer ~15 s (was ~48 s), same accuracy (31/36), repeatable. Not integrated: it needs a second model server and 6 GB of saved state | `docs/MODULE_KV_CACHE_EXPERIMENT_2026-10-04.md` |
| Whole corpus in the model's cache | Rejected: 1 h 51 min to prepare, 64–303 s per answer | `docs/CAG_EXPERIMENT_2026-10-01.md` |
| Copy-based speculative decoding | Rejected: 4–19% of guesses accepted, writing slowed from 5.4 to 1.5–3.9 tokens/s | `docs/MODULE_KV_CACHE_EXPERIMENT_2026-10-04.md` |
| Prompt rule for false premises | Rejected: no effect, one regression | `docs/HOLDOUT_EVAL_2026-10-01.md` |

## Known limitations

Each limitation below was measured and is documented. None of them was hidden.
- **Short or messy questions are rejected as off-topic**, for example "checkout 504" or "dup notif bug??": 3 of 41 holdout questions. This is a limit of the cosine guardrail on short text.
- **Lists are capped at the top 5 retrieved documents**, so open-ended "list all X" questions can miss items that the deterministic routes don't cover.
- **Messy wording can miss the key document** (1 of 41). A query-rewrite step is now justified by evidence but has not been built.
- **qwen3:4b-instruct does not correct false premises.** For example, "Why was BUG-1025 fixed so quickly?" when the bug is Won't Fix. `phi4:14b` does correct them. A prompt rule was tried and reverted (no effect, plus one regression).
- **Generation latency is still ~45 s** per LLM answer on CPU. The answer is streamed, so the first token arrives at ~27 s.
- **Not for public exposure as-is:** there is no auth or rate limiting, and OpenSearch security is disabled.

## Repository map

```
app/                FastAPI app: routers/, search/ (indexing, queries, hybrid, ID resolution),
                    generation/ (prompt, LLM client, guardrail, deterministic routes, cache)
ingest.py           data/ → Postgres (HTML cleanup, error codes, reference graph)
scripts/            build_index.py (Postgres → OpenSearch), generate_synthetic_data.py
eval/               eval_seed.json + run_eval*.py (frozen), holdout.json + run_holdout.py, runs/
diagnostics/        routing_check, threshold and model probes
tests/              unit tests (CI)
docs/               project log, perf report, holdout report, next-steps plan
docs/history/       per-phase handoffs (design decisions, bugs found, real numbers), old verify
                    scripts and notes from Phases 1-8
```
