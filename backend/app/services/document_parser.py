"""文档解析模块：把 PDF/DOCX 转换成可定位、可引用的结构化内容。"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from statistics import median
from typing import Protocol, runtime_checkable

from app.domain.models import BlockType, BoundingBox, ParsedBlock, ParsedDocument, ParsedPage


class DocumentParseError(ValueError):
    """A stable, user-visible parsing failure."""

    code = "PARSE_FAILED"


@runtime_checkable
class DocumentParser(Protocol):
    """Port implemented by every format-specific document parser."""

    name: str
    supported_mime_types: tuple[str, ...]
    supported_extensions: tuple[str, ...]

    def parse(self, data: bytes, filename: str = "") -> ParsedDocument:
        """Return structured content or raise ``DocumentParseError``."""


@dataclass(slots=True)
class _BlockDraft:
    block_type: BlockType
    text: str
    bbox: BoundingBox | None = None
    heading_level: int | None = None
    table_rows: list[list[str]] = field(default_factory=list)
    source_ref: str | None = None


_NUMBERED_HEADING = re.compile(
    r"^(?:第[\u4e00-\u9fff0-9]+[章节条款]|\d+(?:\.\d+)*[.)、]?\s+\S)"
)


def _package_version(distribution: str) -> str:
    # 记录解析器依赖版本，便于复现同一份解析结果；开发环境缺包时保留 unknown。
    try:
        return version(distribution)
    except PackageNotFoundError:  # pragma: no cover - only unusual vendoring
        return "unknown"


def _assemble_page(
    drafts: list[_BlockDraft],
    *,
    page_number: int,
    document_offset: int,
    width: float | None = None,
    height: float | None = None,
) -> ParsedPage:
    """Build page text and make every block offset replayable."""

    blocks: list[ParsedBlock] = []
    text_parts: list[str] = []
    cursor = document_offset
    for draft in drafts:
        # 空块不进入结果，避免 Citation 指向不可见的空白位置。
        block_text = draft.text.strip()
        if not block_text:
            continue
        if text_parts:
            # 页面内块之间使用两个换行，偏移量必须同步增加两个字符。
            cursor += 2  # Pages and blocks both use a blank-line separator.
        start = cursor
        end = start + len(block_text)
        order = len(blocks)
        blocks.append(
            ParsedBlock(
                block_id=f"p{page_number}-b{order}",
                block_type=draft.block_type,
                text=block_text,
                page=page_number,
                order=order,
                char_start=start,
                char_end=end,
                bbox=draft.bbox,
                heading_level=draft.heading_level,
                table_rows=draft.table_rows,
                source_ref=draft.source_ref,
            )
        )
        text_parts.append(block_text)
        cursor = end

    titles = [block.text for block in blocks if block.block_type == "heading"]
    return ParsedPage(
        page=page_number,
        text="\n\n".join(text_parts),
        section=titles[0] if titles else None,
        blocks=blocks,
        title_candidates=titles,
        width=width,
        height=height,
    )


def _heading_level(font_size: float, body_size: float) -> int:
    # 用正文中位字号作为基准，字号越大越可能是一级或二级标题。
    if body_size <= 0 or font_size >= body_size * 1.55:
        return 1
    if font_size >= body_size * 1.3:
        return 2
    return 3


def _looks_like_pdf_heading(
    text: str, *, font_size: float, body_size: float, bold: bool
) -> bool:
    # 标题判断同时参考字号、编号格式和短粗体，降低普通正文被误判的概率。
    compact = " ".join(text.split())
    if not compact or len(compact) > 160 or text.count("\n") > 2:
        return False
    larger = font_size >= max(body_size + 0.75, body_size * 1.12)
    numbered = bool(_NUMBERED_HEADING.match(compact))
    short_bold = bold and len(compact) <= 100 and not compact.endswith(("。", ".", ";", "；"))
    return larger or numbered or short_bold


class PyMuPDFDocumentParser:
    name = "pymupdf"
    supported_mime_types = ("application/pdf",)
    supported_extensions = (".pdf",)

    def parse(self, data: bytes, filename: str = "") -> ParsedDocument:
        # 延迟导入可选依赖，让不需要 PDF 的测试和命令仍能启动服务。
        try:
            import pymupdf  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise DocumentParseError("PDF parsing requires the 'parsing' extra") from exc

        pages: list[ParsedPage] = []
        empty_pages: list[int] = []
        document_offset = 0
        try:
            with pymupdf.open(stream=data, filetype="pdf") as pdf:
                if pdf.needs_pass:
                    raise DocumentParseError("encrypted PDF files are not supported")
                if pdf.page_count == 0:
                    raise DocumentParseError("the PDF contains no pages")

                for page_index, source_page in enumerate(pdf):
                    page_number = page_index + 1
                    # 使用文本字典保留字号、字体和坐标，后续才能生成标题和 Citation。
                    page_dict = source_page.get_text("dict", sort=True)
                    source_blocks = [
                        block for block in page_dict.get("blocks", []) if block.get("type", 0) == 0
                    ]
                    font_sizes = [
                        float(span.get("size", 0.0))
                        for block in source_blocks
                        for line in block.get("lines", [])
                        for span in line.get("spans", [])
                        if str(span.get("text", "")).strip() and float(span.get("size", 0.0)) > 0
                    ]
                    # 中位字号比平均值更不容易被少量大标题影响。
                    body_size = median(font_sizes) if font_sizes else 0.0
                    drafts: list[_BlockDraft] = []
                    for source_index, block in enumerate(source_blocks):
                        line_texts: list[str] = []
                        block_sizes: list[float] = []
                        bold = False
                        for line in block.get("lines", []):
                            spans = line.get("spans", [])
                            line_text = "".join(str(span.get("text", "")) for span in spans).strip()
                            if line_text:
                                line_texts.append(line_text)
                            for span in spans:
                                if str(span.get("text", "")).strip():
                                    block_sizes.append(float(span.get("size", 0.0)))
                                    font_name = str(span.get("font", "")).lower()
                                    bold = bold or "bold" in font_name
                        block_text = "\n".join(line_texts).strip()
                        if not block_text:
                            continue
                        # 一个 PDF 文本块可能包含多行；这里合并为一个可检索的逻辑块。
                        font_size = max(block_sizes, default=body_size)
                        is_heading = _looks_like_pdf_heading(
                            block_text,
                            font_size=font_size,
                            body_size=body_size,
                            bold=bold,
                        )
                        raw_bbox = block.get("bbox")
                        bbox = (
                            tuple(float(value) for value in raw_bbox[:4])
                            if isinstance(raw_bbox, (list, tuple)) and len(raw_bbox) >= 4
                            else None
                        )
                        drafts.append(
                            _BlockDraft(
                                block_type="heading" if is_heading else "paragraph",
                                text=block_text,
                                bbox=bbox,  # type: ignore[arg-type]
                                heading_level=_heading_level(font_size, body_size) if is_heading else None,
                                source_ref=f"pdf:page={page_number}:block={source_index}",
                            )
                        )

                    parsed_page = _assemble_page(
                        drafts,
                        page_number=page_number,
                        document_offset=document_offset,
                        width=float(source_page.rect.width),
                        height=float(source_page.rect.height),
                    )
                    # 记录空页告警，但保留页码，保证原文位置仍然连续。
                    if not parsed_page.text:
                        empty_pages.append(page_number)
                    pages.append(parsed_page)
                    document_offset += len(parsed_page.text) + 2
        except DocumentParseError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize third-party failures
            raise DocumentParseError("the PDF could not be parsed") from exc

        # 整份文档没有可选文本时，明确提示需要 OCR，而不是返回空索引。
        if not any(page.text.strip() for page in pages):
            raise DocumentParseError("the PDF contains no selectable text; OCR is required")
        warnings = []
        if empty_pages:
            page_list = ", ".join(str(page) for page in empty_pages)
            warnings.append(f"pages without selectable text: {page_list}")
        parsed = ParsedDocument(
            pages=pages,
            parser_name=self.name,
            parser_version=_package_version("PyMuPDF"),
            quality_warnings=warnings,
        )
        parsed.validate_locations()
        return parsed


def _docx_heading_level(paragraph: object, style_name: str) -> int | None:
    # DOCX 优先使用 Heading/标题样式，再读取 OOXML 的 outlineLvl 兜底。
    style_match = re.match(r"^(?:Heading|标题)\s*(\d+)", style_name, flags=re.IGNORECASE)
    if style_match:
        return max(1, int(style_match.group(1)))
    if style_name.lower() in {"title", "subtitle"} or style_name in {"题目", "副标题"}:
        return 1
    paragraph_xml = getattr(paragraph, "_p", None)
    properties = getattr(paragraph_xml, "pPr", None)
    outline = getattr(properties, "outlineLvl", None)
    value = getattr(outline, "val", None)
    if value is not None:
        try:
            return int(value) + 1
        except (TypeError, ValueError):
            return None
    return None


class PythonDocxDocumentParser:
    name = "python-docx"
    supported_mime_types = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    supported_extensions = (".docx",)

    def parse(self, data: bytes, filename: str = "") -> ParsedDocument:
        # python-docx 只在真正解析 DOCX 时导入，避免启动阶段强制依赖它。
        try:
            from docx import Document  # type: ignore
            from docx.oxml.table import CT_Tbl  # type: ignore
            from docx.oxml.text.paragraph import CT_P  # type: ignore
            from docx.table import Table  # type: ignore
            from docx.text.paragraph import Paragraph  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise DocumentParseError("DOCX parsing requires the 'parsing' extra") from exc

        try:
            document = Document(io.BytesIO(data))
            drafts: list[_BlockDraft] = []
            for body_index, child in enumerate(document.element.body.iterchildren()):
                source_ref = f"docx:body[{body_index}]"
                if isinstance(child, CT_P):
                    # 段落保留样式层级，标题会成为后续 Chunk 的 section_path。
                    paragraph = Paragraph(child, document)
                    paragraph_text = paragraph.text.strip()
                    if not paragraph_text:
                        continue
                    style_name = paragraph.style.name if paragraph.style is not None else ""
                    heading_level = _docx_heading_level(paragraph, style_name)
                    drafts.append(
                        _BlockDraft(
                            block_type="heading" if heading_level is not None else "paragraph",
                            text=paragraph_text,
                            heading_level=heading_level,
                            source_ref=source_ref,
                        )
                    )
                elif isinstance(child, CT_Tbl):
                    # 表格同时保存二维单元格和可检索的扁平文本。
                    table = Table(child, document)
                    rows = [
                        [cell.text.strip() for cell in row.cells]
                        for row in table.rows
                    ]
                    rows = [row for row in rows if any(cell for cell in row)]
                    if not rows:
                        continue
                    table_text = "\n".join(" | ".join(row) for row in rows)
                    drafts.append(
                        _BlockDraft(
                            block_type="table",
                            text=table_text,
                            table_rows=rows,
                            source_ref=source_ref,
                        )
                    )
        except Exception as exc:  # noqa: BLE001 - normalize third-party failures
            raise DocumentParseError("the DOCX could not be parsed") from exc

        if not drafts:
            raise DocumentParseError("the DOCX contains no extractable text")
        page = _assemble_page(drafts, page_number=1, document_offset=0)
        parsed = ParsedDocument(
            pages=[page],
            parser_name=self.name,
            parser_version=_package_version("python-docx"),
            quality_warnings=[
                "DOCX pagination and coordinates are unavailable; content uses one logical page"
            ],
        )
        parsed.validate_locations()
        return parsed


PARSERS: tuple[DocumentParser, ...] = (
    PyMuPDFDocumentParser(),
    PythonDocxDocumentParser(),
)


def parse_document(source: bytes | Path, mime_type: str, filename: str = "") -> ParsedDocument:
    """Select a format parser and reject empty or unsupported input."""

    # 允许传入 Path 方便后台任务读取文件，也允许传入 bytes 方便 API 和测试复用。
    if isinstance(source, Path):
        data = source.read_bytes()
        filename = filename or source.name
    else:
        data = source
    if not data:
        raise DocumentParseError("document is empty")
    suffix = Path(filename).suffix.lower()
    for parser in PARSERS:
        # MIME 用于准确识别，扩展名用于补充某些上传客户端缺失 MIME 的情况。
        if mime_type in parser.supported_mime_types or suffix in parser.supported_extensions:
            return parser.parse(data, filename)
    raise DocumentParseError("only PDF and DOCX files are supported")
