"""Boundary between natural-language screening and typed conditions."""

from __future__ import annotations

from typing import Protocol

from .models import ScreeningConditions


class QueryParser(Protocol):
    """An LLM or rule-based parser can implement this contract."""

    def parse(self, query: str) -> ScreeningConditions:
        """Return validated conditions or raise a user-visible parse error."""


class StructuredQueryParser:
    """MVP parser for callers that already provide validated conditions."""

    def parse(self, query: str) -> ScreeningConditions:
        raise NotImplementedError(
            "natural-language parsing is intentionally a separate, later adapter"
        )

