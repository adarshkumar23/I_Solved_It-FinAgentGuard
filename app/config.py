from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    llm_validator_enabled: bool = True
    llm_provider: Literal["openai", "anthropic"] = "openai"
    llm_fail_open: bool = True
    llm_timeout_seconds: float = 8.0

    openai_api_key: str | None = None
    openai_model: str = "gpt-4.1-mini"

    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-3-5-sonnet-latest"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
