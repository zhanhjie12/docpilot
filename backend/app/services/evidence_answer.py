"""Use a model to select extractive evidence spans, then build the answer locally."""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field

from app.infrastructure.providers import LLMProvider


class AnswerSpan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    citation_id: str = Field(min_length=1)
    quote_span: str = Field(min_length=1, max_length=180)


class AnswerSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spans: list[AnswerSpan] = Field(max_length=2)


def select_answer_spans(question: str, citations: list[dict], provider: LLMProvider) -> list[dict]:
    """Reject any model text that cannot be replayed inside an allowed quote."""

    if provider.provider_name == "mock" or not citations:
        return []
    sources = [
        {"citation_id": item["citation_id"], "quote": item["quote"][:1200]}
        for item in citations[:3]
    ]
    prompt = (
        "从下列证据中选取至多两段能回答问题的连续原文。只返回 JSON "
        '{"spans":[{"citation_id":"...","quote_span":"原文连续子串"}]}。'
        "没有足够证据时返回空 spans。问题："
        + question[:1000] + "\n证据：" + json.dumps(sources, ensure_ascii=False)
    )
    try:
        result = AnswerSelection.model_validate_json(
            provider.generate(prompt, response_schema=AnswerSelection.model_json_schema())
        )
    except (ValueError, RuntimeError):
        return []
    by_id = {item["citation_id"]: item for item in citations}
    selected: list[dict] = []
    for span in result.spans:
        source = by_id.get(span.citation_id)
        if source is None or span.quote_span not in source["quote"]:
            return []
        selected.append({"citation_id": span.citation_id, "quote_span": span.quote_span})
    return selected
