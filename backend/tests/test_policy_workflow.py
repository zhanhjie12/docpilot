"""Policy questions must use only the authorized effective version."""

from __future__ import annotations

import asyncio
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from app.api.routes.policies import ask_policy, set_policy_metadata
from app.core.exceptions import AppError
from app.domain.schemas import PolicyAskRequest, PolicyMetadataRequest
from app.infrastructure.database import SQLiteDatabase, utc_now
from app.infrastructure.llm.client import MockLLMProvider


@pytest.fixture
def policy_db():
    path = Path(__file__).parent / f".policy_{uuid4().hex}.db"
    db = SQLiteDatabase(path)
    db.initialize()
    now = utc_now()
    for index, text in ((1, "采购审批金额超过100万元须提交总经理审批。"),
                        (2, "采购审批金额超过50万元须提交总经理审批。")):
        doc_id, version_id = f"doc_policy_{index}", f"ver_policy_{index}"
        db.run(
            "INSERT INTO documents(document_id,owner_id,display_name,filename,document_type,mime_type,status,latest_version_id,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (doc_id, "user_1", doc_id, "policy.pdf", "policy", "application/pdf", "indexed", version_id, now, now),
        )
        db.run(
            "INSERT INTO document_versions(version_id,document_id,version_number,filename,mime_type,size_bytes,content_sha256,object_key,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (version_id, doc_id, 1, "policy.pdf", "application/pdf", len(text), sha256(text.encode()).hexdigest(), doc_id, "indexed", now, now),
        )
        db.run(
            "INSERT INTO chunks(chunk_id,document_id,version_id,page,text,char_start,char_end,content_sha256,section_path,token_estimate,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (f"chunk_{index}", doc_id, version_id, 1, text, 0, len(text), sha256(text.encode()).hexdigest(), "审批", len(text), now),
        )
    try:
        yield db
    finally:
        for suffix in ("", "-wal", "-shm"):
            path.with_name(path.name + suffix).unlink(missing_ok=True)


def _metadata(db: SQLiteDatabase, index: int, start: str, end: str | None):
    return set_policy_metadata(
        f"ver_policy_{index}",
        PolicyMetadataRequest(
            policy_family_id="procurement", issuer="采购部", org_scope="总部",
            effective_from=start, effective_to=end, status="active",
        ),
        db=db, current_user={"user_id": "user_1"},
    )


def _ask(db: SQLiteDatabase, as_of: str, owner: str = "user_1"):
    return asyncio.run(ask_policy(
        PolicyAskRequest(
            document_ids=["doc_policy_1", "doc_policy_2"], org_scope="总部",
            as_of_date=as_of, question="采购审批金额超过多少需审批？",
        ),
        db=db, embedding_provider=None, llm_provider=MockLLMProvider(),
        current_user={"user_id": owner},
    ))


def test_ask_selects_effective_version_and_replays_citation(policy_db: SQLiteDatabase):
    _metadata(policy_db, 1, "2026-01-01", "2026-07-01")
    _metadata(policy_db, 2, "2026-07-01", None)
    old = _ask(policy_db, "2026-06-30")
    new = _ask(policy_db, "2026-07-01")
    assert old["status"] == new["status"] == "answered"
    assert {item["version_id"] for item in old["citations"]} == {"ver_policy_1"}
    assert {item["version_id"] for item in new["citations"]} == {"ver_policy_2"}
    assert "100万元" in old["answer"] and "50万元" in new["answer"]


def test_overlap_and_foreign_owner_are_rejected(policy_db: SQLiteDatabase):
    _metadata(policy_db, 1, "2026-01-01", None)
    _metadata(policy_db, 2, "2026-07-01", None)
    with pytest.raises(AppError) as overlap:
        _ask(policy_db, "2026-08-01")
    assert overlap.value.code == "POLICY_VERSION_CONFLICT"
    with pytest.raises(AppError) as foreign:
        _ask(policy_db, "2026-08-01", owner="user_2")
    assert foreign.value.code == "POLICY_SCOPE_INVALID"
