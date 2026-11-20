"""Embedding adapters, persistence, and hybrid retrieval stay deterministic."""

from __future__ import annotations

import math
import shutil
import sys
from pathlib import Path
from types import ModuleType
from typing import Sequence
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import main as main_module
from app.core.config import Settings
from app.infrastructure.database import SQLiteDatabase, utc_now
from app.infrastructure.embedding import (
    EmbeddingProviderError,
    LocalSentenceTransformerEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
)
from app.infrastructure.storage import LocalObjectStorage
from app.rag.chunker import Chunk
from app.rag.embeddings import embed_chunks
from app.rag.search import search_chunks
from app.repositories.document_repository import DocumentRepository
from app.services.document_ingestion import DocumentIngestionService
from app.core.middleware import wrap_http_app


class MappingEmbeddingProvider:
    provider_name = "fake"

    def __init__(self, model_name: str = "fake-v1") -> None:
        self.model_name = model_name

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            if any(term in text for term in ("争议", "仲裁", "纠纷")):
                vectors.append([0.0, 1.0])
            else:
                vectors.append([1.0, 0.0])
        return vectors


class RecordingContext:
    def __init__(self) -> None:
        self.events: list[tuple[int, str]] = []

    def update(
        self,
        progress: int,
        node: str,
        message: str,
        event_type: str = "node.completed",
        data: dict | None = None,
    ) -> None:
        self.events.append((progress, node))


@pytest.fixture
def embedding_root():
    root = (
        Path(__file__).resolve().parents[1]
        / ".test-storage"
        / f"embeddings-{uuid4().hex}"
    )
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _seed_version(db: SQLiteDatabase) -> None:
    now = utc_now()
    db.run(
        "INSERT INTO documents("
        "document_id,display_name,filename,mime_type,status,latest_version_id,"
        "created_at,updated_at"
        ") VALUES(?,?,?,?,?,?,?,?)",
        (
            "doc_1",
            "供应商合同",
            "contract.pdf",
            "application/pdf",
            "parsed",
            "ver_1",
            now,
            now,
        ),
    )
    db.run(
        "INSERT INTO document_versions("
        "version_id,document_id,version_number,filename,mime_type,size_bytes,"
        "content_sha256,object_key,status,created_at,updated_at"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            "ver_1",
            "doc_1",
            1,
            "contract.pdf",
            "application/pdf",
            100,
            "document-hash",
            "documents/doc_1/ver_1.pdf",
            "parsed",
            now,
            now,
        ),
    )


def _chunks() -> list[Chunk]:
    texts = [
        "合同总价为人民币一百万元。",
        "发生争议时，应提交上海仲裁委员会仲裁。",
    ]
    return [
        Chunk(
            chunk_id=f"chunk_{index}",
            document_id="doc_1",
            version_id="ver_1",
            page=index + 1,
            text=text,
            char_start=index * 100,
            char_end=index * 100 + len(text),
            content_sha256=f"hash-{index}",
            section_path="合同条款",
            token_estimate=len(text),
        )
        for index, text in enumerate(texts)
    ]


def test_settings_validate_local_and_relay_embedding_contracts() -> None:
    local = Settings(_env_file=None, embedding_provider="sentence-transformers")
    assert local.embedding_provider == "local"
    assert local.embedding_model == "BAAI/bge-small-zh-v1.5"

    with pytest.raises(ValidationError, match="EMBEDDING_API_KEY"):
        Settings(
            _env_file=None,
            embedding_provider="openai_compatible",
            embedding_model="embedding-model",
        )

    relay = Settings(
        _env_file=None,
        embedding_provider="openai",
        embedding_model="embedding-model",
        llm_api_key="relay-key",
        llm_base_url="https://relay.example.com/v1/",
    )
    assert relay.embedding_provider == "openai_compatible"
    assert relay.effective_embedding_api_key == "relay-key"
    assert relay.effective_embedding_base_url == "https://relay.example.com/v1"


