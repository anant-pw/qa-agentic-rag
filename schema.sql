-- Phase 2 schema: QA corpus (bug reports + test cases)
-- Replaces the arXiv paper metadata schema from the reference repo.
-- Design: class-table inheritance. `documents` holds type-agnostic fields
-- used for indexing/search; `bug_reports` and `test_cases` hold fields
-- specific to each doc type. No arXiv-style single wide table, because
-- the two doc types don't share a field shape (steps_to_reproduce vs
-- preconditions/steps/expected_result).

DROP TABLE IF EXISTS document_error_codes CASCADE;
DROP TABLE IF EXISTS error_codes CASCADE;
DROP TABLE IF EXISTS document_references CASCADE;
DROP TABLE IF EXISTS test_cases CASCADE;
DROP TABLE IF EXISTS bug_reports CASCADE;
DROP TABLE IF EXISTS documents CASCADE;

-- Parent table: one row per ingested document, regardless of type.
-- This is what OpenSearch indexing reads from, and what
-- document_references / document_error_codes point at.
CREATE TABLE documents (
    id              SERIAL PRIMARY KEY,
    doc_type        TEXT NOT NULL CHECK (doc_type IN ('bug_report', 'test_case')),
    external_id     TEXT NOT NULL,          -- e.g. "BUG-014" or "TC-0142"
    title           TEXT NOT NULL,
    module          TEXT,                   -- shared dimension, filterable in both types
    status          TEXT,                   -- bug reports only; NULL for test cases
    date_created    DATE,                   -- bug reports only; NULL for test cases
    source_path     TEXT NOT NULL,          -- original CSV row ref or .md filepath
    raw_text        TEXT NOT NULL,          -- pre-cleaning, for audit/debugging
    cleaned_text    TEXT NOT NULL,          -- HTML-stripped, normalized -> what gets indexed
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (doc_type, external_id)
);

CREATE INDEX idx_documents_doc_type ON documents (doc_type);
CREATE INDEX idx_documents_module   ON documents (module);
CREATE INDEX idx_documents_status   ON documents (status);

-- Bug-report-specific fields
CREATE TABLE bug_reports (
    document_id         INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    description         TEXT NOT NULL,
    steps_to_reproduce  TEXT
);

-- Test-case-specific fields
CREATE TABLE test_cases (
    document_id     INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    preconditions   TEXT,
    steps           TEXT,
    expected_result TEXT
);

-- Cross-references: bug -> test case citation, bug -> bug duplicate/related.
-- target_document_id is nullable because a bug can cite a TC id that
-- doesn't resolve to an ingested file (dangling reference) -- we still
-- want to record that the citation exists, for eval/debugging purposes.
CREATE TABLE document_references (
    id                  SERIAL PRIMARY KEY,
    source_document_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    target_document_id  INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    target_external_id  TEXT NOT NULL,       -- raw id as it appeared in text, e.g. "TC-0142"
    reference_type      TEXT NOT NULL CHECK (
                             reference_type IN ('test_case_citation', 'duplicate_of', 'related_to')
                         ),
    extraction_method   TEXT NOT NULL,       -- 'regex' | 'tfidf_llm_verify'
    confidence          REAL,                -- NULL for regex (deterministic), 0-1 for similarity-based
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_refs_source ON document_references (source_document_id);
CREATE INDEX idx_refs_target ON document_references (target_document_id);
CREATE INDEX idx_refs_type   ON document_references (reference_type);

-- Error codes as a first-class, queryable entity rather than a substring
-- match inside cleaned_text.
CREATE TABLE error_codes (
    id    SERIAL PRIMARY KEY,
    code  TEXT NOT NULL UNIQUE          -- e.g. "ERR_401_UNAUTH"
);

CREATE TABLE document_error_codes (
    document_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    error_code_id   INTEGER NOT NULL REFERENCES error_codes(id) ON DELETE CASCADE,
    PRIMARY KEY (document_id, error_code_id)
);
