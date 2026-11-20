"""用 ACORD 的 test 切分测量 DocPilot 现有检索原语。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from time import perf_counter

# 评测脚本位于 backend 外；只把本地源码加入导入路径，不触碰业务数据库。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.infrastructure.embedding import LocalSentenceTransformerEmbeddingProvider  # noqa: E402
from app.rag.chunker import Chunk  # noqa: E402
from app.rag.retriever import (  # noqa: E402
    reciprocal_rank_fusion,
    retrieve,
    retrieve_by_vector,
)


def verify_inputs(raw_dir: Path, manifest: dict) -> None:
    """评测前检查三个实际使用的原件，避免版本漂移污染基线。"""

    expected = {item["path"]: item["sha256"] for item in manifest["files"]}
    for relative in ("corpus.jsonl", "queries.jsonl", "qrels/test.tsv"):
        digest = hashlib.sha256((raw_dir / relative).read_bytes()).hexdigest()
        if digest != expected.get(relative):
            raise ValueError(f"ACORD file hash differs from manifest: {relative}")


def load_data(raw_dir: Path) -> tuple[list[Chunk], list[dict], dict[str, dict[str, int]]]:
    """保持 ACORD 原始 ID，并仅加载官方 test 查询和标签。"""

    chunks: list[Chunk] = []
    with (raw_dir / "corpus.jsonl").open(encoding="utf-8") as source:
        for line in source:
            row = json.loads(line)
            value = "\n".join(
                part for part in (row.get("title", ""), row["text"]) if part
            )
            chunks.append(
                Chunk(
                    chunk_id=row["_id"],
                    document_id="acord",
                    version_id="acord_v1",
                    page=None,
                    text=value,
                    char_start=0,
                    char_end=len(value),
                    content_sha256=hashlib.sha256(value.encode("utf-8")).hexdigest(),
                )
            )

    with (raw_dir / "queries.jsonl").open(encoding="utf-8") as source:
        queries = [
            row
            for line in source
            if (row := json.loads(line))["metadata"]["split"] == "test"
        ]
    query_ids = {row["_id"] for row in queries}

    labels: dict[str, dict[str, int]] = defaultdict(dict)
    with (raw_dir / "qrels" / "test.tsv").open(
        encoding="utf-8", newline=""
    ) as source:
        for row in csv.DictReader(source, delimiter="\t"):
            if row["query-id"] in query_ids:
                labels[row["query-id"]][row["corpus-id"]] = int(row["score"])
    if set(labels) != query_ids:
        raise ValueError("test queries and qrels do not match")
    return chunks, queries, labels


def score_query(ids: list[str], labels: dict[str, int], top_k: int) -> dict[str, float]:
    """分数 >=2 为相关；nDCG 保留 ACORD 的 0–4 分等级。"""

    relevant = {item_id for item_id, grade in labels.items() if grade >= 2}
    found = ids[:top_k]
    hits = [index for index, item_id in enumerate(found, 1) if item_id in relevant]
    dcg = sum(
        (2 ** labels.get(item_id, 0) - 1) / math.log2(index + 1)
        for index, item_id in enumerate(found, 1)
    )
    ideal = sum(
        (2**grade - 1) / math.log2(index + 1)
        for index, grade in enumerate(sorted(labels.values(), reverse=True)[:top_k], 1)
    )
    return {
        "recall": len(hits) / len(relevant) if relevant else 0.0,
        "hit_rate": float(bool(hits)),
        "mrr": 1 / hits[0] if hits else 0.0,
        "ndcg": dcg / ideal if ideal else 0.0,
        "judged_fraction": (
            sum(item_id in labels for item_id in found) / len(found) if found else 0.0
        ),
    }


def percentile(values: list[float], fraction: float) -> float:
    """用线性插值计算检索耗时分位数。"""

    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def summarize(rows: list[dict], times_ms: list[float]) -> dict:
    """报告宏平均：每个查询权重相同。"""

    return {
        "query_count": len(rows),
        "recall_at_5": mean(row["at_5"]["recall"] for row in rows),
        "recall_at_10": mean(row["at_10"]["recall"] for row in rows),
        "hit_rate_at_5": mean(row["at_5"]["hit_rate"] for row in rows),
        "mrr_at_10": mean(row["at_10"]["mrr"] for row in rows),
        "ndcg_at_10": mean(row["at_10"]["ndcg"] for row in rows),
        "judged_fraction_at_10": mean(row["at_10"]["judged_fraction"] for row in rows),
        "empty_results": sum(not row["top_ids"] for row in rows),
        "latency_ms_p50": percentile(times_ms, 0.50),
        "latency_ms_p95": percentile(times_ms, 0.95),
    }


def main() -> None:
    """固定 test 数据，运行词法基线和可选的本地向量对比。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=PROJECT_ROOT / "evals/raw/acord")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--query-limit", type=int)
    parser.add_argument("--embedding-model", help="本地已缓存模型；省略时仅跑词法检索")
    args = parser.parse_args()
    if args.query_limit is not None and args.query_limit <= 0:
        parser.error("--query-limit must be positive")

    manifest = json.loads((PROJECT_ROOT / "evals/manifest.json").read_text(encoding="utf-8"))
    verify_inputs(args.raw_dir, manifest)
    chunks, queries, labels = load_data(args.raw_dir)
    if args.query_limit:
        queries = queries[: args.query_limit]
    if not queries:
        raise ValueError("no test queries found")

    # 先计算词法结果。候选池与生产 search_chunks 一致，取 max(20, top_k*4)。
    lexical_results: dict[str, list] = {}
    lexical_times: dict[str, float] = {}
    for query in queries:
        start = perf_counter()
        lexical_results[query["_id"]] = retrieve(chunks, query["text"], top_k=40)
        lexical_times[query["_id"]] = (perf_counter() - start) * 1000

    vector_results: dict[str, list] = {}
    vector_times: dict[str, float] = {}
    index_seconds: float | None = None
    if args.embedding_model:
        # 强制仅用本地模型文件，评测不会意外下载模型或调用中转站。
        from sentence_transformers import SentenceTransformer

        provider = LocalSentenceTransformerEmbeddingProvider(
            args.embedding_model,
            model_loader=lambda name, device: SentenceTransformer(
                name, device=device, local_files_only=True
            ),
        )
        print(f"Encoding {len(chunks)} ACORD clauses with {args.embedding_model}...", flush=True)
        start = perf_counter()
        vectors = provider.embed([chunk.text for chunk in chunks])
        index_seconds = perf_counter() - start
        print(f"Corpus encoding completed in {index_seconds:.1f}s", flush=True)
        stored = list(zip(chunks, vectors))
        for query in queries:
            start = perf_counter()
            query_vector = provider.embed([query["text"]])[0]
            vector_results[query["_id"]] = retrieve_by_vector(
                stored, query_vector, top_k=40, min_score=0.25
            )
            vector_times[query["_id"]] = (perf_counter() - start) * 1000

    modes: dict[str, dict] = {}
    for mode in ("lexical", "vector", "hybrid") if vector_results else ("lexical",):
        rows: list[dict] = []
        times: list[float] = []
        for query in queries:
            query_id = query["_id"]
            lexical = lexical_results[query_id]
            vector = vector_results.get(query_id, [])
            if mode == "lexical":
                ranked = lexical[:10]
                elapsed = lexical_times[query_id]
            elif mode == "vector":
                ranked = vector[:10]
                elapsed = vector_times[query_id]
            else:
                # 与生产逻辑一致：双路有结果才融合，否则退回可用单路。
                ranked = (
                    reciprocal_rank_fusion((lexical, vector), top_k=10)
                    if lexical and vector
                    else (vector or lexical)[:10]
                )
                elapsed = lexical_times[query_id] + vector_times[query_id]
            top_ids = [item.chunk.chunk_id for item in ranked]
            rows.append(
                {
                    "query_id": query_id,
                    "top_ids": top_ids,
                    "at_5": score_query(top_ids, labels[query_id], 5),
                    "at_10": score_query(top_ids, labels[query_id], 10),
                }
            )
            times.append(elapsed)
        modes[mode] = {"summary": summarize(rows, times), "per_query": rows}

    # 固定发布包哈希，避免以后下载到不同版本却误用当前基线。
    report = {
        "dataset": "acord_v1",
        "source_revision": manifest["source_revision"],
        "evaluated_at_utc": datetime.now(timezone.utc).isoformat(),
        "split": "test",
        "corpus_count": len(chunks),
        "test_query_count": len(queries),
        "query_limit": args.query_limit,
        "relevance_threshold": 2,
        "top_k": [5, 10],
        "candidate_limit": 40,
        "vector_min_score": 0.25 if vector_results else None,
        "embedding_model": args.embedding_model,
        "embedding_provider": "sentence_transformers" if vector_results else None,
        "embedding_device": "cpu" if vector_results else None,
        "index_embedding_seconds": index_seconds,
        "scope": "in-memory retriever primitives; no FastAPI or SQLite indexing",
        "unjudged_policy": "qrels 中无分数的候选按 0 分计算；judged_fraction 单独报告",
        "modes": modes,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value["summary"] for key, value in modes.items()}, indent=2))
    print(f"report: {args.output}")


if __name__ == "__main__":
    main()
