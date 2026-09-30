"""Application settings, loaded from environment variables and `.env`."""

from enum import StrEnum
from functools import lru_cache

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMProvider(StrEnum):
    OLLAMA = "ollama"
    GEMINI = "gemini"
    OPENROUTER = "openrouter"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Tally
    tally_host: str = "http://localhost"
    tally_port: int = 9000
    tally_company: str | None = None
    tally_timeout: float = 120.0

    # LLM
    llm_provider: LLMProvider = LLMProvider.OLLAMA
    llm_temperature: float = 0.0

    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "gemma4:e4b"
    ollama_timeout: float = 300.0

    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-2.5-flash-lite"

    openrouter_api_key: SecretStr | None = None
    openrouter_model: str = "meta-llama/llama-3.3-70b-instruct:free"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # App
    log_level: str = "INFO"

    @field_validator("tally_host")
    @classmethod
    def _add_scheme(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        return value if value.startswith(("http://", "https://")) else f"http://{value}"

    @field_validator("tally_company", "gemini_api_key", "openrouter_api_key", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("llm_provider", mode="before")
    @classmethod
    def _lower_provider(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @property
    def tally_url(self) -> str:
        return f"{self.tally_host}:{self.tally_port}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
