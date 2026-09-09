from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_user: str = "rag_user"
    postgres_password: str = "rag_password"
    postgres_db: str = "rag_db"

    opensearch_host: str = "opensearch"
    opensearch_port: int = 9200
    opensearch_index_alias: str = "qa_documents"

    # Ollama runs natively on the host, not as a container — see docker-compose.yml
    ollama_host: str = "host.docker.internal"
    ollama_port: int = 11434
    ollama_keep_alive: str = "24h"
    # Phase 6 addition: was hardcoded as CHAT_MODEL in llm.py, which meant
    # comparing models required a code edit instead of a config change.
    # Exposed here so a swap is one env var, matching how every other
    # infra choice in this project is already settings-driven.
    ollama_chat_model: str = "gpt-oss:20b"
    

    # Phase 6: Redis response cache for /generate. Containerized (unlike
    # Ollama) since there's no GPU/acceleration reason to run it natively,
    # and it's a single lightweight container — not the same local-first
    # tension Langfuse's 6-container stack presented (see
    # app/generation/cache.py and app/observability/logger.py docstrings).
    redis_host: str = "redis"
    redis_port: int = 6379

    class Config:
        env_file = ".env"


settings = Settings()
