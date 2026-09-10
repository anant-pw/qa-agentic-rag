from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

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
    ollama_chat_model: str
    ollama_temperature: float
    ollama_think: bool
    ollama_connect_timeout: float
    ollama_chat_timeout: float
    ollama_embedding_model: str
    ollama_embedding_dimension: int
    ollama_embedding_timeout: float

    retrieval_size: int
    context_top_n: int
    postgres_connect_timeout: int
    groq_api_key: str | None = None

    # Phase 6: Redis response cache for /generate. Containerized (unlike
    # Ollama) since there's no GPU/acceleration reason to run it natively,
    # and it's a single lightweight container — not the same local-first
    # tension Langfuse's 6-container stack presented (see
    # app/generation/cache.py and app/observability/logger.py docstrings).
    redis_host: str = "redis"
    redis_port: int = 6379

settings = Settings()
