"""Background runners for deterministic document screening.

The runner lives in the service layer instead of an API route so it can be
rebuilt from a persisted task payload after an application restart.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.infrastructure.database import SQLiteDatabase, json_dumps, json_loads, utc_now
from app.infrastructure.providers import EmbeddingProvider
from app.infrastructure.tasks import TaskContext
from app.screening.models import ComparisonOperator
from app.services.review_service import review_runner


def _compare(actual: Any, operator: ComparisonOperator, expected: float) -> bool:
    """Evaluate one normalized screening value against its requested condition."""

    if actual is None:
        return False
    if operator == ComparisonOperator.EQ:
        return actual == expected
    if operator == ComparisonOperator.GT:
        return actual > expected
    if operator == ComparisonOperator.GTE:
        return actual >= expected
    if operator == ComparisonOperator.LT:
        return actual < expected
    return actual <= expected


def _enqueue_review_for_match(
    context: TaskContext,
    db: SQLiteDatabase,
    document_id: str,
    owner_id: str | None,
    embedding_provider: EmbeddingProvider | None,
) -> tuple[str, str]:
    """Create one matched-document Review and Task atomically before dispatching it."""

    review_id = f"review_{uuid4().hex}"
    task_id = f"task_{uuid4().hex}"
    review_payload = {
        "review_id": review_id,
        "document_ids": [document_id],
        "version_ids": [],
        "owner_id": owner_id,
    }
    now = utc_now()
    # The Review, durable Task and its first event form one business creation.
    # The task runner is submitted only after this transaction commits.
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO reviews(review_id,owner_id,status,document_ids,version_ids,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (review_id, owner_id, "queued", json_dumps([document_id]), "[]", now, now),
        )
        context.manager.create_in_transaction(conn, task_id, "review", review_payload, owner_id)
    context.manager.start(
        task_id,
        review_payload,
        review_runner(db, review_payload, embedding_provider),
    )
    return review_id, task_id


def screen_runner(
    db: SQLiteDatabase,
    payload: dict[str, Any],
    embedding_provider: EmbeddingProvider | None,
):
    """Build a durable screening runner from serializable task payload data."""

    def run(context: TaskContext, _payload: dict[str, Any]) -> dict[str, Any]:
        document_ids = payload["document_ids"]
        conditions = payload.get("conditions") or {}
        owner_id = payload.get("owner_id")
        items = []
        context.update(15, "filter", "正在读取文档字段", "node.started")
        for document_id in document_ids:
            row = db.one(
                "SELECT * FROM documents WHERE document_id=? AND owner_id IS ?",
                (document_id, owner_id),
            )
            facts = json_loads(row["extracted_facts"], {}) if row else {}
            matched_conditions: list[str] = []
            missing: list[str] = []
            for name in ("amount", "duration_days"):
                condition = conditions.get(name)
                if not condition:
                    continue
                actual = facts.get(name)
                if actual is None:
                    missing.append(name)
                elif _compare(actual, condition["operator"], condition["value"]):
                    matched_conditions.append(name)
            if conditions.get("risk"):
                if facts.get("risk") is None:
                    missing.append("risk")
                elif facts.get("risk") == conditions["risk"]:
                    matched_conditions.append("risk")
            expected = [key for key, value in conditions.items() if value is not None]
            matched = not missing and len(matched_conditions) == len(expected)
            items.append(
                {
                    "document_id": document_id,
                    "matched": matched,
                    "matched_conditions": matched_conditions,
                    "missing_fields": missing,
                    "facts": facts,
                }
            )
        matched_count = sum(1 for item in items if item["matched"])
        if payload.get("deep_analysis"):
            # Each matching document has a separate Review so its evidence and
            # human decisions cannot be mixed with another contract.
            context.update(65, "review", "正在为命中文档创建 Agent 审阅", "node.started")
            for item in items:
                if not item["matched"]:
                    continue
                review_id, review_task_id = _enqueue_review_for_match(
                    context,
                    db,
                    item["document_id"],
                    owner_id,
                    embedding_provider,
                )
                item["review_id"] = review_id
                item["review_task_id"] = review_task_id
        db.run(
            "UPDATE screening_results SET total=?,matched=?,items=?,updated_at=? WHERE task_id=?",
            (len(items), matched_count, json_dumps(items), utc_now(), context.task_id),
        )
        context.update(90, "filter", f"已完成 {len(items)} 份文档筛选")
        return {"result_ref": context.task_id, "total": len(items), "matched": matched_count}

    return run
