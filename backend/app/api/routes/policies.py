"""Version-aware, owner-scoped policy metadata and evidence question answering."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_database, get_embedding_provider, get_llm_provider, get_request_user
from app.core.exceptions import AppError
from app.domain.schemas import PolicyAskRequest, PolicyMetadataRequest
from app.infrastructure.database import SQLiteDatabase, utc_now
from app.infrastructure.providers import EmbeddingProvider, LLMProvider
from app.rag.citations import citation_from_chunk
from app.rag.search import search_chunks
from app.services.evidence_answer import select_answer_spans


router = APIRouter()


@router.put("/versions/{version_id}/metadata", summary="确认制度版本适用信息")
def set_policy_metadata(
    version_id: str,
    payload: PolicyMetadataRequest,
    db: SQLiteDatabase = Depends(get_database),
    current_user: dict[str, object] | None = Depends(get_request_user),
):
    """Do not infer policy authority or dates from a filename or model output."""

    owner_id = str(current_user["user_id"]) if current_user else None
    version = db.one(
        "SELECT v.version_id FROM document_versions v JOIN documents d ON d.document_id=v.document_id "
        "WHERE v.version_id=? AND d.owner_id IS ? AND d.document_type='policy'",
        (version_id, owner_id),
    )
    if version is None:
        raise AppError(code="POLICY_VERSION_NOT_FOUND", message="制度版本不存在", status_code=404)
    db.run(
        "INSERT INTO policy_versions(version_id,policy_family_id,issuer,org_scope,effective_from,effective_to,status,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(version_id) DO UPDATE SET "
        "policy_family_id=excluded.policy_family_id,issuer=excluded.issuer,org_scope=excluded.org_scope,"
        "effective_from=excluded.effective_from,effective_to=excluded.effective_to,"
        "status=excluded.status,updated_at=excluded.updated_at",
        (
            version_id, payload.policy_family_id, payload.issuer, payload.org_scope,
            payload.effective_from.isoformat(),
            payload.effective_to.isoformat() if payload.effective_to else None,
            payload.status, utc_now(),
        ),
    )
    return {"version_id": version_id, **payload.model_dump(mode="json")}


@router.post("/ask", summary="限定有效版本的制度问答")
async def ask_policy(
    payload: PolicyAskRequest,
    db: SQLiteDatabase = Depends(get_database),
    embedding_provider: EmbeddingProvider | None = Depends(get_embedding_provider),
    llm_provider: LLMProvider = Depends(get_llm_provider),
    current_user: dict[str, object] | None = Depends(get_request_user),
):
    """Select effective versions before retrieval; surface overlaps for review."""

    owner_id = str(current_user["user_id"]) if current_user else None
    placeholders = ",".join("?" for _ in payload.document_ids)
    rows = db.all(
        "SELECT d.document_id,d.document_type,v.version_id,v.status AS version_status,"
        "p.policy_family_id,p.issuer,p.org_scope,p.effective_from,p.effective_to,p.status AS policy_status "
        "FROM documents d JOIN document_versions v ON v.document_id=d.document_id "
        "LEFT JOIN policy_versions p ON p.version_id=v.version_id "
        f"WHERE d.document_id IN ({placeholders}) AND d.owner_id IS ?",
        (*payload.document_ids, owner_id),
    )
    found = {row["document_id"] for row in rows}
    if found != set(payload.document_ids) or any(row["document_type"] != "policy" for row in rows):
        raise AppError(code="POLICY_SCOPE_INVALID", message="查询范围包含无权或非制度文档", status_code=404)
    as_of = payload.as_of_date.isoformat()
    applicable = [
        row for row in rows
        if row["version_status"] == "indexed" and row["policy_status"] == "active"
        and row["org_scope"] in {payload.org_scope, "*"}
        and row["effective_from"] <= as_of
        and (row["effective_to"] is None or as_of < row["effective_to"])
    ]
    families: dict[str, list[dict]] = {}
    for row in applicable:
        families.setdefault(row["policy_family_id"], []).append(row)
    if any(len(versions) > 1 for versions in families.values()):
        raise AppError(code="POLICY_VERSION_CONFLICT", message="存在同时适用的制度版本，请人工确认", status_code=409)
    if not applicable:
        return {"answer": None, "status": "insufficient_evidence", "needs_confirmation": True,
                "reason": "没有已确认且在指定日期生效的制度版本", "citations": [], "policy_versions": []}
    version_ids = [row["version_id"] for row in applicable]
    results = await run_in_threadpool(
        search_chunks, db, payload.question, version_ids=version_ids,
        top_k=payload.top_k, embedding_provider=embedding_provider,
    )
    citations = [citation_from_chunk(item.chunk, section=item.chunk.section_path).model_dump() for item in results]
    spans = await run_in_threadpool(select_answer_spans, payload.question, citations, llm_provider)
    answer = (
        "根据原文：" + "；".join(f"{span['quote_span']} [{span['citation_id']}]" for span in spans)
        if spans else "\n".join(f"{item['quote']} [{item['citation_id']}]" for item in citations[:2]) or None
    )
    return {
        "answer": answer, "status": "answered" if answer else "insufficient_evidence",
        "needs_confirmation": not bool(answer), "citations": citations,
        "policy_versions": [
            {key: row[key] for key in ("version_id", "policy_family_id", "issuer", "org_scope", "effective_from", "effective_to")}
            for row in applicable
        ],
    }
