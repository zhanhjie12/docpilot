"""Deterministic lexical, vector, and hybrid retrieval primitives."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.rag.chunker import Chunk
from app.rag.embeddings import cosine_similarity


@dataclass(slots=True)
class RetrievedChunk:
    """候选块及词法、向量分数，供混合检索和证据筛选复用。"""

    chunk: Chunk
    score: float
    lexical_score: float = 0.0
    vector_score: float = 0.0


def _query_terms(question: str) -> list[str]:
    """Keep Latin terms and add CJK bigrams for unsegmented Chinese queries."""

    value = question.lower()
    terms = re.findall(r"[a-z0-9_]{2,}", value)
    for segment in re.findall(r"[\u3400-\u9fff]+", value):
        if len(segment) >= 2:
            terms.append(segment)
            terms.extend(segment[index : index + 2] for index in range(len(segment) - 1))
    return list(dict.fromkeys(terms))


def retrieve(chunks: list[Chunk], question: str, top_k: int = 5) -> list[RetrievedChunk]:
    """Rank chunks by exact terms while remaining useful for Chinese text."""

    # 先把中文拆成二元词组，再按出现次数计算可解释的词法分数。
    terms = _query_terms(question)
    if not terms:
        return []
    ranked: list[RetrievedChunk] = []
    for chunk in chunks:
        lower = chunk.text.lower()
        score = sum(
            lower.count(term) * max(1.0, min(len(term), 8) / 2)
            for term in terms
        )
        if score:
            ranked.append(
                RetrievedChunk(
                    chunk=chunk,
                    score=float(score),
                    lexical_score=float(score),
                )
            )
    ranked.sort(key=lambda item: item.score, reverse=True)
    return ranked[: max(1, top_k)]


def retrieve_by_vector(
    chunks_and_vectors: Sequence[tuple[Chunk, Sequence[float]]],
    query_vector: Sequence[float],
    *,
    top_k: int = 5,
    min_score: float = 0.25,
) -> list[RetrievedChunk]:
    """Rank stored vectors by cosine similarity and apply an evidence threshold."""

    # 过滤低于阈值的结果，避免语义相似度很低的片段进入回答证据。
    ranked: list[RetrievedChunk] = []
    for chunk, vector in chunks_and_vectors:
        try:
            score = cosine_similarity(query_vector, vector)
        except ValueError:
            continue
        if score >= min_score:
            ranked.append(
                RetrievedChunk(
                    chunk=chunk,
                    score=score,
                    vector_score=score,
                )
            )
    ranked.sort(key=lambda item: item.vector_score, reverse=True)
    return ranked[: max(1, top_k)]


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[RetrievedChunk]],
    *,
    top_k: int = 5,
    rank_constant: int = 60,
) -> list[RetrievedChunk]:
    """Fuse scores from differently scaled retrievers without calibration."""

    if rank_constant < 0:
        raise ValueError("rank_constant cannot be negative")
    # RRF 只融合排名，不直接比较词法分数和向量分数的量纲。
    chunks: dict[str, Chunk] = {}
    scores: dict[str, float] = {}
    lexical_scores: dict[str, float] = {}
    vector_scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            chunk_id = item.chunk.chunk_id
            chunks[chunk_id] = item.chunk
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (
                rank_constant + rank
            )
            lexical_scores[chunk_id] = max(
                lexical_scores.get(chunk_id, 0.0),
                item.lexical_score,
            )
            vector_scores[chunk_id] = max(
                vector_scores.get(chunk_id, 0.0),
                item.vector_score,
            )

    fused = [
        RetrievedChunk(
            chunk=chunks[chunk_id],
            score=score,
            lexical_score=lexical_scores.get(chunk_id, 0.0),
            vector_score=vector_scores.get(chunk_id, 0.0),
        )
        for chunk_id, score in scores.items()
    ]
    fused.sort(
        key=lambda item: (
            item.score,
            item.vector_score,
            item.lexical_score,
            item.chunk.chunk_id,
        ),
        reverse=True,
    )
    return fused[: max(1, top_k)]
