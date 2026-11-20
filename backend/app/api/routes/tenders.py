"""Evidence alignment for tender requirements and bid responses."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_database, get_embedding_provider, get_request_user
from app.core.exceptions import AppError
from app.domain.schemas import TenderCheckRequest
from app.infrastructure.database import SQLiteDatabase
from app.infrastructure.providers import EmbeddingProvider
from app.rag.citations import citation_from_chunk
from app.rag.search import search_chunks
from app.rag.search import load_scoped_chunks


router = APIRouter()


@router.get("/versions/{version_id}/requirements", summary="提取招标要求候选清单")
def list_tender_requirements(
    version_id: str,
    db: SQLiteDatabase = Depends(get_database),
    current_user: dict[str, object] | None = Depends(get_request_user),
):
    """Extract requirement sentences while retaining a replayable chunk citation."""

    owner_id = str(current_user["user_id"]) if current_user else None
    row = db.one(
        "SELECT v.status,d.document_type FROM document_versions v "
        "JOIN documents d ON d.document_id=v.document_id "
        "WHERE v.version_id=? AND d.owner_id IS ?",
        (version_id, owner_id),
    )
    if row is None:
        raise AppError(code="TENDER_VERSION_NOT_FOUND", message="招标文件版本不存在", status_code=404)
    if row["document_type"] != "tender" or row["status"] != "indexed":
        raise AppError(code="TENDER_SCOPE_INVALID", message="需要已索引的招标文件", status_code=409)
    items: list[dict] = []
    seen: set[str] = set()
    for chunk in load_scoped_chunks(db, version_ids=[version_id]):
        citation = citation_from_chunk(chunk, section=chunk.section_path).model_dump()
        for sentence in re.split(r"(?<=[。；;\n])", chunk.text):
            query = sentence.strip()
            if not 8 <= len(query) <= 220 or not re.search(
                r"须|应当|必须|不得|提供|提交|交付|资格|业绩|保证金|截止", query
            ) or query in seen:
                continue
            seen.add(query)
            items.append({"requirement_query": query, "citation": citation,
                          "status": "needs_review"})
            if len(items) >= 50:
                break
        if len(items) >= 50:
            break
    return {"version_id": version_id, "items": items, "needs_confirmation": True,
            "truncated": len(items) >= 50}


@router.post("/check", summary="逐条对齐招标要求与投标证据")
async def check_tender(
    payload: TenderCheckRequest,
    db: SQLiteDatabase = Depends(get_database),
    embedding_provider: EmbeddingProvider | None = Depends(get_embedding_provider),
    current_user: dict[str, object] | None = Depends(get_request_user),
):
    """Return source evidence pairs and leave semantic compliance to a reviewer."""

    owner_id = str(current_user["user_id"]) if current_user else None
    rows = db.all(
        "SELECT v.version_id,v.status,d.document_type FROM document_versions v "
        "JOIN documents d ON d.document_id=v.document_id "
        "WHERE v.version_id IN (?,?) AND d.owner_id IS ?",
        (payload.tender_version_id, payload.bid_version_id, owner_id),
    )
    versions = {row["version_id"]: row for row in rows}
    for version_id, required_type in (
        (payload.tender_version_id, "tender"),
        (payload.bid_version_id, "bid"),
    ):
        row = versions.get(version_id)
        if row is None:
            raise AppError(code="TENDER_VERSION_NOT_FOUND", message="文件版本不存在", status_code=404)
        if row["document_type"] != required_type or row["status"] != "indexed":
            raise AppError(code="TENDER_SCOPE_INVALID", message="文件类型或索引状态不符合要求", status_code=409)

    items = []
    for query in payload.requirement_queries:
        # Search each side separately so a bid cannot masquerade as a tender source.
        tender_hits = await run_in_threadpool(
            search_chunks, db, query, version_ids=[payload.tender_version_id],
            top_k=payload.top_k, embedding_provider=embedding_provider,
        )
        bid_hits = await run_in_threadpool(
            search_chunks, db, query, version_ids=[payload.bid_version_id],
            top_k=payload.top_k, embedding_provider=embedding_provider,
        )
        tender_citations = [
            citation_from_chunk(hit.chunk, section=hit.chunk.section_path).model_dump()
            for hit in tender_hits
        ]
        bid_citations = [
            citation_from_chunk(hit.chunk, section=hit.chunk.section_path).model_dump()
            for hit in bid_hits
        ]
        if not tender_citations:
            status = "requirement_unverified"
        elif not bid_citations:
            status = "bid_evidence_missing"
        else:
            status = "needs_review"
        items.append({
            "requirement_query": query,
            "status": status,
            "tender_citations": tender_citations,
            "bid_citations": bid_citations,
        })
    return {
        "tender_version_id": payload.tender_version_id,
        "bid_version_id": payload.bid_version_id,
        "items": items,
        "needs_confirmation": True,
    }
