"""
Ollama /api/chat streaming client for Phase 5 generation.

Raw requests, matching the convention already established in
app/search/embeddings.py and app/main.py's check_ollama() -- this project
talks to Ollama via direct HTTP calls, not the `ollama` Python package, so
there's one HTTP-client pattern in the codebase, not two.

Confirmed against this project's actual Ollama install (2026-08-30):
`phi4-mini:latest` -- 3.8B params, 131072 context length, completion
capability confirmed via a real /api/chat call returning "hello" for a
one-word-reply prompt (prompt_eval_count=10, eval_count=2). Not assumed.

--- Temperature: added after Phase 5 verification, not part of the original design ---

Ollama's default temperature (~0.8) is tuned for varied, creative output --
the wrong setting for a system whose entire job is "say only what's in the
documents." This was a real, demonstrated gap, not a hypothetical
tightening: the same duplicate-classification question, run four times
with identical retrieval and an unchanged prompt, produced four
meaningfully different answers (some correct, some not) purely from
sampling variance. GENERATION_TEMPERATURE defaults low (0.1, not 0.0 --
some models degrade into repetition loops at exactly zero) to prioritize
consistency. This does not fully fix grounding on its own -- seeing
fetch_references() in context.py / Rule 3 in prompt.py for the other half
of that specific fix -- but it closes a real, separate source of
run-to-run inconsistency that would otherwise undermine any verification
run's repeatability.
"""


import json
import logging
import time

import requests

CHAT_MODEL = "gpt-oss:20b"
GENERATION_TEMPERATURE = 0.1

logger = logging.getLogger(__name__)


class GenerationError(Exception):
    """Raised whether the failure happens before any tokens are streamed
    (connection refused, model not found, non-2xx response) or mid-stream
    (connection drops partway through). Either way, the caller needs to
    know generation did not complete cleanly -- see app/routers/generate.py
    for how a mid-stream failure affects whether the sources block is sent."""


def stream_chat(
    messages: list[dict],
    host: str,
    port: int,
    model: str = CHAT_MODEL,
    timeout: float = 600.0,  # 10 minutes; reasoning models can be slow
    temperature: float = GENERATION_TEMPERATURE,
    log_stats: bool = True,
    think: bool | None = None,
    keep_alive: str | None = None,
):
    """Yields (content, maybe_done_chunk) as it streams from Ollama's /api/chat.

    keep_alive: Ollama's per-request override for how long to keep a model
    resident in memory after this call (e.g. "30m", "1h", "-1" for
    indefinite). Added after a real, measured finding: two qwen3.5:9b
    diagnostic calls six minutes apart both showed time_to_first_token_sec
    ~112s, nearly identical -- consistent with Ollama's default 5-minute
    keep_alive unloading the model between calls, not with genuine warm
    generation. Left as None (Ollama's server-wide default, itself
    5 minutes unless OLLAMA_KEEP_ALIVE is set when `ollama serve` starts)
    unless a caller explicitly passes a value -- this lets production and
    diagnostics choose independently rather than one setting silently
    governing both.

    think: Ollama's API accepts a top-level "think" boolean for
    hybrid-reasoning models (Qwen3/3.5, DeepSeek-R1, etc.) that support a
    "thinking" mode -- an internal reasoning trace generated before the
    visible answer. Added after a real, measured finding: qwen3.5:9b
    produced 1200-1580 total_tokens for a visible answer of ~40-80 words
    when this project first tried it, and total_generation_time was
    500-670s -- an order of magnitude slower than gpt-oss:20b for an
    equally correct answer. The token count gap strongly suggests most of
    that time went into an unrequested reasoning trace, not the visible
    response. Left as None (Ollama's per-model default) unless a caller
    explicitly passes True/False, so this does not change behavior for
    phi4-mini/llama3.1/gpt-oss, none of which expose a thinking mode --
    the field is simply omitted from the request payload for those, not
    sent as a no-op, since an unrecognized field being silently ignored
    by every model was an assumption worth not relying on."""
    url = f"http://{host}:{port}/api/chat"

    start_time = time.perf_counter()
    first_token_time = None
    total_tokens = None
    done_chunk_seen = False

    try:
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": temperature},
        }
        if think is not None:
            payload["think"] = think
        if keep_alive is not None:
            payload["keep_alive"] = keep_alive

        resp = requests.post(
            url,
            json=payload,
            stream=True,
            timeout=(10.0, timeout),  # (connect_timeout, read_timeout)
        )
        resp.raise_for_status()

        for line in resp.iter_lines():
            if not line:
                continue
            chunk = json.loads(line)

            content = chunk.get("message", {}).get("content", "")
            is_done = chunk.get("done", False)

            if content:
                if first_token_time is None:
                    first_token_time = time.perf_counter()

            if is_done:
                done_chunk_seen = True
                total_tokens = chunk.get("eval_count")
                # Yield final content (may be empty) plus the done chunk
                yield content, chunk
                break
            else:
                yield content, None

    except requests.RequestException as e:
        raise GenerationError(f"Ollama chat request failed: {e}") from e

    finally:
        end_time = time.perf_counter()
        total_generation_time = end_time - start_time
        time_to_first_token = (
            (first_token_time - start_time) if first_token_time is not None else None
        )

        if log_stats:
            logger.info(
                "ollama_chat_stats: model=%s time_to_first_token_sec=%.3f total_generation_time_sec=%.3f total_tokens=%s",
                model,
                time_to_first_token or 0.0,
                total_generation_time,
                total_tokens,
            )