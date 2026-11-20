"""Domain records for structured, evidence-preserving document parsing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypeAlias


BlockType: TypeAlias = Literal["heading", "paragraph", "table"]
BoundingBox: TypeAlias = tuple[float, float, float, float]


@dataclass(slots=True)
class ParsedBlock:
    """One source block with a stable location in ``ParsedDocument.text``.

    ``char_start`` and ``char_end`` are document-global offsets. PDF blocks
    additionally carry a page coordinate box; DOCX blocks use ``source_ref``
    to identify their position in the Word document body.
    """

    block_id: str
    block_type: BlockType
    text: str
    page: int
    order: int
    char_start: int
    char_end: int
    bbox: BoundingBox | None = None
    heading_level: int | None = None
    table_rows: list[list[str]] = field(default_factory=list)
    source_ref: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "block_id": self.block_id,
            "block_type": self.block_type,
            "text": self.text,
            "page": self.page,
            "order": self.order,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "bbox": list(self.bbox) if self.bbox is not None else None,
            "heading_level": self.heading_level,
            "table_rows": self.table_rows,
            "source_ref": self.source_ref,
        }


@dataclass(slots=True)
class ParsedPage:
    page: int
    text: str
    section: str | None = None
    blocks: list[ParsedBlock] = field(default_factory=list)
    title_candidates: list[str] = field(default_factory=list)
    width: float | None = None
    height: float | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "page": self.page,
            "text": self.text,
            "section": self.section,
            "blocks": [block.to_dict() for block in self.blocks],
            "title_candidates": self.title_candidates,
            "width": self.width,
            "height": self.height,
        }


@dataclass(slots=True)
class ParsedDocument:
    pages: list[ParsedPage] = field(default_factory=list)
    parser_name: str = "builtin"
    parser_version: str = "1"
    quality_warnings: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        # Empty pages remain in the join so every block and chunk uses the
        # same document-global coordinate system.
        return "\n\n".join(page.text for page in self.pages)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def validate_locations(self) -> None:
        """Fail fast if a parser produced evidence offsets that cannot replay."""

        full_text = self.text
        for page in self.pages:
            if page.page < 1:
                raise ValueError("page numbers must start at 1")
            for block in page.blocks:
                if block.page != page.page:
                    raise ValueError(f"block {block.block_id} points to the wrong page")
                if block.char_start < 0 or block.char_end <= block.char_start:
                    raise ValueError(f"block {block.block_id} has invalid character offsets")
                if full_text[block.char_start:block.char_end] != block.text:
                    raise ValueError(f"block {block.block_id} cannot be located in parsed text")

    def to_dict(self) -> dict[str, object]:
        return {
            "pages": [page.to_dict() for page in self.pages],
            "page_count": self.page_count,
            "parser_name": self.parser_name,
            "parser_version": self.parser_version,
            "quality_warnings": self.quality_warnings,
        }
