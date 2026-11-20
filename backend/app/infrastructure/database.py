"""SQLite persistence adapter for the local DocPilot MVP.

Each operation uses a short lived connection.  This keeps the adapter safe for
FastAPI worker threads and makes it straightforward to replace with PostgreSQL
later without leaking connections into route handlers.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.core.config import BACKEND_ROOT, Settings


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class SQLiteDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._write_lock = threading.RLock()

    @classmethod
    def from_settings(cls, settings: Settings) -> "SQLiteDatabase":
        url = settings.database_url
        if url.startswith("sqlite:///"):
            raw = url.removeprefix("sqlite:///")
        else:
            raw = url
        path = Path(raw)
        if not path.is_absolute():
            path = BACKEND_ROOT / path
        return cls(path.resolve())

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        with self._write_lock, self.connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    last_login_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
                CREATE TABLE IF NOT EXISTS documents (
                    document_id TEXT PRIMARY KEY,
                    owner_id TEXT,
                    display_name TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    document_group_id TEXT,
                    document_type TEXT NOT NULL DEFAULT 'contract',
                    mime_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    latest_version_id TEXT,
                    page_count INTEGER,
                    parser_name TEXT,
                    parser_version TEXT,
                    quality_warnings TEXT NOT NULL DEFAULT '[]',
                    failure_code TEXT,
                    failure_message TEXT,
                    extracted_facts TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS document_versions (
                    version_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
                    version_number INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    object_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    page_count INTEGER,
                    parser_name TEXT,
                    parser_version TEXT,
                    quality_warnings TEXT NOT NULL DEFAULT '[]',
                    failure_code TEXT,
                    failure_message TEXT,
                    parsed_content TEXT NOT NULL DEFAULT '{}',
                    extracted_facts TEXT NOT NULL DEFAULT '{}',
                    extracted_text TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(document_id, version_number),
                    UNIQUE(document_id, content_sha256)
                );
                CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
                CREATE INDEX IF NOT EXISTS idx_versions_document ON document_versions(document_id, version_number DESC);
                -- Policy applicability is separate from the upload revision number.
                CREATE TABLE IF NOT EXISTS policy_versions (
                    version_id TEXT PRIMARY KEY REFERENCES document_versions(version_id) ON DELETE CASCADE,
                    policy_family_id TEXT NOT NULL,
                    issuer TEXT NOT NULL,
                    org_scope TEXT NOT NULL,
                    effective_from TEXT NOT NULL,
                    effective_to TEXT,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_policy_applicability
                    ON policy_versions(policy_family_id,org_scope,effective_from,effective_to);
                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
                    version_id TEXT NOT NULL REFERENCES document_versions(version_id) ON DELETE CASCADE,
                    page INTEGER,
                    text TEXT NOT NULL,
                    char_start INTEGER NOT NULL,
                    char_end INTEGER NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    section_path TEXT,
                    token_estimate INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_chunks_version ON chunks(version_id);
                CREATE TABLE IF NOT EXISTS chunk_embeddings (
                    chunk_id TEXT NOT NULL REFERENCES chunks(chunk_id) ON DELETE CASCADE,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    dimension INTEGER NOT NULL,
                    embedding BLOB NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(chunk_id, provider, model)
                );
                CREATE INDEX IF NOT EXISTS idx_chunk_embeddings_model
                    ON chunk_embeddings(provider, model);
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    owner_id TEXT,
                    task_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0,
                    current_node TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    failure_code TEXT,
                    failure_message TEXT,
                    result_ref TEXT,
                    worker_id TEXT,
                    lease_expires_at TEXT,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_events (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    node TEXT,
                    progress INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    data TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(task_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS screening_results (
                    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
                    query TEXT NOT NULL,
                    conditions TEXT NOT NULL DEFAULT '{}',
                    total INTEGER NOT NULL DEFAULT 0,
                    matched INTEGER NOT NULL DEFAULT 0,
                    items TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    review_id TEXT PRIMARY KEY,
                    owner_id TEXT,
                    status TEXT NOT NULL,
                    document_ids TEXT NOT NULL,
                    version_ids TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    resolution_note TEXT,
                    resolved_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS findings (
                    finding_id TEXT PRIMARY KEY,
                    review_id TEXT NOT NULL REFERENCES reviews(review_id) ON DELETE CASCADE,
                    document_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    rule_id TEXT NOT NULL,
                    rule_version TEXT NOT NULL,
                    playbook_id TEXT NOT NULL DEFAULT 'legacy',
                    title TEXT NOT NULL,
                    rule_hit INTEGER NOT NULL,
                    explanation TEXT NOT NULL,
                    recommendation TEXT NOT NULL,
                    citations TEXT NOT NULL DEFAULT '[]',
                    review_status TEXT NOT NULL,
                    review_note TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_findings_review ON findings(review_id);
                -- Agent 状态按任务保存，供刷新页面后恢复节点结果和证据引用。
                CREATE TABLE IF NOT EXISTS agent_runs (
                    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
                    review_id TEXT NOT NULL REFERENCES reviews(review_id) ON DELETE CASCADE,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_runs_review ON agent_runs(review_id);
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    session_id TEXT PRIMARY KEY,
                    owner_id TEXT,
                    document_ids TEXT NOT NULL,
                    version_ids TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chat_messages (
                    message_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES chat_sessions(session_id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    citations TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    key TEXT PRIMARY KEY,
                    owner_id TEXT,
                    operation TEXT NOT NULL,
                    request_hash TEXT,
                    response TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            # Keep databases created by previous MVP iterations compatible.
            migrations = {
                "chunks": (
                    ("section_path", "TEXT"),
                    ("token_estimate", "INTEGER NOT NULL DEFAULT 0"),
                ),
                "documents": (
                    ("owner_id", "TEXT"),
                    ("document_type", "TEXT NOT NULL DEFAULT 'contract'"),
                    ("failure_code", "TEXT"),
                    ("failure_message", "TEXT"),
                ),
                "document_versions": (
                    ("failure_code", "TEXT"),
                    ("failure_message", "TEXT"),
                    ("parsed_content", "TEXT NOT NULL DEFAULT '{}'"),
                ),
                "tasks": (("owner_id", "TEXT"), ("worker_id", "TEXT"),
                          ("lease_expires_at", "TEXT")),
                "reviews": (("owner_id", "TEXT"), ("resolution_note", "TEXT"),
                            ("resolved_at", "TEXT")),
                "findings": (("playbook_id", "TEXT NOT NULL DEFAULT 'legacy'"),),
                "chat_sessions": (("owner_id", "TEXT"),),
                # ``request_hash`` remains nullable so records written by older
                # upload and screening endpoints stay readable during a rolling
                # upgrade. New Review records always populate the column.
                "idempotency_keys": (
                    ("owner_id", "TEXT"),
                    ("request_hash", "TEXT"),
                ),
            }
            for table, additions in migrations.items():
                columns = {
                    row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
                for column, definition in additions:
                    if column not in columns:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            # 索引必须在兼容迁移完成后创建，旧数据库可能刚刚补上 owner_id。
            conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_owner ON documents(owner_id, created_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_owner ON tasks(owner_id, created_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_reviews_owner ON reviews(owner_id, created_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_sessions_owner ON chat_sessions(owner_id, created_at DESC)")

    def run(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self.transaction() as conn:
            cursor = conn.execute(sql, params)
            return cursor.rowcount

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """Run related writes in one commit under the process write lock.

        ``immediate=True`` obtains SQLite's write reservation before a caller
        reads or claims a shared record. It is used for idempotent creation so
        two application workers cannot both observe a missing key and create
        separate resources.
        """

        with self._write_lock, self.connection() as conn:
            if immediate:
                conn.execute("BEGIN IMMEDIATE")
            yield conn

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def close(self) -> None:
        """Kept for lifecycle symmetry; connections are operation scoped."""


def row_document(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key in ("quality_warnings", "extracted_facts"):
        result[key] = json_loads(result.get(key), [] if key == "quality_warnings" else {})
    return result


def row_version(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["quality_warnings"] = json_loads(result.get("quality_warnings"), [])
    result["extracted_facts"] = json_loads(result.get("extracted_facts"), {})
    result["parsed_content"] = json_loads(result.get("parsed_content"), {})
    return result
