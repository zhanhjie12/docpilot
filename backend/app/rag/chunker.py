"""Page and clause aware chunking for evidence retrieval.

The splitter keeps offsets in the same coordinate system as
``ParsedDocument.text``.  It prefers paragraph and sentence boundaries, then
falls back to a bounded character window with overlap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Iterable


@dataclass(slots=True)
class Chunk:
    """用于检索和引用的文本块，保存原文坐标与章节元数据。"""

    chunk_id: str
    document_id: str
    version_id: str
    page: int | None
    text: str
    char_start: int
    char_end: int
    content_sha256: str
    section_path: str | None = None
    token_estimate: int = 0


_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_HEADING_RE = re.compile(
    r"^(?:第[\u4e00-\u9fff0-9]+[章节条款].*|\d+(?:\.\d+)*[.)、]?\s+\S.*)$"
)


def estimate_tokens(value: str) -> int:
    """Estimate tokens without requiring a model tokenizer dependency.

    CJK characters are counted individually; Latin words and punctuation are
    counted as small token groups.  This is a planning bound, not a provider
    billing count.
    """

    cjk_count = len(_CJK_RE.findall(value))
    latin_part = _CJK_RE.sub(" ", value)
    groups = re.findall(r"\w+|[^\w\s]", latin_part, flags=re.UNICODE)
    return max(1, cjk_count + len(groups)) if value.strip() else 0


def _section_at(text: str, offset: int, inherited: str | None = None) -> str | None:
    """查找指定字符偏移之前最近的标题，作为块的章节路径。"""

    section = inherited
    for match in re.finditer(r"(?m)^([^\n]{1,100})", text):
        if match.start() > offset:
            break
        candidate = match.group(1).strip()
        if _HEADING_RE.match(candidate):
            section = candidate
    return section


def _preferred_boundary(text: str, start: int, end: int) -> int:
    """在预算窗口内选择最近的换行或句末，尽量避免截断句子。"""

    candidates = [
        text.rfind("\n", start, end),
        text.rfind("。", start, end),
        text.rfind("！", start, end),
        text.rfind("？", start, end),
        text.rfind(".", start, end),
    ]
    return max(candidates)


def chunk_text(
    text: str,
    document_id: str,
    version_id: str,
    *,
    max_chars: int = 1200,
    overlap: int = 120,
    page: int | None = 1,
    base_offset: int = 0,
    section_path: str | None = None,
    max_tokens: int | None = None,
) -> list[Chunk]:
    """Split one page or text block while preserving global offsets."""

    # 参数错误会导致死循环或错误引用，因此在进入切分循环前立即拒绝。
    if max_chars <= 0 or overlap < 0 or overlap >= max_chars:
        raise ValueError("overlap must be smaller than max_chars")
    if max_tokens is not None and max_tokens <= 0:
        raise ValueError("max_tokens must be positive")
    chunks: list[Chunk] = []
    start = 0
    counter = 0
    while start < len(text):
        # 先按字符上限取窗口，再根据 token 预算和自然边界缩短窗口。
        end = min(len(text), start + max_chars)
        if max_tokens is not None and end > start:
            # Binary search the largest character boundary that stays inside
            # the approximate token budget.  The fallback keeps progress even
            # when one unbroken token is larger than the budget.
            low, high, best = start + 1, end, start + 1
            while low <= high:
                middle = (low + high) // 2
                if estimate_tokens(text[start:middle]) <= max_tokens:
                    best = middle
                    low = middle + 1
                else:
                    high = middle - 1
            end = max(best, min(len(text), start + overlap + 1))
        if end < len(text):
            boundary = _preferred_boundary(text, start, end)
            if boundary > start + (end - start) // 2 and (
                max_tokens is None or estimate_tokens(text[start : boundary + 1]) <= max_tokens
            ):
                end = boundary + 1
        # 保留 strip 后的内容，同时把去掉的前导空白计入全局坐标。
        raw = text[start:end]
        value = raw.strip()
        if value:
            leading = len(raw) - len(raw.lstrip())
            content_start = base_offset + start + leading
            content_end = content_start + len(value)
            current_section = _section_at(text, start, section_path)
            digest = sha256(value.encode("utf-8")).hexdigest()
            chunks.append(
                Chunk(
                    chunk_id=f"chunk_{counter}",
                    document_id=document_id,
                    version_id=version_id,
                    page=page,
                    text=value,
                    char_start=content_start,
                    char_end=content_end,
                    content_sha256=digest,
                    section_path=current_section,
                    token_estimate=estimate_tokens(value),
                )
            )
            counter += 1
        if end >= len(text):
            break
        # 重叠窗口让跨块的句子仍能被检索到，并确保 start 始终前进。
        start = max(0, end - overlap)
    return chunks


def chunk_pages(
    pages: Iterable[object],
    document_id: str,
    version_id: str,
    *,
    max_chars: int = 1200,
    overlap: int = 120,
    max_tokens: int | None = 320,
) -> list[Chunk]:
    """Chunk parsed pages and keep page numbers plus document-global offsets."""

    # 页面文本按两个换行拼接，页面之间的偏移需要同步累加。
    chunks: list[Chunk] = []
    document_offset = 0
    for page_record in pages:
        page_number = getattr(page_record, "page", None)
        page_text = getattr(page_record, "text", "") or ""
        page_chunks = chunk_text(
            page_text,
            document_id,
            version_id,
            max_chars=max_chars,
            overlap=overlap,
            page=page_number,
            base_offset=document_offset,
            max_tokens=max_tokens,
        )
        for index, page_chunk in enumerate(page_chunks, start=len(chunks)):
            page_chunk.chunk_id = f"chunk_{index}"
        chunks.extend(page_chunks)
        # ParsedDocument.text joins pages with two newline characters.
        document_offset += len(page_text) + 2
    return chunks
