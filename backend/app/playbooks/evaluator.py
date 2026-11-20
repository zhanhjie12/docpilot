"""Deterministic risk decisions from cited fields and a pinned playbook."""

from __future__ import annotations

import re
from hashlib import sha256

from app.agents.state import EvidenceRef, ExtractedField, RiskFinding
from app.services.extraction_service import extract_facts

from .loader import load_playbook
from .models import PlaybookRule


def _renewal_notice_days(text: str) -> int | None:
    """Require automatic renewal and its notice window in the same sentence."""

    for sentence in re.split(r"[。；;\n]", text):
        if not re.search(r"自动续约|自动续期|自动延长", sentence):
            continue
        if re.search(r"不(?:会|得|予)?自动(?:续约|续期|延长)|不设自动续|须另行签订", sentence):
            continue
        match = re.search(r"提前\s*(\d{1,3})\s*(?:个)?(?:自然)?[日天]", sentence)
        if match:
            return int(match.group(1))
    return None


def _supports(rule: PlaybookRule, field: ExtractedField, quote: str) -> bool:
    """Evaluate only explicitly supported positive evidence."""

    if field.status != "found" or field.name != rule.field:
        return False
    if rule.predicate == "explicit_high_risk":
        return field.value == "high" and extract_facts(quote).get("risk") == "high"
    if rule.predicate == "payment_days_gt":
        return (
            type(field.value) in (int, float)
            and field.value > rule.threshold_days
            and extract_facts(quote).get("payment_days") == field.value
        )
    if rule.predicate == "auto_renewal_short_notice":
        days = _renewal_notice_days(quote)
        return days is not None and days < rule.threshold_days
    if rule.predicate == "unlimited_liability":
        return any(
            re.search(r"无限(?:赔偿)?责任|不设(?:赔偿)?责任上限|无(?:赔偿)?责任上限", sentence)
            and not re.search(r"不承担无限(?:赔偿)?责任|不适用无限(?:赔偿)?责任|不属于无限(?:赔偿)?责任", sentence)
            for sentence in re.split(r"[。；;\n]", quote)
        )
    if rule.predicate == "counterparty_venue":
        return any(
            re.search(r"(?:争议|纠纷).{0,24}(?:对方|乙方)所在地.{0,12}(?:法院|人民法院)", sentence)
            and not re.search(r"不由(?:对方|乙方)所在地|不受(?:对方|乙方)所在地", sentence)
            for sentence in re.split(r"[。；;\n]", quote)
        )
    return False


def evaluate_playbook(task_id: str, fields: list[ExtractedField]) -> list[RiskFinding]:
    """Create stable findings; every finding carries the exact supporting quotes."""

    playbook = load_playbook()
    findings: list[RiskFinding] = []
    scopes = sorted({(field.document_id, field.version_id) for field in fields if field.document_id and field.version_id})
    for document_id, version_id in scopes:
        scoped = [field for field in fields if field.document_id == document_id and field.version_id == version_id]
        for rule in playbook.rules:
            citations: dict[str, EvidenceRef] = {}
            for field in scoped:
                for citation in field.citations:
                    if (
                        citation.document_id == document_id
                        and citation.version_id == version_id
                        and _supports(rule, field, citation.quote)
                    ):
                        citations[citation.citation_id] = citation
            if not citations:
                continue
            raw_id = f"{task_id}\0{document_id}\0{version_id}\0{playbook.playbook_id}\0{playbook.version}\0{rule.rule_id}"
            findings.append(
                RiskFinding(
                    finding_id=f"finding_{sha256(raw_id.encode('utf-8')).hexdigest()[:24]}",
                    document_id=document_id,
                    version_id=version_id,
                    playbook_id=playbook.playbook_id,
                    severity=rule.severity,
                    rule_id=rule.rule_id,
                    rule_version=playbook.version,
                    title=rule.title,
                    rule_hit=True,
                    explanation=rule.explanation,
                    recommendation=rule.recommendation,
                    citations=list(citations.values()),
                )
            )
    return findings
