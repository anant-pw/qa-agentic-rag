"""
OpenSearch index mapping for the QA document corpus.

Design decisions (the "why" matters more than the JSON):

1. IDs are NOT trusted to survive standard-analyzer tokenization.
   "BUG-1013" under the standard analyzer becomes tokens ["bug", "1013"] --
   the hyphen is a token boundary. That means a free-text query for
   "BUG-1013" would actually match any document containing "bug" AND
   "1013" as separate tokens anywhere, which is a false-precision risk
   (e.g. a doc mentioning "1013 requests/sec" and unrelated "bug" text).
   Rather than fight the analyzer with char_filters, we do exact entity
   extraction at INDEX TIME (same regexes ingest.py already uses for
   TC_ID_RE / BUG_ID_RE) and store them as `keyword` fields:
   external_id, mentioned_bug_ids, mentioned_tc_ids, error_codes.
   A query for "BUG-1013" gets boosted via an exact term match on these
   fields, with the analyzed full-text match as a secondary signal.
   Trade-off: if a document mentions an ID pattern not caught by these
   regexes (5-digit IDs, different prefixes), it silently falls back to
   analyzed-text recall only. Flagged, not fixed, in this phase.

2. `standard` analyzer, not `english`. The `english` analyzer stems
   ("logging" -> "log", "duplicated" -> "duplic") which trades precision
   for recall. On a 28-document technical corpus where module names,
   status values, and short phrases carry most of the signal, stemming
   is more likely to blur distinct terms than to rescue a missed query.
   This is a reversible decision -- if Phase 3 testing shows real
   free-text queries missing obvious matches due to stemming, that's
   the first knob to turn, not the last.

3. `keyword` fields use a lowercase normalizer so `module=login` and
   `module=Login` both filter correctly, and a case-typo in an ID query
   ("bug-1013" vs "BUG-1013") still resolves via term query. Filters
   should not silently depend on the user matching stored casing exactly.

4. `number_of_replicas: 0`. This is a single-node dev OpenSearch
   cluster (per docker-compose.yml / phase-01-handoff.md); leaving the
   default 1 replica means the index sits in `yellow` health forever
   because there's no second node to host it. That yellow status is
   normal for the cluster overall (verify_infra.py already treats it as
   healthy), but there's no reason to manufacture unassigned shards for
   an index we control.
"""

INDEX_SETTINGS = {
    "index": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
    },
    "analysis": {
        "normalizer": {
            "lowercase_normalizer": {
                "type": "custom",
                "filter": ["lowercase"],
            }
        }
    },
}

INDEX_MAPPING = {
    "properties": {
        "document_id": {"type": "integer"},
        "doc_type": {"type": "keyword", "normalizer": "lowercase_normalizer"},
        "external_id": {"type": "keyword", "normalizer": "lowercase_normalizer"},
        "title": {
            "type": "text",
            "analyzer": "standard",
            "fields": {
                "keyword": {"type": "keyword", "ignore_above": 256}
            },
        },
        "module": {"type": "keyword", "normalizer": "lowercase_normalizer"},
        "status": {"type": "keyword", "normalizer": "lowercase_normalizer"},
        "date_created": {"type": "date", "format": "strict_date_optional_time||epoch_millis"},
        "cleaned_text": {"type": "text", "analyzer": "standard"},
        "error_codes": {"type": "keyword", "normalizer": "lowercase_normalizer"},
        "mentioned_bug_ids": {"type": "keyword", "normalizer": "lowercase_normalizer"},
        "mentioned_tc_ids": {"type": "keyword", "normalizer": "lowercase_normalizer"},
        "ingested_at": {"type": "date"},
    }
}

INDEX_BODY = {"settings": INDEX_SETTINGS, "mappings": INDEX_MAPPING}
