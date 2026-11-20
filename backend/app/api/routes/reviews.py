"""合同审阅入口：范围校验、Agent 任务、人工决定和报告导出。"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any, Callable, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool

from app.agents.graph import build_default_graph
from app.agents.supervisor import Supervisor
from app.api.deps import (
    IdempotencyKey,
    get_database,
    get_embedding_provider,
    get_llm_provider,
    get_request_user,
    get_task_manager,
)
from app.core.exceptions import AppError
from app.domain.schemas import CompareVersionsRequest, ReviewCreateRequest, ReviewDecisionRequest, ReviewUnresolvedRequest
from app.infrastructure.database import SQLiteDatabase, json_dumps, json_loads, utc_now
from app.infrastructure.providers import EmbeddingProvider, LLMProvider
from app.infrastructure.tasks import TaskManager
from app.services.report_service import render_report
from app.services.review_service import review_runner


router = APIRouter()


# The separator cannot appear in the public Idempotency-Key header because the
# header accepts visible ASCII only. This makes the internal Review key immune
# to collisions with legacy upload keys such as ``anonymous:review:example``.
_IDEMPOTENCY_SEPARATOR = "\x1f"
_REVIEW_OPERATION = "review"


def _normalized_review_scope(payload: ReviewCreateRequest) -> dict[str, list[str]]:
    """Return the semantic Review scope in a stable order for hashing and replay."""

    # Document order has no effect on the Agent workflow, so a reordered list
    # represents the same logical create request.
    return {
        "document_ids": sorted(payload.document_ids or []),
        "version_ids": sorted(payload.version_ids or []),
    }


def _review_request_hash(payload: ReviewCreateRequest) -> str:
    """Create a deterministic fingerprint for one Review creation request."""

    canonical = json.dumps(
        _normalized_review_scope(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _review_idempotency_storage_key(owner_id: str | None, raw_key: str) -> str:
    """Build the collision-safe internal key for a newly created Review."""

    return _IDEMPOTENCY_SEPARATOR.join((_REVIEW_OPERATION, owner_id or "anonymous", raw_key))


def _legacy_review_idempotency_storage_key(owner_id: str | None, raw_key: str) -> str:
    """Build the former Review key format so existing records still replay."""

    return f"{owner_id or 'anonymous'}:{_REVIEW_OPERATION}:{raw_key}"


def _idempotency_conflict() -> AppError:
    """Return the shared conflict without exposing another request's payload."""

    return AppError(
        code="IDEMPOTENCY_CONFLICT",
        message="幂等键已用于不同审阅范围",
        status_code=409,
    )


def _replay_response(
    record: dict[str, Any],
    request_hash: str,
    expected_scope: dict[str, list[str]],
    load_review_scope: Callable[[str], dict[str, list[str]] | None],
) -> dict[str, Any]:
    """Validate a stored record and return its original create response.

    New records compare the stored request hash. Legacy records have no hash,
    so they fall back to the persisted Review scope for backward compatibility.
    """

    response = json_loads(record.get("response"), {})
    if not isinstance(response, dict) or not response.get("review_id") or not response.get("task_id"):
        raise AppError(
            code="IDEMPOTENCY_RECORD_INVALID",
            message="幂等记录不完整，无法安全重放请求",
            status_code=500,
        )
    stored_hash = record.get("request_hash")
    if stored_hash:
        if stored_hash != request_hash:
            raise _idempotency_conflict()
    else:
        legacy_scope = load_review_scope(str(response["review_id"]))
        if legacy_scope is None:
            raise AppError(
                code="IDEMPOTENCY_RECORD_INVALID",
                message="幂等记录指向的审阅不存在",
                status_code=500,
            )
        if legacy_scope != expected_scope:
            raise _idempotency_conflict()
    return response


