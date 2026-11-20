"""Citation construction and evidence coverage checks."""

from __future__ import annotations

from app.domain.schemas import Citation
from app.rag.chunker import Chunk


def citation_from_chunk(chunk: Chunk, *, section: str | None = None) -> Citation:
    return Citation(citation_id=f"cite_{chunk.chunk_id}", document_id=chunk.document_id, version_id=chunk.version_id, page=chunk.page, section=section, quote=chunk.text, char_start=chunk.char_start, char_end=chunk.char_end)


def evidence_covers(answer: str, citations: list[Citation]) -> bool:
    if not answer.strip() or not citations:
        return False
    corpus = " ".join(citation.quote for citation in citations).lower()
    return all(token.lower() in corpus for token in answer.split() if len(token) > 2)
