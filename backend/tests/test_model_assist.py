"""Model suggestions are drafts and must point into approved source quotes."""

from __future__ import annotations

import json

from app.agents.model_assist import suggest_review_assistance
from app.agents.state import EvidenceRef, RiskFinding, TaskState


class StaticProvider:
    provider_name = "openai_compatible"

    def __init__(self, body: dict):
        self.body = body

    def generate(self, _prompt: str, *, response_schema: dict) -> str:
        assert response_schema["type"] == "object"
        return json.dumps(self.body, ensure_ascii=False)


def _state() -> TaskState:
    state = TaskState(task_id="task", task_type="review", document_ids=["doc"])
    citation = EvidenceRef(
        citation_id="cite_chunk", document_id="doc", version_id="ver",
        quote="乙方承担无限责任。", page=1, char_start=0, char_end=10,
    )
    state.evidence = [citation]
    state.risks = [RiskFinding(
        finding_id="finding", document_id="doc", version_id="ver",
        severity="high", rule_id="unlimited_liability", rule_version="1",
        title="无限责任", rule_hit=True, explanation="原文有无限责任",
        recommendation="请审阅", citations=[citation],
    )]
    return state


def test_model_assistance_filters_unreplayable_values_and_foreign_findings():
    provider = StaticProvider({
        "fields": [
            {"name": "liability_cap", "value": "无限责任", "citation_id": "cite_chunk"},
            {"name": "amount", "value": "100万元", "citation_id": "cite_chunk"},
        ],
        "risk_explanations": [
            {"finding_id": "finding", "citation_id": "cite_chunk", "quote_span": "无限责任", "explanation": "需要人工审阅责任范围"},
            {"finding_id": "foreign", "citation_id": "cite_chunk", "quote_span": "无限责任", "explanation": "伪造风险"},
        ],
    })
    result = suggest_review_assistance(_state(), provider)
    assert result["status"] == "suggested"
    assert [(item["name"], item["status"]) for item in result["fields"]] == [
        ("liability_cap", "needs_review")
    ]
    assert [(item["finding_id"], item["status"]) for item in result["risk_explanations"]] == [
        ("finding", "draft_needs_review")
    ]


def test_model_assistance_invalid_json_fails_closed():
    class BrokenProvider(StaticProvider):
        def generate(self, _prompt: str, *, response_schema: dict) -> str:
            return "not-json"

    result = suggest_review_assistance(_state(), BrokenProvider({}))
    assert result == {"fields": [], "risk_explanations": [], "status": "unavailable"}
