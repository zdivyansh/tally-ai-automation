import pytest

from tally_ai.config import LLMProvider, Settings
from tally_ai.llm import LLMConfigError, create_chat_model


def settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def test_defaults() -> None:
    s = settings()
    assert s.llm_provider is LLMProvider.OLLAMA
    assert s.ollama_model == "gemma4:e4b"
    assert s.tally_url == "http://localhost:9000"
    assert s.llm_temperature == 0


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TALLY_HOST", "192.168.1.3/")
    monkeypatch.setenv("TALLY_PORT", "9001")
    monkeypatch.setenv("LLM_PROVIDER", "Gemini")
    monkeypatch.setenv("TALLY_COMPANY", "  ")
    s = settings()
    assert s.tally_url == "http://192.168.1.3:9001"
    assert s.llm_provider is LLMProvider.GEMINI
    assert s.tally_company is None


def test_ollama_model() -> None:
    model = create_chat_model(settings(ollama_model="gemma4:e4b"))
    assert type(model).__name__ == "ChatOllama"
    assert model.model == "gemma4:e4b"  # type: ignore[attr-defined]
    assert model.temperature == 0  # type: ignore[attr-defined]


def test_gemini_model() -> None:
    model = create_chat_model(settings(llm_provider="gemini", gemini_api_key="k"))
    assert type(model).__name__ == "ChatGoogleGenerativeAI"


def test_openrouter_model() -> None:
    model = create_chat_model(settings(llm_provider="openrouter", openrouter_api_key="k"))
    assert type(model).__name__ == "ChatOpenAI"
    assert "openrouter.ai" in str(model.openai_api_base)  # type: ignore[attr-defined]


@pytest.mark.parametrize("provider", ["gemini", "openrouter"])
def test_missing_api_key(provider: str) -> None:
    with pytest.raises(LLMConfigError, match="API_KEY"):
        create_chat_model(settings(llm_provider=provider, gemini_api_key="", openrouter_api_key=""))