def test_local_provider_is_lazy_and_normalizes_vectors() -> None:
    loads: list[tuple[str, str]] = []

    class FakeModel:
        def encode(self, texts, **kwargs):
            assert kwargs["normalize_embeddings"] is True
            return [[3.0, 4.0] for _ in texts]

    def load(model_name: str, device: str):
        loads.append((model_name, device))
        return FakeModel()

    provider = LocalSentenceTransformerEmbeddingProvider(
        "BAAI/bge-small-zh-v1.5",
        device="cpu",
        model_loader=load,
    )
    assert loads == []

    vectors = provider.embed(["第一条", "第二条"])
    provider.embed(["第三条"])

    assert loads == [("BAAI/bge-small-zh-v1.5", "cpu")]
    assert vectors[0] == pytest.approx([0.6, 0.8])
    assert vectors[1] == pytest.approx([0.6, 0.8])


def test_local_provider_prefers_cache_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[bool] = []

    class FakeModel:
        def encode(self, texts, **kwargs):
            return [[1.0, 0.0] for _ in texts]

    def sentence_transformer(model_name, *, device, local_files_only):
        attempts.append(local_files_only)
        if local_files_only:
            raise OSError("cache miss")
        return FakeModel()

    fake_module = ModuleType("sentence_transformers")
    fake_module.SentenceTransformer = sentence_transformer
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)
    provider = LocalSentenceTransformerEmbeddingProvider(
        "BAAI/bge-small-zh-v1.5",
        device="cpu",
    )

    assert provider.embed(["合同条款"]) == [[1.0, 0.0]]
    assert attempts == [True, False]


def test_openai_compatible_provider_uses_embeddings_endpoint_and_input_order() -> None:
    class Response:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class Client:
        def __init__(self):
            self.calls = []

        def post(self, url, *, headers, json):
            self.calls.append((url, headers, json))
            data = [
                {"index": index, "embedding": [float(index + 1), 1.0]}
                for index in range(len(json["input"]))
            ]
            return Response({"data": list(reversed(data))})

    client = Client()
    provider = OpenAICompatibleEmbeddingProvider(
        "embedding-model",
        api_key="secret-key",
        base_url="https://relay.example.com/v1/",
        batch_size=2,
        client=client,
    )

    vectors = provider.embed(["a", "b", "c"])

    assert len(client.calls) == 2
    assert all(call[0] == "https://relay.example.com/v1/embeddings" for call in client.calls)
    assert all(call[1]["Authorization"] == "Bearer secret-key" for call in client.calls)
    assert all(math.isclose(sum(value * value for value in vector), 1.0) for vector in vectors)
    assert vectors[0][0] < vectors[1][0]


def test_api_provider_failure_does_not_expose_response_content() -> None:
    class Client:
        def post(self, *args, **kwargs):
            raise RuntimeError("private contract text and secret-key")

    provider = OpenAICompatibleEmbeddingProvider(
        "embedding-model",
        api_key="secret-key",
        base_url="https://relay.example.com/v1",
        client=Client(),
    )

    with pytest.raises(EmbeddingProviderError) as caught:
        provider.embed(["sensitive contract clause"])

    assert "private contract" not in str(caught.value)
    assert "secret-key" not in str(caught.value)


def test_embeddings_are_persisted_and_enable_semantic_retrieval(
    embedding_root: Path,
) -> None:
    db = SQLiteDatabase(embedding_root / "embedding.db")
    db.initialize()
    _seed_version(db)
    chunks = _chunks()
    provider = MappingEmbeddingProvider()
    batch = embed_chunks(provider, chunks)

    DocumentRepository(db).replace_chunks_and_mark_indexed(
        "doc_1",
        "ver_1",
        chunks,
        {},
        batch,
    )

    rows = db.all(
        "SELECT provider,model,dimension,length(embedding) AS bytes "
        "FROM chunk_embeddings ORDER BY chunk_id"
    )
    assert rows == [
        {"provider": "fake", "model": "fake-v1", "dimension": 2, "bytes": 8},
        {"provider": "fake", "model": "fake-v1", "dimension": 2, "bytes": 8},
    ]

    results = search_chunks(
        db,
        "纠纷处理机构",
        document_ids=["doc_1"],
        top_k=1,
        embedding_provider=provider,
        min_vector_score=0.5,
    )

    assert len(results) == 1
    assert "仲裁委员会" in results[0].chunk.text
    assert results[0].vector_score > 0.99


