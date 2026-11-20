"""Durable queued-task recovery tests."""

from __future__ import annotations

import time
from pathlib import Path
import shutil
import sqlite3
from uuid import uuid4

import pytest

from app.infrastructure.database import SQLiteDatabase
from app.infrastructure.tasks import TaskManager
from app.infrastructure.database import json_dumps, utc_now
from app.infrastructure.tasks import TaskContext
from app.agents.state import TaskState
from app.services.review_service import review_runner
from app.api.routes.reviews import close_unresolved_review
from app.domain.schemas import ReviewUnresolvedRequest
from app.core.exceptions import AppError


@pytest.fixture
def recovery_database():
    """Use the repository test-storage folder because the system temp folder is restricted."""

    root = Path(__file__).resolve().parents[1] / ".test-storage" / uuid4().hex
    root.mkdir(parents=True)
    database = SQLiteDatabase(root / "recovery.db")
    database.initialize()
    try:
        yield database
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_recover_queued_submits_the_existing_task_once(recovery_database):
    """Restart recovery reuses the persisted task ID instead of creating another task."""

    database = recovery_database
    first_manager = TaskManager(database, workers=1)
    task_id = "task_recover_once"
    payload = {"review_id": "review_recover_once"}
    first_manager.create(task_id, "review", payload)
    first_manager.close()

    recovered_calls: list[tuple[str, dict[str, object]]] = []
    second_manager = TaskManager(database, workers=1)

    def resolve_runner(task_type: str, stored_payload: dict[str, object]):
        """Return a minimal runner while asserting the persisted task contract."""

        assert task_type == "review"

        def run(_context, runner_payload):
            recovered_calls.append((task_type, runner_payload))
            return {"result_ref": runner_payload["review_id"]}

        return run

    try:
        assert second_manager.recover_queued(resolve_runner) == [task_id]
        # Calling recovery again while the same Future is active must not
        # schedule a duplicate worker for the same durable task.
        assert second_manager.recover_queued(resolve_runner) == []
        for _ in range(100):
            task = second_manager.get(task_id)
            if task and task["status"] == "succeeded":
                break
            time.sleep(0.01)
        assert task and task["status"] == "succeeded"
        assert recovered_calls == [("review", payload)]
        assert database.one("SELECT COUNT(*) AS total FROM tasks")["total"] == 1
        event_types = [row["type"] for row in database.all("SELECT type FROM task_events WHERE task_id=? ORDER BY sequence", (task_id,))]
        assert "task.recovered" in event_types
    finally:
        second_manager.close()


def test_recover_queued_marks_an_unknown_task_type_failed(recovery_database):
    """Unsupported persisted work becomes visible failure instead of a permanent queue entry."""

    database = recovery_database
    manager = TaskManager(database, workers=1)
    try:
        manager.create("task_unknown", "unknown_type", {"value": 1})
        assert manager.recover_queued(lambda _task_type, _payload: None) == []
        task = manager.get("task_unknown")
        assert task and task["status"] == "failed"
        assert task["failure"]["code"] == "TASK_RECOVERY_UNSUPPORTED"
    finally:
        manager.close()


def test_expired_running_review_reuses_task_id_and_fences_old_worker(recovery_database):
    """Only one new worker can claim a review after its lease expires."""

    db = recovery_database
    old = TaskManager(db, workers=1)
    task_id = "task_expired_review"
    payload = {"review_id": "review_expired"}
    old.create(task_id, "review", payload)
    db.run(
        "UPDATE tasks SET status='running',worker_id='dead-worker',lease_expires_at=? WHERE task_id=?",
        ("2020-01-01T00:00:00Z", task_id),
    )
    old.close()
    calls: list[str] = []
    new = TaskManager(db, workers=1)

    def resolve(_task_type, _payload):
        def run(context, _stored):
            calls.append(context.task_id)
            return {"result_ref": "review_expired"}
        return run

    try:
        assert new.recover_expired(resolve) == [task_id]
        assert new.recover_expired(resolve) == []
        for _ in range(100):
            if new.get(task_id)["status"] == "succeeded":
                break
            time.sleep(0.01)
        assert calls == [task_id]
        assert new.get(task_id)["status"] == "succeeded"
        assert new.get(task_id)["result_ref"] == "review_expired"
    finally:
        new.close()


