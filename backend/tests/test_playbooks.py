"""Business rule coverage and conservative handling of incomplete evidence."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agents.state import EvidenceRef, ExtractedField
from app.playbooks.evaluator import evaluate_playbook
from app.playbooks.loader import load_playbook
from app.playbooks.models import Playbook


def _field(name: str, value: object, quote: str, *, status: str = "found") -> ExtractedField:
    """Build a version-scoped field with an exact source object."""

    citation = EvidenceRef(
        citation_id=f"cite_{name}", document_id="doc_1", version_id="ver_1",
        page=1, section="合同条款", quote=quote, char_start=0, char_end=len(quote),
    )
    return ExtractedField(
        name=name, document_id="doc_1", version_id="ver_1",
        value=value, status=status, confidence=0.9, citations=[citation],
    )


def test_four_business_rules_have_stable_cited_results():
    """Each shipped predicate emits a traceable result and stable retry ID."""

    fields = [
        _field("payment_days", 90, "付款期限：90日。"),
        _field("renewal", "到期自动续约，提前15日书面通知。", "到期自动续约，提前15日书面通知。"),
        _field("liability_cap", "乙方承担无限责任。", "乙方承担无限责任。"),
        _field("dispute_resolution", "争议由对方所在地人民法院管辖。", "争议由对方所在地人民法院管辖。"),
    ]
    findings = evaluate_playbook("task_1", fields)
    assert {item.rule_id for item in findings} == {
        "payment_days_gt_60", "auto_renewal_short_notice",
        "unlimited_liability", "counterparty_venue",
    }
    assert all(item.citations and item.playbook_id == "supplier_contract" for item in findings)
    assert [item.finding_id for item in findings] == [
        item.finding_id for item in evaluate_playbook("task_1", fields)
    ]


@pytest.mark.parametrize("field", [
    _field("payment_days", 60, "付款期限：60日。"),
    _field("payment_days", 90, "付款期限：60日。"),
    _field("renewal", "自动续约。", "自动续约。"),
    _field("liability_cap", "责任上限为合同总价。", "责任上限为合同总价。"),
    _field("payment_days", 90, "付款期限：90日。", status="unknown"),
    _field("renewal", "本合同不自动续约，提前15日另行协商。", "本合同不自动续约，提前15日另行协商。"),
    _field("liability_cap", "乙方不承担无限责任。", "乙方不承担无限责任。"),
    _field("dispute_resolution", "争议不由乙方所在地人民法院管辖。", "争议不由乙方所在地人民法院管辖。"),
])
def test_uncertain_or_below_threshold_fields_do_not_create_risks(field: ExtractedField):
    assert evaluate_playbook("task_1", [field]) == []


def test_playbook_rejects_unknown_predicate_and_duplicate_rule_ids():
    """Untrusted configuration cannot add executable or ambiguous rules."""

    valid = load_playbook().model_dump()
    valid["rules"][0]["predicate"] = "python_eval"
    with pytest.raises(ValidationError):
        Playbook.model_validate(valid)
    valid = load_playbook().model_dump()
    valid["rules"][1]["rule_id"] = valid["rules"][0]["rule_id"]
    with pytest.raises(ValidationError):
        Playbook.model_validate(valid)
