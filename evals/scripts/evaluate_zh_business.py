"""Score annotated Chinese business passages without exporting source text."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from statistics import mean
from time import perf_counter

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.rag.chunker import chunk_text  # noqa: E402
from app.rag.retriever import retrieve  # noqa: E402


def load_annotated_cases(path: Path) -> list[dict]:
    """Reject missing provenance, bad labels and unverifiable gold offsets."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(payload.get("cases"), list):
        raise ValueError("dataset must use schema_version=1 and contain cases")
    cases = payload["cases"]
    if not cases or not any(case.get("split") == "test" for case in cases):
        raise ValueError("dataset must include annotated test cases")
    case_ids: set[str] = set()
    query_ids: set[str] = set()
    split_by_source: dict[str, str] = {}
    for case in cases:
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id or case_id in case_ids:
            raise ValueError("case_id must be unique and non-empty")
        case_ids.add(case_id)
        source_id, split = case.get("source_id"), case.get("split")
        if not isinstance(source_id, str) or not source_id or split not in {"train", "dev", "test"}:
            raise ValueError(f"invalid source or split: {case_id}")
        if source_id in split_by_source and split_by_source[source_id] != split:
            raise ValueError(f"source leaks across splits: {source_id}")
        split_by_source[source_id] = split
        if case.get("document_type") not in {"contract", "tender", "bid", "policy"}:
            raise ValueError(f"invalid document_type: {case_id}")
        text = case.get("redacted_text")
        if not isinstance(text, str) or not text.strip() or not any("\u3400" <= char <= "\u9fff" for char in text):
            raise ValueError(f"Chinese redacted_text required: {case_id}")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if case.get("text_sha256") != digest:
            raise ValueError(f"redacted text hash differs: {case_id}")
        for key in ("source_url", "license_note", "redaction_note", "annotator", "reviewer"):
            if not isinstance(case.get(key), str) or not case[key].strip():
                raise ValueError(f"missing {key}: {case_id}")
        if not isinstance(case.get("queries"), list) or not case["queries"]:
            raise ValueError(f"queries required: {case_id}")
        for query in case["queries"]:
            query_id = query.get("query_id")
            if not isinstance(query_id, str) or not query_id or query_id in query_ids:
                raise ValueError("query_id must be globally unique")
            query_ids.add(query_id)
            if not isinstance(query.get("question"), str) or not query["question"].strip():
                raise ValueError(f"question required: {query_id}")
            if not isinstance(query.get("answerable"), bool) or not isinstance(query.get("gold_spans"), list):
                raise ValueError(f"answerability and gold_spans required: {query_id}")
            if query["answerable"] != bool(query["gold_spans"]):
                raise ValueError(f"gold spans contradict answerability: {query_id}")
            for span in query["gold_spans"]:
                start, end, quote = span.get("start"), span.get("end"), span.get("quote")
                if not isinstance(start, int) or not isinstance(end, int) or not isinstance(quote, str) \
                        or start < 0 or end <= start or text[start:end] != quote:
                    raise ValueError(f"gold span does not replay: {query_id}")
    return cases


def evaluate(cases: list[dict], *, top_k: int = 5) -> dict:
    """Measure source recall, false retrieval and exact citation replay."""

    if top_k < 1:
        raise ValueError("top_k must be positive")
    rows: list[dict] = []
    for case in cases:
        if case["split"] != "test":
            continue
        text = case["redacted_text"]
        chunks = chunk_text(text, case["case_id"], case["case_id"] + "_v1")
        for query in case["queries"]:
            started = perf_counter()
            hits = retrieve(chunks, query["question"], top_k=top_k)
            latency_ms = (perf_counter() - started) * 1000
            gold = query["gold_spans"]
            recalled = bool(gold) and any(
                chunk.chunk.char_start <= span["start"] and chunk.chunk.char_end >= span["end"]
                for chunk in hits for span in gold
            )
            replayable = all(
                text[hit.chunk.char_start:hit.chunk.char_end] == hit.chunk.text
                for hit in hits
            )
            rows.append({
                "query_id": query["query_id"], "case_id": case["case_id"],
                "answerable": query["answerable"], "recall_hit": recalled,
                "false_retrieval": not query["answerable"] and bool(hits),
                "citation_replayable": replayable,
                "retrieved_chunk_ids": [hit.chunk.chunk_id for hit in hits],
                "latency_ms": round(latency_ms, 3),
            })
    positive = [row for row in rows if row["answerable"]]
    negative = [row for row in rows if not row["answerable"]]
    report = {
        "case_count": len({row["case_id"] for row in rows}),
        "query_count": len(rows), "top_k": top_k,
        "recall_at_k": mean(row["recall_hit"] for row in positive) if positive else None,
        "false_retrieval_rate": mean(row["false_retrieval"] for row in negative) if negative else None,
        "citation_replay_rate": mean(row["citation_replayable"] for row in rows) if rows else None,
        "mean_latency_ms": round(mean(row["latency_ms"] for row in rows), 3) if rows else None,
        "rows": rows,
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()
    if args.output.resolve() == args.dataset.resolve():
        raise ValueError("output must differ from source dataset")
    cases = load_annotated_cases(args.dataset)
    report = evaluate(cases, top_k=args.top_k)
    report["dataset_sha256"] = hashlib.sha256(args.dataset.read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Scored {report['query_count']} test queries; report: {args.output}")


if __name__ == "__main__":
    main()
