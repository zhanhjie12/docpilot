"""Pure vector validation and serialization helpers for the RAG index."""

from __future__ import annotations

import math
import struct
from collections.abc import Sequence
from dataclasses import dataclass

from app.infrastructure.providers import EmbeddingProvider
from app.rag.chunker import Chunk


@dataclass(frozen=True, slots=True)
class EmbeddingBatch:
    """保存一次批量向量化结果及其提供方元数据。"""

    provider_name: str
    model_name: str
    vectors: tuple[tuple[float, ...], ...]

    @property
    def dimension(self) -> int:
        return len(self.vectors[0]) if self.vectors else 0


def _normalized(vector: Sequence[float]) -> tuple[float, ...]:
    """验证向量并归一化，保证余弦相似度可以直接计算。"""

    values = tuple(float(value) for value in vector)
    if not values or any(not math.isfinite(value) for value in values):
        raise ValueError("embedding vectors must contain finite numeric values")
    norm = math.sqrt(math.fsum(value * value for value in values))
    if norm <= 1e-12:
        raise ValueError("embedding vectors cannot be zero")
    return tuple(value / norm for value in values)


def embed_chunks(
    provider: EmbeddingProvider,
    chunks: Sequence[Chunk],
) -> EmbeddingBatch:
    """Embed chunks in one ordered batch and validate the provider contract."""

    # 保持输入块和返回向量的顺序，索引时才能把向量写回正确 chunk。
    if not chunks:
        return EmbeddingBatch(provider.provider_name, provider.model_name, ())
    raw_vectors = provider.embed([chunk.text for chunk in chunks])
    if len(raw_vectors) != len(chunks):
        raise ValueError("embedding provider returned the wrong number of vectors")
    vectors = tuple(_normalized(vector) for vector in raw_vectors)
    dimensions = {len(vector) for vector in vectors}
    if len(dimensions) != 1:
        raise ValueError("embedding vectors must all have the same dimension")
    return EmbeddingBatch(provider.provider_name, provider.model_name, vectors)


def serialize_vector(vector: Sequence[float]) -> bytes:
    """把归一化向量保存为紧凑 little-endian float32 二进制。"""

    values = _normalized(vector)
    return struct.pack(f"<{len(values)}f", *values)


def deserialize_vector(value: bytes | memoryview, dimension: int) -> tuple[float, ...]:
    """按数据库记录的维度恢复向量，并先校验字节长度。"""

    if dimension <= 0:
        raise ValueError("embedding dimension must be positive")
    raw = bytes(value)
    expected_size = dimension * 4
    if len(raw) != expected_size:
        raise ValueError(
            f"embedding blob has {len(raw)} bytes; expected {expected_size}"
        )
    return tuple(struct.unpack(f"<{dimension}f", raw))


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """计算两个非零等维向量的余弦相似度。"""

    if len(left) != len(right) or not left:
        raise ValueError("vectors must have the same non-zero dimension")
    left_norm = math.sqrt(math.fsum(value * value for value in left))
    right_norm = math.sqrt(math.fsum(value * value for value in right))
    if left_norm <= 1e-12 or right_norm <= 1e-12:
        raise ValueError("cannot compare a zero vector")
    return math.fsum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)
