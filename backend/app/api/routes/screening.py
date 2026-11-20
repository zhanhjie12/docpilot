"""Deterministic multi-document screening endpoints (no Agent call)."""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, status

from app.api.deps import IdempotencyKey, get_database, get_embedding_provider, get_request_user, get_task_manager
from app.core.exceptions import AppError
from app.infrastructure.database import SQLiteDatabase, json_dumps, json_loads, utc_now
from app.infrastructure.providers import EmbeddingProvider
from app.infrastructure.tasks import TaskManager
from app.screening.models import ScreeningRequest
from app.services.screening_service import screen_runner

router = APIRouter()


@router.post("/screen", status_code=status.HTTP_202_ACCEPTED, summary="创建文档筛选任务")
async def screen_documents(payload: ScreeningRequest, idempotency_key: IdempotencyKey = None, db: SQLiteDatabase = Depends(get_database), tasks: TaskManager = Depends(get_task_manager), embedding_provider: EmbeddingProvider | None = Depends(get_embedding_provider), current_user: dict[str, object] | None = Depends(get_request_user)):
    owner_id = str(current_user["user_id"]) if current_user else None
    # 自然语言解析器尚未接入；不能把没有结构化条件的请求当成全部命中。
    if payload.conditions is None or not any(
        value is not None for value in payload.conditions.model_dump().values()
    ):
        raise AppError(
            code="SCREENING_CONDITIONS_REQUIRED",
            message="请提供明确的金额、期限或风险筛选条件",
            status_code=422,
        )
    placeholders = ",".join("?" for _ in payload.document_ids)
    # 匿名和登录用户使用同一所有权过滤，防止跨身份引用文档。
    rows = db.all(
        f"SELECT document_id FROM documents WHERE document_id IN ({placeholders}) AND owner_id IS ?",
        (*payload.document_ids, owner_id),
    )
    found = {row["document_id"] for row in rows}
    missing = [document_id for document_id in payload.document_ids if document_id not in found]
    if missing:
        raise AppError(code="DOCUMENT_NOT_FOUND", message="筛选范围包含不存在的文档", status_code=404, details={"document_ids": missing})
    conditions = payload.conditions.model_dump(mode="json") if payload.conditions else {}
    task_payload = {"document_ids": payload.document_ids, "conditions": conditions, "query": payload.query, "deep_analysis": payload.deep_analysis, "owner_id": owner_id}
    key = f"{owner_id or 'anonymous'}:screen:{idempotency_key}" if idempotency_key else None
    if key:
        previous = db.one(
            "SELECT response FROM idempotency_keys WHERE key=? AND operation='screen' AND owner_id IS ?",
            (key, owner_id),
        )
        if previous:
            response = json_loads(previous["response"], {})
            old_task = db.one("SELECT payload FROM tasks WHERE task_id=? AND owner_id IS ?", (response.get("task_id"), owner_id))
            if old_task and json_loads(old_task["payload"], {}) == task_payload:
                return response
            raise AppError(code="IDEMPOTENCY_CONFLICT", message="幂等键已用于不同筛选条件", status_code=409)
    task_id = f"task_{uuid4().hex}"
    tasks.create(task_id, "screen", task_payload, owner_id)
    db.run(
        "INSERT INTO screening_results(task_id,query,conditions,created_at,updated_at) VALUES(?,?,?,?,?)",
        (task_id, payload.query, json_dumps(conditions), utc_now(), utc_now()),
    )
    response = {"task_id": task_id, "status": "queued", "query": payload.query, "conditions": conditions, "document_count": len(payload.document_ids)}
    if key:
        db.run(
            "INSERT INTO idempotency_keys(key,owner_id,operation,response,created_at) VALUES(?,?,?,?,?)",
            (key, owner_id, "screen", json_dumps(response), utc_now()),
        )
    tasks.start(task_id, task_payload, screen_runner(db, task_payload, embedding_provider))
    return response