def _review_scope_from_row(row: dict[str, Any] | None) -> dict[str, list[str]] | None:
    """Convert a minimal Review row into the same normalized shape as a request."""

    if not row:
        return None
    return {
        "document_ids": sorted(json_loads(row.get("document_ids"), [])),
        "version_ids": sorted(json_loads(row.get("version_ids"), [])),
    }


def _review_scope_from_connection(
    conn: Any,
    review_id: str,
    owner_id: str | None,
) -> dict[str, list[str]] | None:
    """Read a legacy Review scope through an already-open creation transaction."""

    row = conn.execute(
        "SELECT document_ids,version_ids FROM reviews WHERE review_id=? AND owner_id IS ?",
        (review_id, owner_id),
    ).fetchone()
    return _review_scope_from_row(dict(row) if row else None)


def _find_review_idempotency(
    db: SQLiteDatabase,
    owner_id: str | None,
    storage_key: str,
    legacy_storage_key: str,
) -> dict[str, Any] | None:
    """Look up a new-format key first, then safely support old Review records."""

    for key in (storage_key, legacy_storage_key):
        row = db.one(
            "SELECT response,request_hash FROM idempotency_keys "
            "WHERE key=? AND operation=? AND owner_id IS ?",
            (key, _REVIEW_OPERATION, owner_id),
        )
        if row:
            return row
    return None


