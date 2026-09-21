"""Configuration management for the indexer service.

Single source of truth is ``config.yml`` (see ``config.example.yml``). Real
environment variables override it; nested groups use a double-underscore
delimiter (``EMBEDDING__API_KEY``, ...). The file path defaults to
``./config.yml`` and can be relocated with ``CONFIG_YAML``.
"""

import os
from pathlib import Path

from pydantic import BaseModel
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)


class EmbeddingConfig(BaseModel):
    """The OpenAI-compatible embedding endpoint (key, base URL, model id).

    Vendors (SiliconFlow, OpenAI, a self-hosted gateway, ...) differ only in
    these values, never in code.
    """

    api_key: str = ""
    base_url: str = ""  # empty -> client default (api.openai.com)
    model: str = ""

    def client_kwargs(self) -> dict:
        """Keyword args for an OpenAI-compatible client built from this group."""
        if not self.api_key:
            raise ValueError(
                "Configuration error: embedding.api_key must be set in config.yml"
            )
        kwargs: dict = {"api_key": self.api_key}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return kwargs


class Settings(BaseSettings):
    """Application settings for the indexer service."""

    model_config = SettingsConfigDict(
        yaml_file=str(Path(os.environ.get("CONFIG_YAML", "config.yml"))),
        env_nested_delimiter="__",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
        **kwargs,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Precedence: init kwargs > real environment > config.yml (if present)."""
        return (
            init_settings,
            env_settings,
            YamlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )

    # Embedding endpoint — provider-agnostic (see EmbeddingConfig).
    embedding: EmbeddingConfig = EmbeddingConfig()

    # Database configuration
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "postgres_db"
    postgres_user: str = "postgres_user"
    postgres_password: str = "postgres_password"

    # Embedding batch size (applies to all providers)
    embedding_batch_size: int = 20

    # Indexer configuration
    indexer_cron_schedule: str = "0 * * * *"  # cron: "minute hour day month weekday"
    indexer_scope: str = "full"


settings = Settings()
