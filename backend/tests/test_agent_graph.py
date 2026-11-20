"""Agent 图的终态、范围、重试和双版本证据测试。"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from app.agents.graph import AgentGraph, _compare_node, _screen_node, build_default_graph
from app.agents.state import NodeResult
from app.agents.supervisor import Supervisor, WorkflowPolicy
from app.infrastructure.database import SQLiteDatabase, json_dumps, utc_now


def _state(task_type: str = "review", **kwargs):
    """创建带最小显式范围的状态；测试可以缩短计划以定位执行器行为。"""

    return Supervisor().create_state(
        task_id="task_test", task_type=task_type, document_ids=["doc_1"], **kwargs
    )


@pytest.fixture
def agent_db():
    """独立数据库放在已有目录中，结束后删除 SQLite 及其日志文件。"""

    path = Path(__file__).parent / f".agent_graph_{uuid4().hex}.sqlite"
    db = SQLiteDatabase(path)
    db.initialize()
    try:
        yield db
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{path}{suffix}").unlink(missing_ok=True)


def _document(db: SQLiteDatabase, document_id: str, owner_id: str, *, amount: int = 10) -> None:
    """写入带明确所有者和筛选字段的测试文档。"""

    now = utc_now()
    db.run(
        "INSERT INTO documents(document_id,owner_id,display_name,filename,mime_type,status,"
        "extracted_facts,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            document_id,
            owner_id,
            document_id,
            f"{document_id}.pdf",
            "application/pdf",
            "indexed",
            json_dumps({"amount": amount}),
            now,
            now,
        ),
    )


def _version(db: SQLiteDatabase, version_id: str, document_id: str, number: int, text: str) -> None:
    """模拟已解析版本，正文与页面文本使用同一坐标。"""

    now = utc_now()
    db.run(
        "INSERT INTO document_versions(version_id,document_id,version_number,filename,mime_type,"
        "size_bytes,content_sha256,object_key,status,page_count,parsed_content,extracted_text,"
        "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            version_id,
            document_id,
            number,
            f"v{number}.pdf",
            "application/pdf",
            len(text),
            version_id,
            version_id,
            "indexed",
            1,
            json_dumps({"pages": [{"page": 1, "text": text, "section": "付款条款"}]}),
            text,
            now,
            now,
        ),
    )


def test_terminal_snapshots_do_not_restart_or_become_success() -> None:
    """持久化快照内已有的人工暂停和失败结果不能被下次运行覆盖。"""

    calls = 0

    def operation(_state):
        nonlocal calls
        calls += 1
        return NodeResult(node="retrieve", status="succeeded")

    graph = AgentGraph({"retrieve": operation})
    for node_status in ("waiting_human", "failed"):
        state = _state()
        state.plan = ["retrieve"]
        state.node_results["retrieve"] = NodeResult(node="retrieve", status=node_status)
        result = graph.run(state)
        assert result.status == node_status
        assert not any(event.type == "task.completed" for event in result.events)
    assert calls == 0


def test_exact_step_budget_succeeds_and_callback_receives_live_events() -> None:
    """最后一个节点恰好用完预算时仍成功，回调实时看到执行进度。"""

    state = _state()
    state.plan = ["retrieve"]
    received = []
    graph = AgentGraph(
        {"retrieve": lambda _state: NodeResult(node="retrieve", status="succeeded")},
        WorkflowPolicy(max_steps=1),
    )
    result = graph.run(state, on_event=lambda snapshot, event: received.append((snapshot.status, event.type)))
    assert result.status == "succeeded"
    assert [event_type for _, event_type in received] == [
        "task.started", "node.started", "node.succeeded", "task.completed"
    ]


def test_failed_result_retries_once_and_attempts_are_counted() -> None:
    """节点返回 failed 与抛异常遵守同一重试上限。"""

    state = _state()
    state.plan = ["retrieve"]
    attempts = 0

    def flaky(_state):
        nonlocal attempts
        attempts += 1
        return NodeResult(node="retrieve", status="failed" if attempts == 1 else "succeeded")

    result = AgentGraph(
        {"retrieve": flaky}, WorkflowPolicy(max_steps=2, max_retries_per_node=1)
    ).run(state)
    assert result.status == "succeeded"
    assert attempts == 2
    assert result.retry_count == 1
    assert result.node_results["retrieve"].attempts == 2


def test_step_budget_exhaustion_is_failed() -> None:
    """还有未执行节点时，预算耗尽必须返回失败。"""

    state = _state()
    state.plan = ["retrieve", "extract"]
    result = AgentGraph(
        {
            "retrieve": lambda _state: NodeResult(node="retrieve", status="succeeded"),
            "extract": lambda _state: NodeResult(node="extract", status="succeeded"),
        },
        WorkflowPolicy(max_steps=1),
    ).run(state)
    assert result.status == "failed"
    assert "extract" not in result.node_results


def test_screen_zero_match_finishes_without_retrieval_or_invalid_sql(agent_db) -> None:
    """有候选但无命中时直接输出空报告，不让检索使用空范围。"""

    db = agent_db
    _document(db, "doc_1", "user_1", amount=10)
    state = _state(
        "screen_and_review",
        user_id="user_1",
        conditions={"amount": {"field": "amount", "operator": "gt", "value": 100.0}},
    )
    result = build_default_graph(db).run(state)
    assert result.status == "succeeded"
    assert result.node_results["screen"].output["matched_count"] == 0
    assert result.report["matched_document_count"] == 0
    assert "retrieve" not in result.node_results
    assert result.document_ids == []
    # 模拟节点提交后任务进程中断，重放快照仍只能得到空结果。
    result.status = "running"
    result.report = None
    resumed = build_default_graph(db).run(result)
    assert resumed.status == "succeeded"
    assert resumed.report["matched_document_count"] == 0
    assert "retrieve" not in resumed.node_results


def test_screen_rejects_other_owner_and_more_than_twenty_documents(agent_db) -> None:
    """初筛节点必须按所有者隔离，并在查询前拒绝超量范围。"""

    db = agent_db
    _document(db, "doc_1", "user_2", amount=200)
    state = _state(
        "screen_and_review",
        user_id="user_1",
        conditions={"amount": {"field": "amount", "operator": "gt", "value": 100.0}},
    )
    assert build_default_graph(db).run(state).status == "waiting_human"
    too_many = _state(
        "screen_and_review",
        user_id="user_1",
        conditions={"amount": {"field": "amount", "operator": "gt", "value": 100.0}},
    )
    too_many.document_ids = [f"doc_{index}" for index in range(21)]
    assert build_default_graph(db).run(too_many).status == "failed"


def test_screen_requires_conditions_and_shrinks_version_scope(agent_db) -> None:
    """版本范围和文档范围同步收缩，避免检索层 OR 条件带回未命中版本。"""

    db = agent_db
    _document(db, "doc_1", "user_1", amount=200)
    _document(db, "doc_2", "user_1", amount=10)
    _version(db, "ver_1", "doc_1", 1, "金额200万元")
    _version(db, "ver_2", "doc_2", 1, "金额10万元")
    no_conditions = _state("screen_and_review", user_id="user_1")
    assert _screen_node(no_conditions, db).status == "waiting_human"

    state = _state(
        "screen_and_review",
        user_id="user_1",
        version_ids=["ver_1", "ver_2"],
        conditions={"amount": {"field": "amount", "operator": "gt", "value": 100.0}},
    )
    state.document_ids.append("doc_2")
    screened = _screen_node(state, db)
    assert screened.status == "succeeded"
    assert state.document_ids == ["doc_1"]
    assert state.version_ids == ["ver_1"]


def test_compare_reads_owned_versions_with_replayable_citations(agent_db) -> None:
    """真实默认比较节点从数据库读版本，变更两侧都能定位到原文。"""

    db = agent_db
    _document(db, "doc_1", "user_1")
    base_text = "第一条 合同金额100万元\n付款期限30天"
    target_text = "第一条 合同金额100万元\n付款期限60天"
    _version(db, "ver_1", "doc_1", 1, base_text)
    _version(db, "ver_2", "doc_1", 2, target_text)
    state = _state("compare", user_id="user_1", version_ids=["ver_1", "ver_2"])
    comparison = _compare_node(state, db)
    assert comparison.status == "succeeded"
    assert comparison.output["changed"] is True
    assert len(comparison.output["changes"]) == 1
    assert {item.version_id for item in state.evidence} == {"ver_1", "ver_2"}
    for item in state.evidence:
        expected = base_text if item.version_id == "ver_1" else target_text
        assert expected[item.char_start : item.char_end] == item.quote
        assert item.page == 1

    full_run = build_default_graph(db).run(
        _state("compare", user_id="user_1", version_ids=["ver_1", "ver_2"])
    )
    assert full_run.status == "succeeded"
    assert full_run.node_results["verify"].status == "succeeded"
    assert len(full_run.report["changes"]) == 1

    # 同一版本 ID 即使已知，只要不属于当前用户也不能进入比较状态。
    foreign = _state("compare", user_id="user_2", version_ids=["ver_1", "ver_2"])
    assert _compare_node(foreign, db).status == "waiting_human"


def test_compare_identical_versions_finishes_without_invented_evidence(agent_db) -> None:
    """两个版本内容相同时报告空变更，无需伪造引用。"""

    db = agent_db
    _document(db, "doc_1", "user_1")
    text = "付款期限30天"
    _version(db, "ver_1", "doc_1", 1, text)
    _version(db, "ver_2", "doc_1", 2, text)
    result = build_default_graph(db).run(
        _state("compare", user_id="user_1", version_ids=["ver_1", "ver_2"])
    )
    assert result.status == "succeeded"
    assert result.report["changes"] == []
    assert result.evidence == []