def test_review_runner_rechecks_committed_agent_checkpoint(recovery_database):
    """A completed snapshot without replayable evidence cannot be republished."""

    db = recovery_database
    manager = TaskManager(db, workers=1)
    payload = {"review_id": "review_checkpoint", "document_ids": ["doc_scope"],
               "version_ids": [], "owner_id": "owner"}
    now = utc_now()
    db.run(
        "INSERT INTO reviews(review_id,owner_id,status,document_ids,version_ids,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        ("review_checkpoint", "owner", "running", '["doc_scope"]', "[]", now, now),
    )
    manager.create("task_checkpoint", "review", payload, "owner")
    db.run("UPDATE tasks SET status='running',worker_id=? WHERE task_id='task_checkpoint'",
           (manager.worker_id,))
    state = TaskState(task_id="task_checkpoint", task_type="review", user_id="owner",
                      document_ids=["doc_scope"], plan=["retrieve", "extract", "risk", "verify", "report"],
                      status="succeeded", report={"risks": [], "fields": [], "citations": []})
    db.run(
        "INSERT INTO agent_runs(task_id,review_id,state,created_at,updated_at) VALUES(?,?,?,?,?)",
        ("task_checkpoint", "review_checkpoint", json_dumps(state.model_dump(mode="json")), now, now),
    )
    try:
        result = review_runner(db, payload)(TaskContext(manager, "task_checkpoint"), payload)
        assert result["status"] == "waiting_human"
        assert result["finding_count"] == 0
        assert db.one("SELECT status FROM reviews WHERE review_id='review_checkpoint'")["status"] == "needs_review"
    finally:
        manager.close()


def test_human_can_close_no_evidence_review_without_false_success(recovery_database):
    """Review and Task close atomically with a note and optimistic version."""

    db = recovery_database
    manager = TaskManager(db, workers=1)
    now = utc_now()
    db.run(
        "INSERT INTO reviews(review_id,owner_id,status,document_ids,version_ids,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        ("review_unknown", "owner", "needs_review", '["doc"]', "[]", now, now),
    )
    manager.create("task_unknown_evidence", "review", {"review_id": "review_unknown"}, "owner")
    db.run(
        "UPDATE tasks SET status='waiting_human',result_ref='review_unknown' WHERE task_id='task_unknown_evidence'"
    )
    try:
        request = ReviewUnresolvedRequest(note="检索证据不足，交由法务线下确认", expected_version=1)
        result = close_unresolved_review("review_unknown", request, db=db,
                                         tasks=manager, current_user={"user_id": "owner"})
        assert result["status"] == "closed_unresolved"
        assert manager.get("task_unknown_evidence")["status"] == "cancelled"
        assert db.one("SELECT resolution_note,version FROM reviews WHERE review_id='review_unknown'") == {
            "resolution_note": "检索证据不足，交由法务线下确认", "version": 2,
        }
        with pytest.raises(AppError) as stale:
            close_unresolved_review("review_unknown", request, db=db,
                                    tasks=manager, current_user={"user_id": "owner"})
        assert stale.value.code == "REVIEW_CONFLICT"
    finally:
        manager.close()


def test_unresolved_close_waits_for_task_human_gate(recovery_database):
    """A review cannot close during the gap before its worker commits waiting_human."""

    db = recovery_database
    manager = TaskManager(db, workers=1)
    now = utc_now()
    db.run(
        "INSERT INTO reviews(review_id,owner_id,status,document_ids,version_ids,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        ("review_early", "owner", "needs_review", '["doc"]', "[]", now, now),
    )
    manager.create("task_early", "review", {"review_id": "review_early"}, "owner")
    try:
        with pytest.raises(AppError) as early:
            close_unresolved_review(
                "review_early", ReviewUnresolvedRequest(note="需要线下确认", expected_version=1),
                db=db, tasks=manager, current_user={"user_id": "owner"},
            )
        assert early.value.code == "TASK_NOT_READY"
        assert db.one("SELECT status,version FROM reviews WHERE review_id='review_early'") == {
            "status": "needs_review", "version": 1,
        }
    finally:
        manager.close()


def test_database_migrates_legacy_idempotency_table_without_rebuilding_it(recovery_database):
    """Existing upload and screening records remain compatible after adding request hashes."""

    legacy_path = recovery_database.path.parent / "legacy-idempotency.db"
    with sqlite3.connect(legacy_path) as connection:
        connection.execute(
            "CREATE TABLE idempotency_keys ("
            "key TEXT PRIMARY KEY, operation TEXT NOT NULL, response TEXT NOT NULL, created_at TEXT NOT NULL"
            ")"
        )
        connection.execute(
            "INSERT INTO idempotency_keys(key,operation,response,created_at) VALUES(?,?,?,?)",
            ("anonymous:old-upload", "upload", "{}", "2026-10-09T00:00:00Z"),
        )

    migrated = SQLiteDatabase(legacy_path)
    migrated.initialize()
    columns = {column["name"] for column in migrated.all("PRAGMA table_info(idempotency_keys)")}
    assert {"key", "owner_id", "operation", "request_hash", "response", "created_at"} <= columns
    row = migrated.one("SELECT response,request_hash FROM idempotency_keys WHERE key=?", ("anonymous:old-upload",))
    assert row == {"response": "{}", "request_hash": None}
