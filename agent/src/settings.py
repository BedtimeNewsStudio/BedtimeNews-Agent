"""Configuration management for the agent service.

Single source of truth is ``config.yml`` (see ``config.example.yml``). Real
environment variables override it; nested groups use a double-underscore
delimiter (``GENERATION__API_KEY``, ``EMBEDDING__MODEL``, ...). The file path
defaults to ``./config.yml`` and can be relocated with ``CONFIG_YAML``.
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


class EndpointConfig(BaseModel):
    """One OpenAI-compatible endpoint: credential, base URL and model ids.

    ``generation`` and ``embedding`` each carry their own group — vendors
    (DeepSeek, SiliconFlow, OpenAI, a self-hosted vLLM/OneAPI gateway, ...)
    differ only in these values, never in code.
    """

    api_key: str = ""
    base_url: str = ""  # empty -> client default (api.openai.com)
    model: str = ""
    fast_model: str = ""  # cheap calls (relevance grading); empty -> model

    def client_kwargs(self, label: str) -> dict:
        """Keyword args for an OpenAI-compatible client built from this group."""
        if not self.api_key:
            raise ValueError(
                f"Configuration error: {label}.api_key must be set in config.yml"
            )
        kwargs: dict = {"api_key": self.api_key}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return kwargs


class Settings(BaseSettings):
    """Application settings."""

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

    # Model endpoints — provider-agnostic (see EndpointConfig).
    generation: EndpointConfig = EndpointConfig()
    embedding: EndpointConfig = EndpointConfig()

    # Database configuration
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "postgres_db"
    postgres_user: str = "postgres_user"
    postgres_password: str = "postgres_password"

    # Vector search configuration
    match_threshold: float = 0.4

    # Retrieval configuration
    retrieval_match_count: int = 30
    retrieval_top_k: int = 15
    grading_parallel_threshold: int = 30
    # How much of each chunk the relevance grader gets to see. Headings alone are
    # not enough to judge relevance: they average ~29 characters for chunks of
    # ~736 words, and over half the corpus is headed by the show's opening
    # greeting, which says nothing about the content.
    grading_excerpt_chars: int = 350


settings = Settings()
