"""文档上传、版本管理和解析索引编排。"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.config import Settings
from app.infrastructure.database import SQLiteDatabase, row_document, row_version, utc_now
from app.infrastructure.providers import EmbeddingProvider
from app.infrastructure.storage import ObjectStorage
from app.infrastructure.tasks import TaskContext
from app.repositories.document_repository import DocumentRepository
from app.services.document_parser import parse_document
from app.services.extraction_service import extract_facts
from app.rag.chunker import chunk_pages
from app.rag.embeddings import embed_chunks


ALLOWED_MIME = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}


class IngestionError(ValueError):
    """把上传或解析失败转换为稳定的 API 错误结构。"""

    def __init__(self, code: str, message: str, *, status_code: int = 400, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.details = details or {}


class DocumentIngestionService:
    """协调对象存储、数据库、解析器、切块和向量索引。"""

    def __init__(
        self,
        db: SQLiteDatabase,
        storage: ObjectStorage,
        settings: Settings,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self.db = db
        self.storage = storage
        self.settings = settings
        self.embedding_provider = embedding_provider
        self.documents = DocumentRepository(db)

    def validate_upload(self, filename: str, mime_type: str, data: bytes) -> None:
        """在写入对象存储前校验扩展名、内容签名和大小。"""

        suffix = Path(filename or "").suffix.lower()
        if not suffix or suffix not in {".pdf", ".docx"}:
            raise IngestionError("UNSUPPORTED_FILE", "仅支持 PDF 或 DOCX 文件", details={"allowed_extensions": [".pdf", ".docx"]})
        expected = ALLOWED_MIME.get(mime_type)
        if expected and expected != suffix:
            raise IngestionError("UNSUPPORTED_FILE", "文件扩展名与 MIME 类型不匹配")
        if mime_type not in ALLOWED_MIME and mime_type not in {"application/octet-stream", ""}:
            raise IngestionError("UNSUPPORTED_FILE", "仅支持 PDF 或 DOCX 文件", details={"allowed_types": list(ALLOWED_MIME)})
        if not data:
            raise IngestionError("EMPTY_FILE", "文件不能为空")
        if suffix == ".pdf" and not data.lstrip().startswith(b"%PDF"):
            raise IngestionError("UNSUPPORTED_FILE", "文件内容不是有效的 PDF")
        if suffix == ".docx" and not data.startswith(b"PK"):
            raise IngestionError("UNSUPPORTED_FILE", "文件内容不是有效的 DOCX")
        if len(data) > self.settings.max_upload_bytes:
            raise IngestionError("FILE_TOO_LARGE", "文件超过大小限制", details={"max_bytes": self.settings.max_upload_bytes}, status_code=413)

    def create_version(self, *, filename: str, display_name: str | None, document_group_id: str | None, mime_type: str, data: bytes, owner_id: str | None = None, document_type: str = "contract") -> dict[str, Any]:
        """保存新版本；相同内容命中哈希时返回已有文档而不重复建任务。"""

        self.validate_upload(filename, mime_type, data)
        if document_type not in {"contract", "tender", "bid", "policy", "other"}:
            raise IngestionError("DOCUMENT_TYPE_INVALID", "不支持的文档类型")
        digest = hashlib.sha256(data).hexdigest()
        duplicate_sql = (
            "SELECT d.*, v.version_id, v.version_number, v.size_bytes, v.content_sha256, v.object_key "
            "FROM documents d JOIN document_versions v ON v.version_id=d.latest_version_id "
            "WHERE v.content_sha256=? AND d.owner_id=? AND d.document_type=?"
        )
        duplicate_params: tuple[Any, ...] = (digest, owner_id, document_type)
        if owner_id is None:
            duplicate_sql = duplicate_sql.replace("d.owner_id=?", "d.owner_id IS NULL")
            duplicate_params = (digest, document_type)
        existing = self.db.one(duplicate_sql, duplicate_params)
        if existing:
            return {"duplicate": True, **self._document_payload(existing)}
        now = utc_now()
        document_id = f"doc_{uuid4().hex}"
        version_id = f"ver_{uuid4().hex}"
        suffix = Path(filename).suffix.lower()
        object_key = f"documents/{document_id}/{version_id}{suffix}"
        self.storage.put(object_key, io.BytesIO(data))
        try:
            self.db.run(
                "INSERT INTO documents(document_id,owner_id,display_name,filename,document_group_id,document_type,mime_type,status,latest_version_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (document_id, owner_id, display_name or filename, filename, document_group_id, document_type, mime_type or "application/octet-stream", "uploaded", version_id, now, now),
            )
            self.db.run(
                "INSERT INTO document_versions(version_id,document_id,version_number,filename,mime_type,size_bytes,content_sha256,object_key,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (version_id, document_id, 1, filename, mime_type or "application/octet-stream", len(data), digest, object_key, "uploaded", now, now),
            )
        except Exception:
            self.storage.delete(object_key)
            raise
        return {"duplicate": False, "document_id": document_id, "version_id": version_id, "document_type": document_type, "filename": filename, "mime_type": mime_type, "size_bytes": len(data), "content_sha256": digest, "created_at": now, "status": "uploaded", "object_key": object_key}

    def parse_version(self, context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        """执行解析、事实抽取、切块、向量化并最终标记版本已索引。"""

        document_id = payload["document_id"]
        version_id = payload["version_id"]
        version = self.documents.get_version(document_id, version_id)
        if not version:
            raise IngestionError("DOCUMENT_NOT_FOUND", "文档版本不存在", status_code=404)
        self.documents.mark_parsing(document_id, version_id)
        context.update(15, "parse", "正在解析文档", "node.started")
        try:
            with self.storage.open(version["object_key"]) as stream:
                parsed = parse_document(stream.read(), version["mime_type"], version["filename"])
            # 解析成功立即保存页和块结构；后续索引失败时仍保留原文证据，
            # 同时把文档准确地停留在 parsed 状态。
            self.documents.save_parsed(document_id, version_id, parsed)
        except Exception as exc:
            message = str(exc)[:240]
            self.documents.mark_failed(
                document_id,
                version_id,
                code=getattr(exc, "code", "PARSE_FAILED"),
                message=message or "文档解析失败",
            )
            raise
        context.update(60, "parse", f"已解析 {len(parsed.pages)} 页")
        facts = extract_facts(parsed.text)
        chunks = chunk_pages(parsed.pages, document_id, version_id)
        embedding_batch = None
        if self.embedding_provider is not None:
            context.update(75, "embed", "正在生成文本向量", "node.started")
            embedding_batch = embed_chunks(self.embedding_provider, chunks)
            context.update(
                85,
                "embed",
                f"已生成 {len(embedding_batch.vectors)} 个文本向量",
            )
        self.documents.replace_chunks_and_mark_indexed(
            document_id,
            version_id,
            chunks,
            facts,
            embedding_batch,
        )
        context.update(95, "index", "已生成可筛选文本和结构化字段")
        return {
            "result_ref": version_id,
            "page_count": len(parsed.pages),
            "missing_fields": facts.get("missing_fields", []),
            "embedding_model": (
                embedding_batch.model_name if embedding_batch is not None else None
            ),
        }

    def _document_payload(self, row: dict[str, Any]) -> dict[str, Any]:
        """把上传记录转换为创建接口使用的轻量响应。"""

        return {
            "document_id": row["document_id"], "version_id": row.get("version_id") or row.get("latest_version_id"),
            "status": row["status"], "filename": row["filename"], "document_type": row.get("document_type", "contract"), "mime_type": row["mime_type"],
            "size_bytes": row.get("size_bytes"), "content_sha256": row.get("content_sha256"),
            "created_at": row["created_at"], "updated_at": row.get("updated_at", row["created_at"]),
        }

    def get(self, document_id: str, owner_id: str | None = None) -> dict[str, Any] | None:
        """读取当前身份的文档；匿名身份只读取匿名文档。"""

        row = self.db.one(
            "SELECT * FROM documents WHERE document_id=? AND owner_id IS ?",
            (document_id, owner_id),
        )
        if not row:
            return None
        version = self.db.one("SELECT * FROM document_versions WHERE version_id=?", (row["latest_version_id"],))
        doc = row_document(row)
        return {
            "document_id": doc["document_id"], "filename": doc["filename"], "display_name": doc["display_name"],
            "document_group_id": doc["document_group_id"], "document_type": doc.get("document_type", "contract"), "mime_type": doc["mime_type"], "status": doc["status"],
            "page_count": doc["page_count"], "parser_name": doc["parser_name"], "parser_version": doc["parser_version"],
            "quality_warnings": doc["quality_warnings"], "facts": doc["extracted_facts"],
            "failure": ({"code": doc["failure_code"], "message": doc["failure_message"]} if doc.get("failure_code") else None),
            "latest_version_id": doc["latest_version_id"], "created_at": doc["created_at"], "updated_at": doc["updated_at"],
            "latest_version": row_version(version) if version else None,
        }

    def list(self, status: str | None, limit: int, cursor: str | None, owner_id: str | None = None) -> dict[str, Any]:
        """按创建时间倒序分页返回文档。"""

        offset = int(cursor) if cursor and cursor.isdigit() else 0
        params: list[Any] = [owner_id]
        clauses: list[str] = ["owner_id IS ?"]
        if status:
            clauses.append("status=?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.all(f"SELECT * FROM documents {where} ORDER BY created_at DESC LIMIT ? OFFSET ?", tuple([*params, limit + 1, offset]))
        has_more = len(rows) > limit
        rows = rows[:limit]
        return {"items": [self.get(row["document_id"], owner_id) for row in rows], "next_cursor": str(offset + limit) if has_more else None}

    def versions(self, document_id: str, owner_id: str | None = None) -> list[dict[str, Any]]:
        """返回文档的全部版本摘要。"""

        rows = self.db.all(
            "SELECT v.* FROM document_versions v "
            "JOIN documents d ON d.document_id=v.document_id "
            "WHERE v.document_id=? AND d.owner_id IS ? ORDER BY v.version_number DESC",
            (document_id, owner_id),
        )
        return [row_version(row) for row in rows]
