"""按文档和版本独立抽取字段，并保留恰好支持该字段的引用。"""

from __future__ import annotations

import re
from typing import Any

from app.services.extraction_service import extract_facts

from .state import EvidenceRef, ExtractedField, NodeResult, TaskState


FIELD_NAMES = (
    "amount",
    "duration_days",
    "payment_days",
    "payment_terms",
    "acceptance",
    "liability_cap",
    "renewal",
    "confidentiality",
    "dispute_resolution",
    "risk",
)

# 新字段先做可回放的原文条款定位；具体合同优劣须由版本化 Playbook 决定。
_CLAUSE_PATTERNS = {
    "payment_terms": re.compile(r"付款|支付|结算|账期|\bpayment\b", re.I),
    "acceptance": re.compile(r"验收|交付确认|\bacceptance\b", re.I),
    "liability_cap": re.compile(r"责任上限|赔偿上限|赔偿责任|无限责任|liability cap|limitation of liability|uncapped liability", re.I),
    "renewal": re.compile(r"续约|续期|自动延长|\brenewal\b|\brenew\b", re.I),
    "confidentiality": re.compile(r"保密|机密|\bconfidentiality\b|non-disclosure", re.I),
    "dispute_resolution": re.compile(r"争议解决|仲裁|管辖法院|诉讼|dispute resolution|\barbitration\b|\bjurisdiction\b", re.I),
}


def field_value_from_quote(quote: str, name: str) -> Any:
    """以单个引用为边界提取字段，避免从其他片段借值。"""

    pattern = _CLAUSE_PATTERNS.get(name)
    if pattern is None:
        return extract_facts(quote).get(name)
    for sentence in re.split(r"(?<=[。！？；;\n])", quote):
        value = sentence.strip()
        if pattern.search(value):
            return value[:500]
    return None


def _candidates(evidence: list[EvidenceRef], name: str) -> list[tuple[Any, EvidenceRef]]:
    """只收集在同一段证据中实际识别出该字段的值。"""

    found: list[tuple[Any, EvidenceRef]] = []
    for citation in evidence:
        value = field_value_from_quote(citation.quote, name)
        if value is not None:
            found.append((value, citation))
    return found


def _field_for_scope(
    document_id: str,
    version_id: str,
    name: str,
    evidence: list[EvidenceRef],
) -> ExtractedField:
    """重复值可以合并；冲突值必须标记待确认，不猜测哪一处为准。"""

    candidates = _candidates(evidence, name)
    if not candidates:
        return ExtractedField(
            name=name,
            document_id=document_id,
            version_id=version_id,
            status="unknown",
            confidence=0.0,
            reason="当前检索片段未找到明确字段，不能断言整份合同缺失",
        )
    values = {str(value) for value, _ in candidates}
    citations = list(dict.fromkeys(citation.citation_id for _, citation in candidates))
    sources = [
        next(citation for _, citation in candidates if citation.citation_id == citation_id)
        for citation_id in citations
    ]
    if len(values) > 1:
        return ExtractedField(
            name=name,
            document_id=document_id,
            version_id=version_id,
            status="needs_review",
            confidence=0.0,
            citations=sources,
            reason="同一字段在该版本的不同证据中出现冲突值",
        )
    return ExtractedField(
        name=name,
        document_id=document_id,
        version_id=version_id,
        value=candidates[0][0],
        status="found",
        confidence=0.9,
        citations=sources,
    )


def extract_node(state: TaskState) -> NodeResult:
    """按每个 ``document_id + version_id`` 分组，避免批量合同字段串用。"""

    if not state.evidence:
        state.extracted_fields = [
            ExtractedField(name=name, status="unknown", confidence=0.0, reason="没有可用证据")
            for name in FIELD_NAMES
        ]
        return NodeResult(
            node="extract",
            status="waiting_human",
            output={"field_count": len(FIELD_NAMES)},
            error="没有可用证据，无法抽取字段",
        )

    grouped: dict[tuple[str, str], list[EvidenceRef]] = {}
    for citation in state.evidence:
        grouped.setdefault((citation.document_id, citation.version_id), []).append(citation)
    fields = [
        _field_for_scope(document_id, version_id, name, evidence)
        for (document_id, version_id), evidence in grouped.items()
        for name in FIELD_NAMES
    ]
    state.extracted_fields = fields
    conflicting = [
        {"document_id": field.document_id, "version_id": field.version_id, "name": field.name}
        for field in fields
        if field.status == "needs_review"
    ]
    found_count = sum(field.status == "found" for field in fields)
    return NodeResult(
        node="extract",
        status="waiting_human" if conflicting or not found_count else "succeeded",
        output={
            "field_count": len(fields),
            "found_count": found_count,
            "conflicting_fields": conflicting,
        },
        error="字段存在冲突或没有抽取到明确值" if conflicting or not found_count else None,
    )


class ExtractionAgent:
    """确定性抽取服务的工作流适配器。"""

    def run(self, state: TaskState) -> NodeResult:
        return extract_node(state)
