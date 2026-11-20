"""API contract regression tests for the implemented local backend."""

from pathlib import Path
import re
import shutil
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app.core.config import Settings
from app.core.middleware import wrap_http_app
from app.main import create_app


@pytest.fixture
def client():
    """Run the real lifespan against isolated storage for every test."""

    workspace = Path(__file__).resolve().parents[1]
    storage_parent = (workspace / ".test-storage").resolve()
    root = (storage_parent / uuid4().hex).resolve()
    assert root.is_relative_to(workspace) and root.parent == storage_parent
    root.mkdir(parents=True)
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{(root / 'contract.db').as_posix()}",
        upload_dir=root / "uploads",
        embedding_provider="disabled",
        task_workers=1,
    )
    application = create_app(settings)
    try:
        with TestClient(wrap_http_app(application)) as instance:
            yield instance
    finally:
        assert root.is_relative_to(workspace) and root.parent == storage_parent
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.parametrize(
    "path,kwargs",
    [
        ("/api/v1/documents", {}),
        ("/api/v1/documents", {"json": {"file": "not-a-file"}}),
        ("/api/v1/documents/screen", {"json": {"document_ids": [], "query": "金额超过100万"}}),
        ("/api/v1/documents/screen", {"json": {"document_ids": ["doc_1"], "query": " "}}),
        (
            "/api/v1/documents/screen",
            {
                "json": {
                    "document_ids": ["doc_1"],
                    "query": "金额超过100万",
                    "conditions": {
                        "amount": {
                            "field": "duration_days",
                            "operator": "gt",
                            "value": 1_000_000,
                        }
                    },
                }
            },
        ),
        (
            "/api/v1/documents/screen",
            {
                "json": {
                    "document_ids": ["doc_1"],
                    "query": "金额超过100万",
                    "conditons": {},
                }
            },
        ),
        ("/api/v1/chat/sessions", {"json": {}}),
        ("/api/v1/chat/session_1/ask", {"json": {"question": "付款条件是什么？"}}),
        (
            "/api/v1/chat/session_1/ask",
            {"json": {"question": " ", "document_ids": ["doc_1"]}},
        ),
        (
            "/api/v1/chat/session_1/ask",
            {"json": {"question": "付款条件", "version_ids": ["ver_1"], "top_k": 0}},
        ),
        ("/api/v1/reviews", {"json": {"document_ids": []}}),
        (
            "/api/v1/reviews/review_1/findings/finding_1/decision",
            {"json": {"decision": "approved"}},
        ),
        (
            "/api/v1/reviews/review_1/findings/finding_1/decision",
            {"json": {"decision": "approved", "expected_version": 0}},
        ),
        (
            "/api/v1/reviews/review_1/findings/finding_1/decision",
            {"json": {"decision": "maybe", "expected_version": 1}},
        ),
        (
            "/api/v1/reviews/review_1/compare",
            {"json": {"base_version_id": "ver_1", "target_version_id": "ver_1"}},
        ),
    ],
)
def test_invalid_requests_are_rejected_before_business(client, path, kwargs):
    response = client.post(path, headers={"X-Request-ID": "contract-invalid"}, **kwargs)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["request_id"] == response.headers["X-Request-ID"] == "contract-invalid"


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/documents?limit=0",
        "/api/v1/documents?limit=-1",
        "/api/v1/documents?limit=101",
        "/api/v1/documents?status=done",
        "/api/v1/reviews/review_1/report?format=pdf",
    ],
)
def test_invalid_query_values(client, path):
    response = client.get(path)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    "path,payload,expected_code",
    [
        (
            "/api/v1/chat/session_1/ask",
            {"question": "付款条件是什么？", "version_ids": ["ver_1"], "top_k": 5},
            "SESSION_NOT_FOUND",
        ),
        (
            "/api/v1/reviews/review_1/findings/finding_1/decision",
            {"decision": "approved", "note": "接受该风险", "expected_version": 2},
            "REVIEW_NOT_FOUND",
        ),
        (
            "/api/v1/reviews/review_1/compare",
            {"base_version_id": "ver_1", "target_version_id": "ver_2"},
            "REVIEW_NOT_FOUND",
        ),
        (
            "/api/v1/documents/screen",
            {
                "document_ids": ["doc_001", "doc_002"],
                "query": "金额超过100万元、期限超过一年且风险较高",
                "conditions": {
                    "amount": {"field": "amount", "operator": "gt", "value": 1_000_000},
                    "duration_days": {
                        "field": "duration_days",
                        "operator": "gt",
                        "value": 365,
                    },
                    "risk": "high",
                },
                "deep_analysis": True,
            },
            "DOCUMENT_NOT_FOUND",
        ),
    ],
)
def test_valid_requests_reach_implemented_business_boundaries(
    client, path, payload, expected_code
):
    response = client.post(path, json=payload)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == expected_code


