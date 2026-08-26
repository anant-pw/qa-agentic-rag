from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_user: str = "rag_user"
    postgres_password: str = "rag_password"
    postgres_db: str = "rag_db"

    opensearch_host: str = "opensearch"
    opensearch_port: int = 9200

    # Ollama runs natively on the host, not as a container — see docker-compose.yml
    ollama_host: str = "host.docker.internal"
    ollama_port: int = 11434

    class Config:
        env_file = ".env"


settings = Settings()
