"""验证 ACORD 原件校验器的通过、定位与只读行为。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from evals.scripts.validate_acord import main, validate


FIXTURE = Path(__file__).parent / "fixtures" / "acord_valid"


def _dataset(tmp_path: Path) -> tuple[Path, Path]:
    """复制最小 BEIR 数据并固定真实文件哈希。"""

    raw = tmp_path / "raw"
    shutil.copytree(FIXTURE, raw)
    manifest = tmp_path / "manifest.json"
    initial = {"files": []}
    manifest.write_text(json.dumps(initial), encoding="utf-8")
    files = validate(raw, manifest, verify_hashes=False)["files"]
    manifest.write_text(json.dumps({"files": files}), encoding="utf-8")
    return raw, manifest


def _reasons(report: dict) -> list[str]:
    """只提取错误原因，保持断言聚焦于校验行为。"""

    return [item["reason"] for item in report["errors"]]


def test_valid_dataset_preserves_raw_files_and_zero_relevance(tmp_path: Path) -> None:
    """合法数据通过；显式标注的 0 分 qrels 不能被丢弃。"""

    raw, manifest = _dataset(tmp_path)
    before = {item["path"]: item["sha256"] for item in validate(raw, manifest, verify_hashes=True)["files"]}
    report_path = tmp_path / "processed" / "validation_report.json"

    assert main(["--raw-dir", str(raw), "--manifest", str(manifest), "--report", str(report_path), "--verify-hashes"]) == 0
    result = json.loads(report_path.read_text(encoding="utf-8"))
    after = {item["path"]: item["sha256"] for item in result["files"]}
    assert result["valid"] is True
    assert result["summary"]["qrels_count"] == 3
    assert before == after


@pytest.mark.parametrize(
    ("relative_path", "replacement", "expected"),
    [
        ("corpus.jsonl", b'{"_id":"clause_1","text":"x"}\n{"_id":"clause_1","text":"y"}\n', "重复 _id"),
        ("queries.jsonl", b'{"_id":"query_1","text":"   "}\n', "text 不能为空"),
        ("queries.jsonl", b'{"_id":"query_1","text":"x"}\n\xff', "不是有效的 UTF-8"),
        ("qrels/test.tsv", b"query-id\tcorpus-id\tscore\nmissing\tclause_1\t4\n", "query-id 未在"),
        ("qrels/test.tsv", b"query-id\tcorpus-id\tscore\nquery_1\tmissing\t4\n", "corpus-id 未在"),
        ("qrels/test.tsv", b"query-id\tcorpus-id\tscore\nquery_1\tclause_1\t-1\n", "score 必须"),
        ("qrels/test.tsv", b"query-id\tcorpus-id\tscore\nquery_1\tclause_1\t5\n", "score 必须"),
        ("qrels/test.tsv", b"query-id\tcorpus-id\tscore\n\xff\n", "不是有效的 UTF-8"),
    ],
)
def test_invalid_record_identifies_file_line_and_reason(
    tmp_path: Path, relative_path: str, replacement: bytes, expected: str
) -> None:
    """格式错误应可定位，不能仅返回笼统失败。"""

    raw, manifest = _dataset(tmp_path)
    (raw / relative_path).write_bytes(replacement)
    report = validate(raw, manifest, verify_hashes=False)
    assert report["valid"] is False
    assert any(expected in reason for reason in _reasons(report))
    assert any(item["file"] == relative_path and item["line"] is not None for item in report["errors"])


def test_changed_bytes_and_unregistered_files_fail_hash_verification(tmp_path: Path) -> None:
    """清单必须覆盖全部原件，修改或新增文件均不能悄悄通过。"""

    raw, manifest = _dataset(tmp_path)
    (raw / "corpus.jsonl").write_text('{"_id":"clause_1","text":"changed"}\n', encoding="utf-8")
    (raw / "notes.txt").write_text("extra", encoding="utf-8")
    report = validate(raw, manifest, verify_hashes=True)
    assert report["valid"] is False
    assert any("SHA-256" in reason for reason in _reasons(report))
    assert any("未登记" in reason for reason in _reasons(report))


def test_manifest_cannot_reference_outside_raw_directory(tmp_path: Path) -> None:
    """清单中的路径穿越必须被拒绝。"""

    raw, manifest = _dataset(tmp_path)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["files"].append({"path": "../outside.txt", "bytes": 0, "sha256": "0" * 64})
    manifest.write_text(json.dumps(data), encoding="utf-8")
    report = validate(raw, manifest, verify_hashes=True)
    assert report["valid"] is False
    assert any("目录内" in reason for reason in _reasons(report))


def test_missing_source_files_fail_instead_of_appearing_empty(tmp_path: Path) -> None:
    """未下载 ACORD 时报告缺失文件，不宣称评测集有效。"""

    raw = tmp_path / "raw"
    raw.mkdir()
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"files": []}', encoding="utf-8")
    report = validate(raw, manifest, verify_hashes=True)
    assert report["valid"] is False
    assert any(item["file"] == "qrels/test.tsv" for item in report["errors"])
