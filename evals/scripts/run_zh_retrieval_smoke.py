"""用自编中文合同条款验证词法、向量和 RRF 检索功能。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from time import perf_counter

# 沿用 ACORD 基线中已经核对过的指标公式，避免两套口径漂移。
from run_acord_retrieval_eval import PROJECT_ROOT, score_query, summarize

from app.infrastructure.embedding import LocalSentenceTransformerEmbeddingProvider
from app.rag.chunker import Chunk
from app.rag.retriever import reciprocal_rank_fusion, retrieve, retrieve_by_vector


def load_dataset(path: Path) -> tuple[list[Chunk], list[dict]]:
    """校验合成语料 ID 和答案引用，防止无效答案抬高指标。"""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("dataset_id") != "zh_contract_smoke_v1":
        raise ValueError("unexpected Chinese smoke dataset ID")
    ids = [row["id"] for row in payload["corpus"]]
    if len(set(ids)) != len(ids) or not ids:
        raise ValueError("corpus IDs must be unique and non-empty")
    if any(
        not isinstance(row.get("text"), str)
        or not re.search(r"[\u3400-\u9fff]", row["text"])
        for row in payload["corpus"]
    ):
        raise ValueError("every corpus passage must contain Chinese text")
    if not payload["queries"]:
        raise ValueError("Chinese smoke dataset has no queries")
    query_ids = [row["id"] for row in payload["queries"]]
    if len(set(query_ids)) != len(query_ids):
        raise ValueError("query IDs must be unique")
    for row in payload["queries"]:
        if not isinstance(row.get("text"), str) or not row["text"].strip():
            raise ValueError(f"query text is empty: {row['id']}")
        if not row["relevant_ids"] or not set(row["relevant_ids"]).issubset(ids):
            raise ValueError(f"invalid relevant IDs for query {row['id']}")
        if len(set(row["relevant_ids"])) != len(row["relevant_ids"]):
            raise ValueError(f"duplicate relevant IDs for query {row['id']}")

    # 每条合成条款对应一个 Chunk；这些数据只存在于本轮评测内存中。
    chunks = [
        Chunk(
            chunk_id=row["id"],
            document_id="zh_smoke",
            version_id="zh_smoke_v1",
            page=None,
            text=row["text"],
            char_start=0,
            char_end=len(row["text"]),
            content_sha256=hashlib.sha256(row["text"].encode("utf-8")).hexdigest(),
        )
        for row in payload["corpus"]
    ]
    return chunks, payload["queries"]


def main() -> None:
    """运行固定中文冒烟集，输出可复查的逐问题指标和候选 ID。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=PROJECT_ROOT / "evals/datasets/zh_contract_smoke_v1.json",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--embedding-model",
        default="BAAI/bge-small-zh-v1.5",
        help="本地已缓存的中文 SentenceTransformer 模型",
    )
    args = parser.parse_args()
    chunks, queries = load_dataset(args.dataset)
    data_sha256 = hashlib.sha256(args.dataset.read_bytes()).hexdigest()

    lexical_results: dict[str, list] = {}
    lexical_times: dict[str, float] = {}
    for query in queries:
        start = perf_counter()
        lexical_results[query["id"]] = retrieve(chunks, query["text"], top_k=40)
        lexical_times[query["id"]] = (perf_counter() - start) * 1000

    # 禁止缺缓存时自动联网下载；实际运行模型与语料哈希写进报告。
    from sentence_transformers import SentenceTransformer

    provider = LocalSentenceTransformerEmbeddingProvider(
        args.embedding_model,
        model_loader=lambda name, device: SentenceTransformer(
            name, device=device, local_files_only=True
        ),
    )
    start = perf_counter()
    vectors = provider.embed([chunk.text for chunk in chunks])
    index_seconds = perf_counter() - start
    stored = list(zip(chunks, vectors))

    vector_results: dict[str, list] = {}
    vector_times: dict[str, float] = {}
    for query in queries:
        start = perf_counter()
        query_vector = provider.embed([query["text"]])[0]
        vector_results[query["id"]] = retrieve_by_vector(
            stored, query_vector, top_k=40, min_score=0.25
        )
        vector_times[query["id"]] = (perf_counter() - start) * 1000

    modes: dict[str, dict] = {}
    all_ids = {chunk.chunk_id for chunk in chunks}
    for mode in ("lexical", "vector", "hybrid"):
        rows: list[dict] = []
        times: list[float] = []
        for query in queries:
            query_id = query["id"]
            lexical = lexical_results[query_id]
            vector = vector_results[query_id]
            if mode == "lexical":
                ranked = lexical[:10]
                elapsed = lexical_times[query_id]
            elif mode == "vector":
                ranked = vector[:10]
                elapsed = vector_times[query_id]
            else:
                # 模拟生产逻辑：双路存在时 RRF 融合，否则选可用一路。
                ranked = (
                    reciprocal_rank_fusion((lexical, vector), top_k=10)
                    if lexical and vector
                    else (vector or lexical)[:10]
                )
                elapsed = lexical_times[query_id] + vector_times[query_id]
            top_ids = [item.chunk.chunk_id for item in ranked]
            relevant = set(query["relevant_ids"])
            labels = {item_id: 2 if item_id in relevant else 0 for item_id in all_ids}
            rows.append(
                {
                    "query_id": query_id,
                    "relevant_ids": sorted(relevant),
                    "top_ids": top_ids,
                    "at_5": score_query(top_ids, labels, 5),
                    "at_10": score_query(top_ids, labels, 10),
                }
            )
            times.append(elapsed)
        modes[mode] = {"summary": summarize(rows, times), "per_query": rows}

    report = {
        "dataset": "zh_contract_smoke_v1",
        "dataset_sha256": data_sha256,
        "synthetic": True,
        "scope": "自编条款级内存冒烟测试；非真实合同、非端到端质量评测",
        "corpus_count": len(chunks),
        "query_count": len(queries),
        "embedding_provider": provider.provider_name,
        "embedding_model": provider.model_name,
        "embedding_device": "cpu",
        "index_embedding_seconds": index_seconds,
        "relevance_policy": "每个查询人工指定 relevant_ids；其余语料均为 0 分",
        "modes": modes,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value["summary"] for key, value in modes.items()}, indent=2))
    print(f"report: {args.output}")


if __name__ == "__main__":
    main()
