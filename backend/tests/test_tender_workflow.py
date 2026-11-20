"""Tender evidence alignment must preserve source roles and user scope."""

from __future__ import annotations

import asyncio
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from app.api.routes.tenders import check_tender, list_tender_requirements
from app.core.exceptions import AppError
from app.domain.schemas import TenderCheckRequest
from app.infrastructure.database import SQLiteDatabase, utc_now


@pytest.fixture
def tender_db():
    path = Path(__file__).parent / f".tender_{uuid4().hex}.db"
    db = SQLiteDatabase(path)
    db.initialize()
    now = utc_now()
    for kind, text in (
        ("tender", "投标人须提供近三年同类项目业绩证明。"),
        ("bid", "我方提供近三年同类项目业绩证明，详见附件。"),
    ):
        doc_id, version_id = f"doc_{kind}", f"ver_{kind}"
        db.run(
            "INSERT INTO documents(document_id,owner_id,display_name,filename,document_type,mime_type,status,latest_version_id,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (doc_id, "owner", doc_id, f"{kind}.pdf", kind, "application/pdf", "indexed", version_id, now, now),
        )
        db.run(
            "INSERT INTO document_versions(version_id,document_id,version_number,filename,mime_type,size_bytes,content_sha256,object_key,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (version_id, doc_id, 1, f"{kind}.pdf", "application/pdf", len(text), sha256(text.encode()).hexdigest(), doc_id, "indexed", now, now),
        )
        db.run(
            "INSERT INTO chunks(chunk_id,document_id,version_id,page,text,char_start,char_end,content_sha256,section_path,token_estimate,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (f"chunk_{kind}", doc_id, version_id, 1, text, 0, len(text), sha256(text.encode()).hexdigest(), "资格要求", len(text), now),
        )
    try:
        yield db
    finally:
        for suffix in ("", "-wal", "-shm"):
            path.with_name(path.name + suffix).unlink(missing_ok=True)


def _check(db: SQLiteDatabase, owner: str = "owner"):
    return asyncio.run(check_tender(
        TenderCheckRequest(
            tender_version_id="ver_tender", bid_version_id="ver_bid",
            requirement_queries=["近三年同类项目业绩证明"],
        ),
        db=db, embedding_provider=None, current_user={"user_id": owner},
    ))


def test_tender_pairs_replayable_citations_without_claiming_compliance(tender_db: SQLiteDatabase):
    result = _check(tender_db)
    item = result["items"][0]
    assert result["needs_confirmation"] is True
    assert item["status"] == "needs_review"
    assert {cite["version_id"] for cite in item["tender_citations"]} == {"ver_tender"}
    assert {cite["version_id"] for cite in item["bid_citations"]} == {"ver_bid"}


def test_tender_rejects_foreign_owner_and_wrong_type(tender_db: SQLiteDatabase):
    with pytest.raises(AppError) as foreign:
        _check(tender_db, owner="other")
    assert foreign.value.code == "TENDER_VERSION_NOT_FOUND"
    tender_db.run("UPDATE documents SET document_type='contract' WHERE document_id='doc_bid'")
    with pytest.raises(AppError) as wrong_type:
        _check(tender_db)
    assert wrong_type.value.code == "TENDER_SCOPE_INVALID"


def test_requirement_candidates_keep_tender_citation(tender_db: SQLiteDatabase):
    result = list_tender_requirements("ver_tender", db=tender_db,
                                      current_user={"user_id": "owner"})
    assert result["needs_confirmation"] is True
    assert result["items"][0]["status"] == "needs_review"
    assert "近三年同类项目业绩证明" in result["items"][0]["requirement_query"]
    assert result["items"][0]["citation"]["version_id"] == "ver_tender"
    with pytest.raises(AppError) as foreign:
        list_tender_requirements("ver_tender", db=tender_db,
                                 current_user={"user_id": "other"})
    assert foreign.value.code == "TENDER_VERSION_NOT_FOUND"
