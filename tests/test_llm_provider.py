import pytest

from app.llm_provider import (
    DocumentIntelligenceProvider,
    LLMProviderError,
    LaunchpadProvider,
    OllamaProvider,
    get_provider,
)


@pytest.mark.parametrize(
    ("name", "expected_type"),
    [
        ("launchpad", LaunchpadProvider),
        ("ollama", OllamaProvider),
        ("doc_intelligence", DocumentIntelligenceProvider),
    ],
)
def test_provider_selection(name, expected_type):
    assert isinstance(get_provider(name), expected_type)


def test_unsupported_provider():
    with pytest.raises(LLMProviderError, match="Unsupported LLM provider"):
        get_provider("unknown")
