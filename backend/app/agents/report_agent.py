"""仅把验证节点放行的结构化结果编排成报告。"""

from __future__ import annotations

from .state import EvidenceRef, NodeResult, TaskState


def _qa_answer(evidence: list[EvidenceRef]) -> str:
    """用最多两段已验证原文作摘录式回答，避免无依据的自由推断。"""

    excerpts = []
    for item in evidence[:2]:
        quote = item.quote.strip()
        if len(quote) > 180:
            quote = quote[:180] + "…"
        excerpts.append(f"{quote} [{item.citation_id}]")
    return "\n".join(excerpts)


def report_node(state: TaskState) -> NodeResult:
    """按 verify 结果筛选字段、风险与引用；无证据的 QA 不生成回答。"""

    verification = state.node_results.get("verify")
    if verification is None or verification.status != "succeeded":
        state.status = "waiting_human"
        return NodeResult(node="report", status="waiting_human", error="报告必须建立在验证通过的结果上")

    approved_field_indexes = set(verification.output.get("approved_field_indexes", []))
    approved_risk_ids = set(verification.output.get("approved_risk_ids", []))
    verified_citation_ids = set(verification.output.get("verified_citation_ids", []))
    evidence = [item for item in state.evidence if item.citation_id in verified_citation_ids]
    fields = [
        field.model_dump(mode="json")
        for index, field in enumerate(state.extracted_fields)
        if index in approved_field_indexes
    ]
    risks = [
        risk.model_dump(mode="json")
        for risk in state.risks
        if risk.finding_id in approved_risk_ids
    ]
    if state.task_type == "qa" and not evidence:
        state.status = "waiting_human"
        return NodeResult(node="report", status="waiting_human", error="没有已验证证据，无法回答")

    report = {
        "task_id": state.task_id,
        "task_type": state.task_type,
        "status": state.status,
        "fields": fields,
        "risks": risks,
        "citations": [item.model_dump(mode="json") for item in evidence],
    }
    # Model notes remain drafts even when their cited source has been verified.
    assistance = state.node_results.get("risk")
    if assistance is not None:
        suggestions = assistance.output.get("model_assistance")
        if isinstance(suggestions, dict):
            report["model_assistance"] = {
                "status": suggestions.get("status", "disabled"),
                "fields": [item for item in suggestions.get("fields", [])
                           if item.get("citation_id") in verified_citation_ids],
                "risk_explanations": [item for item in suggestions.get("risk_explanations", [])
                                      if item.get("citation_id") in verified_citation_ids
                                      and item.get("finding_id") in approved_risk_ids],
            }
    if state.task_type == "qa":
        report["answer"] = _qa_answer(evidence)
        report["answer_type"] = "quoted_evidence"
    if state.task_type == "compare":
        compare = state.node_results.get("compare")
        report["changes"] = list(compare.output.get("changes", [])) if compare else []
    state.report = report
    return NodeResult(
        node="report",
        status="succeeded",
        output={"field_count": len(fields), "finding_count": len(risks)},
    )


class ReportAgent:
    """报告编排的工作流适配器。"""

    def run(self, state: TaskState) -> NodeResult:
        return report_node(state)
