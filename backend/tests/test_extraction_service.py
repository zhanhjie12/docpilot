"""事实抽取回归测试，覆盖合同期限与付款期限的字段边界。"""

from app.services.extraction_service import extract_facts


def test_payment_period_is_not_contract_duration() -> None:
    """付款期限出现时不应填充合同存续期限。"""

    facts = extract_facts(
        "1. Payment Terms\nPayment is due within 30 days after acceptance."
    )

    assert facts["payment_days"] == 30
    assert facts["duration_days"] is None
    assert "duration_days" in facts["missing_fields"]


def test_explicit_contract_duration_is_normalized_separately() -> None:
    """显式合同期限和付款期限应分别抽取并换算为天数。"""

    facts = extract_facts(
        "Contract term: 2 years. Payment terms: 30 days after acceptance."
    )

    assert facts["duration_days"] == 730
    assert facts["payment_days"] == 30
