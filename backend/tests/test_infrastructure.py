"""Tests for replaceable infrastructure boundaries used by the MVP."""

import io
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from app.infrastructure.storage import LocalObjectStorage


@pytest.fixture
def storage_root():
    root = Path(__file__).resolve().parents[1] / ".test-storage" / uuid4().hex
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_local_storage_round_trip(storage_root):
    storage = LocalObjectStorage(storage_root)

    storage.put("tenant/document.bin", io.BytesIO(b"document"))

    with storage.open("tenant/document.bin") as stream:
        assert stream.read() == b"document"


def test_local_storage_rejects_path_escape(storage_root):
    storage = LocalObjectStorage(storage_root)

    with pytest.raises(ValueError):
        storage.put("../outside.bin", io.BytesIO(b"blocked"))
