"""只读校验 ACORD 的 BEIR 数据、文件清单和原件哈希。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


REQUIRED_FILES = ("corpus.jsonl", "queries.jsonl", "qrels/test.tsv")
QRELS_HEADERS = (
    ("query-id", "corpus-id", "score"),
    ("query_id", "corpus_id", "score"),
)


def _error(
    errors: list[dict[str, Any]],
    file: str,
    reason: str,
    *,
    line: int | None = None,
    record_id: str | None = None,
) -> None:
    """保存可定位错误，不把合同原文写进校验报告。"""

    errors.append({"file": file, "line": line, "record_id": record_id, "reason": reason})


def _relative_file(raw_dir: Path, value: str) -> Path | None:
    """仅允许清单引用 raw_dir 内的相对文件。"""

    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts or ":" in value:
        return None
    resolved = (raw_dir / path).resolve()
    return resolved if resolved.is_relative_to(raw_dir.resolve()) else None


def _sha256(path: Path) -> str:
    """分块计算哈希，避免把完整公开语料读入内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_manifest(path: Path, errors: list[dict[str, Any]]) -> dict[str, Any]:
    """读取来源清单；数据尚未下载时允许初始 files 为空。"""

    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _error(errors, str(path), f"无法读取 manifest：{type(exc).__name__}")
        return {}
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        _error(errors, str(path), "manifest 必须是对象且 files 必须是数组")
        return {}
    return manifest


def _scan_jsonl(path: Path, label: str, errors: list[dict[str, Any]]) -> set[str]:
    """逐行检查 BEIR 的 ID 和文本，严格拒绝非法 UTF-8。"""

    found: set[str] = set()
    relative = path.name
    try:
        with path.open("rb") as stream:
            for line_number, raw_line in enumerate(stream, 1):
                try:
                    line = raw_line.decode("utf-8")
                except UnicodeDecodeError:
                    _error(errors, relative, "不是有效的 UTF-8", line=line_number)
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    _error(errors, relative, "无效 JSONL 记录", line=line_number)
                    continue
                if not isinstance(item, dict):
                    _error(errors, relative, "记录必须是 JSON 对象", line=line_number)
                    continue
                record_id = item.get("_id")
                if not isinstance(record_id, str) or not record_id.strip():
                    _error(errors, relative, "_id 必须是非空字符串", line=line_number)
                    continue
                if record_id in found:
                    _error(errors, relative, "重复 _id", line=line_number, record_id=record_id)
                found.add(record_id)
                value = item.get("text")
                if not isinstance(value, str) or not value.strip():
                    _error(errors, relative, f"{label} 的 text 不能为空", line=line_number, record_id=record_id)
    except OSError as exc:
        _error(errors, relative, f"无法读取文件：{type(exc).__name__}")
    if not found:
        _error(errors, relative, "文件中没有有效记录")
    return found


def _scan_qrels(
    path: Path,
    raw_dir: Path,
    queries: set[str],
    corpus: set[str],
    errors: list[dict[str, Any]],
) -> int:
    """验证每个相关度标签能回连到当前语料与问题。"""

    relative = path.relative_to(raw_dir).as_posix()
    count = 0
    pairs: set[tuple[str, str]] = set()
    try:
        # BEIR qrels 每行恰有三列；逐行解码便于精确定位非法 UTF-8。
        with path.open("rb") as stream:
            for line_number, raw_line in enumerate(stream, 1):
                try:
                    line = raw_line.decode("utf-8")
                except UnicodeDecodeError:
                    _error(errors, relative, "不是有效的 UTF-8", line=line_number)
                    continue
                row = next(csv.reader([line], delimiter="\t"))
                if line_number == 1:
                    if tuple(row) not in QRELS_HEADERS:
                        _error(errors, relative, "qrels 表头应为 query-id、corpus-id、score", line=1)
                        return 0
                    continue
                if len(row) != 3:
                    _error(errors, relative, "qrels 记录必须恰好有三列", line=line_number)
                    continue
                query_id, corpus_id, raw_score = row
                record_id = f"{query_id}/{corpus_id}"
                count += 1
                if not query_id or not corpus_id:
                    _error(errors, relative, "query-id 和 corpus-id 不能为空", line=line_number, record_id=record_id)
                if query_id not in queries:
                    _error(errors, relative, "query-id 未在 queries.jsonl 中出现", line=line_number, record_id=record_id)
                if corpus_id not in corpus:
                    _error(errors, relative, "corpus-id 未在 corpus.jsonl 中出现", line=line_number, record_id=record_id)
                pair = (query_id, corpus_id)
                if pair in pairs:
                    _error(errors, relative, "重复的 query-id/corpus-id 关联", line=line_number, record_id=record_id)
                pairs.add(pair)
                if not raw_score.isascii() or not raw_score.isdecimal() or int(raw_score) > 4:
                    _error(errors, relative, "score 必须是 0–4 的整数", line=line_number, record_id=record_id)
    except OSError as exc:
        _error(errors, relative, f"无法读取文件：{type(exc).__name__}")
    if count == 0:
        _error(errors, relative, "qrels 中没有有效记录")
    return count


