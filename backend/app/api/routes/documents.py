"""Document upload, version and ingestion endpoints."""

from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status

from app.api.deps import (
    IdempotencyKey,
    get_database,
    get_embedding_provider,
    get_request_user,
    get_storage,
    get_task_manager,
)
from app.core.config import Settings
from app.core.exceptions import AppError
from app.infrastructure.database import SQLiteDatabase, json_dumps
from app.infrastructure.providers import EmbeddingProvider
from app.infrastructure.storage import ObjectStorage
from app.infrastructure.tasks import TaskManager
from app.services.document_ingestion import DocumentIngestionService

router = APIRouter()


def _settings(request: Request) -> Settings:
    return request.app.state.settings


@router.post("", status_code=status.HTTP_202_ACCEPTED, summary="上传文档")
async def upload_document(
    file: Annotated[UploadFile, File(description="待解析的 PDF 或 DOCX 文件")],
    display_name: Annotated[str | None, Form()] = None,
    document_group_id: Annotated[str | None, Form()] = None,
    document_type: Annotated[Literal["contract", "tender", "bid", "policy", "other"], Form()] = "contract",
    idempotency_key: IdempotencyKey = None,
    request: Request = None,
    db: SQLiteDatabase = Depends(get_database),
    storage: ObjectStorage = Depends(get_storage),
    tasks: TaskManager = Depends(get_task_manager),
    embedding_provider: EmbeddingProvider | None = Depends(get_embedding_provider),
    current_user: dict[str, object] | None = Depends(get_request_user),
):
    owner_id = str(current_user["user_id"]) if current_user else None
    if idempotency_key:
        # 将幂等键按用户分区，避免不同用户复用同一个键时读到彼此的上传结果。
        idempotency_scope = f"{owner_id or 'anonymous'}:{idempotency_key}"
        previous = db.one(
            "SELECT response FROM idempotency_keys WHERE key=? AND operation=?",
            (idempotency_scope, "upload"),
        )
        if previous:
            import json
            return json.loads(previous["response"])
    data = await file.read()
    service = DocumentIngestionService(
        db,
        storage,
        _settings(request),
        embedding_provider,
    )
    try:
        created = service.create_version(
            filename=file.filename or "document",
            display_name=display_name,
            document_group_id=document_group_id,
            mime_type=file.content_type or "application/octet-stream",
            data=data,
            owner_id=owner_id,
            document_type=document_type,
        )
    except Exception as exc:
        if isinstance(exc, AppError):
            raise
        if hasattr(exc, "code"):
            raise AppError(code=exc.code, message=str(exc), status_code=getattr(exc, "status_code", 400), details=getattr(exc, "details", {})) from exc
        raise
    if created.get("duplicate"):
        return {**created, "task_id": None}
    task_id = f"task_{uuid4().hex}"
    tasks.enqueue(task_id, "parse_and_index", {"document_id": created["document_id"], "version_id": created["version_id"]}, service.parse_version, owner_id)
    response = {key: value for key, value in created.items() if key != "duplicate" and key != "object_key"}
    response.update({"task_id": task_id, "status": "uploaded"})
    if idempotency_key:
        # 使用作用域键兼容现有以 key 为主键的 SQLite 表，同时保留 owner_id 便于审计。
        idempotency_scope = f"{owner_id or 'anonymous'}:{idempotency_key}"
        db.run(
            "INSERT OR REPLACE INTO idempotency_keys(key,owner_id,operation,response,created_at) VALUES(?,?,?,?,datetime('now'))",
            (idempotency_scope, owner_id, "upload", json_dumps(response)),
        )
    return response


@router.get("", summary="查询文档列表")
async def list_documents(
    status: Literal["uploaded", "parsing", "parsed", "indexed", "failed"] | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(min_length=1)] = None,
    db: SQLiteDatabase = Depends(get_database),
    storage: ObjectStorage = Depends(get_storage),
    request: Request = None,
    current_user: dict[str, object] | None = Depends(get_request_user),
):
    owner_id = str(current_user["user_id"]) if current_user else None
    return DocumentIngestionService(db, storage, _settings(request)).list(status, limit, cursor, owner_id)


@router.get("/{document_id}", summary="查询文档详情")
async def get_document(document_id: str, db: SQLiteDatabase = Depends(get_database), storage: ObjectStorage = Depends(get_storage), request: Request = None, current_user: dict[str, object] | None = Depends(get_request_user)):
    owner_id = str(current_user["user_id"]) if current_user else None
    result = DocumentIngestionService(db, storage, _settings(request)).get(document_id, owner_id)
    if not result:
        raise AppError(code="DOCUMENT_NOT_FOUND", message="文档不存在", status_code=404)
    return result


@router.get("/{document_id}/versions", summary="查询文档版本")
async def list_document_versions(document_id: str, db: SQLiteDatabase = Depends(get_database), storage: ObjectStorage = Depends(get_storage), request: Request = None, current_user: dict[str, object] | None = Depends(get_request_user)):
    service = DocumentIngestionService(db, storage, _settings(request))
    owner_id = str(current_user["user_id"]) if current_user else None
    if not service.get(document_id, owner_id):
        raise AppError(code="DOCUMENT_NOT_FOUND", message="文档不存在", status_code=404)
    return {"document_id": document_id, "items": service.versions(document_id, owner_id)}


@router.post("/{document_id}/reindex", status_code=status.HTTP_202_ACCEPTED, summary="重新索引文档")
async def reindex_document(
    document_id: str,
    idempotency_key: IdempotencyKey = None,
    db: SQLiteDatabase = Depends(get_database),
    storage: ObjectStorage = Depends(get_storage),
    tasks: TaskManager = Depends(get_task_manager),
    embedding_provider: EmbeddingProvider | None = Depends(get_embedding_provider),
    request: Request = None,
    current_user: dict[str, object] | None = Depends(get_request_user),
):
    owner_id = str(current_user["user_id"]) if current_user else None
    # SQLite 的 IS 同时支持具体用户 ID 和匿名资源的 NULL 比较。
    row = db.one(
        "SELECT latest_version_id FROM documents WHERE document_id=? AND owner_id IS ?",
        (document_id, owner_id),
    )
    if not row:
        raise AppError(code="DOCUMENT_NOT_FOUND", message="文档不存在", status_code=404)
    task_id = f"task_{uuid4().hex}"
    service = DocumentIngestionService(
        db,
        storage,
        _settings(request),
        embedding_provider,
    )
    tasks.enqueue(task_id, "parse_and_index", {"document_id": document_id, "version_id": row["latest_version_id"]}, service.parse_version, owner_id)
    return {"task_id": task_id, "status": "queued"}
