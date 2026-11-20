"""Evaluate versioned, cited contract rules after structured extraction."""

from __future__ import annotations

from app.playbooks.evaluator import evaluate_playbook
from app.infrastructure.providers import LLMProvider

from .model_assist import suggest_review_assistance
from .state import NodeResult, TaskState


def risk_node(state: TaskState, llm_provider: LLMProvider | None = None) -> NodeResult:
    """规则命中必须对应真实证据；未知字段只列入待确认摘要。"""

    if not state.evidence:
        return NodeResult(node="risk", status="waiting_human", error="风险判断缺少证据")

    unresolved = [
        {
            "document_id": field.document_id,
            "version_id": field.version_id,
            "name": field.name,
            "status": field.status,
        }
        for field in state.extracted_fields
        if field.status in {"missing", "unknown", "needs_review"}
    ]
    # The evaluator reads only cited, found fields and returns deterministic IDs.
    state.risks = evaluate_playbook(state.task_id, state.extracted_fields)
    suggestions = suggest_review_assistance(state, llm_provider)
    return NodeResult(
        node="risk",
        status="succeeded",
        output={"finding_count": len(state.risks), "unresolved_fields": unresolved,
                "model_assistance": suggestions},
    )


class RiskAgent:
    """固定 Playbook 风险规则的工作流适配器。"""

    def run(self, state: TaskState) -> NodeResult:
        return risk_node(state)
