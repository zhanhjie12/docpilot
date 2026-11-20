"""Model transport errors and extractive answers remain bounded and replayable."""

from __future__ import annotations

import httpx

from app.core.config import Settings
from app.infrastructure.llm.client import LLMProviderError, OpenAICompatibleLLMProvider
from app.services.evidence_answer import select_answer_spans


def _settings() -> Settings:
    return Settings(
        llm_provider="openai_compatible", llm_api_key="test-only",
        llm_base_url="https://relay.example/v1", llm_model="test-model",
    )


def test_relay_adapter_reads_json_content_without_logging_secret():
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-only"
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"spans":[]}'}}]})

    client = httpx.Client(transport=httpx.MockTransport(handle), base_url="https://relay.example/v1/")
    provider = OpenAICompatibleLLMProvider(_settings(), client=client)
    assert provider.generate("问题", response_schema={}) == '{"spans":[]}'
    client.close()


def test_answer_rejects_fabricated_span_and_bad_upstream_status():
    citations = [{"citation_id": "cite_1", "quote": "收到发票后30日内付款。"}]

    class StaticProvider:
        provider_name = "openai_compatible"

        def generate(self, _prompt: str, *, response_schema: dict) -> str:
            return '{"spans":[{"citation_id":"cite_1","quote_span":"90日内付款"}]}'

    assert select_answer_spans("何时付款", citations, StaticProvider()) == []

    client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(401, text="secret-upstream")),
        base_url="https://relay.example/v1/",
    )
    provider = OpenAICompatibleLLMProvider(_settings(), client=client)
    try:
        provider.generate("问题", response_schema={})
        assert False, "expected sanitized upstream failure"
    except LLMProviderError as exc:
        assert str(exc) == "LLM_AUTH_FAILED"
        assert "secret-upstream" not in str(exc)
    finally:
        client.close()
