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

    # Sales vouchers (see docs/sales-rules.md)
    sales_voucher_type: str = "Sales"
    sales_ledger: str = "Sales"
    sales_interstate_ledger: str | None = None  # None = use sales_ledger for IGST sales too
    round_off_ledger: str | None = None  # None = the single ledger whose name contains "round"
    cgst_ledger: str | None = None  # None = the single ledger with GST duty head CGST
    sgst_ledger: str | None = None  # ... SGST/UTGST
    igst_ledger: str | None = None  # ... IGST
    sales_number_prefix: str | None = None  # None = derived from existing numbers, e.g. "ABC"
    godown: str | None = "Main Location"
    batch: str | None = "Primary Batch"

    # Purchase invoices (see docs/purchase-invoices.md)
    purchase_invoice_dir: str | None = None  # full path of the folder where invoice PDFs are saved
    purchase_voucher_type: str = "Purchase"
    purchase_ledger: str = "Purchase"
    purchase_watch_seconds: float = 5.0

    # App
    log_level: str = "INFO"
    audit_db_path: str = "data/audit.db"  # local only: contains customer data

    @field_validator("tally_host")
    @classmethod
    def _add_scheme(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        return value if value.startswith(("http://", "https://")) else f"http://{value}"

    @field_validator(
        "tally_company",
        "gemini_api_key",
        "openrouter_api_key",
        "sales_interstate_ledger",
        "round_off_ledger",
        "cgst_ledger",
        "sgst_ledger",
        "igst_ledger",
        "sales_number_prefix",
        "godown",
        "batch",
        "purchase_invoice_dir",
        mode="before",
    )
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
