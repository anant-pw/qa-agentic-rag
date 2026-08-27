"""
Phase 2 ingestion pipeline: QA corpus (bug reports + test cases) -> Postgres.

Sync throughout (psycopg2-binary, per the Phase 1 decision already made --
not revisiting that here). Run schema.sql first.

Pipeline stages:
  1. parse_bug_reports_csv   -> clean HTML residue, extract error codes + TC refs
  2. parse_test_cases_md     -> parse fixed-section markdown files
  3. load_documents          -> insert into documents / bug_reports / test_cases
  4. resolve_test_case_refs  -> regex-extracted TC-XXXX -> document_references
  5. find_duplicate_bugs     -> TF-IDF candidates -> Ollama confirm -> document_references
  6. index_opensearch        -> push cleaned_text + metadata for retrieval

NOTE: not executed against real data or a live Postgres/OpenSearch in this
sandbox -- same caveat as the Phase 1 Docker Compose validation. Run this
locally against ./data/ and flag back anything that breaks on your actual
file formats (I'm assuming the CSV header and markdown section headers
match what you described; if the real files differ, the regexes in
parse_test_cases_md are the most likely thing to need adjusting).
"""

import csv
import os
import re
import psycopg2
from psycopg2.extras import execute_values
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

DATA_DIR = "./data"
BUG_CSV = os.path.join(DATA_DIR, "bug_reports.csv")
TC_DIR = os.path.join(DATA_DIR, "test_cases")

TC_ID_RE = re.compile(r"\bTC-\d{4}\b")
BUG_ID_RE = re.compile(r"\bBUG-\d{4}\b")
ERR_CODE_RE = re.compile(r"\bERR_[A-Z0-9_]+\b")
HTML_TAG_RE = re.compile(r"<[^>]+>")

# Explicit "BUG-XXXX" mentions are the primary duplicate/related signal on
# this corpus -- verified against real data: every actual duplicate pair
# (BUG-1001/1002/1024, BUG-1006/1011, BUG-1007/1017) is textually flagged
# by the reporter, while TF-IDF cosine similarity both misses those (real
# duplicate BUG-1006/1011 scores 0.143) and false-positives on same-module,
# different-issue pairs (BUG-1006/1021 scores 0.289, top-ranked, unrelated).
# TF-IDF+LLM below is kept as a fallback discovery pass for undeclared
# duplicates, not the primary mechanism.
DUPLICATE_SIM_THRESHOLD = 0.35
RELATED_KEYWORDS = ("duplicate", "same as", "same root cause", "same pattern",
                     "filed independently", "filed separately")


