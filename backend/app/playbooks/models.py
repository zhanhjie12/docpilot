"""Strict, data-only schema for an approved contract review playbook."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Predicate = Literal[
    "explicit_high_risk", "payment_days_gt", "auto_renewal_short_notice",
    "unlimited_liability", "counterparty_venue",
]


class PlaybookRule(BaseModel):
    """One whitelisted decision over a cited contract field."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    rule_id: str = Field(min_length=1)
    predicate: Predicate
    field: str = Field(min_length=1)
    severity: Literal["low", "medium", "high"]
    threshold_days: int | None = Field(default=None, ge=0)
    title: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    recommendation: str = Field(min_length=1)

    @model_validator(mode="after")
    def check_threshold(self) -> "PlaybookRule":
        needs_threshold = self.predicate in {"payment_days_gt", "auto_renewal_short_notice"}
        if needs_threshold != (self.threshold_days is not None):
            raise ValueError("threshold_days must be set exactly for threshold predicates")
        return self


class Playbook(BaseModel):
    """Immutable version whose rule IDs must be unique."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    playbook_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    rules: tuple[PlaybookRule, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_rules(self) -> "Playbook":
        ids = [rule.rule_id for rule in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate rule_id")
        return self
