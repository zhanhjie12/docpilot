"""Regression tests for deterministic multi-document screening."""

import pytest

from app.screening.filter_engine import screen_metadata
from app.screening.models import (
    ComparisonOperator,
    NumericCondition,
    ScreeningConditions,
)


def test_screening_matches_all_explicit_conditions():
    conditions = ScreeningConditions(
        amount=NumericCondition(field="amount", operator=ComparisonOperator.GT, value=1_000_000),
        duration_days=NumericCondition(
            field="duration_days", operator=ComparisonOperator.GT, value=365
        ),
        risk="high",
    )

    results = screen_metadata(
        [
            {"document_id": "a", "amount": 2_000_000, "duration_days": 730, "risk": "high"},
            {"document_id": "b", "amount": 2_000_000, "duration_days": 730, "risk": "low"},
        ],
        conditions,
    )

    assert [item.document_id for item in results if item.matched] == ["a"]
    assert results[1].matched is False


def test_screening_does_not_guess_missing_fields():
    conditions = ScreeningConditions(
        amount=NumericCondition(field="amount", operator=ComparisonOperator.GT, value=1)
    )

    result = screen_metadata([{"document_id": "missing"}], conditions)[0]

    assert result.matched is False
    assert result.missing_fields == ["amount"]


@pytest.mark.parametrize("amount", [True, False, float("inf"), float("-inf"), float("nan"), -1, 10**400])
def test_screening_never_matches_invalid_numeric_metadata(amount):
    conditions = ScreeningConditions(
        amount=NumericCondition(field="amount", operator=ComparisonOperator.GTE, value=0)
    )
    result = screen_metadata([{"document_id": "doc_1", "amount": amount}], conditions)[0]
    assert result.matched is False
    assert result.missing_fields == ["amount"]
