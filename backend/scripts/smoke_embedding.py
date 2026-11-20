"""Run a real local embedding smoke test without printing source text."""

from __future__ import annotations

import json
import math
import tempfile
import time
from pathlib import Path

from app.core.config import Settings
from app.infrastructure.database import SQLiteDatabase, utc_now
from app.infrastructure.embedding import EmbeddingProviderError, create_embedding_provider
from app.rag.chunker import Chunk
from app.rag.embeddings import cosine_similarity, embed_chunks
from app.rag.search import search_chunks
from app.repositories.document_repository import DocumentRepository


EXPECTED_MODEL_NAME = "BAAI/bge-small-zh-v1.5"
EXPECTED_MODEL_DIMENSION = 512
SEMANTIC_MARGIN = 0.03


def _seed_document(db: SQLiteDatabase) -> None:
    now = utc_now()
    db.run(
        "INSERT INTO documents("
        "document_id,display_name,filename,mime_type,status,latest_version_id,"
        "created_at,updated_at"
        ") VALUES(?,?,?,?,?,?,?,?)",
        (
            "doc_smoke",
            "Embedding smoke test",
            "smoke.txt",
            "text/plain",
            "parsed",
            "ver_smoke",
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
            "ver_smoke",
            "doc_smoke",
            1,
            "smoke.txt",
            "text/plain",
            1,
            "document-smoke-hash",
            "smoke.txt",
            "parsed",
            now,
            now,
        ),
    )


