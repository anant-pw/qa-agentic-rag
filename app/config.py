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
    # Bounds TOTAL streamed-answer wall-clock time, not the per-token read
    # gap ollama_chat_timeout already bounds -- see stream_chat()'s
    # wall_clock_timeout docstring in app/generation/llm.py.
    #
    # 900.0 (the original value) was raised to 2400.0 after real eval runs
    # surfaced TWO real, correct, ID-bearing answers that would have been
    # wrongly aborted by 900s: 1795.7s (machine A, BUG-1023) and 1207.7s
    # (machine B, BUG-1024) -- 2 of 9 semantic-path eval questions, not a
    # rare tail. 900s was a judgment call made before this data existed;
    # 2400s (40 min) is chosen to sit above both observed maxima with
    # margin, not derived from a larger sample -- revisit if a future eval
    # run produces a real answer approaching it.
    #
    # Defaulted (not required in .env) so existing deployments pick it up
    # without an env change; override via OLLAMA_CHAT_WALL_CLOCK_TIMEOUT,
    # or set to 0 to disable.
    ollama_chat_wall_clock_timeout: float = 2400.0
    ollama_embedding_model: str
    ollama_embedding_dimension: int
    ollama_embedding_timeout: float

    retrieval_size: int
    context_top_n: int
    postgres_connect_timeout: int
    groq_api_key: str | None = None
    deterministic_count_routing: bool = True
    vector_score_guardrail_threshold: float = 0.75
    
    # Phase 6: Redis response cache for /generate. Containerized (unlike
    # Ollama) since there's no GPU/acceleration reason to run it natively,
    # and it's a single lightweight container — not the same local-first
    # tension Langfuse's 6-container stack presented (see
    # app/generation/cache.py and app/observability/logger.py docstrings).
    redis_host: str = "redis"
    redis_port: int = 6379

settings = Settings()
