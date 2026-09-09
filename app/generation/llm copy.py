"""
Ollama /api/chat streaming client for Phase 5 generation.

Raw httpx, matching the convention already established in
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

import httpx

CHAT_MODEL = "phi4-mini:latest"
GENERATION_TEMPERATURE = 0.1


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
    timeout: float = 120.0,
    temperature: float = GENERATION_TEMPERATURE,
):
    """Yields response text as it streams from Ollama's /api/chat endpoint.

    Ollama's streaming response is newline-delimited JSON, one object per
    line, each carrying an incremental `message.content` fragment and a
    `done` flag on the final line. This generator flattens that into plain
    text fragments -- callers that need the raw per-line structure (e.g. to
    inspect token/timing metadata) should call the endpoint directly instead.

    If the request fails before streaming starts, or the connection drops
    mid-stream, GenerationError propagates out of this generator on the
    `next()` call where the failure occurred -- by definition, this can
    happen after some text has already been yielded to the caller.
    """
    url = f"http://{host}:{port}/api/chat"
    try:
        with httpx.stream(
            "POST",
            url,
            json={
                "model": model,
                "messages": messages,
                "stream": True,
                "options": {"temperature": temperature},
            },
            timeout=timeout,
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                content = chunk.get("message", {}).get("content", "")
                if content:
                    yield content
                if chunk.get("done"):
                    return
    except httpx.HTTPError as e:
        raise GenerationError(f"Ollama chat request failed: {e}") from e
