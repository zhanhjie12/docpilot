"""HTTP entry-point contracts, including errors and startup/shutdown."""

import asyncio
import io
from pathlib import Path
import shutil
import traceback
from uuid import UUID, uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, field_validator
import pytest
from starlette.responses import StreamingResponse

from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.middleware import wrap_http_app
from app.main import create_app


@pytest.fixture
def application():
    # Keep test files in the workspace (the system temp directory may be restricted).
    workspace = Path(__file__).resolve().parents[1]
    storage_parent = (workspace / ".test-storage").resolve()
    root = (storage_parent / uuid4().hex).resolve()
    assert root.is_relative_to(workspace) and root.parent == storage_parent
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{(root / 'docpilot-test.db').as_posix()}",
        upload_dir=root / "uploads",
        cors_origins="http://localhost:5173",
        embedding_provider="disabled",
        task_workers=1,
    )
    api = create_app(settings)
    try:
        yield api
    finally:
        assert root.resolve().is_relative_to(workspace) and root.resolve().parent == storage_parent
        shutil.rmtree(root, ignore_errors=True)


def test_health_liveness_uses_isolated_application(application):
    with TestClient(wrap_http_app(application)) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert UUID(response.headers["X-Request-ID"]).version == 4


def test_storage_lifecycle_and_readiness(application):
    http_app = wrap_http_app(application)
    with TestClient(http_app) as client:
        assert client.get("/ready").json() == {"status": "ready"}
        application.state.storage.put("sample.txt", io.BytesIO(b"sample"))
        with application.state.storage.open("sample.txt") as stream:
            assert stream.read() == b"sample"
    assert application.state.ready is False
    assert not hasattr(application.state, "storage")
    with TestClient(http_app) as client:
        assert client.get("/ready").status_code == 200
        with application.state.storage.open("sample.txt") as stream:
            assert stream.read() == b"sample"


def test_ready_before_startup(application):
    client = TestClient(wrap_http_app(application))
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_NOT_READY"


