"""Configuration management for the indexer service."""

import os

from pydantic_settings import BaseSettings


def _get_required_env(key: str) -> str:
    """Get a required environment variable.

    Args:
        key: Environment variable name

    Returns:
        The environment variable value

    Raises:
        ValueError: If the environment variable is not set
    """
    value = os.environ.get(key)
    if not value:
        raise ValueError(f"Configuration error: {key} must be set in .env")
    return value


# Map provider names to their environment variable prefixes
_PROVIDER_ENV_PREFIX = {
    "openai": "OPENAI",
    "siliconflow": "SILICONFLOW",
}


def _embedding_env_prefix() -> str:
    """Resolve the env var prefix for the configured embedding provider."""
    provider = os.environ.get(
        "EMBEDDING_PROVIDER", os.environ.get("LLM_PROVIDER", "openai")
    )
    return _PROVIDER_ENV_PREFIX.get(provider, provider.upper())


class Settings(BaseSettings):
    """Application settings for the indexer service."""

    # Provider configuration
    # The indexer only generates embeddings, so it follows embedding_provider
    # (which defaults to LLM_PROVIDER when EMBEDDING_PROVIDER is unset).
    embedding_provider: str = os.environ.get(
        "EMBEDDING_PROVIDER", os.environ.get("LLM_PROVIDER", "openai")
    )

    # Database configuration
    postgres_host: str = os.environ.get("POSTGRES_HOST", "postgres")
    postgres_port: int = int(os.environ.get("POSTGRES_PORT", "5432"))
    postgres_db: str = os.environ.get("POSTGRES_DB", "postgres_db")
    postgres_user: str = os.environ.get("POSTGRES_USER", "postgres_user")
    postgres_password: str = os.environ.get("POSTGRES_PASSWORD", "postgres_password")

    # Embedding configuration — reads {EMBEDDING_PROVIDER}_EMBEDDING_MODEL
    embedding_model: str = _get_required_env(
        f"{_embedding_env_prefix()}_EMBEDDING_MODEL"
    )
    embedding_batch_size: int = int(os.environ.get("EMBEDDING_BATCH_SIZE", "20"))
    # API keys are read by provider implementations, not here

    # Indexer configuration
    indexer_cron_schedule: str = os.environ.get("INDEXER_CRON_SCHEDULE", "0 * * * *")
    indexer_scope: str = os.environ.get("INDEXER_SCOPE", "full")


settings = Settings()
