"""解析质量检查节点。

解析本身仍由 ``DocumentParser`` 负责；本节点只读取解析结果的质量信息，避免
Agent 重复实现 PDF/DOCX 处理逻辑。
"""

from __future__ import annotations

from typing import Any

from .state import NodeResult, TaskState


def parser_quality_node(state: TaskState, parsed: dict[str, Any]) -> NodeResult:
    """检查页数、质量告警和空文本，异常时进入人工确认。"""

    page_count = parsed.get("page_count")
    pages = parsed.get("pages") or []
    warnings = parsed.get("quality_warnings") or []
    if (
        isinstance(page_count, bool)
        or not isinstance(page_count, int)
        or page_count <= 0
        or not isinstance(pages, list)
        or len(pages) != page_count
    ):
        return NodeResult(node="parse", status="waiting_human", error="解析页数与页面结果不一致")
    # 页码必须连续且可回放，异常页不能被后续引用伪装成有效证据。
    if any(
        not isinstance(page, dict) or page.get("page") not in (None, index)
        for index, page in enumerate(pages, start=1)
    ):
        return NodeResult(node="parse", status="waiting_human", error="解析页码不连续")
    empty_pages = [
        index for index, page in enumerate(pages, start=1) if not str(page.get("text") or "").strip()
    ]
    if len(empty_pages) == page_count:
        return NodeResult(node="parse", status="waiting_human", error="解析结果没有可用文本")
    if empty_pages:
        return NodeResult(
            node="parse",
            status="waiting_human",
            output={"page_count": page_count, "empty_pages": empty_pages, "warning_count": len(warnings)},
            error="部分页面没有可用文本，需要检查是否为扫描页",
        )
    return NodeResult(
        node="parse", status="succeeded", output={"page_count": page_count, "empty_pages": [], "warning_count": len(warnings)}
    )


class ParserAgent:
    """解析器结果质量检查适配器。"""

    def run(self, state: TaskState, parsed: dict[str, Any]) -> NodeResult:
        return parser_quality_node(state, parsed)
