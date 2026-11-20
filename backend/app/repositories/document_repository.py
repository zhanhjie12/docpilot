"""Persistence operations for document parsing and version state."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Any

from app.domain.models import ParsedDocument
from app.infrastructure.database import SQLiteDatabase, json_dumps, utc_now
from app.rag.chunker import Chunk
from app.rag.embeddings import EmbeddingBatch, serialize_vector


class DocumentRepository:
    """Keep document and latest-version parse metadata consistent."""

    def __init__(self, db: SQLiteDatabase) -> None:
        self.db = db

    @staticmethod
    def _require_one(cursor: sqlite3.Cursor, record: str) -> None:
        if cursor.rowcount != 1:
            raise LookupError(f"{record} changed or no longer exists")

    def get_version(self, document_id: str, version_id: str) -> dict[str, Any] | None:
        return self.db.one(
            "SELECT * FROM document_versions WHERE version_id=? AND document_id=?",
            (version_id, document_id),
        )

    def mark_parsing(self, document_id: str, version_id: str) -> None:
        now = utc_now()
        with self.db.transaction() as conn:
            version = conn.execute(
                "UPDATE document_versions SET status=?,failure_code=NULL,failure_message=NULL,updated_at=? "
                "WHERE version_id=? AND document_id=?",
                ("parsing", now, version_id, document_id),
            )
            document = conn.execute(
                "UPDATE documents SET status=?,failure_code=NULL,failure_message=NULL,updated_at=? "
                "WHERE document_id=? AND latest_version_id=?",
                ("parsing", now, document_id, version_id),
            )
            self._require_one(version, "document version")
            self._require_one(document, "document")

    def mark_failed(
        self,
        document_id: str,
        version_id: str,
        *,
        code: str,
        message: str,
    ) -> None:
        now = utc_now()
        warnings = json_dumps([message])
        with self.db.transaction() as conn:
            version = conn.execute(
                "UPDATE document_versions SET status=?,quality_warnings=?,failure_code=?,failure_message=?,updated_at=? "
                "WHERE version_id=? AND document_id=?",
                ("failed", warnings, code, message, now, version_id, document_id),
            )
            document = conn.execute(
                "UPDATE documents SET status=?,quality_warnings=?,failure_code=?,failure_message=?,updated_at=? "
                "WHERE document_id=? AND latest_version_id=?",
                ("failed", warnings, code, message, now, document_id, version_id),
            )
            self._require_one(version, "document version")
            self._require_one(document, "document")

    def save_parsed(
        self,
        document_id: str,
        version_id: str,
        parsed: ParsedDocument,
    ) -> None:
        """Persist the replayable page/block structure before indexing."""

        now = utc_now()
        warnings = json_dumps(parsed.quality_warnings)
        parsed_content = json_dumps(parsed.to_dict())
        with self.db.transaction() as conn:
            version = conn.execute(
                "UPDATE document_versions SET status=?,page_count=?,parser_name=?,parser_version=?,"
                "quality_warnings=?,failure_code=NULL,failure_message=NULL,parsed_content=?,extracted_text=?,updated_at=? "
                "WHERE version_id=? AND document_id=?",
                (
                    "parsed",
                    parsed.page_count,
                    parsed.parser_name,
                    parsed.parser_version,
                    warnings,
                    parsed_content,
                    parsed.text,
                    now,
                    version_id,
                    document_id,
                ),
            )
            document = conn.execute(
                "UPDATE documents SET status=?,page_count=?,parser_name=?,parser_version=?,quality_warnings=?,"
                "failure_code=NULL,failure_message=NULL,updated_at=? WHERE document_id=? AND latest_version_id=?",
                (
                    "parsed",
                    parsed.page_count,
                    parsed.parser_name,
                    parsed.parser_version,
                    warnings,
                    now,
                    document_id,
                    version_id,
                ),
            )
            self._require_one(version, "document version")
            self._require_one(document, "document")

    def mark_indexed(
        self,
        document_id: str,
        version_id: str,
        extracted_facts: dict[str, Any],
    ) -> None:
        with self.db.transaction() as conn:
            self._mark_indexed(conn, document_id, version_id, extracted_facts)

    def replace_chunks_and_mark_indexed(
        self,
        document_id: str,
        version_id: str,
        chunks: Sequence[Chunk],
        extracted_facts: dict[str, Any],
        embedding_batch: EmbeddingBatch | None = None,
    ) -> None:
        """Atomically publish a complete index for a parsed version."""

        if embedding_batch is not None and len(embedding_batch.vectors) != len(chunks):
            raise ValueError("one embedding vector is required for every chunk")

        created_at = utc_now()
        chunk_ids = [f"{version_id}_{chunk.chunk_id}" for chunk in chunks]
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM chunks WHERE version_id=?", (version_id,))
            conn.executemany(
                "INSERT INTO chunks(chunk_id,document_id,version_id,page,text,char_start,char_end,"
                "content_sha256,section_path,token_estimate,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        chunk_id,
                        document_id,
                        version_id,
                        chunk.page,
                        chunk.text,
                        chunk.char_start,
                        chunk.char_end,
                        chunk.content_sha256,
                        chunk.section_path,
                        chunk.token_estimate,
                        created_at,
                    )
                    for chunk_id, chunk in zip(chunk_ids, chunks)
                ],
            )
            if embedding_batch is not None and embedding_batch.vectors:
                conn.executemany(
                    "INSERT INTO chunk_embeddings("
                    "chunk_id,provider,model,dimension,embedding,content_sha256,created_at"
                    ") VALUES(?,?,?,?,?,?,?)",
                    [
                        (
                            chunk_id,
                            embedding_batch.provider_name,
                            embedding_batch.model_name,
                            len(vector),
                            sqlite3.Binary(serialize_vector(vector)),
                            chunk.content_sha256,
                            created_at,
                        )
                        for chunk_id, chunk, vector in zip(
                            chunk_ids,
                            chunks,
                            embedding_batch.vectors,
                        )
                    ],
                )
            self._mark_indexed(conn, document_id, version_id, extracted_facts)

    def _mark_indexed(
        self,
        conn: sqlite3.Connection,
        document_id: str,
        version_id: str,
        extracted_facts: dict[str, Any],
    ) -> None:
        now = utc_now()
        facts = json_dumps(extracted_facts)
        version = conn.execute(
            "UPDATE document_versions SET status=?,extracted_facts=?,updated_at=? "
            "WHERE version_id=? AND document_id=?",
            ("indexed", facts, now, version_id, document_id),
        )
        document = conn.execute(
            "UPDATE documents SET status=?,extracted_facts=?,updated_at=? "
            "WHERE document_id=? AND latest_version_id=?",
            ("indexed", facts, now, document_id, version_id),
        )
        self._require_one(version, "document version")
        self._require_one(document, "document")