def test_reindex_replaces_vectors_from_the_previous_model(
    embedding_root: Path,
) -> None:
    db = SQLiteDatabase(embedding_root / "reindex.db")
    db.initialize()
    _seed_version(db)
    chunks = _chunks()
    repository = DocumentRepository(db)

    for model_name in ("fake-v1", "fake-v2"):
        provider = MappingEmbeddingProvider(model_name)
        repository.replace_chunks_and_mark_indexed(
            "doc_1",
            "ver_1",
            chunks,
            {},
            embed_chunks(provider, chunks),
        )

    rows = db.all(
        "SELECT DISTINCT provider,model FROM chunk_embeddings ORDER BY model"
    )
    assert rows == [{"provider": "fake", "model": "fake-v2"}]


def test_document_ingestion_generates_embeddings_before_publish(
    embedding_root: Path,
) -> None:
    database = SQLiteDatabase(embedding_root / "ingestion.db")
    database.initialize()
    storage_root = embedding_root / "uploads"
    storage_root.mkdir()
    provider = MappingEmbeddingProvider()
    service = DocumentIngestionService(
        database,
        LocalObjectStorage(storage_root),
        Settings(_env_file=None),
        provider,
    )
    fixture = Path(__file__).parent / "fixtures" / "contract_simple.pdf"
    created = service.create_version(
        filename=fixture.name,
        display_name="Supplier agreement",
        document_group_id=None,
        mime_type="application/pdf",
        data=fixture.read_bytes(),
    )
    context = RecordingContext()

    result = service.parse_version(
        context,
        {
            "document_id": created["document_id"],
            "version_id": created["version_id"],
        },
    )

    chunk_count = database.one(
        "SELECT COUNT(*) AS count FROM chunks WHERE version_id=?",
        (created["version_id"],),
    )
    embedding_count = database.one(
        "SELECT COUNT(*) AS count FROM chunk_embeddings e "
        "JOIN chunks c ON c.chunk_id=e.chunk_id WHERE c.version_id=?",
        (created["version_id"],),
    )
    assert chunk_count is not None and embedding_count is not None
    assert chunk_count["count"] == embedding_count["count"] > 0
    assert result["embedding_model"] == provider.model_name
    assert (75, "embed") in context.events


def test_chat_route_uses_configured_embedding_provider(
    embedding_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = MappingEmbeddingProvider()
    monkeypatch.setattr(
        main_module,
        "create_embedding_provider",
        lambda settings: provider,
    )
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{(embedding_root / 'api.db').as_posix()}",
        upload_dir=embedding_root / "uploads",
        embedding_provider="local",
        embedding_model=provider.model_name,
        embedding_min_score=0.5,
    )
    application = main_module.create_app(settings)

    with TestClient(wrap_http_app(application)) as client:
        db = application.state.database
        _seed_version(db)
        chunks = _chunks()
        DocumentRepository(db).replace_chunks_and_mark_indexed(
            "doc_1",
            "ver_1",
            chunks,
            {},
            embed_chunks(provider, chunks),
        )

        session_response = client.post(
            "/api/v1/chat/sessions",
            json={"document_ids": ["doc_1"]},
        )
        assert session_response.status_code == 201
        session_id = session_response.json()["session_id"]

        answer_response = client.post(
            f"/api/v1/chat/{session_id}/ask",
            json={
                "document_ids": ["doc_1"],
                "question": "纠纷处理机构",
                "top_k": 1,
            },
        )

    assert answer_response.status_code == 200
    payload = answer_response.json()
    assert payload["status"] == "answered"
    assert len(payload["citations"]) == 1
    assert "仲裁委员会" in payload["citations"][0]["quote"]
