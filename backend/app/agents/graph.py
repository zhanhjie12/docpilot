"""固定计划的顺序 Agent 执行器，负责范围门控、重试和任务终态。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.infrastructure.database import json_loads
from app.screening.filter_engine import screen_metadata
from app.screening.models import ScreeningConditions

from .comparison_agent import compare_node
from .extraction_agent import extract_node
from .report_agent import report_node
from .retrieval_agent import retrieve_node
from .risk_agent import risk_node
from .state import NodeResult, TaskState, WorkflowEvent
from .supervisor import Supervisor, WorkflowPolicy
from .verification_agent import verify_node


NodeCallable = Callable[[TaskState], NodeResult]
EventCallback = Callable[[TaskState, WorkflowEvent], None]


def _screen_node(state: TaskState, db: Any) -> NodeResult:
    """在用户所属的文档范围内做确定性初筛，缺失条件绝不视为命中。"""

    if not state.document_ids and not state.version_ids:
        return NodeResult(node="screen", status="waiting_human", error="初筛缺少文档范围")
    selected_ids = list(dict.fromkeys(state.document_ids))
    version_documents: dict[str, str] = {}
    if state.version_ids:
        placeholders = ",".join("?" for _ in state.version_ids)
        version_rows = db.all(
            "SELECT v.version_id,v.document_id FROM document_versions v "
            "JOIN documents d ON d.document_id=v.document_id "
            f"WHERE v.version_id IN ({placeholders}) AND d.owner_id IS ?",
            (*state.version_ids, state.user_id),
        )
        version_documents = {row["version_id"]: row["document_id"] for row in version_rows}
        if len(version_documents) != len(state.version_ids):
            return NodeResult(node="screen", status="waiting_human", error="初筛范围包含不可访问的版本")
        if selected_ids and any(document_id not in selected_ids for document_id in version_documents.values()):
            return NodeResult(node="screen", status="waiting_human", error="版本不属于所选文档")
        if not selected_ids:
            selected_ids = list(dict.fromkeys(version_documents.values()))
    if len(selected_ids) > 20:
        return NodeResult(node="screen", status="failed", error="初筛最多支持 20 份文档")
    placeholders = ",".join("?" for _ in selected_ids)
    rows = db.all(
        f"SELECT document_id,extracted_facts FROM documents WHERE document_id IN ({placeholders}) "
        "AND owner_id IS ?",
        (*selected_ids, state.user_id),
    )
    by_id = {row["document_id"]: row for row in rows}
    if len(by_id) != len(selected_ids):
        return NodeResult(node="screen", status="waiting_human", error="初筛范围包含不可访问的文档")
    try:
        conditions = ScreeningConditions.model_validate(state.conditions)
    except ValueError:
        return NodeResult(node="screen", status="waiting_human", error="筛选条件无效")
    if not conditions.model_dump(exclude_none=True):
        return NodeResult(node="screen", status="waiting_human", error="请提供可校验的结构化筛选条件")
    documents = [
        {"document_id": document_id, **(json_loads(by_id[document_id]["extracted_facts"], {}) or {})}
        for document_id in selected_ids
    ]
    matches = screen_metadata(documents, conditions)
    matched_ids = {item.document_id for item in matches if item.matched}
    # 检索层对 document_ids/version_ids 使用 OR，因此两个范围必须同步收缩。
    state.document_ids = [document_id for document_id in selected_ids if document_id in matched_ids]
    state.version_ids = [
        version_id for version_id in state.version_ids if version_documents[version_id] in matched_ids
    ]
    return NodeResult(
        node="screen",
        status="succeeded",
        output={
            "candidate_count": len(selected_ids),
            "matched_count": len(matched_ids),
            "missing_fields": sorted({field for item in matches for field in item.missing_fields}),
        },
    )


def _compare_node(state: TaskState, db: Any) -> NodeResult:
    """从数据库读取所属用户的两个已解析版本，再交给纯比较节点。"""

    if len(state.version_ids) != 2:
        return NodeResult(node="compare", status="waiting_human", error="版本对比必须提供两个 version_id")
    rows = db.all(
        "SELECT v.version_id,v.document_id,v.extracted_text,v.parsed_content,v.status "
        "FROM document_versions v JOIN documents d ON d.document_id=v.document_id "
        "WHERE v.version_id IN (?,?) AND d.owner_id IS ?",
        (state.version_ids[0], state.version_ids[1], state.user_id),
    )
    versions = {row["version_id"]: row for row in rows}
    if len(versions) != 2:
        return NodeResult(node="compare", status="waiting_human", error="版本不存在或不在当前用户范围内")
    base, target = (versions[version_id] for version_id in state.version_ids)
    if base["document_id"] != target["document_id"]:
        return NodeResult(node="compare", status="waiting_human", error="只能比较同一文档的两个版本")
    if state.document_ids and base["document_id"] not in state.document_ids:
        return NodeResult(node="compare", status="waiting_human", error="版本不属于所选文档")
    pages_by_version: list[list[dict[str, Any]]] = []
    for version in (base, target):
        parsed = json_loads(version["parsed_content"], {}) or {}
        pages = parsed.get("pages") if isinstance(parsed, dict) else None
        if (
            version["status"] != "indexed"
            or not isinstance(pages, list)
            or not pages
            or any(not isinstance(page, dict) for page in pages)
            or "\n\n".join(str(page.get("text") or "") for page in pages) != (version["extracted_text"] or "")
        ):
            return NodeResult(node="compare", status="waiting_human", error="版本尚未完成可定位的解析与索引")
        pages_by_version.append(pages)
    return compare_node(
        state,
        base["extracted_text"],
        target["extracted_text"],
        document_id=base["document_id"],
        base_pages=pages_by_version[0],
        target_pages=pages_by_version[1],
    )


class AgentGraph:
    """执行固定计划，并把失败、人工暂停和成功保持为互斥终态。"""

    def __init__(self, nodes: dict[str, NodeCallable], policy: WorkflowPolicy | None = None) -> None:
        self.nodes = nodes
        self.supervisor = Supervisor(policy)

    @staticmethod
    def _emit(
        state: TaskState,
        event_type: str,
        node: str,
        message: str,
        on_event: EventCallback | None,
        data: dict[str, Any] | None = None,
    ) -> None:
        """先将脱敏事件写入状态，再同步通知任务持久化层。"""

        state.add_event(event_type, node, message, data)
        if on_event is not None:
            on_event(state, state.events[-1])

    def run(self, state: TaskState, on_event: EventCallback | None = None) -> TaskState:
        """同步运行；每次节点尝试占一个 max_steps 名额。"""

        if state.status in {"succeeded", "failed", "waiting_human"}:
            return state
        # 从持久化快照恢复时，不能把已有的失败或人工暂停结果误判为全链成功。
        for node in state.plan:
            previous = state.node_results.get(node)
            if previous is not None and previous.status != "succeeded":
                state.status = previous.status
                return state
        prior_screen = state.node_results.get("screen")
        if (
            state.task_type == "screen_and_review"
            and prior_screen is not None
            and prior_screen.status == "succeeded"
            and prior_screen.output.get("matched_count") == 0
        ):
            # 任务可能恰好在筛选节点提交后中断；恢复时仍保持零命中的安全结论。
            self._finish_empty_screen(state, on_event)
            return state
        try:
            self.supervisor.validate_scope(state)
        except ValueError as exc:
            state.status = "failed"
            state.errors.append(str(exc))
            self._emit(state, "task.failed", "supervisor", str(exc), on_event)
            return state
        steps = sum(
            state.node_results[node].attempts if node in state.node_results else state.retry_counts.get(node, 0)
            for node in state.plan
        )
        state.status = "running"
        self._emit(state, "task.started", "supervisor", "开始执行固定 Agent 工作流", on_event)
        while True:
            node = self.supervisor.next_node(state)
            if node is None:
                state.status = "succeeded"
                if state.report is not None:
                    state.report["status"] = "succeeded"
                self._emit(state, "task.completed", "supervisor", "Agent 工作流已完成", on_event)
                return state
            if steps >= self.supervisor.policy.max_steps:
                state.status = "failed"
                state.errors.append("超过最大工作流步数")
                self._emit(state, "task.failed", "supervisor", "超过最大工作流步数", on_event)
                return state
            operation = self.nodes.get(node)
            if operation is None:
                state.status = "failed"
                state.errors.append(f"未注册节点: {node}")
                self._emit(state, "task.failed", node, "工作流节点未注册", on_event)
                return state
            state.current_node = node
            attempt = state.retry_counts.get(node, 0) + 1
            steps += 1
            self._emit(state, "node.started", node, f"开始执行节点 {node}", on_event, {"attempt": attempt})
            try:
                result = operation(state)
                if result.node != node:
                    raise ValueError(f"节点返回名称不一致: {result.node}")
            except Exception as exc:  # noqa: BLE001 - 节点故障统一进入有界重试。
                result = NodeResult(node=node, status="failed", error=str(exc)[:500])
            result.attempts = attempt
            if result.status == "failed" and attempt <= self.supervisor.policy.max_retries_per_node:
                state.retry_count += 1
                state.retry_counts[node] = attempt
                self._emit(
                    state,
                    "node.retry",
                    node,
                    "节点失败，准备有限重试",
                    on_event,
                    {"attempt": attempt, "error": (result.error or "节点失败")[:200]},
                )
                continue
            state.node_results[node] = result
            self._emit(
                state,
                f"node.{result.status}",
                node,
                result.error or f"节点 {node} 完成",
                on_event,
                {"attempts": attempt, "status": result.status},
            )
            if result.status == "failed":
                state.status = "failed"
                state.errors.append(result.error or f"节点 {node} 失败")
                self._emit(state, "task.failed", node, "Agent 节点失败", on_event)
                return state
            if result.status == "waiting_human":
                state.status = "waiting_human"
                self._emit(state, "task.waiting_human", node, "等待人工确认", on_event)
                return state
            if node == "screen" and result.output.get("matched_count") == 0:
                # 零命中是已完成的空结果，后续检索不能拿空范围执行。
                self._finish_empty_screen(state, on_event)
                return state

    def _finish_empty_screen(self, state: TaskState, on_event: EventCallback | None) -> None:
        """统一生成零命中的终态报告，供正常执行和快照恢复复用。"""

        state.status = "succeeded"
        state.report = {
            "task_id": state.task_id,
            "task_type": state.task_type,
            "status": "succeeded",
            "matched_document_count": 0,
            "fields": [],
            "risks": [],
            "citations": [],
        }
        if not any(event.type == "task.completed" for event in state.events):
            self._emit(state, "task.completed", "supervisor", "筛选未命中文档", on_event)


def build_default_graph(
    db: Any,
    embedding_provider: Any = None,
    *,
    top_k: int = 5,
    llm_provider: Any = None,
) -> AgentGraph:
    """装配固定节点，数据库依赖仅进入需要查询或核验证据的节点。"""

    return AgentGraph(
        {
            "screen": lambda state: _screen_node(state, db),
            "retrieve": lambda state: retrieve_node(state, db, embedding_provider, top_k=top_k),
            "extract": extract_node,
            "risk": lambda state: risk_node(state, llm_provider),
            "verify": lambda state: verify_node(state, db),
            "report": report_node,
            "compare": lambda state: _compare_node(state, db),
        }
    )
