"""Ports for model providers; no SDK imports belong in domain or route modules."""

from typing import Protocol, Sequence


class LLMProvider(Protocol):
    provider_name: str

    def generate(self, prompt: str, *, response_schema: dict) -> str:
        """Return structured text that the caller validates with Pydantic."""


class EmbeddingProvider(Protocol):
    provider_name: str
    model_name: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one normalized vector per input text, preserving input order."""


class Reranker(Protocol):
    def rank(self, query: str, passages: Sequence[str]) -> list[float]:
        """Return one relevance score per passage."""
