"""在每份合同的明确版本内检索证据，避免批量审阅互相污染。"""

from __future__ import annotations

from app.infrastructure.database import SQLiteDatabase
from app.infrastructure.providers import EmbeddingProvider
from app.rag.citations import citation_from_chunk
from app.rag.chunker import Chunk
from app.rag.search import search_chunks

from .state import EvidenceRef, NodeResult, TaskState
from .review_intents import INTENT_VERSION, REVIEW_INTENTS


def _version_scopes(state: TaskState, db: SQLiteDatabase) -> list[tuple[str, str]]:
    """将调用方范围解析为数据库真实存在的文档与版本配对。"""

    scopes: list[str] = []
    params: list[str | None] = []
    if state.document_ids:
        scopes.append(
            f"(v.document_id IN ({','.join('?' for _ in state.document_ids)}) "
            "AND v.version_id=d.latest_version_id)"
        )
        params.extend(state.document_ids)
    if state.version_ids:
        scopes.append(f"v.version_id IN ({','.join('?' for _ in state.version_ids)})")
        params.extend(state.version_ids)
    if not scopes:
        return []
    # 匿名任务只访问匿名文档；不能让 NULL 用户绕过资源归属过滤。
    params.append(state.user_id)
    rows = db.all(
        "SELECT v.document_id,v.version_id FROM document_versions v "
        "JOIN documents d ON d.document_id=v.document_id WHERE ("
        + " OR ".join(scopes)
        + ") AND d.owner_id IS ?"
        + " ORDER BY v.document_id,v.version_number",
        tuple(params),
    )
    return [(row["document_id"], row["version_id"]) for row in rows]


def retrieve_node(
    state: TaskState,
    db: SQLiteDatabase,
    embedding_provider: EmbeddingProvider | None = None,
    *,
    top_k: int = 5,
) -> NodeResult:
    """逐版本检索；无问题的审阅至少为关键条款预留取证名额。"""

    if not state.document_ids and not state.version_ids:
        return NodeResult(node="retrieve", status="waiting_human", error="缺少明确的文档范围")
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    if state.task_type == "qa" and not state.request.strip():
        return NodeResult(node="retrieve", status="waiting_human", error="问答任务缺少问题")

    scopes = _version_scopes(state, db)
    found_documents = {document_id for document_id, _ in scopes}
    found_versions = {version_id for _, version_id in scopes}
    if (
        not scopes
        or not set(state.document_ids).issubset(found_documents)
        or not set(state.version_ids).issubset(found_versions)
    ):
        state.evidence = []
        return NodeResult(node="retrieve", status="waiting_human", error="文档范围内存在无权或无可检索版本")

    evidence: list[EvidenceRef] = []
    scores: list[float] = []
    missing_scopes: list[dict[str, str]] = []
    retrieval_traces: list[dict[str, object]] = []
    for document_id, version_id in scopes:
        if state.request.strip():
            # search_chunks 的版本范围足以限定一个版本；下方仍逐项核对双 ID。
            results = search_chunks(
                db,
                state.request,
                version_ids=[version_id],
                top_k=top_k,
                embedding_provider=embedding_provider,
            )
            scoped = [
                (item.chunk, item.score)
                for item in results
                if item.chunk.document_id == document_id and item.chunk.version_id == version_id
            ]
        else:
            # Each intent searches this exact version and records an independent
            # evidence-insufficient result instead of treating a miss as absence.
            scoped_by_id: dict[str, tuple[Chunk, float]] = {}
            for intent in REVIEW_INTENTS:
                matches = search_chunks(
                    db, intent.query, version_ids=[version_id],
                    top_k=1, embedding_provider=embedding_provider,
                )
                matches = [
                    item for item in matches
                    if item.chunk.document_id == document_id and item.chunk.version_id == version_id
                ]
                retrieval_traces.append({
                    "document_id": document_id, "version_id": version_id,
                    "intent_id": intent.intent_id,
                    "status": "matched" if matches else "evidence_insufficient",
                    "citation_ids": [f"cite_{item.chunk.chunk_id}" for item in matches],
                })
                for item in matches:
                    scoped_by_id[item.chunk.chunk_id] = (item.chunk, item.score)
            scoped = list(scoped_by_id.values())
        if not scoped:
            missing_scopes.append({"document_id": document_id, "version_id": version_id})
        for chunk, score in scoped:
            evidence.append(
                EvidenceRef.model_validate(
                    citation_from_chunk(chunk, section=chunk.section_path).model_dump()
                )
            )
            scores.append(score)

    state.evidence = evidence
    # 一份合同无证据时暂停整批任务，不能借其他合同的片段生成该份报告。
    status = "succeeded" if evidence and not missing_scopes else "waiting_human"
    return NodeResult(
        node="retrieve",
        status=status,
        output={
            "evidence_count": len(evidence),
            "scope_count": len(scopes),
            "missing_scopes": missing_scopes,
            "scores": scores,
            "intent_version": INTENT_VERSION if not state.request.strip() else None,
            "retrieval_traces": retrieval_traces,
        },
        error=None if status == "succeeded" else "部分文档没有可验证的证据",
    )


class RetrievalAgent:
    """面向 graph 的小型适配器，避免节点依赖 FastAPI 路由。"""

    def __init__(self, db: SQLiteDatabase, embedding_provider: EmbeddingProvider | None = None) -> None:
        self.db = db
        self.embedding_provider = embedding_provider

    def run(self, state: TaskState, *, top_k: int = 5) -> NodeResult:
        return retrieve_node(state, self.db, self.embedding_provider, top_k=top_k)
