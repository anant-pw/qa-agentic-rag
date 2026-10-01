"""
Unit-test setup. These tests need no Docker, Postgres, OpenSearch, Redis or
Ollama: they cover the pure decision logic (routing rules, rank fusion,
query building, grading) and the SQL-backed lookups through a fake
connection.

app.config.Settings has required fields with no defaults (OLLAMA_CHAT_MODEL
etc.), so placeholder values are set before any app module is imported.
setdefault() means a real .env / environment still wins locally.
"""

import os

for key, value in {
    "OLLAMA_CHAT_MODEL": "test-model",
    "OLLAMA_TEMPERATURE": "0.1",
    "OLLAMA_THINK": "false",
    "OLLAMA_CONNECT_TIMEOUT": "1",
    "OLLAMA_CHAT_TIMEOUT": "1",
    "OLLAMA_EMBEDDING_MODEL": "nomic-embed-text",
    "OLLAMA_EMBEDDING_DIMENSION": "768",
    "OLLAMA_EMBEDDING_TIMEOUT": "1",
    "RETRIEVAL_SIZE": "10",
    "CONTEXT_TOP_N": "5",
    "POSTGRES_CONNECT_TIMEOUT": "1",
}.items():
    os.environ.setdefault(key, value)


class _Column:
    def __init__(self, name):
        self.name = name


class FakeCursor:
    """Returns queued (columns, rows) results, one per execute() call."""

    def __init__(self, results):
        self._results = list(results)
        self.executed = []
        self.description = None
        self._rows = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        columns, rows = self._results.pop(0)
        self.description = [_Column(c) for c in columns]
        self._rows = list(rows)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, *results):
        self.cursor_obj = FakeCursor(results)

    def cursor(self):
        return self.cursor_obj
