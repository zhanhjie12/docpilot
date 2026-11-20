"""固定任务类型和步骤的 Supervisor。

Supervisor 只从白名单选择流程，不让模型自由生成下一步，避免 Agent 在生产环境
无限循环或越过证据验证节点。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .state import TaskState


WORKFLOW_PLANS: dict[str, tuple[str, ...]] = {
    "screen_and_review": ("screen", "retrieve", "extract", "risk", "verify", "report"),
    "review": ("retrieve", "extract", "risk", "verify", "report"),
    "compare": ("compare", "verify", "report"),
    "qa": ("retrieve", "verify", "report"),
}


@dataclass(frozen=True, slots=True)
class WorkflowPolicy:
    """有界执行策略；超过限制的任务会安全失败或转人工。"""

    max_steps: int = 12
    max_retries_per_node: int = 1

    def __post_init__(self) -> None:
        """拒绝无界或无法执行的策略配置。"""

        if self.max_steps < 1 or self.max_retries_per_node < 0:
            raise ValueError("workflow limits must be non-negative and max_steps positive")


class Supervisor:
    """创建状态并计算固定计划中的下一个节点。"""

    def __init__(self, policy: WorkflowPolicy | None = None) -> None:
        self.policy = policy or WorkflowPolicy()

    def plan_for(self, task_type: str) -> tuple[str, ...]:
        """校验任务类型，只返回预先审核过的节点链。"""

        if task_type not in WORKFLOW_PLANS:
            raise ValueError(f"unsupported task_type: {task_type}")
        return WORKFLOW_PLANS[task_type]

    def create_state(
        self,
        *,
        task_id: str,
        task_type: str,
        request: str = "",
        document_ids: list[str] | None = None,
        version_ids: list[str] | None = None,
        conditions: dict[str, Any] | None = None,
        user_id: str | None = None,
    ) -> TaskState:
        """创建 queued 状态；文档范围必须由调用方显式传入。"""

        plan = self.plan_for(task_type)
        state = TaskState(
            task_id=task_id,
            task_type=task_type,  # type: ignore[arg-type]
            user_id=user_id,
            document_ids=list(dict.fromkeys(document_ids or [])),
            version_ids=list(dict.fromkeys(version_ids or [])),
            request=request,
            conditions=conditions or {},
            plan=list(plan),
        )
        state.add_event("task.queued", "supervisor", "任务已进入固定工作流", {"task_type": task_type})
        return state

    def next_node(self, state: TaskState) -> str | None:
        """返回首个尚未得到成功结果的节点。"""

        for node in state.plan:
            result = state.node_results.get(node)
            if result is None:
                return node
            if result.status != "succeeded":
                return None
        return None

    def validate_scope(self, state: TaskState) -> None:
        """检索型流程必须带明确的文档或版本范围。"""

        if state.task_type in {"review", "qa", "screen_and_review"} and not (
            state.document_ids or state.version_ids
        ):
            raise ValueError("agent task requires document_ids or version_ids")
        # 批量初筛只处理 20 份合同；显式比较必须且只能指向两个版本。
        if state.task_type == "screen_and_review" and len(state.document_ids) > 20:
            raise ValueError("screen_and_review supports at most 20 documents")
        if state.task_type == "compare" and len(state.version_ids) != 2:
            raise ValueError("compare requires exactly two version_ids")