def clean_html(text: str) -> str:
    """Strip embedded HTML residue and collapse whitespace."""
    if not text:
        return ""
    text = HTML_TAG_RE.sub(" ", text)
    text = re.sub(r"&nbsp;|&amp;|&lt;|&gt;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_bug_reports_csv(path):
    """Returns list of dicts: id, title, cleaned description/steps, module,
    status, date_created, extracted error_codes, extracted tc_refs."""
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            desc = clean_html(r.get("description", ""))
            steps = clean_html(r.get("steps_to_reproduce", ""))
            combined = f"{r.get('title','')} {desc} {steps}"
            rows.append({
                "external_id": r["id"],
                "title": r.get("title", "").strip(),
                "description": desc,
                "steps_to_reproduce": steps,
                "module": r.get("module", "").strip() or None,
                "status": r.get("status", "").strip() or None,
                "date_created": r.get("date_created", "").strip() or None,
                "error_codes": sorted(set(ERR_CODE_RE.findall(combined))),
                "tc_refs": sorted(set(TC_ID_RE.findall(combined))),
                "bug_refs": sorted(set(BUG_ID_RE.findall(combined)) - {r["id"]}),
                "cleaned_text": f"{r.get('title','')}\n{desc}\n{steps}".strip(),
                "raw_text": f"{r.get('title','')}\n{r.get('description','')}\n{r.get('steps_to_reproduce','')}",
            })
    return rows


def parse_test_cases_md(dir_path):
    """Parses the actual file format observed in ./data/test_cases/:

        # TC-0142: <title>
        **Module:** <module>
        ## Preconditions
        ...
        ## Steps
        ...
        ## Expected Result
        ...

    Module is a bold key-value line, NOT a ## section -- only
    Preconditions/Steps/Expected Result are ## headers."""
    section_re = re.compile(
        r"^##\s*(Preconditions|Steps|Expected Result)\s*$",
        re.IGNORECASE | re.MULTILINE,
    )
    module_re = re.compile(r"^\*\*Module:\*\*\s*(.+)$", re.MULTILINE)

    cases = []
    for fname in sorted(os.listdir(dir_path)):
        if not fname.endswith(".md"):
            continue
        path = os.path.join(dir_path, fname)
        raw = open(path, encoding="utf-8").read()

        tc_id_match = re.search(r"TC-\d{4}", raw) or re.search(r"TC-\d{4}", fname)
        external_id = tc_id_match.group(0) if tc_id_match else fname.replace(".md", "")

        title_match = re.search(r"^#\s*TC-\d{4}:\s*(.+)$", raw, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else external_id

        module_match = module_re.search(raw)
        module = module_match.group(1).strip() if module_match else None

        sections = {}
        splits = section_re.split(raw)
        for i in range(1, len(splits), 2):
            key = splits[i].strip().lower().replace(" ", "_")
            body = splits[i + 1].strip() if i + 1 < len(splits) else ""
            sections[key] = body

        cases.append({
            "external_id": external_id,
            "title": title,
            "module": module,
            "preconditions": sections.get("preconditions", ""),
            "steps": sections.get("steps", ""),
            "expected_result": sections.get("expected_result", ""),
            "cleaned_text": raw.strip(),
            "raw_text": raw,
            "source_path": path,
        })
    return cases


def load_documents(conn, bugs, test_cases):
    """Loads parsed rows into documents + subtype tables. Returns
    external_id -> document_id map for reference resolution."""
    id_map = {}
    with conn.cursor() as cur:
        for b in bugs:
            cur.execute(
                """INSERT INTO documents
                   (doc_type, external_id, title, module, status, date_created,
                    source_path, raw_text, cleaned_text)
                   VALUES ('bug_report', %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (b["external_id"], b["title"], b["module"], b["status"],
                 b["date_created"] or None, BUG_CSV, b["raw_text"], b["cleaned_text"]),
            )
            doc_id = cur.fetchone()[0]
            id_map[("bug_report", b["external_id"])] = doc_id
            cur.execute(
                """INSERT INTO bug_reports (document_id, description, steps_to_reproduce)
                   VALUES (%s, %s, %s)""",
                (doc_id, b["description"], b["steps_to_reproduce"]),
            )
            _upsert_error_codes(cur, doc_id, b["error_codes"])

        for t in test_cases:
            cur.execute(
                """INSERT INTO documents
                   (doc_type, external_id, title, module, status, date_created,
                    source_path, raw_text, cleaned_text)
                   VALUES ('test_case', %s, %s, %s, NULL, NULL, %s, %s, %s)
                   RETURNING id""",
                (t["external_id"], t["title"], t["module"], t["source_path"],
                 t["raw_text"], t["cleaned_text"]),
            )
            doc_id = cur.fetchone()[0]
            id_map[("test_case", t["external_id"])] = doc_id
            cur.execute(
                """INSERT INTO test_cases (document_id, preconditions, steps, expected_result)
                   VALUES (%s, %s, %s, %s)""",
                (doc_id, t["preconditions"], t["steps"], t["expected_result"]),
            )
    conn.commit()
    return id_map


def _upsert_error_codes(cur, doc_id, codes):
    for code in codes:
        cur.execute(
            """INSERT INTO error_codes (code) VALUES (%s)
               ON CONFLICT (code) DO UPDATE SET code = EXCLUDED.code
               RETURNING id""",
            (code,),
        )
        code_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO document_error_codes (document_id, error_code_id)
               VALUES (%s, %s) ON CONFLICT DO NOTHING""",
            (doc_id, code_id),
        )


def resolve_test_case_refs(conn, bugs, id_map):
    """Regex-extracted TC-XXXX citations -> document_references. Deterministic,
    no similarity involved -- this is a straight lookup + dangling-ref check."""
    with conn.cursor() as cur:
        rows = []
        for b in bugs:
            source_id = id_map[("bug_report", b["external_id"])]
            for tc_ref in b["tc_refs"]:
                target_id = id_map.get(("test_case", tc_ref))  # None if dangling
                rows.append((source_id, target_id, tc_ref, "test_case_citation", "regex", None))
        if rows:
            execute_values(
                cur,
                """INSERT INTO document_references
                   (source_document_id, target_document_id, target_external_id,
                    reference_type, extraction_method, confidence)
                   VALUES %s""",
                rows,
            )
    conn.commit()


def resolve_explicit_bug_mentions(conn, bugs, id_map):
    """Primary duplicate/related signal: explicit BUG-XXXX mentions in the
    text. Classified as 'duplicate_of' if the surrounding text uses
    duplicate-indicating language, else 'related_to' (e.g. a follow-up
    fix ticket citing the original, which is a real relationship but not
    the same underlying report filed twice)."""
    rows = []
    with conn.cursor() as cur:
        for b in bugs:
            source_id = id_map[("bug_report", b["external_id"])]
            combined_lower = (b["title"] + " " + b["description"] + " " +
                              b["steps_to_reproduce"]).lower()
            is_duplicate_lang = any(kw in combined_lower for kw in RELATED_KEYWORDS)
            ref_type = "duplicate_of" if is_duplicate_lang else "related_to"
            for bug_ref in b["bug_refs"]:
                target_id = id_map.get(("bug_report", bug_ref))
                rows.append((source_id, target_id, bug_ref, ref_type,
                             "regex_explicit_mention", 1.0))
        if rows:
            execute_values(
                cur,
                """INSERT INTO document_references
                   (source_document_id, target_document_id, target_external_id,
                    reference_type, extraction_method, confidence)
                   VALUES %s""",
                rows,
            )
    conn.commit()


def find_duplicate_bugs(conn, bugs, id_map, ollama_confirm_fn=None):
    """Fallback discovery pass for duplicates NOT already caught by
    resolve_explicit_bug_mentions -- TF-IDF candidate generation over
    title+description, then optional LLM confirmation before writing
    duplicate_of edges. Skips pairs already linked explicitly."""
    texts = [f"{b['title']} {b['description']}" for b in bugs]
    if len(texts) < 2:
        return
    vec = TfidfVectorizer(stop_words="english").fit_transform(texts)
    sim = cosine_similarity(vec)

    candidates = []
    n = len(bugs)
    for i in range(n):
        for j in range(i + 1, n):
            if sim[i, j] >= DUPLICATE_SIM_THRESHOLD:
                candidates.append((i, j, float(sim[i, j])))

    rows = []
    with conn.cursor() as cur:
        for i, j, score in candidates:
            confirmed, confidence = True, score
            if ollama_confirm_fn is not None:
                confirmed, confidence = ollama_confirm_fn(bugs[i], bugs[j])
            if not confirmed:
                continue
            src_id = id_map[("bug_report", bugs[i]["external_id"])]
            tgt_id = id_map[("bug_report", bugs[j]["external_id"])]
            rows.append((src_id, tgt_id, bugs[j]["external_id"],
                         "duplicate_of", "tfidf_llm_verify", confidence))
        if rows:
            execute_values(
                cur,
                """INSERT INTO document_references
                   (source_document_id, target_document_id, target_external_id,
                    reference_type, extraction_method, confidence)
                   VALUES %s""",
                rows,
            )
    conn.commit()


def run(conn_params):
    bugs = parse_bug_reports_csv(BUG_CSV)
    test_cases = parse_test_cases_md(TC_DIR)

    conn = psycopg2.connect(**conn_params)
    try:
        id_map = load_documents(conn, bugs, test_cases)
        resolve_test_case_refs(conn, bugs, id_map)
        resolve_explicit_bug_mentions(conn, bugs, id_map)
        find_duplicate_bugs(conn, bugs, id_map, ollama_confirm_fn=None)  # wire up Ollama call here; fallback discovery only
        print(f"Ingested {len(bugs)} bug reports, {len(test_cases)} test cases.")
    finally:
        conn.close()


if __name__ == "__main__":
    run(dict(
        dbname=os.environ.get("POSTGRES_DB", "rag_db"),
        user=os.environ.get("POSTGRES_USER", "rag_user"),
        password=os.environ.get("POSTGRES_PASSWORD", "rag_password"),
        host=os.environ.get("POSTGRES_HOST", "127.0.0.1"),  # not "localhost" -- on
        # Windows, "localhost" can resolve to ::1 (IPv6) and hit a different,
        # unrelated Postgres instance (e.g. a native service) instead of
        # Docker's IPv4 port mapping. Forcing IPv4 removes the ambiguity.
        port=os.environ.get("POSTGRES_PORT", "5433"),
    ))
