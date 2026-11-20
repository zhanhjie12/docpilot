"""Database-backed hybrid search for the small SQLite MVP."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from app.infrastructure.database import SQLiteDatabase
from app.infrastructure.providers import EmbeddingProvider
from app.rag.chunker import Chunk
from app.rag.embeddings import deserialize_vector
from app.rag.retriever import (
    RetrievedChunk,
    reciprocal_rank_fusion,
    retrieve,
    retrieve_by_vector,
)


logger = logging.getLogger("docpilot.rag")


def _scope_clause(
    document_ids: Sequence[str],
    version_ids: Sequence[str],
) -> tuple[str, tuple[str, ...]]:
    """生成参数化的文档/版本范围条件，避免检索越过会话边界。"""

    clauses: list[str] = []
    params: list[str] = []
    if document_ids:
        clauses.append(
            f"c.document_id IN ({','.join('?' for _ in document_ids)})"
        )
        params.extend(document_ids)
    if version_ids:
        clauses.append(f"c.version_id IN ({','.join('?' for _ in version_ids)})")
        params.extend(version_ids)
    if not clauses:
        return "", ()
    return f"WHERE ({' OR '.join(clauses)})", tuple(params)


def _chunk_from_row(row: dict[str, Any]) -> Chunk:
    return Chunk(
        chunk_id=row["chunk_id"],
        document_id=row["document_id"],
        version_id=row["version_id"],
        page=row["page"],
        text=row["text"],
        char_start=row["char_start"],
        char_end=row["char_end"],
        content_sha256=row["content_sha256"],
        section_path=row.get("section_path"),
        token_estimate=row.get("token_estimate") or 0,
    )


def load_scoped_chunks(
    db: SQLiteDatabase,
    *,
    document_ids: Sequence[str] = (),
    version_ids: Sequence[str] = (),
) -> list[Chunk]:
    """按显式范围加载块，未指定范围时返回空列表。"""

    where, params = _scope_clause(document_ids, version_ids)
    if not where:
        return []
    rows = db.all(
        "SELECT DISTINCT c.* FROM chunks c "
        f"{where} ORDER BY c.document_id,c.version_id,c.char_start",
        params,
    )
    return [_chunk_from_row(row) for row in rows]


def _load_vectors(
    db: SQLiteDatabase,
    provider: EmbeddingProvider,
    *,
    document_ids: Sequence[str],
    version_ids: Sequence[str],
) -> list[tuple[Chunk, tuple[float, ...]]]:
    """加载指定 embedding 提供方和模型的已存向量。"""

    where, scope_params = _scope_clause(document_ids, version_ids)
    if not where:
        return []
    rows = db.all(
        "SELECT c.*,e.dimension,e.embedding FROM chunks c "
        "JOIN chunk_embeddings e ON e.chunk_id=c.chunk_id "
        "AND e.provider=? AND e.model=? "
        f"{where} ORDER BY c.document_id,c.version_id,c.char_start",
        (provider.provider_name, provider.model_name, *scope_params),
    )
    loaded: list[tuple[Chunk, tuple[float, ...]]] = []
    for row in rows:
        try:
            vector = deserialize_vector(row["embedding"], int(row["dimension"]))
        except (TypeError, ValueError):
            logger.warning(
                "Ignoring invalid stored embedding chunk_id=%s provider=%s model=%s",
                row["chunk_id"],
                provider.provider_name,
                provider.model_name,
            )
            continue
        loaded.append((_chunk_from_row(row), vector))
    return loaded


def search_chunks(
    db: SQLiteDatabase,
    question: str,
    *,
    document_ids: Sequence[str] = (),
    version_ids: Sequence[str] = (),
    top_k: int = 5,
    embedding_provider: EmbeddingProvider | None = None,
    min_vector_score: float = 0.25,
) -> list[RetrievedChunk]:
    """Search only the explicit document scope and fall back to lexical ranking."""

    # 先执行词法检索作为稳定基线；向量服务故障时仍能返回可解释结果。
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    document_scope = tuple(dict.fromkeys(document_ids))
    version_scope = tuple(dict.fromkeys(version_ids))
    chunks = load_scoped_chunks(
        db,
        document_ids=document_scope,
        version_ids=version_scope,
    )
    if not chunks:
        return []

    candidate_limit = max(20, top_k * 4)
    lexical = retrieve(chunks, question, top_k=candidate_limit)
    if embedding_provider is None:
        return lexical[:top_k]

    stored = _load_vectors(
        db,
        embedding_provider,
        document_ids=document_scope,
        version_ids=version_scope,
    )
    if not stored:
        return lexical[:top_k]

    try:
        query_vectors = embedding_provider.embed([question])
        if len(query_vectors) != 1:
            raise ValueError("embedding provider returned the wrong query vector count")
        vector = retrieve_by_vector(
            stored,
            query_vectors[0],
            top_k=candidate_limit,
            min_score=min_vector_score,
        )
    except Exception as exc:  # retrieval remains available if a remote model is down
        logger.warning(
            "Vector query failed; using lexical retrieval provider=%s model=%s error=%s",
            embedding_provider.provider_name,
            embedding_provider.model_name,
            type(exc).__name__,
        )
        return lexical[:top_k]

    # 两路都有结果时用 RRF 合并，否则返回可用的单路结果。
    if lexical and vector:
        return reciprocal_rank_fusion((lexical, vector), top_k=top_k)
    if vector:
        return vector[:top_k]
    return lexical[:top_k]