def _review(db: SQLiteDatabase, review_id: str, owner_id: str | None) -> dict:
    """只读取当前身份创建的审阅，匿名身份仅能读取匿名审阅。"""

    row = db.one(
        "SELECT * FROM reviews WHERE review_id=? AND owner_id IS ?",
        (review_id, owner_id),
    )
    if not row:
        raise AppError(code="REVIEW_NOT_FOUND", message="审阅不存在", status_code=404)
    return {
        "review_id": row["review_id"],
        "status": row["status"],
        "document_ids": json_loads(row["document_ids"], []),
        "version_ids": json_loads(row["version_ids"], []),
        "version": row["version"],
        "resolution_note": row.get("resolution_note"),
        "resolved_at": row.get("resolved_at"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _validate_scope(db: SQLiteDatabase, payload: ReviewCreateRequest, owner_id: str | None) -> None:
    """审阅启动前验证每个文档和版本的归属及索引状态。"""

    # 每次最多审阅二十个显式对象，避免本地任务线程长时间占满。
    if len(payload.document_ids or []) + len(payload.version_ids or []) > 20:
        raise AppError(code="REVIEW_SCOPE_TOO_LARGE", message="每次最多审阅 20 个文档或版本", status_code=422)
    if payload.document_ids:
        placeholders = ",".join("?" for _ in payload.document_ids)
        rows = db.all(
            f"SELECT document_id,status FROM documents WHERE document_id IN ({placeholders}) AND owner_id IS ?",
            (*payload.document_ids, owner_id),
        )
        found = {row["document_id"] for row in rows}
        missing = [item for item in payload.document_ids if item not in found]
        if missing:
            raise AppError(
                code="DOCUMENT_NOT_FOUND",
                message="审阅范围包含不存在的文档",
                status_code=404,
                details={"document_ids": missing},
            )
        unready = [row["document_id"] for row in rows if row["status"] != "indexed"]
        if unready:
            raise AppError(
                code="DOCUMENT_NOT_READY",
                message="文档尚未完成索引",
                status_code=409,
                details={"document_ids": unready},
            )
    if payload.version_ids:
        placeholders = ",".join("?" for _ in payload.version_ids)
        rows = db.all(
            "SELECT v.version_id,v.status FROM document_versions v "
            "JOIN documents d ON d.document_id=v.document_id "
            f"WHERE v.version_id IN ({placeholders}) AND d.owner_id IS ?",
            (*payload.version_ids, owner_id),
        )
        found = {row["version_id"] for row in rows}
        missing = [item for item in payload.version_ids if item not in found]
        if missing:
            raise AppError(
                code="VERSION_NOT_FOUND",
                message="审阅范围包含不存在的文档版本",
                status_code=404,
                details={"version_ids": missing},
            )
        unready = [row["version_id"] for row in rows if row["status"] != "indexed"]
        if unready:
            raise AppError(
                code="DOCUMENT_NOT_READY",
                message="文档版本尚未完成索引",
                status_code=409,
                details={"version_ids": unready},
            )


@router.post("", status_code=status.HTTP_202_ACCEPTED, summary="创建 Agent 审阅")
async def create_review(
    payload: ReviewCreateRequest,
    idempotency_key: IdempotencyKey = None,
    db: SQLiteDatabase = Depends(get_database),
    tasks: TaskManager = Depends(get_task_manager),
    embedding_provider: EmbeddingProvider | None = Depends(get_embedding_provider),
    llm_provider: LLMProvider = Depends(get_llm_provider),
    current_user: dict[str, object] | None = Depends(get_request_user),
):
    """Create one Review and durable Task, with atomic idempotent replay.

    The request key identifies a client-side create attempt, while ``review_id``
    identifies the resulting business resource and ``task_id`` identifies its
    background execution. The latter two are generated only by the winner that
    successfully claims the idempotency key.
    """

    owner_id = str(current_user["user_id"]) if current_user else None
    normalized_scope = _normalized_review_scope(payload)
    request_hash = _review_request_hash(payload)
    storage_key = None
    legacy_storage_key = None

    if idempotency_key:
        storage_key = _review_idempotency_storage_key(owner_id, idempotency_key)
        legacy_storage_key = _legacy_review_idempotency_storage_key(owner_id, idempotency_key)
        previous = _find_review_idempotency(db, owner_id, storage_key, legacy_storage_key)
        if previous:
            # A replay must remain valid even when the original document was
            # later deleted or re-indexed. Only a first creation validates the
            # mutable document state.
            return _replay_response(
                previous,
                request_hash,
                normalized_scope,
                lambda review_id: _review_scope_from_row(
                    db.one(
                        "SELECT document_ids,version_ids FROM reviews WHERE review_id=? AND owner_id IS ?",
                        (review_id, owner_id),
                    )
                ),
            )

    _validate_scope(db, payload, owner_id)
    review_id = f"review_{uuid4().hex}"
    task_id = f"task_{uuid4().hex}"
    now = utc_now()
    document_ids = payload.document_ids or []
    version_ids = payload.version_ids or []
    task_payload = {
        "review_id": review_id,
        "document_ids": document_ids,
        "version_ids": version_ids,
        "owner_id": owner_id,
    }
    response = {"review_id": review_id, "task_id": task_id, "status": "queued", "created_at": now}
    replay: dict[str, Any] | None = None

    # ``BEGIN IMMEDIATE`` serializes competing creators before they can both
    # observe a missing key. All durable resources are written through this one
    # connection, so any failure rolls back the Review, Task, event and key.
    with db.transaction(immediate=True) as conn:
        if storage_key and legacy_storage_key:
            previous_row = None
            for key in (storage_key, legacy_storage_key):
                row = conn.execute(
                    "SELECT response,request_hash FROM idempotency_keys "
                    "WHERE key=? AND operation=? AND owner_id IS ?",
                    (key, _REVIEW_OPERATION, owner_id),
                ).fetchone()
                if row:
                    previous_row = dict(row)
                    break
            if previous_row:
                replay = _replay_response(
                    previous_row,
                    request_hash,
                    normalized_scope,
                    lambda existing_review_id: _review_scope_from_connection(
                        conn,
                        existing_review_id,
                        owner_id,
                    ),
                )
            else:
                # Reserve a placeholder first. It cannot become observable
                # without the final response because the surrounding
                # transaction rolls back on every creation error.
                claimed = conn.execute(
                    "INSERT INTO idempotency_keys(key,owner_id,operation,request_hash,response,created_at) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(key) DO NOTHING",
                    (storage_key, owner_id, _REVIEW_OPERATION, request_hash, "{}", now),
                ).rowcount
                if not claimed:
                    row = conn.execute(
                        "SELECT response,request_hash FROM idempotency_keys "
                        "WHERE key=? AND operation=? AND owner_id IS ?",
                        (storage_key, _REVIEW_OPERATION, owner_id),
                    ).fetchone()
                    if not row:
                        raise AppError(
                            code="IDEMPOTENCY_RECORD_INVALID",
                            message="幂等键占用状态无法读取",
                            status_code=500,
                        )
                    replay = _replay_response(
                        dict(row),
                        request_hash,
                        normalized_scope,
                        lambda existing_review_id: _review_scope_from_connection(
                            conn,
                            existing_review_id,
                            owner_id,
                        ),
                    )

        if replay is None:
            conn.execute(
                "INSERT INTO reviews(review_id,owner_id,status,document_ids,version_ids,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (review_id, owner_id, "queued", json_dumps(document_ids), json_dumps(version_ids), now, now),
            )
            tasks.create_in_transaction(conn, task_id, _REVIEW_OPERATION, task_payload, owner_id)
            if storage_key:
                conn.execute(
                    "UPDATE idempotency_keys SET response=? WHERE key=? AND operation=? AND owner_id IS ?",
                    (json_dumps(response), storage_key, _REVIEW_OPERATION, owner_id),
                )

    if replay is not None:
        return replay

    # The worker runs after commit. A crash in this narrow interval leaves a
    # durable queued task, which TaskManager recovers during the next startup.
    tasks.start(
        task_id,
        task_payload,
        review_runner(db, task_payload, embedding_provider, llm_provider),
    )
    return response


@router.get("/{review_id}", summary="查询审阅")
async def get_review(
    review_id: str,
    db: SQLiteDatabase = Depends(get_database),
    current_user: dict[str, object] | None = Depends(get_request_user),
):
    owner_id = str(current_user["user_id"]) if current_user else None
    return _review(db, review_id, owner_id)


@router.get("/{review_id}/workflow", summary="查询 Agent 节点与证据快照")
async def get_review_workflow(
    review_id: str,
    db: SQLiteDatabase = Depends(get_database),
    current_user: dict[str, object] | None = Depends(get_request_user),
):
    """返回已持久化的结构化状态，不含完整文档原文。"""

    owner_id = str(current_user["user_id"]) if current_user else None
    review = _review(db, review_id, owner_id)
    row = db.one(
        "SELECT task_id,state,updated_at FROM agent_runs WHERE review_id=? ORDER BY updated_at DESC LIMIT 1",
        (review_id,),
    )
    return {
        "review_id": review_id,
        "status": review["status"],
        "task_id": row["task_id"] if row else None,
        "state": json_loads(row["state"], None) if row else None,
        "updated_at": row["updated_at"] if row else review["updated_at"],
    }


@router.get("/{review_id}/findings", summary="查询风险项")
async def list_review_findings(
    review_id: str,
    db: SQLiteDatabase = Depends(get_database),
    current_user: dict[str, object] | None = Depends(get_request_user),
):
    owner_id = str(current_user["user_id"]) if current_user else None
    _review(db, review_id, owner_id)
    rows = db.all(
        "SELECT * FROM findings WHERE review_id=? ORDER BY "
        "CASE severity WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, created_at",
        (review_id,),
    )
    return {
        "review_id": review_id,
        "items": [
            {
                "finding_id": row["finding_id"],
                "document_id": row["document_id"],
                "version_id": row["version_id"],
                "severity": row["severity"],
                "rule_id": row["rule_id"],
                "rule_version": row["rule_version"],
                "playbook_id": row["playbook_id"],
                "title": row["title"],
                "rule_hit": bool(row["rule_hit"]),
                "explanation": row["explanation"],
                "recommendation": row["recommendation"],
                "citations": json_loads(row["citations"], []),
                "review_status": row["review_status"],
                "review_note": row["review_note"],
                "version": row["version"],
            }
            for row in rows
        ],
    }


@router.post("/{review_id}/findings/{finding_id}/decision", summary="人工确认风险项")
async def decide_review_finding(
    review_id: str,
    finding_id: str,
    payload: ReviewDecisionRequest,
    db: SQLiteDatabase = Depends(get_database),
    tasks: TaskManager = Depends(get_task_manager),
    current_user: dict[str, object] | None = Depends(get_request_user),
):
    """通过乐观锁保存人工决定；全部处理后结束等待中的审阅任务。"""

    owner_id = str(current_user["user_id"]) if current_user else None
    _review(db, review_id, owner_id)
    new_version = payload.expected_version + 1
    changed = db.run(
        "UPDATE findings SET review_status=?,review_note=?,version=?,updated_at=? "
        "WHERE finding_id=? AND review_id=? AND version=?",
        (payload.decision, payload.note, new_version, utc_now(), finding_id, review_id, payload.expected_version),
    )
    if not changed:
        current = db.one("SELECT version FROM findings WHERE finding_id=? AND review_id=?", (finding_id, review_id))
        if not current:
            raise AppError(code="FINDING_NOT_FOUND", message="风险项不存在", status_code=404)
        raise AppError(
            code="REVIEW_CONFLICT",
            message="风险项已被其他人更新",
            status_code=409,
            details={"current_version": current["version"]},
        )
    pending = db.one(
        "SELECT COUNT(*) AS total FROM findings WHERE review_id=? AND review_status='needs_review'",
        (review_id,),
    )
    if pending and pending["total"] == 0:
        db.run(
            "UPDATE reviews SET status='completed',updated_at=? WHERE review_id=?",
            (utc_now(), review_id),
        )
        tasks.finish_human_review(review_id, owner_id)
    return {
        "finding_id": finding_id,
        "review_status": payload.decision,
        "review_note": payload.note,
        "version": new_version,
    }


@router.post("/{review_id}/close-unresolved", summary="人工标记证据不足并结案")
def close_unresolved_review(
    review_id: str,
    payload: ReviewUnresolvedRequest,
    db: SQLiteDatabase = Depends(get_database),
    tasks: TaskManager = Depends(get_task_manager),
    current_user: dict[str, object] | None = Depends(get_request_user),
):
    """Close a no-finding human gate with an auditable note and version check."""

    owner_id = str(current_user["user_id"]) if current_user else None
    with db.transaction(immediate=True) as conn:
        row = conn.execute(
            "SELECT version,status FROM reviews WHERE review_id=? AND owner_id IS ?",
            (review_id, owner_id),
        ).fetchone()
        if row is None:
            raise AppError(code="REVIEW_NOT_FOUND", message="审阅不存在", status_code=404)
        if row["status"] != "needs_review" or row["version"] != payload.expected_version:
            raise AppError(code="REVIEW_CONFLICT", message="审阅状态已变化", status_code=409,
                           details={"current_version": row["version"]})
        pending = conn.execute(
            "SELECT COUNT(*) AS total FROM findings WHERE review_id=? AND review_status='needs_review'",
            (review_id,),
        ).fetchone()["total"]
        if pending:
            raise AppError(code="FINDINGS_PENDING", message="请先逐项处理风险发现", status_code=409)
        now = utc_now()
        conn.execute(
            "UPDATE reviews SET status='closed_unresolved',resolution_note=?,resolved_at=?,"
            "version=version+1,updated_at=? WHERE review_id=?",
            (payload.note.strip(), now, now, review_id),
        )
        if not tasks.close_unresolved_in_transaction(conn, review_id, owner_id):
            raise AppError(code="TASK_NOT_READY", message="审阅任务尚未进入人工确认状态", status_code=409)
    return {"review_id": review_id, "status": "closed_unresolved",
            "resolution_note": payload.note.strip(), "version": payload.expected_version + 1}


@router.post("/{review_id}/compare", summary="比较审阅范围内的版本")
async def compare_review_versions(
    review_id: str,
    payload: CompareVersionsRequest,
    db: SQLiteDatabase = Depends(get_database),
    current_user: dict[str, object] | None = Depends(get_request_user),
):
    """仅允许比较本审阅范围中的本人版本，避免通过版本 ID 读取他人原文。"""

    owner_id = str(current_user["user_id"]) if current_user else None
    review = _review(db, review_id, owner_id)
    rows = db.all(
        "SELECT v.version_id,v.document_id,v.extracted_text FROM document_versions v "
        "JOIN documents d ON d.document_id=v.document_id "
        "WHERE v.version_id IN (?,?) AND d.owner_id IS ?",
        (payload.base_version_id, payload.target_version_id, owner_id),
    )
    versions = {row["version_id"]: row for row in rows}
    allowed_documents = set(review["document_ids"])
    allowed_versions = set(review["version_ids"])
    for version_id in (payload.base_version_id, payload.target_version_id):
        row = versions.get(version_id)
        if not row or (version_id not in allowed_versions and row["document_id"] not in allowed_documents):
            raise AppError(code="VERSION_NOT_FOUND", message="文档版本不在审阅范围内", status_code=404)
    # 比较、双侧引用和证据验证由同一 Agent 图完成，避免路由再次实现一套 diff。
    state = Supervisor().create_state(
        task_id=f"compare_{uuid4().hex}",
        task_type="compare",
        document_ids=[versions[payload.base_version_id]["document_id"]],
        version_ids=[payload.base_version_id, payload.target_version_id],
        user_id=owner_id,
    )
    state = await run_in_threadpool(build_default_graph(db).run, state)
    if state.status != "succeeded":
        node_result = state.node_results.get(state.current_node or "")
        detail = state.errors[-1] if state.errors else (
            node_result.error if node_result and node_result.error else "版本缺少可验证的双侧引用"
        )
        raise AppError(
            code="VERSION_COMPARE_NEEDS_REVIEW",
            message=detail,
            status_code=409,
            details={"node": state.current_node},
        )
    result = state.node_results["compare"].output
    return {
        "review_id": review_id,
        "base_version_id": payload.base_version_id,
        "target_version_id": payload.target_version_id,
        "changed": result["changed"],
        "diff": result["diff"],
        "changes": result["changes"],
        "citations": state.report["citations"] if state.report else [],
    }


@router.get("/{review_id}/report", summary="导出审阅报告")
async def get_review_report(
    review_id: str,
    report_format: Literal["json", "markdown", "csv"] = Query("json", alias="format"),
    db: SQLiteDatabase = Depends(get_database),
    current_user: dict[str, object] | None = Depends(get_request_user),
):
    """导出通过证据验证的 Agent 结果和人工处理状态。"""

    owner_id = str(current_user["user_id"]) if current_user else None
    review = _review(db, review_id, owner_id)
    rows = db.all("SELECT * FROM findings WHERE review_id=? ORDER BY created_at", (review_id,))
    findings = [
        {
            "finding_id": row["finding_id"],
            "document_id": row["document_id"],
            "version_id": row["version_id"],
            "severity": row["severity"],
            "rule_id": row["rule_id"],
            "title": row["title"],
            "review_status": row["review_status"],
            "explanation": row["explanation"],
            "recommendation": row["recommendation"],
            "citations": json_loads(row["citations"], []),
        }
        for row in rows
    ]
    run = db.one("SELECT state FROM agent_runs WHERE review_id=? ORDER BY updated_at DESC LIMIT 1", (review_id,))
    agent_report = (json_loads(run["state"], {}).get("report") if run else None)
    body, media_type = render_report(review, findings, report_format, agent_report)
    if report_format == "json":
        return JSONResponse(content=json.loads(body), media_type=media_type)
    return PlainTextResponse(body, media_type=media_type)
