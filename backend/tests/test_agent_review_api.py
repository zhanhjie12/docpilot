"""审阅 HTTP 入口、Agent 证据和用户范围的集成回归。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import time
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app.core.config import Settings
from app.core.middleware import wrap_http_app
from app.infrastructure.database import SQLiteDatabase, json_dumps, utc_now
from app.main import create_app


@pytest.fixture
def review_client():
    """用独立 SQLite 和禁用向量模型的应用测试真实 HTTP 工作流。"""

    backend = Path(__file__).resolve().parents[1]
    root = backend / ".test-storage" / uuid4().hex
    root.mkdir(parents=True)
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{(root / 'review.db').as_posix()}",
        upload_dir=root / "uploads",
        embedding_provider="disabled",
        auth_required=False,
        task_workers=1,
    )
    application = create_app(settings)
    try:
        with TestClient(wrap_http_app(application)) as client:
            yield client, application.state.database
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _seed_document(db: SQLiteDatabase, owner_id: str | None, text: str) -> tuple[str, str]:
    """插入一份已索引的合同和真实字符范围，避免测试依赖模型或 PDF 渲染器。"""

    document_id = f"doc_{uuid4().hex}"
    version_id = f"ver_{uuid4().hex}"
    chunk_id = f"chunk_{uuid4().hex}"
    now = utc_now()
    db.run(
        "INSERT INTO documents(document_id,owner_id,display_name,filename,mime_type,status,"
        "latest_version_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (document_id, owner_id, "测试合同", "contract.pdf", "application/pdf", "indexed", version_id, now, now),
    )
    db.run(
        "INSERT INTO document_versions(version_id,document_id,version_number,filename,mime_type,"
        "size_bytes,content_sha256,object_key,status,extracted_text,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (version_id, document_id, 1, "contract.pdf", "application/pdf", len(text.encode()),
         uuid4().hex, "test.pdf", "indexed", text, now, now),
    )
    db.run(
        "INSERT INTO chunks(chunk_id,document_id,version_id,page,text,char_start,char_end,"
        "content_sha256,section_path,token_estimate,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (chunk_id, document_id, version_id, 1, text, 0, len(text), uuid4().hex,
         "合同条款", len(text), now),
    )
    return document_id, version_id


def _wait_for_task(client: TestClient, task_id: str, headers: dict[str, str] | None = None) -> dict:
    """有限轮询后台任务，避免测试依赖线程调度时机。"""

    for _ in range(200):
        response = client.get(f"/api/v1/tasks/{task_id}", headers=headers)
        assert response.status_code == 200
        task = response.json()
        if task["status"] in {"succeeded", "waiting_human", "failed"}:
            return task
        time.sleep(0.01)
    raise AssertionError("审阅任务未在预期时间内结束")


def test_review_api_runs_agent_and_persists_real_evidence(review_client):
    """入口真正运行 Agent；引用必须来自所选版本的已索引原文。"""

    client, db = review_client
    source = "合同金额：人民币120万元。合同期限：365天。付款期限：60天。高风险：乙方承担无限责任。"
    document_id, version_id = _seed_document(db, None, source)
    created = client.post(
        "/api/v1/reviews",
        json={"document_ids": [document_id]},
        headers={"Idempotency-Key": "same-contract-review"},
    )
    assert created.status_code == 202
    replay = client.post(
        "/api/v1/reviews",
        json={"document_ids": [document_id]},
        headers={"Idempotency-Key": "same-contract-review"},
    )
    assert replay.json() == created.json()
    review_id = created.json()["review_id"]
    task = _wait_for_task(client, created.json()["task_id"])
    assert task["status"] in {"succeeded", "waiting_human"}, task

    workflow = client.get(f"/api/v1/reviews/{review_id}/workflow")
    assert workflow.status_code == 200
    state = workflow.json()["state"]
    assert state["task_type"] == "review"
    assert state["node_results"]["verify"]["status"] == "succeeded"
    assert state["evidence"]
    for citation in state["evidence"]:
        assert citation["document_id"] == document_id
        assert citation["version_id"] == version_id
        assert citation["quote"] in source

    findings = client.get(f"/api/v1/reviews/{review_id}/findings").json()["items"]
    assert findings, "明确标注的高风险合同应产生可核验的风险项"
    for finding in findings:
        assert finding["document_id"] == document_id
        assert finding["version_id"] == version_id
        assert all(citation["quote"] in source for citation in finding["citations"])
    report = client.get(f"/api/v1/reviews/{review_id}/report?format=json")
    assert report.status_code == 200
    assert report.json()["agent"]["fields"]
    # 全部风险确认后，审阅和后台任务都应离开人工等待状态。
    for finding in findings:
        decided = client.post(
            f"/api/v1/reviews/{review_id}/findings/{finding['finding_id']}/decision",
            json={"decision": "approved", "note": "人工确认", "expected_version": finding["version"]},
        )
        assert decided.status_code == 200
    assert client.get(f"/api/v1/reviews/{review_id}").json()["status"] == "completed"
    assert client.get(f"/api/v1/tasks/{created.json()['task_id']}").json()["status"] == "succeeded"
    # 同一合同再次审阅应有自己的风险项，不能被全局 finding_id 去重掉。
    next_review = client.post("/api/v1/reviews", json={"document_ids": [document_id]}).json()
    assert _wait_for_task(client, next_review["task_id"])["status"] == "waiting_human"
    next_findings = client.get(f"/api/v1/reviews/{next_review['review_id']}/findings").json()["items"]
    assert next_findings
    assert next_findings[0]["finding_id"] != findings[0]["finding_id"]


def test_review_idempotency_replays_after_scope_changes_and_rejects_conflicts(review_client):
    """A valid replay returns the original IDs without revalidating mutable document state."""

    client, db = review_client
    first_document_id, _ = _seed_document(db, None, "合同金额：100万元。付款期限：30天。")
    second_document_id, _ = _seed_document(db, None, "合同金额：200万元。付款期限：60天。")
    headers = {"Idempotency-Key": "review-replay-after-change"}
    created = client.post("/api/v1/reviews", json={"document_ids": [first_document_id]}, headers=headers)
    assert created.status_code == 202
    _wait_for_task(client, created.json()["task_id"])

    # The original resource may change after creation. Replaying the same
    # client operation must still return its original resource identifiers.
    db.run("UPDATE documents SET status='failed' WHERE document_id=?", (first_document_id,))
    replay = client.post("/api/v1/reviews", json={"document_ids": [first_document_id]}, headers=headers)
    assert replay.status_code == 202
    assert replay.json() == created.json()

    conflict = client.post("/api/v1/reviews", json={"document_ids": [second_document_id]}, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert db.one("SELECT COUNT(*) AS total FROM reviews")["total"] == 1
    assert db.one("SELECT COUNT(*) AS total FROM tasks")["total"] == 1
    assert db.one("SELECT COUNT(*) AS total FROM idempotency_keys WHERE operation='review'")["total"] == 1


def test_review_idempotency_treats_reordered_scope_as_the_same_request(review_client):
    """Document order has no business meaning, so it must not create another Agent task."""

    client, db = review_client
    first_document_id, _ = _seed_document(db, None, "合同金额：100万元。")
    second_document_id, _ = _seed_document(db, None, "合同金额：200万元。")
    headers = {"Idempotency-Key": "review-scope-order"}
    first = client.post(
        "/api/v1/reviews",
        json={"document_ids": [first_document_id, second_document_id]},
        headers=headers,
    )
    second = client.post(
        "/api/v1/reviews",
        json={"document_ids": [second_document_id, first_document_id]},
        headers=headers,
    )
    assert first.status_code == second.status_code == 202
    assert second.json() == first.json()
    assert db.one("SELECT COUNT(*) AS total FROM reviews")["total"] == 1
    assert db.one("SELECT COUNT(*) AS total FROM tasks")["total"] == 1


def test_review_idempotency_concurrent_requests_create_one_review_and_task(review_client):
    """Concurrent HTTP retries claim one key and cannot leave an orphan Review."""

    client, db = review_client
    document_id, _ = _seed_document(db, None, "合同金额：100万元。付款期限：30天。")
    headers = {"Idempotency-Key": "review-concurrent-claim"}

    def create_once():
        return client.post("/api/v1/reviews", json={"document_ids": [document_id]}, headers=headers)

    # A pool creates real overlapping route calls instead of relying on two
    # sequential requests that cannot expose a select-then-insert race.
    with ThreadPoolExecutor(max_workers=12) as executor:
        responses = list(executor.map(lambda _index: create_once(), range(12)))

    assert {response.status_code for response in responses} == {202}
    identifiers = {(response.json()["review_id"], response.json()["task_id"]) for response in responses}
    assert len(identifiers) == 1
    assert db.one("SELECT COUNT(*) AS total FROM reviews")["total"] == 1
    assert db.one("SELECT COUNT(*) AS total FROM tasks")["total"] == 1
    assert db.one("SELECT COUNT(*) AS total FROM task_events")["total"] >= 1
    assert db.one("SELECT COUNT(*) AS total FROM idempotency_keys WHERE operation='review'")["total"] == 1
    _wait_for_task(client, next(iter(identifiers))[1])


def test_review_creation_rolls_back_when_task_persistence_fails(review_client):
    """A Task insertion failure cannot leave a Review or idempotency record behind."""

    client, db = review_client
    document_id, _ = _seed_document(db, None, "合同金额：100万元。")
    db.run(
        "CREATE TRIGGER reject_review_task BEFORE INSERT ON tasks "
        "WHEN NEW.task_type='review' "
        "BEGIN SELECT RAISE(ABORT, 'injected review task failure'); END"
    )
    # The outer ASGI wrapper deliberately re-raises unexpected exceptions in
    # TestClient mode after emitting the production 500 response.
    with pytest.raises(RuntimeError, match="IntegrityError"):
        client.post(
            "/api/v1/reviews",
            json={"document_ids": [document_id]},
            headers={"Idempotency-Key": "review-transaction-rollback"},
        )
    assert db.one("SELECT COUNT(*) AS total FROM reviews")["total"] == 0
    assert db.one("SELECT COUNT(*) AS total FROM tasks")["total"] == 0
    assert db.one("SELECT COUNT(*) AS total FROM task_events")["total"] == 0
    assert db.one("SELECT COUNT(*) AS total FROM idempotency_keys WHERE operation='review'")["total"] == 0


def test_review_scope_blocks_anonymous_and_foreign_versions(review_client):
    """匿名身份不能读取登录用户资源，比较接口不能引用范围外版本。"""

    client, db = review_client
    first = client.post(
        "/api/v1/auth/register",
        json={"email": "review-a@example.com", "password": "correct-horse-battery"},
    ).json()
    second = client.post(
        "/api/v1/auth/register",
        json={"email": "review-b@example.com", "password": "correct-horse-battery"},
    ).json()
    first_headers = {"Authorization": f"Bearer {first['access_token']}"}
    second_headers = {"Authorization": f"Bearer {second['access_token']}"}
    first_doc, first_version = _seed_document(db, first["user"]["user_id"], "合同金额：100万元。")
    _, second_version = _seed_document(db, second["user"]["user_id"], "合同金额：200万元。")

    assert client.post("/api/v1/reviews", json={"version_ids": [first_version]}).status_code == 404
    assert client.post("/api/v1/reviews", json={"version_ids": [second_version]}, headers=first_headers).status_code == 404
    created = client.post("/api/v1/reviews", json={"document_ids": [first_doc]}, headers=first_headers)
    assert created.status_code == 202
    review_id = created.json()["review_id"]
    task_id = created.json()["task_id"]
    assert client.get(f"/api/v1/reviews/{review_id}").status_code == 404
    assert client.get(f"/api/v1/reviews/{review_id}/workflow", headers=second_headers).status_code == 404
    assert client.get(f"/api/v1/tasks/{task_id}").status_code == 404
    assert client.get(f"/api/v1/documents/{first_doc}").status_code == 404
    compared = client.post(
        f"/api/v1/reviews/{review_id}/compare",
        json={"base_version_id": first_version, "target_version_id": second_version},
        headers=first_headers,
    )
    assert compared.status_code == 404


def test_screening_starts_agent_only_for_matching_documents(review_client):
    """深度分析只为命中的合同创建审阅，非命中合同没有子任务。"""

    client, db = review_client
    first_doc, _ = _seed_document(db, None, "合同金额：200万元。高风险：无限责任。")
    second_doc, _ = _seed_document(db, None, "合同金额：50万元。")
    db.run("UPDATE documents SET extracted_facts=? WHERE document_id=?", ('{"amount":2000000}', first_doc))
    db.run("UPDATE documents SET extracted_facts=? WHERE document_id=?", ('{"amount":500000}', second_doc))

    ambiguous = client.post(
        "/api/v1/documents/screen",
        json={"document_ids": [first_doc], "query": "金额超过100万元"},
    )
    assert ambiguous.status_code == 422
    assert ambiguous.json()["error"]["code"] == "SCREENING_CONDITIONS_REQUIRED"

    request = {
        "document_ids": [first_doc, second_doc],
        "query": "金额超过100万元",
        "conditions": {"amount": {"field": "amount", "operator": "gt", "value": 1000000}},
        "deep_analysis": True,
    }
    created = client.post(
        "/api/v1/documents/screen",
        json=request,
        headers={"Idempotency-Key": "screen-and-review"},
    )
    assert created.status_code == 202
    replay = client.post(
        "/api/v1/documents/screen",
        json=request,
        headers={"Idempotency-Key": "screen-and-review"},
    )
    assert replay.json() == created.json()
    assert _wait_for_task(client, created.json()["task_id"])["status"] == "succeeded"
    result = client.get(f"/api/v1/tasks/{created.json()['task_id']}/screening-result").json()
    assert result["total"] == 2
    assert result["matched"] == 1
    matched = next(item for item in result["items"] if item["document_id"] == first_doc)
    unmatched = next(item for item in result["items"] if item["document_id"] == second_doc)
    assert matched["review_id"] and matched["review_task_id"]
    assert "review_id" not in unmatched
    assert _wait_for_task(client, matched["review_task_id"])["status"] in {"succeeded", "waiting_human"}
    assert client.get(f"/api/v1/reviews/{matched['review_id']}/workflow").json()["state"]


def test_compare_endpoint_uses_verified_two_sided_agent_citations(review_client):
    """同一合同的两个版本通过 Agent 比较，并能回到双方原文位置。"""

    client, db = review_client
    base_text = "付款期限：30天。"
    target_text = "付款期限：60天。"
    document_id, base_version = _seed_document(db, None, base_text)
    target_version = f"ver_{uuid4().hex}"
    now = utc_now()
    db.run(
        "UPDATE document_versions SET parsed_content=? WHERE version_id=?",
        (json_dumps({"pages": [{"page": 1, "section": "付款", "text": base_text}]}), base_version),
    )
    db.run(
        "INSERT INTO document_versions(version_id,document_id,version_number,filename,mime_type,"
        "size_bytes,content_sha256,object_key,status,parsed_content,extracted_text,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (target_version, document_id, 2, "contract-v2.pdf", "application/pdf", len(target_text.encode()),
         uuid4().hex, "test-v2.pdf", "indexed",
         json_dumps({"pages": [{"page": 1, "section": "付款", "text": target_text}]}), target_text, now, now),
    )
    created = client.post("/api/v1/reviews", json={"document_ids": [document_id]})
    assert created.status_code == 202
    result = client.post(
        f"/api/v1/reviews/{created.json()['review_id']}/compare",
        json={"base_version_id": base_version, "target_version_id": target_version},
    )
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["changed"] is True
    assert body["changes"][0]["base_citation"]["quote"] == base_text
    assert body["changes"][0]["target_citation"]["quote"] == target_text
    assert {item["version_id"] for item in body["citations"]} == {base_version, target_version}