def test_fastapi_instance_alone_has_request_id(application):
    # create_app() remains usable for mounted tests without the outer ASGI wrapper.
    with TestClient(application) as client:
        response = client.post(
            "/api/v1/documents/screen",
            json={"document_ids": ["doc_1"], "query": "金额超过100万", "conditions": {"amount": {"field": "amount", "operator": "gt", "value": 1000000}}},
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"
    assert UUID(response.json()["error"]["request_id"]).version == 4


@pytest.mark.parametrize("method,path,kwargs,expected_status,code", [
    ("GET", "/not-a-route", {}, 404, "NOT_FOUND"),
    ("DELETE", "/health", {}, 405, "METHOD_NOT_ALLOWED"),
    ("POST", "/api/v1/documents", {"files": {"file": ("test.txt", b"not a contract", "text/plain")}}, 400, "UNSUPPORTED_FILE"),
    ("POST", "/api/v1/documents/screen", {"json": {"document_ids": ["doc_1"], "query": "金额超过100万", "conditions": {"amount": {"field": "amount", "operator": "gt", "value": 1000000}}}}, 404, "DOCUMENT_NOT_FOUND"),
    ("GET", "/api/v1/tasks/task_1/screening-result", {}, 404, "TASK_NOT_FOUND"),
    ("POST", "/api/v1/chat/session_1/ask", {"json": {"document_ids": ["doc_1"], "question": "付款条件是什么？"}}, 404, "SESSION_NOT_FOUND"),
    ("POST", "/api/v1/reviews", {"json": {"document_ids": ["doc_1"]}}, 404, "DOCUMENT_NOT_FOUND"),
])
def test_error_contract(application, method, path, kwargs, expected_status, code):
    with TestClient(wrap_http_app(application)) as client:
        response = client.request(method, path, headers={
            "X-Request-ID": "req_contract", "Origin": "http://localhost:5173",
        }, **kwargs)
    assert response.status_code == expected_status
    error = response.json()["error"]
    assert set(error) == {"code", "message", "details", "retryable", "request_id"}
    assert error["code"] == code
    assert error["request_id"] == response.headers["X-Request-ID"] == "req_contract"
    assert response.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"
    if expected_status == 405:
        assert "GET" in response.headers["Allow"]


def test_validation_business_and_unexpected_errors(application):
    class Input(BaseModel):
        value: str

        @field_validator("value")
        @classmethod
        def reject_value(cls, value):
            raise ValueError(f"private input: {value}")

    @application.post("/test/validate")
    async def validate(payload: Input):
        return payload

    @application.get("/test/business")
    async def business():
        raise AppError(
            code="REVIEW_CONFLICT", message="审阅版本已更新", status_code=409,
            details={"expected_version": 2},
        )

    @application.get("/test/http")
    async def http_error():
        raise HTTPException(401, "需要身份验证", headers={"WWW-Authenticate": "Bearer"})

    @application.get("/test/crash")
    async def crash():
        raise RuntimeError("private document content")

    with TestClient(wrap_http_app(application), raise_server_exceptions=False) as client:
        invalid = client.post("/test/validate", json={"value": "secret-contract"})
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"
        assert "secret-contract" not in invalid.text
        assert "private input" not in invalid.text

        conflict = client.get("/test/business")
        assert conflict.status_code == 409
        assert conflict.json()["error"]["details"] == {"expected_version": 2}
        assert client.get("/test/http").headers["WWW-Authenticate"] == "Bearer"

        failed = client.get("/test/crash", headers={
            "Origin": "http://localhost:5173", "X-Request-ID": "req_crash",
        })
        assert failed.status_code == 500
        assert failed.json()["error"]["code"] == "INTERNAL_ERROR"
        assert failed.json()["error"]["retryable"] is False
        assert failed.headers["X-Request-ID"] == failed.json()["error"]["request_id"] == "req_crash"
        assert failed.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"
        assert "X-Request-ID" in failed.headers["Access-Control-Expose-Headers"]
        assert "private document" not in failed.text

    # ASGI servers log the propagated exception even after a 500 response was sent.
    with TestClient(wrap_http_app(application)) as client:
        with pytest.raises(RuntimeError) as caught:
            client.get("/test/crash", headers={"X-Request-ID": "req_server_log"})
    server_trace = "".join(traceback.format_exception(caught.value))
    assert "req_server_log" in server_trace
    assert "private document content" not in server_trace


def test_cors_preflight_and_invalid_request_id(application):
    with TestClient(wrap_http_app(application)) as client:
        headers = {
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,idempotency-key,last-event-id,x-request-id",
        }
        allowed = client.options("/api/v1/documents", headers=headers)
        assert allowed.status_code == 200
        assert UUID(allowed.headers["X-Request-ID"]).version == 4

        rejected = client.options("/api/v1/documents", headers={
            **headers, "Origin": "https://untrusted.invalid",
        })
        assert rejected.status_code == 400
        assert rejected.json()["error"]["code"] == "CORS_REJECTED"
        assert rejected.json()["error"]["request_id"] == rejected.headers["X-Request-ID"]
        assert "Access-Control-Allow-Origin" not in rejected.headers

        response = client.get("/health", headers={"X-Request-ID": "a" * 129})
        assert UUID(response.headers["X-Request-ID"]).version == 4


def test_openapi_routes_and_error_schema(application):
    with TestClient(wrap_http_app(application)) as client:
        assert client.get("/docs").status_code == 200
        schema = client.get("/openapi.json").json()
    assert {tag["name"] for tag in schema["tags"]} == {
            "health", "auth", "documents", "tasks", "screening", "chat", "reviews", "policies", "tenders",
    }
    assert "/api/v1/tasks/{task_id}/events" in schema["paths"]
    assert "/api/v1/reviews/{review_id}/findings/{finding_id}/decision" in schema["paths"]
    upload_responses = schema["paths"]["/api/v1/documents"]["post"]["responses"]
    assert "202" in upload_responses
    assert upload_responses["422"]["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorResponse")
    assert "200" not in upload_responses

    assert "200" in schema["paths"]["/api/v1/documents"]["get"]["responses"]
    assert "202" in schema["paths"]["/api/v1/documents/screen"]["post"]["responses"]
    assert "201" in schema["paths"]["/api/v1/chat/sessions"]["post"]["responses"]
    assert "202" in schema["paths"]["/api/v1/reviews"]["post"]["responses"]


def test_sse_first_chunk_is_forwarded_before_producer_finishes(application):
    # A buffering middleware would deadlock: the producer waits until send receives chunk 1.
    async def run():
        delivered = asyncio.Event()
        messages = []

        @application.get("/test/events")
        async def events():
            async def generate():
                yield b"data: first\n\n"
                await asyncio.wait_for(delivered.wait(), timeout=2)
                yield b"data: second\n\n"
            return StreamingResponse(generate(), media_type="text/event-stream")

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)
            if message["type"] == "http.response.body" and message.get("body") == b"data: first\n\n":
                delivered.set()

        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "scheme": "http", "method": "GET",
            "path": "/test/events", "raw_path": b"/test/events", "query_string": b"",
            "root_path": "", "headers": [], "server": ("test", 80), "client": ("test", 1234),
        }
        await asyncio.wait_for(wrap_http_app(application)(scope, receive, send), timeout=3)
        assert delivered.is_set()
        assert b"x-request-id" in dict(messages[0]["headers"])
        assert b"data: second\n\n" in [message.get("body") for message in messages]

    asyncio.run(run())


def test_startup_failure_does_not_mark_ready(application):
    upload_dir = application.state.settings.upload_dir
    upload_dir.parent.mkdir(parents=True, exist_ok=True)
    upload_dir.write_text("this is a file, not a directory", encoding="utf-8")
    try:
        with pytest.raises(FileExistsError):
            with TestClient(wrap_http_app(application)):
                pass
        assert application.state.ready is False
        assert not hasattr(application.state, "storage")
    finally:
        upload_dir.unlink()
