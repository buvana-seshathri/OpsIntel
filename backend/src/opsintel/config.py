from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://opsintel:opsintel@localhost:5432/opsintel"

    llm_provider: Literal["mock", "groq", "openai"] = "mock"
    llm_model: str = "openai/gpt-oss-120b"
    groq_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    llm_timeout_seconds: float = 60.0

    # "fastembed" runs a real embedding model locally; "hashing" needs no download.
    embedder: Literal["fastembed", "hashing"] = "fastembed"
    embedding_model: str = "BAAI/bge-small-en-v1.5"

    # HS256 signing key for access tokens. The default is for local development only.
    jwt_secret: SecretStr = SecretStr("dev-only-insecure-secret-change-me-0123456789")
    jwt_issuer: str = "opsintel"
    jwt_audience: str = "opsintel-mcp"
    mcp_base_url: str = "http://localhost:8001"


@lru_cache
def get_settings() -> Settings:
    return Settings()
