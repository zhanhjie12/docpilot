"""Run the real PDF -> chunks -> embeddings -> RAG search smoke test."""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from app.core.config import Settings
from app.infrastructure.database import SQLiteDatabase
from app.infrastructure.embedding import EmbeddingProviderError, create_embedding_provider
from app.infrastructure.storage import LocalObjectStorage
from app.rag.search import search_chunks
from app.services.document_ingestion import DocumentIngestionService


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


def main() -> int:
    settings = Settings()
    provider = create_embedding_provider(settings)
    if provider is None:
        raise RuntimeError("EMBEDDING_PROVIDER is disabled")

    fixture = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "contract_simple.pdf"
    temp_root = Path(__file__).resolve().parents[1] / ".pytest-tmp"
    temp_root.mkdir(parents=True, exist_ok=True)

    started_at = time.perf_counter()
    with tempfile.TemporaryDirectory(dir=temp_root) as temp_dir:
        root = Path(temp_dir)
        db = SQLiteDatabase(root / "rag-embedding.db")
        db.initialize()
        storage = LocalObjectStorage(root / "uploads")
        service = DocumentIngestionService(db, storage, settings, provider)
        created = service.create_version(
            filename=fixture.name,
            display_name="RAG embedding smoke fixture",
            document_group_id=None,
            mime_type="application/pdf",
            data=fixture.read_bytes(),
        )
        context = RecordingContext()
        ingestion_result = service.parse_version(
            context,
            {
                "document_id": created["document_id"],
                "version_id": created["version_id"],
            },
        )

        chunks = db.one(
            "SELECT COUNT(*) AS count FROM chunks WHERE version_id=?",
            (created["version_id"],),
        )
        vectors = db.one(
            "SELECT COUNT(*) AS count,MIN(e.dimension) AS min_dimension,"
            "MAX(e.dimension) AS max_dimension "
            "FROM chunk_embeddings e JOIN chunks c ON c.chunk_id=e.chunk_id "
            "WHERE c.version_id=? AND e.provider=? AND e.model=?",
            (created["version_id"], provider.provider_name, provider.model_name),
        )
        document = db.one(
            "SELECT status,page_count FROM documents WHERE document_id=?",
            (created["document_id"],),
        )

        results = search_chunks(
            db,
            "When must payment be made after acceptance?",
            document_ids=[created["document_id"]],
            version_ids=[created["version_id"]],
            top_k=3,
            embedding_provider=provider,
            min_vector_score=settings.embedding_min_score,
        )
        top_hit = results[0] if results else None
        elapsed_seconds = time.perf_counter() - started_at
        checks = {
            "document_indexed": bool(document) and document["status"] == "indexed",
            "chunks_created": bool(chunks) and chunks["count"] > 0,
            "one_vector_per_chunk": bool(chunks)
            and bool(vectors)
            and vectors["count"] == chunks["count"],
            "model_dimension": bool(vectors)
            and vectors["min_dimension"] == vectors["max_dimension"] == 512,
            "embedding_progress": (75, "embed") in context.events,
            "retrieval_returned": top_hit is not None,
            "payment_clause_top_hit": bool(top_hit)
            and "payment" in top_hit.chunk.text.lower()
            and "30 days" in top_hit.chunk.text.lower(),
            "vector_score_above_threshold": bool(top_hit)
            and top_hit.vector_score >= settings.embedding_min_score,
        }
        db.close()

    print(
        json.dumps(
            {
                "model": provider.model_name,
                "provider": provider.provider_name,
                "fixture": fixture.name,
                "page_count": document["page_count"] if document else 0,
                "chunk_count": chunks["count"] if chunks else 0,
                "vector_count": vectors["count"] if vectors else 0,
                "dimension": vectors["min_dimension"] if vectors else 0,
                "retrieved_count": len(results),
                "top_vector_score": round(top_hit.vector_score, 6) if top_hit else 0.0,
                "elapsed_seconds": round(elapsed_seconds, 3),
                "ingestion_embedding_model": ingestion_result.get("embedding_model"),
                "checks": checks,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EmbeddingProviderError as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": exc.code,
                    "message": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise SystemExit(2)
