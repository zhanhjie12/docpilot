"""有界 Agent 工作流的行为测试。

测试只使用内存中的 fake 节点，不依赖线上模型，覆盖固定路由、证据校验、人工暂停
和有限重试四个关键约束。
"""

from __future__ import annotations

from app.agents.extraction_agent import extract_node
from app.agents.graph import AgentGraph
from app.agents.comparison_agent import compare_node
from app.agents.parser_agent import parser_quality_node
from app.agents.report_agent import report_node
from app.agents.risk_agent import risk_node
from app.agents.state import EvidenceRef, NodeResult
from app.agents.supervisor import Supervisor, WorkflowPolicy
from app.agents.verification_agent import verify_node


def _evidence() -> EvidenceRef:
    """构造带页面和字符范围的合同证据。"""

    return EvidenceRef(
        citation_id="cite_1",
        document_id="doc_1",
        version_id="ver_1",
        page=1,
        section="付款条款",
        quote="合同金额：人民币 120 万元。付款期限：验收后 60 日内支付。",
        char_start=0,
        char_end=31,
    )


def test_supervisor_rejects_unbounded_task_and_serializes_state():
    """任务类型只能从白名单选择，状态可以直接转换为 JSON。"""

    supervisor = Supervisor()
    state = supervisor.create_state(
        task_id="task_1",
        task_type="review",
        request="付款期限",
        document_ids=["doc_1", "doc_1"],
    )
    assert state.document_ids == ["doc_1"]
    assert state.plan == ["retrieve", "extract", "risk", "verify", "report"]
    assert state.model_dump(mode="json")["task_id"] == "task_1"

    try:
        supervisor.create_state(task_id="task_2", task_type="free_chat")
    except ValueError as exc:
        assert "unsupported task_type" in str(exc)
    else:  # pragma: no cover - assertion documents the safety contract
        raise AssertionError("unbounded task type must be rejected")


def test_review_graph_requires_citations_before_report():
    """有引用的抽取结果可以完成；报告只能出现在验证之后。"""

    supervisor = Supervisor()
    state = supervisor.create_state(
        task_id="task_2",
        task_type="review",
        request="合同金额和付款期限",
        document_ids=["doc_1"],
    )

    def retrieve(state):
        state.evidence = [_evidence()]
        return NodeResult(node="retrieve", status="succeeded", output={"evidence_count": 1})

    graph = AgentGraph(
        {
            "retrieve": retrieve,
            "extract": extract_node,
            "risk": risk_node,
            "verify": verify_node,
            "report": report_node,
        }
    )
    result = graph.run(state)
    assert result.status == "succeeded"
    assert result.report is not None
    assert result.report["status"] == "succeeded"
    assert result.node_results["verify"].status == "succeeded"


def test_missing_evidence_enters_waiting_human():
    """没有证据时必须暂停人工，不能生成肯定报告。"""

    supervisor = Supervisor()
    state = supervisor.create_state(
        task_id="task_3",
        task_type="review",
        request="付款期限",
        document_ids=["doc_1"],
    )
    graph = AgentGraph(
        {
            "retrieve": lambda _state: NodeResult(
                node="retrieve", status="waiting_human", error="未找到证据"
            ),
        }
    )
    result = graph.run(state)
    assert result.status == "waiting_human"
    assert result.report is None


def test_node_failure_is_retried_once_then_succeeds():
    """节点异常只允许配置次数内重试，不会无限循环。"""

    state = Supervisor().create_state(
        task_id="task_4",
        task_type="review",
        request="x",
        document_ids=["doc_1"],
    )
    # 这个单元测试只验证 retrieve 节点的重试策略，不展开完整审阅链。
    state.plan = ["retrieve"]
    calls = {"count": 0}

    def flaky(_state):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("temporary")
        return NodeResult(node="retrieve", status="succeeded")

    graph = AgentGraph({"retrieve": flaky}, WorkflowPolicy(max_steps=3, max_retries_per_node=1))
    result = graph.run(state)
    assert result.status == "succeeded"
    assert calls["count"] == 2
    assert result.retry_count == 1


def test_parser_quality_and_comparison_nodes_are_bounded():
    """解析质量异常和版本文本差异都有稳定的结构化输出。"""

    state = Supervisor().create_state(
        task_id="task_5",
        task_type="compare",
        request="比较付款条款",
        version_ids=["ver_1", "ver_2"],
    )
    comparison = compare_node(state, "付款期限 30 天", "付款期限 60 天")
    assert comparison.status == "succeeded"
    assert comparison.output["changed"] is True
    quality = parser_quality_node(
        state,
        {"page_count": 1, "pages": [{"text": "合同正文"}], "quality_warnings": []},
    )
    assert quality.status == "succeeded"
