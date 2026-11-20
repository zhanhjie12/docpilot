"""Bounded OpenAI-compatible JSON generation behind the application port."""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.core.config import Settings
from app.infrastructure.providers import LLMProvider


class LLMProviderError(RuntimeError):
    """A sanitized error safe to record in a workflow event."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class MockLLMProvider:
    """Offline deterministic provider used in tests and local development."""

    provider_name = "mock"

    def generate(self, prompt: str, *, response_schema: dict) -> str:
        return json.dumps({"candidates": [], "spans": []}, ensure_ascii=False)


class OpenAICompatibleLLMProvider:
    """Call a configured relay without logging prompts, document text or keys."""

    provider_name = "openai_compatible"

    def __init__(self, settings: Settings, *, client: httpx.Client | None = None) -> None:
        self.model = settings.llm_model
        self._api_key = settings.llm_api_key
        self._owns_client = client is None
        self.client = client or httpx.Client(
            base_url=settings.llm_base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            timeout=httpx.Timeout(settings.llm_timeout_seconds),
        )

    def generate(self, prompt: str, *, response_schema: dict) -> str:
        """Return JSON text after checking transport and response envelope."""

        try:
            response = self.client.post(
                "chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self.model,
                    "temperature": 0,
                    "messages": [
                        {"role": "system", "content": "仅使用提供的证据。只返回符合要求的 JSON；无证据时返回空列表。"},
                        {"role": "user", "content": prompt},
                    ],
                },
            )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise LLMProviderError("LLM_TIMEOUT") from exc
        except httpx.HTTPStatusError as exc:
            code = "LLM_AUTH_FAILED" if exc.response.status_code in {401, 403} else (
                "LLM_RATE_LIMITED" if exc.response.status_code == 429 else "LLM_UPSTREAM_UNAVAILABLE"
            )
            raise LLMProviderError(code) from exc
        except httpx.HTTPError as exc:
            raise LLMProviderError("LLM_UPSTREAM_UNAVAILABLE") from exc
        try:
            payload: Any = response.json()
            content = payload["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("empty response")
            return content
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            raise LLMProviderError("LLM_INVALID_RESPONSE") from exc

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


def create_llm_provider(settings: Settings) -> LLMProvider:
    """Select an offline mock or the validated OpenAI-compatible adapter."""

    if settings.llm_provider == "mock":
        return MockLLMProvider()
    if settings.llm_provider == "openai_compatible":
        return OpenAICompatibleLLMProvider(settings)
    raise ValueError("Unsupported LLM_PROVIDER")
