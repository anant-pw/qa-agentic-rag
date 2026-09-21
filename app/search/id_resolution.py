"""
ID resolution for /generate/agentic: a lexical, embedding-independent signal
for questions that name a specific document ("... BUG-1011 ...", "TC-0301").

WHY THIS EXISTS (measured, not assumed -- diagnostics/guardrail_shape_probe*.py,
two machines, 33 probe rows):

  1. The vector_score guardrail false-rejects well-formed ID questions.
     "What are the steps to reproduce BUG-1011?" scored 0.7336 (< 0.75) and
     was rejected; adding the title in parentheses lifted it to 0.8273.
     Across six other IDs the target document was BM25 rank 1 every time,
     yet cosine rejected 6 of 7 phrased/bare ID rows on machine A.
  2. Cosine carries no information about whether an ID EXISTS: the
     nonexistent "What are the steps to reproduce BUG-9999?" scored 0.7581
     (ACCEPTED) -- higher than the real BUG-1023 phrasing (0.7244).
     build_embedding_input() embeds title/description/steps only, never the
     external_id, so the vector leg is structurally blind to IDs (by design).
  3. RRF buries exact-ID hits: a document that is BM25 rank 1 but vector
     rank ~64 fuses to ~0.0245, below a "hub" document at BM25 rank 3 /
     vector rank 1 (~0.0323). On machine A the target landed at fused
     position 4-6; BUG-2015 at 6, outside context_top_n=5, i.e. generation
     would not even have seen the document being asked about.

WHAT IT DOES (three outcomes, decided in the router -- see
app/routers/generate_agentic.py):

  none      no BUG-/TC- ID in the question, or the IDs exist but are excluded
            by the caller's explicit filters -> existing behaviour, unchanged.
  resolved  at least one named ID exists and passes the active filters ->
            pin those documents to the front of the hits and accept; skip the
            cosine gate AND the deterministic count shortcut (which would
            ignore the ID and answer with a filter count).
  unknown   IDs were named but NONE exist in the index -> reject with an
            accurate message instead of the misleading out-of-domain one, and
            skip retrieval entirely.

ACCEPTED, DOCUMENTED TRADE-OFFS (not hidden):
  - "What is the capital of France? BUG-1011" is now ACCEPTED (it was
    rejected on cosine, 0.69-0.71). Same class as the filter-trust trade-off
    already documented in guardrail.py: no cosine floor rescues it either --
    bare "TC-0301" scored 0.6394, below this adversarial row's 0.6901. The
    grounding prompt is the second line of defence; the cost is one wasted
    generation call.
  - A question that merely MENTIONS an ID as an example ("what format do IDs
    like BUG-0001 use?") is rejected as unknown if that ID does not exist.
  - Only BUG-/TC- document IDs are handled. ERR_* codes live in `error_codes`,
    are not document IDs, and are deliberately left on the old path.
  - The unknown-ID message echoes the ID. eval/run_eval_generation.py's
    check_out_of_domain FAILS any answer citing a BUG-/TC- ID, so never grade
    this route with the out_of_domain type (none of the 3 baseline
    out_of_domain questions contains an ID, so the frozen 20-question
    baseline cannot reach this route). Use diagnostics/routing_check.py.
  - Short generic fragments ("test cases", "bugs") are NOT addressed here:
    their cosine range (0.73-0.75) overlaps out-of-domain questions on this
    embedding stack, so no threshold separates them. Known limitation.
"""

from app.search.queries import EMBEDDED_ID_PATTERNS

# queries.py's EMBEDDED_ID_PATTERNS = [BUG-####, TC-####, ERR_*]. Reuse it
# rather than adding a fourth copy of the ID regexes (indexer.py, ingest.py
# and queries.py already duplicate them; that drift risk is on record).
# ERR_* is excluded: an error code is not a document ID.
DOC_ID_PATTERNS = [p for p in EMBEDDED_ID_PATTERNS if not p.pattern.startswith(r"\bERR_")]
assert len(DOC_ID_PATTERNS) == 2, (
    "EMBEDDED_ID_PATTERNS changed shape in app/search/queries.py; "
    "id_resolution.DOC_ID_PATTERNS must be revisited"
)


# IDs pasted from Jira/Slack/Word often carry a typographic dash instead of
# "-" (U+2010..U+2015, U+2212) -- the same variants the eval harness's
# _normalize_dashes() already had to handle for model OUTPUT. Normalise the
# QUESTION the same way before matching.
_DASH_VARIANTS = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"), "-")

# Bounds the terms query and the pinned context for a pathological question
# that lists dozens of IDs.
MAX_IDS = 10


def extract_document_ids(question: str) -> list[str]:
    """BUG-/TC- IDs named in the question: lowercase (index normalizer is
    lowercase), de-duplicated, in order of first appearance, at most MAX_IDS."""
    text = (question or "").translate(_DASH_VARIANTS)
    found: list[tuple[int, str]] = []
    for pattern in DOC_ID_PATTERNS:
        for m in pattern.finditer(text):
            found.append((m.start(), m.group(0).lower()))
    found.sort(key=lambda t: t[0])
    ordered: list[str] = []
    for _, id_ in found:
        if id_ not in ordered:
            ordered.append(id_)
        if len(ordered) >= MAX_IDS:
            break
    return ordered


def passes_filters(hit: dict, doc_type: str | None, module: str | None, status: str | None) -> bool:
    """Same semantics as the OpenSearch filters (case-insensitive exact
    match). A pinned document must not violate the caller's explicit
    filters -- the UI can leave a stale dropdown selected."""
    for want, have in ((doc_type, hit.get("doc_type")), (module, hit.get("module")), (status, hit.get("status"))):
        if want and (have or "").lower() != want.lower():
            return False
    return True


def classify_id_question(ordered_ids: list[str], existing: dict, pinnable: dict) -> str:
    """'none' | 'resolved' | 'unknown'. `existing`: named IDs found in the
    index (any filters). `pinnable`: the subset passing the active filters."""
    if not ordered_ids:
        return "none"
    if not existing:
        return "unknown"
    if pinnable:
        return "resolved"
    return "none"  # exist, but the caller's filters exclude them -> old path


def pin_resolved_hits(hits: list[dict], pinnable: dict, ordered_ids: list[str]) -> list[dict]:
    """Move the named documents to the front, in question order. Reuse the
    fused hit when present (keeps its real bm25/vector/rrf diagnostics);
    otherwise use the shaped lookup hit. Remaining hits keep RRF order.
    De-duplicated by chunk_id (a document's other chunks are not dropped)."""
    pinned: list[dict] = []
    pinned_chunks: set = set()
    for id_ in ordered_ids:
        if id_ not in pinnable:
            continue
        hit = next((h for h in hits if (h.get("external_id") or "").lower() == id_), None) or pinnable[id_]
        if hit["chunk_id"] in pinned_chunks:
            continue
        pinned.append(hit)
        pinned_chunks.add(hit["chunk_id"])
    return pinned + [h for h in hits if h["chunk_id"] not in pinned_chunks]


def unknown_id_message(ids: list[str]) -> str:
    shown = ", ".join(i.upper() for i in ids)
    noun = "document with ID" if len(ids) == 1 else "documents with IDs"
    return (
        f"No {noun} {shown} exists in this corpus (bug reports and test "
        f"cases), so no answer was generated."
    )