def validate(raw_dir: Path, manifest_path: Path, *, verify_hashes: bool) -> dict[str, Any]:
    """完成结构和可选哈希校验，返回不含原文的报告。"""

    errors: list[dict[str, Any]] = []
    raw_dir = raw_dir.resolve()
    manifest = _load_manifest(manifest_path, errors)
    if not raw_dir.is_dir():
        _error(errors, str(raw_dir), "原始数据目录不存在")
    for relative in REQUIRED_FILES:
        if not (raw_dir / relative).is_file():
            _error(errors, relative, "必需文件不存在")

    # 清单覆盖所有实际数据文件，防止只固定一部分原件的哈希。
    data_files = sorted(
        path for path in raw_dir.rglob("*")
        if path.is_file() and path.name != ".gitkeep" and not path.is_symlink()
    ) if raw_dir.is_dir() else []
    files = [
        {"path": path.relative_to(raw_dir).as_posix(), "bytes": path.stat().st_size, "sha256": _sha256(path)}
        for path in data_files
    ]
    if verify_hashes:
        expected: dict[str, dict[str, Any]] = {}
        for item in manifest.get("files", []):
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                _error(errors, str(manifest_path), "files 条目缺少有效 path")
                continue
            relative = item["path"].replace("\\", "/")
            if _relative_file(raw_dir, relative) is None:
                _error(errors, str(manifest_path), "files.path 必须位于原始数据目录内", record_id=relative)
                continue
            if relative in expected:
                _error(errors, str(manifest_path), "manifest 中的文件路径重复", record_id=relative)
            expected[relative] = item
        actual = {item["path"]: item for item in files}
        for relative in sorted(actual.keys() | expected.keys()):
            if relative not in expected:
                _error(errors, relative, "文件未登记在 manifest 中")
            elif relative not in actual:
                _error(errors, relative, "manifest 中的文件不存在")
            else:
                observed, pinned = actual[relative], expected[relative]
                if pinned.get("bytes") != observed["bytes"]:
                    _error(errors, relative, "文件大小与 manifest 不一致")
                if pinned.get("sha256") != observed["sha256"]:
                    _error(errors, relative, "SHA-256 与 manifest 不一致")

    corpus = _scan_jsonl(raw_dir / "corpus.jsonl", "corpus", errors) if (raw_dir / "corpus.jsonl").is_file() else set()
    queries = _scan_jsonl(raw_dir / "queries.jsonl", "queries", errors) if (raw_dir / "queries.jsonl").is_file() else set()
    qrels_dir = raw_dir / "qrels"
    qrels_paths = sorted(qrels_dir.glob("*.tsv")) if qrels_dir.is_dir() else []
    qrels_counts = {
        path.name: _scan_qrels(path, raw_dir, queries, corpus, errors)
        for path in qrels_paths
    }
    return {
        "valid": not errors,
        "summary": {
            "corpus_count": len(corpus),
            "query_count": len(queries),
            "qrels_count": sum(qrels_counts.values()),
            "qrels_by_split": qrels_counts,
            "file_count": len(files),
            "error_count": len(errors),
        },
        "files": files,
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    """提供可在 PowerShell 或 CI 中直接运行的校验命令。"""

    parser = argparse.ArgumentParser(description="校验 ACORD 原件、BEIR 结构和文件哈希")
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--print-hashes", action="store_true")
    mode.add_argument("--verify-hashes", action="store_true")
    args = parser.parse_args(argv)
    raw_dir = args.raw_dir.resolve()
    if args.report is not None and args.report.resolve().is_relative_to(raw_dir):
        parser.error("报告不能写入原始数据目录")
    report = validate(raw_dir, args.manifest, verify_hashes=args.verify_hashes)
    if args.print_hashes:
        print(json.dumps(report["files"], ensure_ascii=False, indent=2))
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"valid": report["valid"], **report["summary"]}, ensure_ascii=False), file=sys.stderr)
    for item in report["errors"]:
        print(f"{item['file']}:{item['line'] or '-'} [{item['record_id'] or '-'}] {item['reason']}", file=sys.stderr)
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
