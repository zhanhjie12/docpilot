"""Readiness reflects storage access, including failures after startup."""

from pathlib import Path
import shutil
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app.core.config import Settings
from app.core.middleware import wrap_http_app
from app.infrastructure import storage as storage_module
from app.infrastructure.storage import LocalObjectStorage
from app.main import create_app


@pytest.fixture
def storage_root():
    workspace = Path(__file__).resolve().parents[1]
    parent = (workspace / ".test-storage").resolve()
    root = (parent / uuid4().hex).resolve()
    assert root.is_relative_to(workspace) and root.parent == parent
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        assert root.resolve().is_relative_to(workspace) and root.resolve().parent == parent
        shutil.rmtree(root)


def test_probe_preserves_objects_and_cleans_temporary_file(storage_root):
    document = storage_root / "contract.pdf"
    document.write_bytes(b"original document")

    LocalObjectStorage(storage_root).check_ready()

    assert document.read_bytes() == b"original document"
    assert list(storage_root.iterdir()) == [document]


def test_probe_cleans_temporary_file_when_read_fails(storage_root, monkeypatch):
    original_factory = storage_module.NamedTemporaryFile

    def broken_read():
        raise OSError("simulated read failure")

    def make_unreadable_probe(*args, **kwargs):
        probe = original_factory(*args, **kwargs)
        probe.read = broken_read
        return probe

    monkeypatch.setattr(storage_module, "NamedTemporaryFile", make_unreadable_probe)

    with pytest.raises(OSError, match="simulated read failure"):
        LocalObjectStorage(storage_root).check_ready()

    assert list(storage_root.iterdir()) == []


def test_ready_reports_permission_loss_and_recovers(storage_root, monkeypatch):
    api = create_app(Settings(_env_file=None, upload_dir=storage_root))
    original_factory = storage_module.NamedTemporaryFile

    def deny_probe(*args, **kwargs):
        raise PermissionError("simulated loss of write access")

    with TestClient(wrap_http_app(api)) as client:
        assert client.get("/ready").status_code == 200

        monkeypatch.setattr(storage_module, "NamedTemporaryFile", deny_probe)
        response = client.get("/ready")

        assert response.status_code == 503
        error = response.json()["error"]
        assert error["code"] == "SERVICE_NOT_READY"
        assert error["retryable"] is True
        assert error["request_id"] == response.headers["X-Request-ID"]
        assert "simulated loss" not in response.text
        assert client.get("/health").status_code == 200

        monkeypatch.setattr(storage_module, "NamedTemporaryFile", original_factory)
        assert client.get("/ready").json() == {"status": "ready"}

    assert not api.state.ready
    assert not hasattr(api.state, "storage")
    assert list(storage_root.iterdir()) == []


def test_startup_probe_failure_never_publishes_ready_storage(storage_root, monkeypatch):
    api = create_app(Settings(_env_file=None, upload_dir=storage_root))

    def deny_probe(*args, **kwargs):
        raise PermissionError("simulated startup write failure")

    with monkeypatch.context() as patch:
        patch.setattr(storage_module, "NamedTemporaryFile", deny_probe)
        with pytest.raises(PermissionError, match="simulated startup write failure"):
            with TestClient(wrap_http_app(api)):
                pass

    assert not api.state.ready
    assert not hasattr(api.state, "storage")
    assert list(storage_root.iterdir()) == []

    # A failed initialization must not poison a later startup after access recovers.
    with TestClient(wrap_http_app(api)) as client:
        assert client.get("/ready").status_code == 200
