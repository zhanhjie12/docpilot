"""Agent 工作流的可序列化状态和节点结果。

Agent 只在状态中保存结构化字段和证据引用，不保存完整原文或隐藏思维过程。
这样任务可以持久化、重试，也可以在前端时间线上解释每一步发生了什么。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


TaskType = Literal["screen_and_review", "review", "compare", "qa"]
WorkflowStatus = Literal["queued", "running", "waiting_human", "succeeded", "failed"]
NodeStatus = Literal["succeeded", "waiting_human", "failed"]
FieldStatus = Literal["found", "missing", "unknown", "needs_review"]
ReviewStatus = Literal["needs_review", "approved", "rejected"]


def utc_now() -> datetime:
    """返回带时区的 UTC 时间，方便事件在不同机器上排序。"""

    return datetime.now(timezone.utc)


class SerializableModel(BaseModel):
    """禁止节点偷偷写入未声明字段，避免状态契约漂移。"""

    model_config = ConfigDict(extra="forbid")


class EvidenceRef(SerializableModel):
    """可以回到原始文档的最小证据引用。"""

    citation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    version_id: str = Field(min_length=1)
    page: int | None = Field(default=None, ge=1)
    section: str | None = None
    quote: str = Field(min_length=1)
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)


class ExtractedField(SerializableModel):
    """字段抽取结果，明确区分缺失、证据不足和需要人工确认。"""

    name: str = Field(min_length=1)
    # 旧任务快照未保存归属，允许两个字段同时为空；新抽取结果必须成对填写。
    document_id: str | None = None
    version_id: str | None = None
    value: Any = None
    status: FieldStatus
    confidence: float = Field(ge=0, le=1)
    citations: list[EvidenceRef] = Field(default_factory=list)
    reason: str | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> "ExtractedField":
        """防止仅有文档或版本 ID 的字段被误归到其他合同。"""

        if (self.document_id is None) != (self.version_id is None):
            raise ValueError("document_id and version_id must both be present or absent")
        return self


class RiskFinding(SerializableModel):
    """规则命中与模型解释分开保存的风险项。"""

    finding_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    version_id: str = Field(min_length=1)
    # Legacy snapshots have no playbook identifier; new findings pin it explicitly.
    playbook_id: str = "legacy"
    severity: Literal["low", "medium", "high"]
    rule_id: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)
    title: str = Field(min_length=1)
    rule_hit: bool
    explanation: str = Field(min_length=1)
    recommendation: str = Field(min_length=1)
    citations: list[EvidenceRef] = Field(default_factory=list)
    review_status: ReviewStatus = "needs_review"


class NodeResult(SerializableModel):
    """一个节点的稳定输出信封，可直接写入事件或任务日志。"""

    node: str = Field(min_length=1)
    status: NodeStatus
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    attempts: int = Field(default=1, ge=1)
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime = Field(default_factory=utc_now)


class WorkflowEvent(SerializableModel):
    """面向前端时间线的脱敏事件。"""

    type: str = Field(min_length=1)
    node: str = Field(min_length=1)
    message: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)
    data: dict[str, Any] = Field(default_factory=dict)


class TaskState(SerializableModel):
    """Agent 任务的完整可序列化快照。"""

    task_id: str = Field(min_length=1)
    task_type: TaskType
    user_id: str | None = None
    document_ids: list[str] = Field(default_factory=list)
    version_ids: list[str] = Field(default_factory=list)
    request: str = ""
    conditions: dict[str, Any] = Field(default_factory=dict)
    plan: list[str] = Field(default_factory=list)
    current_node: str | None = None
    status: WorkflowStatus = "queued"
    node_results: dict[str, NodeResult] = Field(default_factory=dict)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    extracted_fields: list[ExtractedField] = Field(default_factory=list)
    risks: list[RiskFinding] = Field(default_factory=list)
    report: dict[str, Any] | None = None
    errors: list[str] = Field(default_factory=list)
    retry_count: int = Field(default=0, ge=0)
    retry_counts: dict[str, int] = Field(default_factory=dict)
    events: list[WorkflowEvent] = Field(default_factory=list)

    def add_event(
        self,
        event_type: str,
        node: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        """追加脱敏事件，所有节点用同一入口记录进度。"""

        self.events.append(
            WorkflowEvent(
                type=event_type,
                node=node,
                message=message,
                data=data or {},
            )
        )
