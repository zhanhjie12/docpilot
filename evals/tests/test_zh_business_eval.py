"""Annotation gates and metrics for real Chinese business evaluation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from evals.scripts.evaluate_zh_business import evaluate, load_annotated_cases


def _case() -> dict:
    text = "合同签订后30日内付款。"
    return {
        "case_id": "case_1", "source_id": "project_1", "split": "test",
        "document_type": "contract", "source_url": "https://example.org/contract.pdf",
        "license_note": "人工核对公开用途", "redaction_note": "人工复核已脱敏",
        "annotator": "a", "reviewer": "b", "redacted_text": text,
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "queries": [
            {"query_id": "q_1", "question": "付款期限30日", "answerable": True,
             "gold_spans": [{"start": 0, "end": len(text), "quote": text}]},
            {"query_id": "q_2", "question": "仲裁地点在哪里", "answerable": False,
             "gold_spans": []},
        ],
    }


def test_zh_business_gold_offsets_and_metrics():
    path = Path(__file__).parent / f".zh_eval_{uuid4().hex}.json"
    try:
        path.write_text(json.dumps({"schema_version": 1, "cases": [_case()]}, ensure_ascii=False), encoding="utf-8")
        cases = load_annotated_cases(path)
        result = evaluate(cases)
        assert result["query_count"] == 2
        assert result["recall_at_k"] == 1
        assert result["citation_replay_rate"] == 1
        assert result["false_retrieval_rate"] == 0
        bad = _case()
        bad["queries"][0]["gold_spans"][0]["quote"] = "伪造条款"
        path.write_text(json.dumps({"schema_version": 1, "cases": [bad]}, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(ValueError, match="does not replay"):
            load_annotated_cases(path)
    finally:
        path.unlink(missing_ok=True)
