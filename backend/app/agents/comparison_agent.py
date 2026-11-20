"""比较两个合同版本，并保留变更两侧可回放的原文位置。"""

from __future__ import annotations

import difflib
from typing import Any

from .state import EvidenceRef, NodeResult, TaskState


MAX_CHANGES = 100
MAX_QUOTE_CHARS = 240


def _lines_and_offsets(text: str) -> tuple[list[str], list[int]]:
    """以全文字符坐标记录每一行的起点。"""

    raw_lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    position = 0
    for line in raw_lines:
        offsets.append(position)
        position += len(line)
    return [line.rstrip("\r\n") for line in raw_lines], offsets


def _page_at(offset: int, pages: list[dict[str, Any]] | None) -> tuple[int | None, str | None]:
    """按解析器的双换行拼接规则，将全文偏移映射到原始页。"""

    if pages is None:
        return None, None
    cursor = 0
    for page in pages:
        page_text = str(page.get("text") or "")
        if cursor <= offset < cursor + len(page_text):
            return int(page["page"]), page.get("section")
        cursor += len(page_text) + 2
    return None, None


def _citation(
    *,
    text: str,
    lines: list[str],
    offsets: list[int],
    line_start: int,
    line_end: int,
    document_id: str,
    version_id: str,
    pages: list[dict[str, Any]] | None,
) -> EvidenceRef | None:
    """取变更的首个非空行；纯新增或纯删除时取相邻条款作为另一侧锚点。"""

    candidate_indices = list(range(line_start, line_end))
    if not candidate_indices and lines:
        candidate_indices = [min(max(line_start - 1, 0), len(lines) - 1)]
    for index in candidate_indices:
        if not 0 <= index < len(lines):
            continue
        value = lines[index]
        leading = len(value) - len(value.lstrip())
        quote = value.strip()[:MAX_QUOTE_CHARS]
        if not quote:
            continue
        char_start = offsets[index] + leading
        char_end = char_start + len(quote)
        if text[char_start:char_end] != quote:
            continue
        page, section = _page_at(char_start, pages)
        if pages is not None and page is None:
            continue
        return EvidenceRef(
            citation_id=f"cite_compare_{version_id}_{char_start}_{char_end}",
            document_id=document_id,
            version_id=version_id,
            page=page,
            section=section,
            quote=quote,
            char_start=char_start,
            char_end=char_end,
        )
    return None


def compare_node(
    state: TaskState,
    base_text: str,
    target_text: str,
    *,
    document_id: str | None = None,
    base_pages: list[dict[str, Any]] | None = None,
    target_pages: list[dict[str, Any]] | None = None,
) -> NodeResult:
    """生成有界事实差异；页面与语义影响由后续验证和人工确认。"""

    if len(state.version_ids) != 2:
        return NodeResult(node="compare", status="waiting_human", error="版本对比必须提供两个 version_id")
    source_document_id = document_id or (state.document_ids[0] if state.document_ids else "unknown")
    base_lines, base_offsets = _lines_and_offsets(base_text)
    target_lines, target_offsets = _lines_and_offsets(target_text)
    matcher = difflib.SequenceMatcher(a=base_lines, b=target_lines, autojunk=False)
    opcodes = [item for item in matcher.get_opcodes() if item[0] != "equal"]
    if len(opcodes) > MAX_CHANGES:
        return NodeResult(
            node="compare",
            status="waiting_human",
            output={"changed": True, "change_count": len(opcodes), "truncated": True},
            error="版本差异超过自动比较上限，需要人工检查",
        )
    changes: list[dict[str, Any]] = []
    citations: list[EvidenceRef] = []
    for index, (tag, base_start, base_end, target_start, target_end) in enumerate(opcodes, start=1):
        base_citation = _citation(
            text=base_text,
            lines=base_lines,
            offsets=base_offsets,
            line_start=base_start,
            line_end=base_end,
            document_id=source_document_id,
            version_id=state.version_ids[0],
            pages=base_pages,
        )
        target_citation = _citation(
            text=target_text,
            lines=target_lines,
            offsets=target_offsets,
            line_start=target_start,
            line_end=target_end,
            document_id=source_document_id,
            version_id=state.version_ids[1],
            pages=target_pages,
        )
        # 一侧空白或页码不可回放时不输出貌似完整的双版本结论。
        if base_pages is not None and target_pages is not None and (
            base_citation is None or target_citation is None
        ):
            return NodeResult(
                node="compare",
                status="waiting_human",
                output={"changed": True, "change_count": len(opcodes)},
                error="某处版本变更缺少可定位的双版本证据",
            )
        citations.extend(item for item in (base_citation, target_citation) if item is not None)
        changes.append(
            {
                "change_id": f"change_{index}",
                "change_type": {"replace": "modified", "insert": "added", "delete": "removed"}[tag],
                "base_citation": base_citation.model_dump(mode="json") if base_citation else None,
                "target_citation": target_citation.model_dump(mode="json") if target_citation else None,
            }
        )
    state.evidence = list({item.citation_id: item for item in citations}.values())
    # 兼容现有直接调用方的 diff 字段，同时限制每行长度，避免保存整份合同。
    diff = [
        line[:MAX_QUOTE_CHARS]
        for line in difflib.unified_diff(
            base_lines,
            target_lines,
            fromfile=state.version_ids[0],
            tofile=state.version_ids[1],
            lineterm="",
        )
    ][: MAX_CHANGES * 4]
    return NodeResult(
        node="compare",
        status="succeeded",
        output={"changed": bool(opcodes), "change_count": len(changes), "changes": changes, "diff": diff},
    )


class ComparisonAgent:
    """版本差异节点适配器。"""

    def run(self, state: TaskState, base_text: str, target_text: str) -> NodeResult:
        return compare_node(state, base_text, target_text)
