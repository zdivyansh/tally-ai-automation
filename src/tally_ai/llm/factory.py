"""Create a LangChain chat model for the configured provider.

All providers return a `BaseChatModel`, so callers can use
`.with_structured_output(PydanticModel)` regardless of provider.
"""

from langchain_core.language_models import BaseChatModel

from tally_ai.config import LLMProvider, Settings, get_settings


class LLMConfigError(RuntimeError):
    """The selected LLM provider is missing configuration."""


def create_chat_model(settings: Settings | None = None) -> BaseChatModel:
    settings = settings or get_settings()
    provider = settings.llm_provider

    if provider is LLMProvider.OLLAMA:
        from langchain_ollama import ChatOllama

        return ChatOllama(
            model=settings.ollama_model,
            base_url=settings.ollama_base_url,
            temperature=settings.llm_temperature,
            reasoning=False,  # thinking mode is slow and not needed for extraction
            client_kwargs={"timeout": settings.ollama_timeout},
        )

    if provider is LLMProvider.GEMINI:
        if settings.gemini_api_key is None:
            raise LLMConfigError("LLM_PROVIDER=gemini but GEMINI_API_KEY is not set")
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=settings.gemini_model,
            google_api_key=settings.gemini_api_key,
            temperature=settings.llm_temperature,
        )

    if provider is LLMProvider.OPENROUTER:
        if settings.openrouter_api_key is None:
            raise LLMConfigError("LLM_PROVIDER=openrouter but OPENROUTER_API_KEY is not set")
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=settings.openrouter_model,
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            temperature=settings.llm_temperature,
        )

    raise LLMConfigError(f"unsupported LLM provider: {provider}")
