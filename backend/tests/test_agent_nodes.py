"""Agent 节点的跨合同隔离和 Citation 回放测试。"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from app.agents.extraction_agent import extract_node
from app.agents.comparison_agent import compare_node
from app.agents.report_agent import report_node
from app.agents.retrieval_agent import retrieve_node
from app.agents.risk_agent import risk_node
from app.agents.supervisor import Supervisor
from app.agents.verification_agent import verify_node
from app.infrastructure.database import SQLiteDatabase, json_dumps, utc_now


def _seed_document(db: SQLiteDatabase, document_id: str, chunks: list[str]) -> None:
    """给每份合同建立一版正文和可回放的分块。"""

    version_id = f"ver_{document_id}"
    text = "\n\n".join(chunks)
    now = utc_now()
    db.run(
        "INSERT INTO documents(document_id,owner_id,display_name,filename,mime_type,status,"
        "latest_version_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (document_id, "user_1", document_id, "contract.pdf", "application/pdf", "parsed", version_id, now, now),
    )
    db.run(
        "INSERT INTO document_versions(version_id,document_id,version_number,filename,mime_type,"
        "size_bytes,content_sha256,object_key,status,parsed_content,extracted_text,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            version_id, document_id, 1, "contract.pdf", "application/pdf", len(text),
            sha256(text.encode()).hexdigest(), f"objects/{document_id}", "parsed",
            json_dumps({"pages": [{"page": 1, "text": text, "section": "合同条款"}]}),
            text, now, now,
        ),
    )
    offset = 0
    for index, part in enumerate(chunks):
        db.run(
            "INSERT INTO chunks(chunk_id,document_id,version_id,page,text,char_start,char_end,"
            "content_sha256,section_path,token_estimate,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"chunk_{document_id}_{index}", document_id, version_id, 1, part,
                offset, offset + len(part), sha256(part.encode()).hexdigest(),
                "合同条款", len(part), now,
            ),
        )
        offset += len(part) + 2


@pytest.fixture
def db():
    """真实 SQLite 索引用于发现只检查内存引用 ID 的假阳性。"""

    path = Path(__file__).parent / f".agent_nodes_{uuid4().hex}.db"
    database = SQLiteDatabase(path)
    database.initialize()
    _seed_document(database, "doc_a", ["合同金额：120 万元。高风险。", "付款期限：60 日。"])
    _seed_document(database, "doc_b", ["合同金额：20 万元。低风险。"])
    try:
        yield database
    finally:
        # SQLite 短连接已经关闭；清理本测试的数据库和可能残留的 WAL 文件。
        for suffix in ("", "-wal", "-shm"):
            path.with_name(path.name + suffix).unlink(missing_ok=True)


def _run_review(db: SQLiteDatabase):
    """逐节点执行无自然语言问题的批量审阅。"""

    state = Supervisor().create_state(
        task_id="task_review",
        task_type="review",
        document_ids=["doc_a", "doc_b"],
        user_id="user_1",
    )
    state.node_results["retrieve"] = retrieve_node(state, db)
    state.node_results["extract"] = extract_node(state)
    state.node_results["risk"] = risk_node(state)
    state.node_results["verify"] = verify_node(state, db)
    return state


def test_batch_review_keeps_values_and_citations_in_each_document(db: SQLiteDatabase):
    """每份合同单独取证，字段不继承别的合同的金额或引用。"""

    state = _run_review(db)
    assert state.node_results["retrieve"].status == "succeeded"
    assert state.node_results["verify"].status == "succeeded"
    amounts = {field.document_id: field for field in state.extracted_fields if field.name == "amount"}
    assert amounts["doc_a"].value == 1_200_000
    assert amounts["doc_b"].value == 200_000
    assert {item.document_id for item in amounts["doc_a"].citations} == {"doc_a"}
    assert {item.document_id for item in amounts["doc_b"].citations} == {"doc_b"}
    assert {item.citation_id for item in amounts["doc_a"].citations} == {"cite_chunk_doc_a_0"}
    assert len(state.risks) == 1
    assert state.risks[0].document_id == "doc_a"
    assert {item.citation_id for item in state.risks[0].citations} == {"cite_chunk_doc_a_0"}
    first_id = state.risks[0].finding_id
    risk_node(state)
    assert state.risks[0].finding_id == first_id
    assert report_node(state).status == "succeeded"
    assert len(state.report["fields"]) == 20


def test_clause_fields_require_actual_clause_evidence(db: SQLiteDatabase):
    """验收、责任、续约、保密和争议字段只引用出现条款的合同。"""

    _seed_document(
        db,
        "doc_c",
        [
            "验收标准：交付后十日内书面确认。",
            "赔偿责任上限为合同总价。",
            "到期自动续约一年。保密义务持续两年。争议提交上海仲裁委员会。",
        ],
    )
    state = Supervisor().create_state(
        task_id="task_clause", task_type="review", document_ids=["doc_c"], user_id="user_1"
    )
    assert retrieve_node(state, db).status == "succeeded"
    assert extract_node(state).status == "succeeded"
    clauses = {field.name: field for field in state.extracted_fields}
    for name in ("acceptance", "liability_cap", "renewal", "confidentiality", "dispute_resolution"):
        assert clauses[name].status == "found"
        assert clauses[name].citations
        assert {item.document_id for item in clauses[name].citations} == {"doc_c"}
    assert clauses["payment_terms"].status == "unknown"
    assert verify_node(state, db).status == "succeeded"


@pytest.mark.parametrize(
    "tamper",
    [
        {"page": 2},
        {"quote": "合同金额：999 万元。高风险。"},
        {"char_start": 1},
        {"version_id": "ver_doc_b"},
        {"section": "虚构条款"},
    ],
)
def test_verification_rejects_forged_chunk_location(db: SQLiteDatabase, tamper: dict):
    """引用 ID 正确仍不足以通过：来源内容和位置都必须匹配索引。"""

    state = _run_review(db)
    original = state.evidence[0]
    forged = original.model_copy(update=tamper)
    state.evidence[0] = forged
    for field in state.extracted_fields:
        field.citations = [forged if item.citation_id == original.citation_id else item for item in field.citations]
    for risk in state.risks:
        risk.citations = [forged if item.citation_id == original.citation_id else item for item in risk.citations]
    result = verify_node(state, db)
    assert result.status == "waiting_human"
    assert result.output["invalid_fields"] or result.output["invalid_risks"]


def test_qa_returns_only_verified_quoted_evidence(db: SQLiteDatabase):
    """问答输出是有限摘录，答案及引用都来自已验证的单份合同。"""

    state = Supervisor().create_state(
        task_id="task_qa", task_type="qa", request="付款期限",
        document_ids=["doc_a"], user_id="user_1",
    )
    assert retrieve_node(state, db).status == "succeeded"
    state.node_results["verify"] = verify_node(state, db)
    assert state.node_results["verify"].status == "succeeded"
    assert report_node(state).status == "succeeded"
    assert "付款期限：60 日。" in state.report["answer"]
    assert state.report["answer_type"] == "quoted_evidence"
    assert {item["document_id"] for item in state.report["citations"]} == {"doc_a"}


def test_document_and_explicit_version_scopes_form_a_union(db: SQLiteDatabase):
    """独立选择的文档当前版与显式版本均进入范围，仍受用户归属限制。"""

    state = Supervisor().create_state(
        task_id="task_union", task_type="review", document_ids=["doc_a"],
        version_ids=["ver_doc_b"], user_id="user_1",
    )
    assert retrieve_node(state, db).status == "succeeded"
    assert {(item.document_id, item.version_id) for item in state.evidence} == {
        ("doc_a", "ver_doc_a"), ("doc_b", "ver_doc_b")
    }
    assert verify_node(state, db).status == "succeeded"
    db.run("UPDATE documents SET owner_id=? WHERE document_id=?", ("user_2", "doc_b"))
    assert retrieve_node(state, db).status == "waiting_human"
    assert state.evidence == []


def test_compare_rejects_change_with_forged_citation(db: SQLiteDatabase):
    """即使 state.evidence 正确，报告中的变更引用也要逐条核对。"""

    base = db.one("SELECT extracted_text FROM document_versions WHERE version_id=?", ("ver_doc_a",))["extracted_text"]
    target = "合同金额：130 万元。高风险。\n\n付款期限：60 日。"
    now = utc_now()
    db.run(
        "INSERT INTO document_versions(version_id,document_id,version_number,filename,mime_type,"
        "size_bytes,content_sha256,object_key,status,parsed_content,extracted_text,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "ver_doc_a_2", "doc_a", 2, "contract.pdf", "application/pdf", len(target),
            sha256(target.encode()).hexdigest(), "objects/doc_a_2", "parsed",
            json_dumps({"pages": [{"page": 1, "text": target, "section": "合同条款"}]}),
            target, now, now,
        ),
    )
    state = Supervisor().create_state(
        task_id="task_compare", task_type="compare", document_ids=["doc_a"],
        version_ids=["ver_doc_a", "ver_doc_a_2"], user_id="user_1",
    )
    state.node_results["compare"] = compare_node(
        state, base, target, document_id="doc_a",
        base_pages=[{"page": 1, "text": base, "section": "合同条款"}],
        target_pages=[{"page": 1, "text": target, "section": "合同条款"}],
    )
    assert verify_node(state, db).status == "succeeded"
    state.node_results["compare"].output["changes"][0]["target_citation"]["page"] = 2
    result = verify_node(state, db)
    assert result.status == "waiting_human"
    assert "compare_change" in result.output["invalid_risks"]
