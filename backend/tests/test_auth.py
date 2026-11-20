"""用户注册、登录、令牌恢复和强制认证回归测试。"""

from pathlib import Path
import shutil
import time
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app.core.config import Settings
from app.core.middleware import wrap_http_app
from app.main import create_app


@pytest.fixture
def auth_client():
    """为每个认证测试创建隔离的 SQLite 和对象存储目录。"""

    workspace = Path(__file__).resolve().parents[1]
    storage_parent = (workspace / ".test-storage").resolve()
    root = (storage_parent / uuid4().hex).resolve()
    root.mkdir(parents=True)
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{(root / 'auth.db').as_posix()}",
        upload_dir=root / "uploads",
        embedding_provider="disabled",
        auth_required=True,
        jwt_secret="test-secret-with-at-least-32-characters",
        task_workers=1,
    )
    application = create_app(settings)
    try:
        with TestClient(wrap_http_app(application)) as client:
            yield client
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_register_login_and_current_user(auth_client):
    """注册后可直接使用令牌，并能通过登录重新签发令牌。"""

    registered = auth_client.post(
        "/api/v1/auth/register",
        json={
            "email": "Admin@Example.com",
            "password": "correct-horse-battery",
            "display_name": "合同管理员",
        },
    )
    assert registered.status_code == 201
    body = registered.json()
    assert body["user"]["email"] == "admin@example.com"
    assert body["token_type"] == "bearer"
    token = body["access_token"]

    current = auth_client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert current.status_code == 200
    assert current.json()["display_name"] == "合同管理员"

    logged_in = auth_client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.com", "password": "correct-horse-battery"},
    )
    assert logged_in.status_code == 200
    assert logged_in.json()["user"]["user_id"] == body["user"]["user_id"]


def test_auth_errors_and_required_business_access(auth_client):
    """重复注册、错误密码和未登录业务访问返回统一错误结构。"""

    payload = {"email": "user@example.com", "password": "correct-horse-battery"}
    assert auth_client.post("/api/v1/auth/register", json=payload).status_code == 201
    duplicate = auth_client.post("/api/v1/auth/register", json=payload)
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "EMAIL_ALREADY_REGISTERED"

    invalid = auth_client.post(
        "/api/v1/auth/login",
        json={"email": payload["email"], "password": "wrong-password"},
    )
    assert invalid.status_code == 401
    assert invalid.json()["error"]["code"] == "INVALID_CREDENTIALS"

    protected = auth_client.get("/api/v1/documents")
    assert protected.status_code == 401
    assert protected.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


def test_documents_are_scoped_to_authenticated_user(auth_client):
    """用户 A 创建的文档不会出现在用户 B 的列表或详情接口中。"""

    first = auth_client.post(
        "/api/v1/auth/register",
        json={"email": "owner-a@example.com", "password": "correct-horse-battery"},
    ).json()
    upload = auth_client.post(
        "/api/v1/documents",
        headers={"Authorization": f"Bearer {first['access_token']}"},
        files={
            "file": (
                "contract.pdf",
                (Path(__file__).parent / "fixtures" / "contract_simple.pdf").read_bytes(),
                "application/pdf",
            )
        },
    )
    assert upload.status_code == 202
    document_id = upload.json()["document_id"]
    task_id = upload.json()["task_id"]
    for _ in range(100):
        task = auth_client.get(
            f"/api/v1/tasks/{task_id}",
            headers={"Authorization": f"Bearer {first['access_token']}"},
        ).json()
        if task["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.01)
    assert task["status"] == "succeeded"

    second = auth_client.post(
        "/api/v1/auth/register",
        json={"email": "owner-b@example.com", "password": "correct-horse-battery"},
    ).json()
    second_headers = {"Authorization": f"Bearer {second['access_token']}"}
    assert auth_client.get("/api/v1/documents", headers=second_headers).json()["items"] == []
    hidden = auth_client.get(f"/api/v1/documents/{document_id}", headers=second_headers)
    assert hidden.status_code == 404


def test_upload_idempotency_key_is_scoped_to_user(auth_client):
    """同一个客户端幂等键可重放，但不能跨用户复用上传结果。"""

    fixture = (Path(__file__).parent / "fixtures" / "contract_simple.pdf").read_bytes()
    first = auth_client.post(
        "/api/v1/auth/register",
        json={"email": "idempotency-a@example.com", "password": "correct-horse-battery"},
    ).json()
    first_headers = {
        "Authorization": f"Bearer {first['access_token']}",
        "Idempotency-Key": "shared-upload-key",
    }
    first_upload = auth_client.post(
        "/api/v1/documents",
        headers=first_headers,
        files={"file": ("contract.pdf", fixture, "application/pdf")},
    )
    replay = auth_client.post(
        "/api/v1/documents",
        headers=first_headers,
        files={"file": ("contract.pdf", fixture, "application/pdf")},
    )
    assert first_upload.status_code == 202
    assert replay.status_code == 202
    assert replay.json() == first_upload.json()

    second = auth_client.post(
        "/api/v1/auth/register",
        json={"email": "idempotency-b@example.com", "password": "correct-horse-battery"},
    ).json()
    second_upload = auth_client.post(
        "/api/v1/documents",
        headers={
            "Authorization": f"Bearer {second['access_token']}",
            "Idempotency-Key": "shared-upload-key",
        },
        files={"file": ("contract.pdf", fixture, "application/pdf")},
    )
    assert second_upload.status_code == 202
    assert second_upload.json()["document_id"] != first_upload.json()["document_id"]
