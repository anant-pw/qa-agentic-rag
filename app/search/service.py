from opensearchpy import OpenSearch

from app.search.queries import build_search_query


def get_client(host: str, port: int) -> OpenSearch:
    # Matches Phase 1's health-check pattern (app/main.py): plain HTTP,
    # no auth, no TLS -- consistent with `plugins.security.disabled=true`
    # in docker-compose.yml for local dev. Do not reuse this client
    # config against anything internet-facing.
    return OpenSearch(hosts=[{"host": host, "port": port}], use_ssl=False, verify_certs=False)


def search(
    client: OpenSearch,
    alias: str,
    q: str | None = None,
    doc_type: str | None = None,
    module: str | None = None,
    status: str | None = None,
    size: int = 10,
) -> list[dict]:
    body = build_search_query(q=q, doc_type=doc_type, module=module, status=status, size=size)
    resp = client.search(index=alias, body=body)
    return [
        {
            "document_id": hit["_source"]["document_id"],
            "external_id": hit["_source"]["external_id"],
            "title": hit["_source"]["title"],
            "doc_type": hit["_source"]["doc_type"],
            "module": hit["_source"]["module"],
            "status": hit["_source"]["status"],
            "score": hit["_score"],
        }
        for hit in resp["hits"]["hits"]
    ]
