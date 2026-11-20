"""Contracts for turning a user's screening request into safe conditions."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

from app.domain.schemas import DocumentIds, NonBlankString, RequestModel


class ComparisonOperator(StrEnum):
    """Operators supported by the deterministic screening engine."""

    EQ = "eq"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"


class NumericCondition(RequestModel):
    """A typed comparison extracted from natural language by a parser."""

    field: Literal["amount", "duration_days"]
    operator: ComparisonOperator
    value: float = Field(ge=0, strict=True, allow_inf_nan=False)


class ScreeningConditions(RequestModel):
    """Only fields with an explicit value participate in matching."""

    amount: NumericCondition | None = None
    duration_days: NumericCondition | None = None
    risk: Literal["low", "medium", "high"] | None = None

    @model_validator(mode="after")
    def validate_condition_fields(self) -> Self:
        for field in ("amount", "duration_days"):
            condition = getattr(self, field)
            if condition is not None and condition.field != field:
                raise ValueError(f"{field}.field must be {field}")
        return self


class ScreeningRequest(RequestModel):
    """API-level request; ``conditions`` is the validated parser output."""

    document_ids: DocumentIds = Field(min_length=1, max_length=20)
    query: NonBlankString = Field(max_length=2_000)
    conditions: ScreeningConditions | None = None
    deep_analysis: bool = False


class ScreeningMatch(BaseModel):
    """A transparent match result, suitable for later citation enrichment."""

    document_id: str
    matched: bool
    matched_conditions: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
