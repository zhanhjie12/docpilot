"""Deterministic metadata filtering for multi-document screening."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import isfinite

from .models import ComparisonOperator, ScreeningConditions, ScreeningMatch


def _compare(actual: float, operator: ComparisonOperator, expected: float) -> bool:
    if operator is ComparisonOperator.EQ:
        return actual == expected
    if operator is ComparisonOperator.GT:
        return actual > expected
    if operator is ComparisonOperator.GTE:
        return actual >= expected
    if operator is ComparisonOperator.LT:
        return actual < expected
    if operator is ComparisonOperator.LTE:
        return actual <= expected
    raise ValueError(f"unsupported operator: {operator}")


def screen_metadata(
    documents: Sequence[Mapping[str, object]],
    conditions: ScreeningConditions,
) -> list[ScreeningMatch]:
    """Filter metadata without guessing missing values.

    ``documents`` must contain a stable ``document_id``. A missing field fails the
    condition and is reported, allowing a later extraction step to fill the gap.
    """

    results: list[ScreeningMatch] = []
    numeric_conditions = [conditions.amount, conditions.duration_days]
    for document in documents:
        document_id = str(document.get("document_id", ""))
        matched_conditions: list[str] = []
        missing_fields: list[str] = []
        matched = True

        for condition in numeric_conditions:
            if condition is None:
                continue
            actual = document.get(condition.field)
            # bool 是 int 的子类；布尔值和无穷大均不能作为有效合同金额/期限。
            if isinstance(actual, bool) or not isinstance(actual, (int, float)):
                missing_fields.append(condition.field)
                matched = False
                continue
            try:
                numeric_actual = float(actual)
            except OverflowError:
                numeric_actual = float("inf")
            if not isfinite(numeric_actual) or numeric_actual < 0:
                missing_fields.append(condition.field)
                matched = False
                continue
            if _compare(numeric_actual, condition.operator, condition.value):
                matched_conditions.append(condition.field)
            else:
                matched = False

        if conditions.risk is not None:
            actual_risk = document.get("risk")
            if actual_risk is None:
                missing_fields.append("risk")
                matched = False
            elif str(actual_risk).lower() == conditions.risk:
                matched_conditions.append("risk")
            else:
                matched = False

        results.append(
            ScreeningMatch(
                document_id=document_id,
                matched=matched,
                matched_conditions=matched_conditions,
                missing_fields=missing_fields,
            )
        )
    return results
