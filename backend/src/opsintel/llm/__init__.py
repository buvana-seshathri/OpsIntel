from opsintel.config import Settings, get_settings
from opsintel.llm.base import (
    LLMClient,
    LLMResponse,
    MalformedToolCall,
    Message,
    StructuredOutputError,
    ToolCall,
    ToolSpec,
    Usage,
    complete_structured,
)
from opsintel.llm.mock import MockLLM
from opsintel.llm.openai_compat import GROQ_BASE_URL, OpenAICompatClient

__all__ = [
    "LLMClient",
    "LLMResponse",
    "MalformedToolCall",
    "Message",
    "MockLLM",
    "StructuredOutputError",
    "ToolCall",
    "ToolSpec",
    "Usage",
    "complete_structured",
    "make_llm",
]


def make_llm(settings: Settings | None = None) -> LLMClient:
    s = settings or get_settings()
    if s.llm_provider == "mock":
        return MockLLM()
    if s.llm_provider == "groq":
        if s.groq_api_key is None:
            raise ValueError("LLM_PROVIDER=groq but GROQ_API_KEY is not set")
        return OpenAICompatClient(
            model=s.llm_model,
            api_key=s.groq_api_key.get_secret_value(),
            base_url=GROQ_BASE_URL,
            timeout=s.llm_timeout_seconds,
        )
    if s.openai_api_key is None:
        raise ValueError("LLM_PROVIDER=openai but OPENAI_API_KEY is not set")
    return OpenAICompatClient(
        model=s.llm_model,
        api_key=s.openai_api_key.get_secret_value(),
        timeout=s.llm_timeout_seconds,
    )