def test_implemented_collection_and_session_endpoints_enforce_scope(client):
    documents = client.get("/api/v1/documents")
    assert documents.status_code == 200
    assert documents.json()["items"] == []

    session = client.post(
        "/api/v1/chat/sessions",
        json={"document_ids": ["doc_contract_scope"]},
    )
    # 匿名用户不能以猜测的 ID 创建指向他人文档的会话。
    assert session.status_code == 404
    assert session.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


def test_idempotency_and_event_headers_are_validated(client):
    payload = {
        "document_ids": ["doc_1"],
        "query": "金额超过100万",
        "conditions": {"amount": {"field": "amount", "operator": "gt", "value": 1_000_000}},
    }
    invalid = client.post(
        "/api/v1/documents/screen",
        json=payload,
        headers={"Idempotency-Key": " "},
    )
    assert invalid.status_code == 422

    valid = client.post(
        "/api/v1/documents/screen",
        json=payload,
        headers={"Idempotency-Key": "screen-001"},
    )
    assert valid.status_code == 404
    assert valid.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"

    event = client.get(
        "/api/v1/tasks/task_1/events",
        headers={"Last-Event-ID": "evt_009"},
    )
    assert event.status_code == 404
    assert event.json()["error"]["code"] == "TASK_NOT_FOUND"
    assert (
        client.get(
            "/api/v1/tasks/task_1/events",
            headers={"Last-Event-ID": " "},
        ).status_code
        == 422
    )


def test_openapi_matches_documented_paths_and_request_shapes(client):
    schema = client.get("/openapi.json").json()
    contract = Path(__file__).resolve().parents[2] / "docs" / "api_contract.md"
    documented = {
        (method.lower(), path)
        for method, path in re.findall(
            r"^(GET|POST)\s+(/api/v1/[^\s?]*)",
            contract.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
    }
    published = {
        (method, path)
        for path, operations in schema["paths"].items()
        if path.startswith("/api/v1/")
        for method in operations
        if method in {"get", "post"}
    }
    assert len(documented) == 26
    assert documented == published

    def resolve(spec):
        if "$ref" in spec:
            return schema["components"]["schemas"][spec["$ref"].rsplit("/", 1)[1]]
        return spec

    upload = schema["paths"]["/api/v1/documents"]["post"]
    upload_body = resolve(upload["requestBody"]["content"]["multipart/form-data"]["schema"])
    assert "file" in upload_body["required"]
    assert {"file", "display_name", "document_group_id"} <= upload_body["properties"].keys()
    assert {"Idempotency-Key", "X-Request-ID"} <= {
        parameter["name"] for parameter in upload["parameters"]
    }

    screening = schema["paths"]["/api/v1/documents/screen"]["post"]
    screening_body = resolve(
        screening["requestBody"]["content"]["application/json"]["schema"]
    )
    assert {"document_ids", "query"} <= set(screening_body["required"])
    assert screening_body["properties"]["document_ids"]["minItems"] == 1
    assert screening_body["properties"]["document_ids"]["maxItems"] == 20

    success_statuses = {
        ("post", "/api/v1/auth/register"): "201",
        ("post", "/api/v1/documents"): "202",
        ("post", "/api/v1/documents/{document_id}/reindex"): "202",
        ("post", "/api/v1/documents/screen"): "202",
        ("post", "/api/v1/chat/sessions"): "201",
        ("post", "/api/v1/reviews"): "202",
    }
    for method, path in documented:
        operation = schema["paths"][path][method]
        expected = success_statuses.get((method, path), "200")
        assert expected in operation["responses"]
        error_schema = operation["responses"]["422"]["content"]["application/json"]["schema"]
        assert error_schema["$ref"].endswith("/ErrorResponse")