def main() -> int:
    settings = Settings()
    provider = create_embedding_provider(settings)
    if provider is None:
        raise RuntimeError("EMBEDDING_PROVIDER is disabled")

    texts = [
        "合同的付款期限是多少？",
        "甲方应在收到合规发票后30日内支付全部款项。",
        "发生争议时，双方应提交上海仲裁委员会仲裁。",
        "发生合同纠纷应由哪个机构处理？",
        "双方争议应提交上海仲裁委员会按照届时规则仲裁。",
        "乙方应对履约期间知悉的商业秘密承担保密义务。",
        "什么情况下可以解除合同？",
        "一方严重违约且催告后仍未改正，另一方有权书面解除合同。",
        "所有发票应以人民币开具并注明合同编号。",
        "交付地点为采购方指定的仓库。",
        "产品验收标准以技术附件为准。",
        "供应商应为全部货物购买运输保险。",
        "未经书面同意不得转让合同权利义务。",
        "合同自双方盖章之日起生效。",
        "不可抗力持续超过六十日时双方应协商处理。",
        "通知应通过专人、快递或电子邮件送达。",
    ]
    # Put a repeated query after a complete batch so the check remains valid
    # even when EMBEDDING_BATCH_SIZE is overridden in the environment.
    batch_size = getattr(provider, "batch_size", settings.embedding_batch_size)
    while len(texts) < batch_size:
        texts.append(f"用于跨批次测试的合同条款{len(texts)}。")
    texts.append(texts[0])
    duplicate_index = len(texts) - 1
    started_at = time.perf_counter()
    try:
        vectors = provider.embed(texts)
    except EmbeddingProviderError as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "provider": provider.provider_name,
                    "model": provider.model_name,
                    "error_code": exc.code,
                    "message": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    elapsed_seconds = time.perf_counter() - started_at

    dimensions = {len(vector) for vector in vectors}
    norms = [math.sqrt(math.fsum(value * value for value in vector)) for vector in vectors]
    semantic_pairs = (
        (0, 1, 2),
        (3, 4, 5),
        (6, 7, 8),
    )
    semantic_scores = [
        {
            "positive": cosine_similarity(vectors[query], vectors[positive]),
            "negative": cosine_similarity(vectors[query], vectors[negative]),
        }
        for query, positive, negative in semantic_pairs
    ]
    duplicate_similarity = cosine_similarity(vectors[0], vectors[duplicate_index])

    warm_started_at = time.perf_counter()
    warm_vector = provider.embed([texts[0]])[0]
    warm_seconds = time.perf_counter() - warm_started_at
    warm_similarity = cosine_similarity(vectors[0], warm_vector)

    chunks = [
        Chunk(
            chunk_id=f"chunk_{index}",
            document_id="doc_smoke",
            version_id="ver_smoke",
            page=index + 1,
            text=texts[text_index],
            char_start=index * 100,
            char_end=index * 100 + len(texts[text_index]),
            content_sha256=f"chunk-smoke-hash-{index}",
            section_path="合同条款",
            token_estimate=len(texts[text_index]),
        )
        for index, text_index in enumerate((1, 4, 7))
    ]
    temp_root = Path(__file__).resolve().parents[1] / ".pytest-tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=temp_root) as temp_dir:
        db = SQLiteDatabase(Path(temp_dir) / "embedding-smoke.db")
        db.initialize()
        _seed_document(db)
        batch = embed_chunks(provider, chunks)
        DocumentRepository(db).replace_chunks_and_mark_indexed(
            "doc_smoke",
            "ver_smoke",
            chunks,
            {},
            batch,
        )
        stored = db.one(
            "SELECT COUNT(*) AS count,MIN(dimension) AS min_dimension,"
            "MAX(dimension) AS max_dimension,MIN(length(embedding)) AS min_bytes,"
            "MAX(length(embedding)) AS max_bytes FROM chunk_embeddings"
        )
        results = search_chunks(
            db,
            "货款最迟多久能到账？",
            document_ids=["doc_smoke"],
            top_k=1,
            embedding_provider=provider,
            min_vector_score=settings.embedding_min_score,
        )
        db.close()

    expected_bytes = EXPECTED_MODEL_DIMENSION * 4
    e2e_top1_ok = bool(results) and results[0].chunk.content_sha256 == "chunk-smoke-hash-0"
    e2e_vector_score = results[0].vector_score if results else 0.0
    checks = {
        "expected_model": provider.model_name == EXPECTED_MODEL_NAME,
        "vector_count": len(vectors) == len(texts),
        "expected_dimension": dimensions == {EXPECTED_MODEL_DIMENSION},
        "finite": all(math.isfinite(value) for vector in vectors for value in vector),
        "normalized": all(abs(norm - 1.0) < 1e-4 for norm in norms),
        "semantic_margin": all(
            score["positive"] >= settings.embedding_min_score
            and score["positive"] - score["negative"] >= SEMANTIC_MARGIN
            for score in semantic_scores
        ),
        "cross_batch_order": duplicate_similarity > 0.99999,
        "repeatability": warm_similarity > 0.99999,
        "sqlite_rows": bool(stored) and stored["count"] == len(chunks),
        "sqlite_shape": bool(stored)
        and stored["min_dimension"] == EXPECTED_MODEL_DIMENSION
        and stored["max_dimension"] == EXPECTED_MODEL_DIMENSION
        and stored["min_bytes"] == expected_bytes
        and stored["max_bytes"] == expected_bytes,
        "semantic_top1": e2e_top1_ok
        and e2e_vector_score >= settings.embedding_min_score,
    }

    print(
        json.dumps(
            {
                "provider": provider.provider_name,
                "model": provider.model_name,
                "device": settings.embedding_device,
                "vector_count": len(vectors),
                "dimension": next(iter(dimensions), 0),
                "norm_range": [round(min(norms), 6), round(max(norms), 6)],
                "semantic_scores": [
                    {
                        "positive": round(score["positive"], 6),
                        "negative": round(score["negative"], 6),
                        "margin": round(score["positive"] - score["negative"], 6),
                    }
                    for score in semantic_scores
                ],
                "duplicate_similarity": round(duplicate_similarity, 6),
                "warm_similarity": round(warm_similarity, 6),
                "cold_start_seconds": round(elapsed_seconds, 3),
                "warm_query_seconds": round(warm_seconds, 3),
                "sqlite_embedding_rows": stored["count"] if stored else 0,
                "semantic_top1_vector_score": round(e2e_vector_score, 6),
                "checks": checks,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
