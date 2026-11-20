"""Concrete embedding adapters for local and OpenAI-compatible providers."""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Sequence
from typing import Any

from app.core.config import Settings
from app.infrastructure.providers import EmbeddingProvider


class EmbeddingProviderError(RuntimeError):
    """Stable, content-free failure raised by an embedding adapter."""

    code = "EMBEDDING_PROVIDER_FAILED"


def _normalize_vectors(
    vectors: Sequence[Sequence[float]],
    *,
    expected_count: int,
) -> list[list[float]]:
    """校验数量、维度和有限值，并把每个向量归一化。"""

    if len(vectors) != expected_count:
        raise EmbeddingProviderError(
            f"Embedding provider returned {len(vectors)} vectors for "
            f"{expected_count} inputs"
        )

    normalized: list[list[float]] = []
    dimension: int | None = None
    for raw_vector in vectors:
        try:
            vector = [float(value) for value in raw_vector]
        except (TypeError, ValueError) as exc:
            raise EmbeddingProviderError(
                "Embedding provider returned a non-numeric vector"
            ) from exc
        if not vector or any(not math.isfinite(value) for value in vector):
            raise EmbeddingProviderError(
                "Embedding provider returned an empty or non-finite vector"
            )
        if dimension is None:
            dimension = len(vector)
        elif len(vector) != dimension:
            raise EmbeddingProviderError(
                "Embedding provider returned inconsistent vector dimensions"
            )
        norm = math.sqrt(math.fsum(value * value for value in vector))
        if norm <= 1e-12:
            raise EmbeddingProviderError("Embedding provider returned a zero vector")
        normalized.append([value / norm for value in vector])
    return normalized


def _validate_inputs(texts: Sequence[str]) -> list[str]:
    """拒绝空输入，避免远端或本地模型返回不可用向量。"""

    values = list(texts)
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise EmbeddingProviderError("Embedding inputs must be non-empty strings")
    return values


class LocalSentenceTransformerEmbeddingProvider:
    """Lazy local adapter for SentenceTransformer-compatible models."""

    provider_name = "sentence_transformers"

    def __init__(
        self,
        model_name: str,
        *,
        device: str = "cpu",
        batch_size: int = 16,
        model_loader: Callable[[str, str], Any] | None = None,
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.batch_size = batch_size
        self._model_loader = model_loader
        self._model: Any | None = None
        self._lock = threading.RLock()

    def _load_model(self) -> Any:
        """首次调用时懒加载模型，启动健康检查不会强制占用模型内存。"""

        if self._model is not None:
            return self._model
        try:
            if self._model_loader is not None:
                self._model = self._model_loader(self.model_name, self.device)
            else:
                from sentence_transformers import SentenceTransformer

                try:
                    # 优先只读本地缓存，避免每次启动都访问模型仓库。
                    self._model = SentenceTransformer(
                        self.model_name,
                        device=self.device,
                        local_files_only=True,
                    )
                except OSError:
                    # Avoid a Hub metadata request on every startup when the
                    # complete model already exists in the local cache. A
                    # cache miss falls back to the normal download behavior.
                    self._model = SentenceTransformer(
                        self.model_name,
                        device=self.device,
                        local_files_only=False,
                    )
        except ImportError as exc:
            raise EmbeddingProviderError(
                'Local embeddings require: pip install -e ".[embedding-local]"'
            ) from exc
        except Exception as exc:
            raise EmbeddingProviderError(
                f"Could not load local embedding model {self.model_name!r}"
            ) from exc
        return self._model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """批量生成本地向量，并统一转换为归一化 Python 列表。"""

        values = _validate_inputs(texts)
        if not values:
            return []
        try:
            with self._lock:
                model = self._load_model()
                vectors = model.encode(
                    values,
                    batch_size=self.batch_size,
                    normalize_embeddings=True,
                    convert_to_numpy=True,
                    show_progress_bar=False,
                )
        except EmbeddingProviderError:
            raise
        except Exception as exc:
            raise EmbeddingProviderError(
                f"Local embedding model {self.model_name!r} failed"
            ) from exc
        return _normalize_vectors(vectors, expected_count=len(values))


class OpenAICompatibleEmbeddingProvider:
    """Embedding adapter for relays exposing POST /v1/embeddings."""

    provider_name = "openai_compatible"

    def __init__(
        self,
        model_name: str,
        *,
        api_key: str,
        base_url: str,
        timeout_seconds: float = 60.0,
        batch_size: int = 16,
        client: Any | None = None,
    ) -> None:
        self.model_name = model_name
        self.batch_size = batch_size
        self._api_key = api_key
        root = base_url.rstrip("/")
        self._endpoint = root if root.endswith("/embeddings") else f"{root}/embeddings"
        self._owns_client = client is None
        if client is None:
            try:
                import httpx
            except ImportError as exc:
                raise EmbeddingProviderError(
                    'API embeddings require: pip install -e ".[embedding-api]"'
                ) from exc
            client = httpx.Client(timeout=timeout_seconds)
        self._client = client

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """按 batch 调用兼容 OpenAI 的 embeddings 接口并校验响应。"""

        values = _validate_inputs(texts)
        if not values:
            return []

        vectors: list[list[float]] = []
        for start in range(0, len(values), self.batch_size):
            batch = values[start : start + self.batch_size]
            try:
                response = self._client.post(
                    self._endpoint,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json={"model": self.model_name, "input": batch},
                )
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                suffix = f" (HTTP {status_code})" if status_code else ""
                raise EmbeddingProviderError(
                    f"Embedding API request failed{suffix}"
                ) from exc

            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, list):
                raise EmbeddingProviderError(
                    "Embedding API response does not contain a data array"
                )
            try:
                ordered = sorted(data, key=lambda item: int(item["index"]))
                indices = [int(item["index"]) for item in ordered]
                batch_vectors = [item["embedding"] for item in ordered]
            except (KeyError, TypeError, ValueError) as exc:
                raise EmbeddingProviderError(
                    "Embedding API response has an invalid item shape"
                ) from exc
            if indices != list(range(len(batch))):
                raise EmbeddingProviderError(
                    "Embedding API response indices do not match the request"
                )
            vectors.extend(batch_vectors)

        return _normalize_vectors(vectors, expected_count=len(values))

    def close(self) -> None:
        """关闭由适配器创建的 HTTP 客户端，外部注入的客户端不负责关闭。"""

        if self._owns_client:
            self._client.close()


def create_embedding_provider(settings: Settings) -> EmbeddingProvider | None:
    """根据配置创建本地、兼容 API 或禁用状态的 embedding 提供方。"""

    """Build the configured adapter without downloading a local model at startup."""

    if settings.embedding_provider == "disabled":
        return None
    if settings.embedding_provider == "local":
        return LocalSentenceTransformerEmbeddingProvider(
            settings.embedding_model,
            device=settings.embedding_device,
            batch_size=settings.embedding_batch_size,
        )
    if settings.embedding_provider == "openai_compatible":
        return OpenAICompatibleEmbeddingProvider(
            settings.embedding_model,
            api_key=settings.effective_embedding_api_key,
            base_url=settings.effective_embedding_base_url,
            timeout_seconds=settings.embedding_timeout_seconds,
            batch_size=settings.embedding_batch_size,
        )
    raise ValueError(f"Unsupported embedding provider: {settings.embedding_provider}")
