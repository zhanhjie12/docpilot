"""基于词法和向量检索的证据型文档问答。

路由返回带稳定引用的原文片段。后续 Agent 或 LLM 生成答案时可以复用同一组
证据，无需改变检索接口。
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, Request, status
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_database, get_embedding_provider, get_llm_provider, get_request_user
from app.core.exceptions import AppError
from app.domain.schemas import ChatAskRequest, ChatSessionCreateRequest
from app.infrastructure.database import SQLiteDatabase, json_dumps, json_loads, utc_now
from app.infrastructure.providers import EmbeddingProvider, LLMProvider
from app.rag.citations import citation_from_chunk
from app.rag.retriever import RetrievedChunk
from app.rag.search import search_chunks
from app.services.evidence_answer import select_answer_spans


router = APIRouter()


def _select_answer_evidence(
    results: list[RetrievedChunk],
    *,
    min_vector_score: float,
) -> list[RetrievedChunk]:
    """Keep a small, coherent evidence set instead of concatenating every hit.

    Lexical overlap is the strongest explainable signal.  When the question
    and evidence use different wording, retain only vector hits close to the
    best semantic match.  Search order remains stable and at most three source
    chunks are exposed as the evidence-only answer.
    """

    # 没有候选片段时直接返回空集合，让调用方显示“证据不足”。
    if not results:
        return []
    best_lexical = max(item.lexical_score for item in results)
    best_vector = max(item.vector_score for item in results)

    # 有词法命中时优先保留可解释的重叠结果。
    if best_lexical > 0:
        selected = [
            item
            for item in results
            if item.lexical_score >= best_lexical * 0.5
        ]
    # 没有词法命中时，只保留接近最佳语义分数的结果，避免低相关片段污染回答。
    elif best_vector >= min_vector_score:
        relative_threshold = max(min_vector_score, best_vector * 0.9)
        selected = [
            item
            for item in results
            if item.vector_score >= relative_threshold
        ]
    else:
        selected = []
    # 限制证据数量，保证响应短且每条引用都能在页面上展开查看。
    return selected[:3]


def _require_session_scope(
    db: SQLiteDatabase,
    session: dict,
    payload: ChatAskRequest,
) -> None:
    """阻止查询扩大会话创建时固定的文档范围。"""

    # 先验证文档 ID，再验证版本 ID；任何超出会话范围的请求都拒绝。
    requested_documents = set(payload.document_ids or [])
    requested_versions = set(payload.version_ids or [])
    allowed_documents = set(json_loads(session["document_ids"], []))
    allowed_versions = set(json_loads(session["version_ids"], []))

    if not requested_documents.issubset(allowed_documents):
        raise AppError(
            code="DOCUMENT_SCOPE_VIOLATION",
            message="问题超出会话文档范围",
            status_code=403,
        )
    if not requested_versions:
        return

    placeholders = ",".join("?" for _ in requested_versions)
    rows = db.all(
        "SELECT version_id,document_id FROM document_versions "
        f"WHERE version_id IN ({placeholders})",
        tuple(requested_versions),
    )
    version_documents = {row["version_id"]: row["document_id"] for row in rows}
    if set(version_documents) != requested_versions or any(
        version_id not in allowed_versions
        and document_id not in allowed_documents
        for version_id, document_id in version_documents.items()
    ):
        raise AppError(
            code="DOCUMENT_SCOPE_VIOLATION",
            message="问题超出会话文档范围",
            status_code=403,
        )


@router.post(
    "/sessions",
    status_code=status.HTTP_201_CREATED,
    summary="创建文档问答会话",
)
async def create_chat_session(
    payload: ChatSessionCreateRequest,
    db: SQLiteDatabase = Depends(get_database),
    current_user: dict[str, object] | None = Depends(get_request_user),
):
    """保存会话范围，后续问答只能在该范围内检索。"""

    owner_id = str(current_user["user_id"]) if current_user else None
    if payload.document_ids:
        placeholders = ",".join("?" for _ in payload.document_ids)
        owned = db.all(
            f"SELECT document_id FROM documents WHERE document_id IN ({placeholders}) AND owner_id IS ?",
            (*payload.document_ids, owner_id),
        )
        if len(owned) != len(payload.document_ids):
            raise AppError(code="DOCUMENT_NOT_FOUND", message="问答范围包含不存在的文档", status_code=404)
    if payload.version_ids:
        placeholders = ",".join("?" for _ in payload.version_ids)
        owned_versions = db.all(
            f"SELECT v.version_id FROM document_versions v JOIN documents d ON d.document_id=v.document_id WHERE v.version_id IN ({placeholders}) AND d.owner_id IS ?",
            (*payload.version_ids, owner_id),
        )
        if len(owned_versions) != len(payload.version_ids):
            raise AppError(code="DOCUMENT_NOT_FOUND", message="问答范围包含不存在的文档版本", status_code=404)
    session_id = f"chat_{uuid4().hex}"
    created_at = utc_now()
    db.run(
        "INSERT INTO chat_sessions("
        "session_id,owner_id,document_ids,version_ids,created_at"
        ") VALUES(?,?,?,?,?)",
        (
            session_id,
            owner_id,
            json_dumps(payload.document_ids or []),
            json_dumps(payload.version_ids or []),
            created_at,
        ),
    )
    return {
        "session_id": session_id,
        "document_ids": payload.document_ids or [],
        "version_ids": payload.version_ids or [],
        "created_at": created_at,
    }


@router.post("/{session_id}/ask", summary="提交文档问题")
async def ask_question(
    session_id: str,
    payload: ChatAskRequest,
    request: Request,
    db: SQLiteDatabase = Depends(get_database),
    embedding_provider: EmbeddingProvider | None = Depends(get_embedding_provider),
    llm_provider: LLMProvider = Depends(get_llm_provider),
    current_user: dict[str, object] | None = Depends(get_request_user),
):
    """检索相关片段、保存问答消息，并返回可追溯引用。"""

    owner_id = str(current_user["user_id"]) if current_user else None
    session = db.one(
        "SELECT * FROM chat_sessions WHERE session_id=? AND owner_id IS ?",
        (session_id, owner_id),
    )
    if not session:
        raise AppError(
            code="SESSION_NOT_FOUND",
            message="问答会话不存在",
            status_code=404,
        )
    _require_session_scope(db, session, payload)

    settings = request.app.state.settings
    # 检索包含磁盘和向量计算，因此放入线程池以保护异步事件循环。
    results = await run_in_threadpool(
        search_chunks,
        db,
        payload.question,
        document_ids=payload.document_ids or (),
        version_ids=payload.version_ids or (),
        top_k=payload.top_k,
        embedding_provider=embedding_provider,
        min_vector_score=settings.embedding_min_score,
    )
    results = _select_answer_evidence(
        results,
        min_vector_score=settings.embedding_min_score,
    )

    result_document_ids = tuple(
        dict.fromkeys(item.chunk.document_id for item in results)
    )
    filenames: dict[str, str] = {}
    if result_document_ids:
        placeholders = ",".join("?" for _ in result_document_ids)
        filenames = {
            row["document_id"]: row["filename"]
            for row in db.all(
                "SELECT document_id,filename FROM documents "
                f"WHERE document_id IN ({placeholders})",
                result_document_ids,
            )
        }

    citations = [
        citation_from_chunk(
            item.chunk,
            section=item.chunk.section_path,
        ).model_dump()
        for item in results
    ]
    # MVP 阶段直接返回证据片段；后续 Agent 可以在此基础上生成自然语言总结。
    snippets = [
        f"{filenames.get(item.chunk.document_id, item.chunk.document_id)}："
        f"{item.chunk.text}"
        for item in results
    ]
    # A model may select exact source spans; free model prose never becomes a cited answer.
    spans = await run_in_threadpool(select_answer_spans, payload.question, citations, llm_provider)
    if spans:
        answer = "根据原文：" + "；".join(
            f"{item['quote_span']} [{item['citation_id']}]" for item in spans
        )
    else:
        answer = "\n".join(snippets) if snippets else None
    status_value = "answered" if citations else "insufficient_evidence"
    now = utc_now()
    # 用户问题和助手证据都持久化，刷新页面后仍可查看完整对话。
    db.run(
        "INSERT INTO chat_messages("
        "message_id,session_id,role,content,citations,created_at"
        ") VALUES(?,?,?,?,?,?)",
        (
            f"msg_{uuid4().hex}",
            session_id,
            "user",
            payload.question,
            "[]",
            now,
        ),
    )
    db.run(
        "INSERT INTO chat_messages("
        "message_id,session_id,role,content,citations,created_at"
        ") VALUES(?,?,?,?,?,?)",
        (
            f"msg_{uuid4().hex}",
            session_id,
            "assistant",
            answer or "",
            json_dumps(citations),
            now,
        ),
    )
    return {
        "answer": answer,
        "status": status_value,
        "needs_confirmation": False,
        "citations": citations,
        "answer_type": "model_selected_quote" if spans else "quoted_evidence",
    }


@router.get("/{session_id}/messages", summary="查询问答历史")
async def list_chat_messages(
    session_id: str,
    db: SQLiteDatabase = Depends(get_database),
    current_user: dict[str, object] | None = Depends(get_request_user),
):
    """返回指定会话的历史消息和引用。"""

    owner_id = str(current_user["user_id"]) if current_user else None
    if not db.one(
        "SELECT session_id FROM chat_sessions WHERE session_id=? AND owner_id IS ?",
        (session_id, owner_id),
    ):
        raise AppError(
            code="SESSION_NOT_FOUND",
            message="问答会话不存在",
            status_code=404,
        )
    rows = db.all(
        "SELECT * FROM chat_messages WHERE session_id=? ORDER BY created_at",
        (session_id,),
    )
    return {
        "session_id": session_id,
        "items": [
            {
                "message_id": row["message_id"],
                "role": row["role"],
                "content": row["content"],
                "citations": json_loads(row["citations"], []),
                "created_at": row["created_at"],
            }
            for row in rows
        ],
    }
