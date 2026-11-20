"""把审阅 API 接到有界 Agent 工作流，并持久化可核对的结果。"""

from __future__ import annotations

from typing import Any

from app.agents.graph import build_default_graph
from app.agents.state import TaskState, WorkflowEvent
from app.agents.supervisor import Supervisor
from app.infrastructure.database import SQLiteDatabase, json_dumps, utc_now
from app.infrastructure.providers import EmbeddingProvider, LLMProvider
from app.infrastructure.tasks import TaskContext


def _save_state(db: SQLiteDatabase, review_id: str, state: TaskState) -> None:
    """保存节点快照；任务完成后仍可解释每项发现的来源。"""

    now = utc_now()
    db.run(
        "INSERT INTO agent_runs(task_id,review_id,state,created_at,updated_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET "
        "state=excluded.state,updated_at=excluded.updated_at",
        (state.task_id, review_id, json_dumps(state.model_dump(mode="json")), now, now),
    )


def _store_findings(db: SQLiteDatabase, review_id: str, state: TaskState) -> int:
    """仅落库通过验证的风险；重跑时保留已有人工决定。"""

    report_risks: list[dict[str, Any]] = (state.report or {}).get("risks", [])
    now = utc_now()
    with db.transaction() as conn:
        for risk in report_risks:
            citations = risk.get("citations") or []
            # 没有可回溯引用时，不发布风险结论。
            if not citations:
                continue
            conn.execute(
                "INSERT INTO findings(finding_id,review_id,document_id,version_id,"
                "severity,rule_id,rule_version,playbook_id,title,rule_hit,explanation,"
                "recommendation,citations,review_status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(finding_id) DO NOTHING",
                (
                    risk["finding_id"],
                    review_id,
                    risk["document_id"],
                    risk["version_id"],
                    risk["severity"],
                    risk["rule_id"],
                    risk["rule_version"],
                    risk.get("playbook_id", "legacy"),
                    risk["title"],
                    int(risk["rule_hit"]),
                    risk["explanation"],
                    risk["recommendation"],
                    json_dumps(citations),
                    "needs_review",
                    now,
                    now,
                ),
            )
    row = db.one("SELECT COUNT(*) AS total FROM findings WHERE review_id=?", (review_id,))
    return int(row["total"]) if row else 0


def review_runner(
    db: SQLiteDatabase,
    payload: dict[str, Any],
    embedding_provider: EmbeddingProvider | None = None,
    llm_provider: LLMProvider | None = None,
):
    """创建 TaskManager runner；检索、抽取、风险和证据核验由 AgentGraph 执行。"""

    def run(context: TaskContext, _payload: dict[str, Any]) -> dict[str, Any]:
        review_id = payload["review_id"]
        checkpoint = db.one(
            "SELECT state FROM agent_runs WHERE task_id=? AND review_id=?",
            (context.task_id, review_id),
        )
        if checkpoint:
            state = TaskState.model_validate_json(checkpoint["state"])
            if (state.task_id != context.task_id or state.task_type != "review"
                    or state.user_id != payload.get("owner_id")
                    or state.document_ids != (payload.get("document_ids") or [])
                    or state.version_ids != (payload.get("version_ids") or [])):
                raise ValueError("Agent checkpoint does not match the persisted review scope")
            if state.status in {"running", "succeeded"}:
                # A version may change while the worker is down. Recheck saved
                # conclusions against the current indexed source before export.
                state.node_results.pop("verify", None)
                state.node_results.pop("report", None)
                state.report = None
                state.status = "queued"
        else:
            state = Supervisor().create_state(
                task_id=context.task_id,
                task_type="review",
                request="",
                document_ids=payload.get("document_ids") or [],
                version_ids=payload.get("version_ids") or [],
                user_id=payload.get("owner_id"),
            )
        _save_state(db, review_id, state)
        db.run("UPDATE reviews SET status=?,updated_at=? WHERE review_id=?", ("running", utc_now(), review_id))

        def on_event(current: TaskState, event: WorkflowEvent) -> None:
            """同步任务时间线和 Agent 快照，事件只暴露计数等脱敏信息。"""

            _save_state(db, review_id, current)
            completed = sum(result.status == "succeeded" for result in current.node_results.values())
            progress = min(95, 5 + int(90 * completed / max(1, len(current.plan))))
            if event.type.startswith("node."):
                context.update(
                    progress,
                    event.node,
                    event.message,
                    event.type,
                    {"completed_nodes": completed, "total_nodes": len(current.plan)},
                )

        try:
            state = build_default_graph(db, embedding_provider, llm_provider=llm_provider).run(state, on_event=on_event)
            _save_state(db, review_id, state)
            if state.status == "failed":
                raise RuntimeError("Agent 审阅失败：" + "; ".join(state.errors)[:300])
            finding_count = _store_findings(db, review_id, state)
            review_status = "needs_review" if finding_count or state.status == "waiting_human" else "completed"
            db.run(
                "UPDATE reviews SET status=?,updated_at=? WHERE review_id=?",
                (review_status, utc_now(), review_id),
            )
            return {
                "result_ref": review_id,
                "status": "waiting_human" if review_status == "needs_review" else "succeeded",
                "current_node": state.current_node,
                "finding_count": finding_count,
            }
        except Exception:
            # 任务管理器负责公开失败代码；审阅记录也必须同步为失败。
            db.run(
                "UPDATE reviews SET status=?,updated_at=? WHERE review_id=?",
                ("failed", utc_now(), review_id),
            )
            raise

    return run
