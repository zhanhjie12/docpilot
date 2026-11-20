"""Day 3 acceptance tests for structured PDF/DOCX parsing."""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import pymupdf
import pytest
from docx import Document
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.middleware import wrap_http_app
from app.infrastructure.database import SQLiteDatabase
from app.infrastructure.storage import LocalObjectStorage
from app.main import create_app
from app.services.document_ingestion import DocumentIngestionService
from app.services.document_parser import DocumentParseError, parse_document


FIXTURES = Path(__file__).resolve().parent / "fixtures"
PDF_FIXTURE = FIXTURES / "contract_simple.pdf"
DOCX_FIXTURE = FIXTURES / "contract_simple.docx"


@pytest.fixture
def ingestion_root() -> Path:
    root = Path(__file__).resolve().parents[1] / ".test-ingestion" / uuid4().hex
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


class RecordingTaskContext:
    """Minimal TaskContext test double used by the synchronous ingestion call."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def update(
        self,
        progress: int,
        node: str,
        message: str,
        event_type: str = "node.completed",
        data: dict[str, Any] | None = None,
    ) -> None:
        self.events.append(
            {
                "progress": progress,
                "node": node,
                "message": message,
                "event_type": event_type,
                "data": data or {},
            }
        )


def _assert_absolute_block_locations(parsed: Any) -> None:
    blocks = [block for page in parsed.pages for block in page.blocks]
    assert blocks, "the parser must return evidence-bearing blocks"

    previous_start = -1
    for block in blocks:
        assert block.block_type in {"heading", "paragraph", "table"}
        assert block.text
        assert 0 <= block.char_start < block.char_end <= len(parsed.text)
        assert parsed.text[block.char_start : block.char_end] == block.text
        assert block.char_start >= previous_start
        assert block.source_ref
        previous_start = block.char_start


def _blank_pdf() -> bytes:
    pdf = pymupdf.open()
    pdf.new_page()
    data = pdf.tobytes()
    pdf.close()
    return data


def _partially_blank_pdf() -> bytes:
    pdf = pymupdf.open()
    first_page = pdf.new_page()
    first_page.insert_text((72, 72), "Selectable contract text")
    pdf.new_page()
    data = pdf.tobytes()
    pdf.close()
    return data


def _blank_docx() -> bytes:
    stream = io.BytesIO()
    Document().save(stream)
    return stream.getvalue()


def _service(tmp_path: Path) -> tuple[DocumentIngestionService, SQLiteDatabase]:
    database = SQLiteDatabase(tmp_path / "docpilot-test.db")
    database.initialize()
    storage_root = tmp_path / "uploads"
    storage_root.mkdir()
    settings = Settings(
        _env_file=None,
        database_url="sqlite:///unused.db",
        upload_dir=storage_root,
    )
    return DocumentIngestionService(database, LocalObjectStorage(storage_root), settings), database


def test_pdf_fixture_preserves_pages_titles_blocks_and_coordinates() -> None:
    parsed = parse_document(PDF_FIXTURE, "application/pdf", PDF_FIXTURE.name)

    assert parsed.parser_name == "pymupdf"
    assert parsed.parser_version
    assert parsed.quality_warnings == []
    assert [page.page for page in parsed.pages] == [1, 2]
    assert "Supplier Agreement" in parsed.pages[0].title_candidates
    assert "2. Liability" in parsed.pages[1].title_candidates

    _assert_absolute_block_locations(parsed)

    pdf_blocks = [block for page in parsed.pages for block in page.blocks]
    assert any(block.block_type == "heading" for block in pdf_blocks)
    assert all(block.bbox is not None and len(block.bbox) == 4 for block in pdf_blocks)
    assert all(
        block.bbox[0] < block.bbox[2] and block.bbox[1] < block.bbox[3]
        for block in pdf_blocks
    )
    assert parsed.pages[1].blocks[0].char_start > len(parsed.pages[0].text)


def test_docx_fixture_preserves_body_order_and_structural_block_types() -> None:
    parsed = parse_document(
        DOCX_FIXTURE,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        DOCX_FIXTURE.name,
    )

    assert parsed.parser_name == "python-docx"
    assert parsed.parser_version
    assert parsed.quality_warnings
    assert len(parsed.pages) == 1
    page = parsed.pages[0]
    assert page.page == 1
    assert page.title_candidates == ["Supplier Agreement", "Commercial Terms"]
    assert [block.block_type for block in page.blocks] == [
        "heading",
        "paragraph",
        "paragraph",
        "heading",
        "table",
        "paragraph",
    ]

    table = next(block for block in page.blocks if block.block_type == "table")
    assert table.table_rows == [
        ["Term", "Value"],
        ["Payment period", "30 days"],
    ]
    assert table.bbox is None
    _assert_absolute_block_locations(parsed)


def test_pdf_keeps_empty_page_number_and_records_quality_warning() -> None:
    parsed = parse_document(_partially_blank_pdf(), "application/pdf", "partial.pdf")

    assert [page.page for page in parsed.pages] == [1, 2]
    assert parsed.pages[1].text == ""
    assert parsed.quality_warnings == ["pages without selectable text: 2"]


@pytest.mark.parametrize(
    ("source", "mime_type", "filename"),
    [
        (b"", "application/pdf", "empty.pdf"),
        (_blank_pdf(), "application/pdf", "blank.pdf"),
        (
            _blank_docx(),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "blank.docx",
        ),
    ],
    ids=["empty-bytes", "blank-pdf", "blank-docx"],
)
def test_empty_or_textless_documents_raise_parse_error(
    source: bytes,
    mime_type: str,
    filename: str,
) -> None:
    with pytest.raises(DocumentParseError) as error:
        parse_document(source, mime_type, filename)

    assert error.value.code == "PARSE_FAILED"


def test_ingestion_persists_parser_metadata_for_successful_pdf(ingestion_root: Path) -> None:
    service, database = _service(ingestion_root)
    content = PDF_FIXTURE.read_bytes()
    created = service.create_version(
        filename=PDF_FIXTURE.name,
        display_name="Simple supplier agreement",
        document_group_id=None,
        mime_type="application/pdf",
        data=content,
    )

    result = service.parse_version(
        RecordingTaskContext(),
        {"document_id": created["document_id"], "version_id": created["version_id"]},
    )

    document = database.one(
        "SELECT * FROM documents WHERE document_id=?",
        (created["document_id"],),
    )
    version = database.one(
        "SELECT * FROM document_versions WHERE version_id=?",
        (created["version_id"],),
    )
    assert document is not None and version is not None
    assert result["page_count"] == document["page_count"] == version["page_count"] == 2
    assert document["parser_name"] == version["parser_name"] == "pymupdf"
    assert document["parser_version"] == version["parser_version"]
    assert document["parser_version"]
    assert json.loads(document["quality_warnings"]) == []
    assert json.loads(version["quality_warnings"]) == []
    persisted = json.loads(version["parsed_content"])
    assert persisted["page_count"] == 2
    assert persisted["pages"][0]["blocks"][0]["bbox"]
    first_block = persisted["pages"][0]["blocks"][0]
    assert version["extracted_text"][first_block["char_start"] : first_block["char_end"]] == first_block["text"]


def test_ingestion_marks_document_and_version_failed_with_stable_reason(ingestion_root: Path) -> None:
    service, database = _service(ingestion_root)
    created = service.create_version(
        filename="broken.pdf",
        display_name=None,
        document_group_id=None,
        mime_type="application/pdf",
        data=b"%PDF-1.7\nthis is not a valid PDF\n%%EOF",
    )

    with pytest.raises(DocumentParseError):
        service.parse_version(
            RecordingTaskContext(),
            {"document_id": created["document_id"], "version_id": created["version_id"]},
        )

    document = database.one(
        "SELECT * FROM documents WHERE document_id=?",
        (created["document_id"],),
    )
    version = database.one(
        "SELECT * FROM document_versions WHERE version_id=?",
        (created["version_id"],),
    )
    assert document is not None and version is not None
    for record in (document, version):
        assert record["status"] == "failed"
        assert record["failure_code"] == "PARSE_FAILED"
        assert record["failure_message"]


def test_upload_api_runs_parse_task_and_exposes_structured_content(ingestion_root: Path) -> None:
    database_path = (ingestion_root / "api.db").resolve()
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{database_path.as_posix()}",
        upload_dir=ingestion_root / "uploads",
    )
    application = create_app(settings)

    with TestClient(wrap_http_app(application)) as client:
        response = client.post(
            "/api/v1/documents",
            files={
                "file": (
                    PDF_FIXTURE.name,
                    PDF_FIXTURE.read_bytes(),
                    "application/pdf",
                )
            },
        )
        assert response.status_code == 202
        accepted = response.json()

        task = {}
        for _ in range(200):
            task_response = client.get(f"/api/v1/tasks/{accepted['task_id']}")
            assert task_response.status_code == 200
            task = task_response.json()
            if task["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.01)

        assert task["status"] == "succeeded"
        detail_response = client.get(f"/api/v1/documents/{accepted['document_id']}")
        assert detail_response.status_code == 200
        detail = detail_response.json()
        assert detail["status"] == "indexed"
        assert detail["page_count"] == 2
        assert detail["latest_version"]["parsed_content"]["pages"][0]["blocks"]


def test_chunk_publication_rolls_back_as_one_unit(ingestion_root: Path) -> None:
    service, database = _service(ingestion_root)
    created = service.create_version(
        filename=PDF_FIXTURE.name,
        display_name=None,
        document_group_id=None,
        mime_type="application/pdf",
        data=PDF_FIXTURE.read_bytes(),
    )
    database.run(
        "INSERT INTO chunks(chunk_id,document_id,version_id,page,text,char_start,char_end,"
        "content_sha256,section_path,token_estimate,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            "previous_chunk",
            created["document_id"],
            created["version_id"],
            1,
            "old",
            0,
            3,
            "old_hash",
            None,
            1,
            "2026-01-01T00:00:00Z",
        ),
    )
    database.run(
        "CREATE TRIGGER reject_second_page BEFORE INSERT ON chunks "
        "WHEN NEW.page=2 BEGIN SELECT RAISE(ABORT, 'simulated chunk failure'); END"
    )

    with pytest.raises(sqlite3.IntegrityError, match="simulated chunk failure"):
        service.parse_version(
            RecordingTaskContext(),
            {"document_id": created["document_id"], "version_id": created["version_id"]},
        )

    chunks = database.all(
        "SELECT chunk_id,text FROM chunks WHERE version_id=?",
        (created["version_id"],),
    )
    document = database.one(
        "SELECT status FROM documents WHERE document_id=?",
        (created["document_id"],),
    )
    version = database.one(
        "SELECT status,parsed_content FROM document_versions WHERE version_id=?",
        (created["version_id"],),
    )
    assert chunks == [{"chunk_id": "previous_chunk", "text": "old"}]
    assert document is not None and document["status"] == "parsed"
    assert version is not None and version["status"] == "parsed"
    assert json.loads(version["parsed_content"])["page_count"] == 2


def test_document_and_version_status_transition_is_atomic(ingestion_root: Path) -> None:
    service, database = _service(ingestion_root)
    created = service.create_version(
        filename=PDF_FIXTURE.name,
        display_name=None,
        document_group_id=None,
        mime_type="application/pdf",
        data=PDF_FIXTURE.read_bytes(),
    )
    database.run(
        "CREATE TRIGGER reject_document_parsing BEFORE UPDATE OF status ON documents "
        "WHEN NEW.status='parsing' BEGIN SELECT RAISE(ABORT, 'simulated status failure'); END"
    )

    with pytest.raises(sqlite3.IntegrityError, match="simulated status failure"):
        service.documents.mark_parsing(created["document_id"], created["version_id"])

    document = database.one(
        "SELECT status FROM documents WHERE document_id=?",
        (created["document_id"],),
    )
    version = database.one(
        "SELECT status FROM document_versions WHERE version_id=?",
        (created["version_id"],),
    )
    assert document is not None and document["status"] == "uploaded"
    assert version is not None and version["status"] == "uploaded"
