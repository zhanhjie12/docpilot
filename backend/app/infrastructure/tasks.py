"""本地开发使用的轻量级进程内任务队列。

它保留了队列部署所需的可观察状态：任务从 ``queued`` 开始，写入持久化事件，
并可通过 API 轮询。以后替换为 Celery 或 RQ 时无需修改路由契约。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from sqlite3 import Connection
from typing import Any, Callable
from uuid import uuid4

from app.infrastructure.database import SQLiteDatabase, json_dumps, utc_now


Runner = Callable[["TaskContext", dict[str, Any]], dict[str, Any] | None]
RunnerResolver = Callable[[str, dict[str, Any]], Runner | None]


class LostTaskLease(RuntimeError):
    """A former worker must stop writing after another worker claims its task."""


@dataclass(slots=True)
class TaskContext:
    manager: "TaskManager"
    task_id: str

    def update(self, progress: int, node: str, message: str, event_type: str = "node.completed", data: dict[str, Any] | None = None) -> None:
        """向当前任务写入进度，并让 runner 不直接依赖数据库细节。"""

        self.manager.update(self.task_id, progress, node, message, event_type, data or {})


class TaskManager:
    def __init__(self, db: SQLiteDatabase, workers: int = 2) -> None:
        """初始化任务存储和固定大小的后台线程池。"""

        self.db = db
        self.executor = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="docpilot-task")
        self._futures: dict[str, Future[Any]] = {}
        # A runner can be requested by a normal API call and startup recovery at
        # nearly the same time. Guarding the in-memory future map prevents the
        # same process from submitting the same task twice.
        self._futures_lock = threading.RLock()
        self.worker_id = uuid4().hex
        self._stop_monitor = threading.Event()
        self._monitor: threading.Thread | None = None
        self._resolver: RunnerResolver | None = None

    @staticmethod
    def _lease_deadline() -> str:
        return (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat().replace("+00:00", "Z")

    def create(self, task_id: str, task_type: str, payload: dict[str, Any], owner_id: str | None = None) -> dict[str, Any]:
        """Create a queued task and its first event in one database commit."""

        with self.db.transaction() as conn:
            self.create_in_transaction(conn, task_id, task_type, payload, owner_id)
        return self.get(task_id) or {}

    def create_in_transaction(
        self,
        conn: Connection,
        task_id: str,
        task_type: str,
        payload: dict[str, Any],
        owner_id: str | None = None,
    ) -> None:
        """Persist a task inside a caller-owned transaction.

        Review creation uses this method so ``reviews``, ``tasks``, the initial
        task event, and the idempotency record either commit together or all
        roll back. This method deliberately does not submit a background
        runner: callers must do that only after their transaction commits.
        """

        now = utc_now()
        conn.execute(
            "INSERT INTO tasks(task_id,owner_id,task_type,status,progress,current_node,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (task_id, owner_id, task_type, "queued", 0, "queued", json_dumps(payload), now, now),
        )
        self._event_in_transaction(conn, task_id, "task.queued", "queued", 0, "Task queued", {}, now)

    def enqueue(self, task_id: str, task_type: str, payload: dict[str, Any], runner: Runner, owner_id: str | None = None) -> dict[str, Any]:
        """先落库再提交 runner，保证客户端拿到任务 ID 后可立即轮询。"""

        task = self.create(task_id, task_type, payload, owner_id)
        self.start(task_id, payload, runner)
        return task

    def start(
        self,
        task_id: str,
        payload: dict[str, Any],
        runner: Runner,
        *,
        recovered: bool = False,
    ) -> bool:
        """Submit a durable queued task once and return whether this call started it.

        A task is first persisted by ``create`` or ``create_in_transaction``.
        Keeping submission separate allows a process that dies after commit to
        recover the same task ID during its next startup.
        """

        task = self.get(task_id)
        if not task or task["status"] != "queued":
            return False
        with self._futures_lock:
            existing = self._futures.get(task_id)
            if existing is not None and not existing.done():
                return False
            # Recheck after acquiring the local submit lock. A previous runner
            # may have changed the durable task state while this caller waited.
            current = self.get(task_id)
            if not current or current["status"] != "queued":
                return False
            claimed = self.db.run(
                "UPDATE tasks SET worker_id=?,lease_expires_at=? WHERE task_id=? AND status='queued' "
                "AND (worker_id IS NULL OR lease_expires_at<?)",
                (self.worker_id, self._lease_deadline(), task_id, utc_now()),
            )
            if not claimed:
                return False
            if recovered:
                # Persist this before submitting the worker so event order
                # remains queued -> recovered -> started.
                self._event(task_id, "task.recovered", "queued", 0, "Task recovered after restart", {})
            self._futures[task_id] = self.executor.submit(self._run, task_id, payload, runner)
            return True

    def recover_queued(self, resolve_runner: RunnerResolver) -> list[str]:
        """Re-submit durable queued tasks after process startup.

        Claimed queued work and running reviews use an expiring lease. Running
        reviews resume from an Agent checkpoint; other interrupted task types
        fail visibly because their external writes are not safely replayable.
        Unknown types and malformed payloads also fail visibly.
        """

        self._resolver = resolve_runner
        rows = self.db.all(
            "SELECT task_id,task_type,payload FROM tasks WHERE status='queued' "
            "AND (worker_id IS NULL OR lease_expires_at<?) ORDER BY created_at,task_id",
            (utc_now(),),
        )
        recovered: list[str] = []
        for row in rows:
            task_id = row["task_id"]
            try:
                payload = json.loads(row["payload"])
                if not isinstance(payload, dict):
                    raise ValueError("Task payload must be a JSON object")
                runner = resolve_runner(row["task_type"], payload)
                if runner is None:
                    raise LookupError(f"Unsupported task type: {row['task_type']}")
                if self.start(task_id, payload, runner, recovered=True):
                    recovered.append(task_id)
            except Exception as exc:  # noqa: BLE001 - recovery errors must be observable state
                self._mark_recovery_failed(task_id, exc)
        recovered.extend(self.recover_expired(resolve_runner))
        if self._monitor is None:
            self._monitor = threading.Thread(target=self._maintain_leases, daemon=True,
                                             name="docpilot-task-leases")
            self._monitor.start()
        return recovered

    def recover_expired(self, resolve_runner: RunnerResolver) -> list[str]:
        """Resume only expired review jobs from their durable Agent checkpoint."""

        rows = self.db.all(
            "SELECT task_id,task_type,payload FROM tasks WHERE status='running' "
            "AND (lease_expires_at IS NULL OR lease_expires_at<?) ORDER BY created_at,task_id",
            (utc_now(),),
        )
        recovered: list[str] = []
        for row in rows:
            task_id = row["task_id"]
            if row["task_type"] != "review":
                changed = self.db.run(
                    "UPDATE tasks SET status='failed',failure_code='TASK_INTERRUPTED',"
                    "failure_message='Task interrupted before a safe checkpoint',updated_at=? "
                    "WHERE task_id=? AND status='running' AND (lease_expires_at IS NULL OR lease_expires_at<?)",
                    (utc_now(), task_id, utc_now()),
                )
                if changed:
                    self._event(task_id, "task.failed", "recovery", 100,
                                "Task interrupted", {"code": "TASK_INTERRUPTED"})
                continue
            changed = self.db.run(
                "UPDATE tasks SET status='queued',worker_id=NULL,lease_expires_at=NULL,updated_at=? "
                "WHERE task_id=? AND status='running' AND (lease_expires_at IS NULL OR lease_expires_at<?)",
                (utc_now(), task_id, utc_now()),
            )
            if not changed:
                continue
            try:
                payload = json.loads(row["payload"])
                if not isinstance(payload, dict):
                    raise ValueError("Task payload must be a JSON object")
                runner = resolve_runner(row["task_type"], payload)
                if runner is None:
                    raise LookupError("Review runner unavailable")
                if self.start(task_id, payload, runner, recovered=True):
                    recovered.append(task_id)
            except Exception as exc:  # noqa: BLE001 - visible terminal failure
                self._mark_recovery_failed(task_id, exc)
        return recovered

    def _maintain_leases(self) -> None:
        """Heartbeat active local workers and periodically reclaim expired work."""

        while not self._stop_monitor.wait(10):
            with self._futures_lock:
                self._futures = {task_id: future for task_id, future in self._futures.items()
                                 if not future.done()}
                active = [task_id for task_id, future in self._futures.items() if not future.done()]
            for task_id in active:
                self.db.run(
                    "UPDATE tasks SET lease_expires_at=? WHERE task_id=? AND worker_id=? "
                    "AND status IN ('queued','running')",
                    (self._lease_deadline(), task_id, self.worker_id),
                )
            if self._resolver is not None:
                # Includes queued rows abandoned after lease claim but before
                # executor submission, as well as expired running reviews.
                self.recover_queued(self._resolver)

    def _run(self, task_id: str, payload: dict[str, Any], runner: Runner) -> None:
        """维护执行状态；需要人工确认时保留独立终态。"""

        try:
            self.update(task_id, 1, "start", "Task started", "task.started")
        except LostTaskLease:
            return
        try:
            result = runner(TaskContext(self, task_id), payload) or {}
            result_ref = result.get("result_ref") if isinstance(result, dict) else None
            waiting_human = isinstance(result, dict) and result.get("status") == "waiting_human"
            final_status = "waiting_human" if waiting_human else "succeeded"
            final_node = result.get("current_node") or "review" if waiting_human else "completed"
            changed = self.db.run(
                "UPDATE tasks SET status=?,progress=?,current_node=?,result_ref=?,updated_at=?,"
                "worker_id=NULL,lease_expires_at=NULL WHERE task_id=? AND worker_id=?",
                (final_status, 100, final_node, result_ref, utc_now(), task_id, self.worker_id),
            )
            if not changed:
                return
            self._event(
                task_id,
                "task.waiting_human" if waiting_human else "task.succeeded",
                final_node,
                100,
                "等待人工确认" if waiting_human else "Task completed",
                result,
            )
        except LostTaskLease:
            return
        except Exception as exc:  # noqa: BLE001 - task failures are user-visible state
            # 任务异常也必须持久化，否则前端只能看到任务一直 running。
            changed = self.db.run(
                "UPDATE tasks SET status=?,current_node=?,failure_code=?,failure_message=?,updated_at=?,"
                "worker_id=NULL,lease_expires_at=NULL WHERE task_id=? AND worker_id=?",
                ("failed", "failed", getattr(exc, "code", "TASK_FAILED"), str(exc)[:500], utc_now(), task_id, self.worker_id),
            )
            if changed:
                self._event(task_id, "task.failed", "failed", 100, "Task failed", {"code": getattr(exc, "code", "TASK_FAILED")})

    def update(self, task_id: str, progress: int, node: str, message: str, event_type: str, data: dict[str, Any] | None = None) -> None:
        """更新任务快照，并追加一条事件供时间线消费。"""

        current = self.db.one("SELECT status FROM tasks WHERE task_id=?", (task_id,))
        if not current:
            return
        status = "running" if event_type not in {"task.succeeded", "task.failed"} else current["status"]
        changed = self.db.run(
            "UPDATE tasks SET status=?,progress=?,current_node=?,updated_at=?,lease_expires_at=? "
            "WHERE task_id=? AND worker_id=? AND status IN ('queued','running')",
            (status, max(0, min(100, progress)), node, utc_now(), self._lease_deadline(),
             task_id, self.worker_id),
        )
        if not changed:
            raise LostTaskLease("Task lease no longer belongs to this worker")
        self._event(task_id, event_type, node, progress, message, data or {})

    def finish_human_review(self, review_id: str, owner_id: str | None) -> None:
        """所有风险项人工处理后，关闭该审阅对应的等待任务。"""

        rows = self.db.all(
            "SELECT task_id FROM tasks WHERE result_ref=? AND task_type='review' "
            "AND owner_id IS ? AND status='waiting_human'",
            (review_id, owner_id),
        )
        for row in rows:
            task_id = row["task_id"]
            self.db.run(
                "UPDATE tasks SET status='succeeded',current_node='completed',updated_at=? "
                "WHERE task_id=? AND status='waiting_human'",
                (utc_now(), task_id),
            )
            self._event(
                task_id,
                "task.succeeded",
                "completed",
                100,
                "人工确认已完成",
                {"result_ref": review_id},
            )

    def close_unresolved_in_transaction(
        self, conn: Connection, review_id: str, owner_id: str | None
    ) -> int:
        """Atomically close a human-gated task without claiming Agent success."""

        rows = conn.execute(
            "SELECT task_id FROM tasks WHERE result_ref=? AND task_type='review' "
            "AND owner_id IS ? AND status='waiting_human'",
            (review_id, owner_id),
        ).fetchall()
        for row in rows:
            task_id = row["task_id"]
            conn.execute(
                "UPDATE tasks SET status='cancelled',current_node='human_resolution',updated_at=? "
                "WHERE task_id=? AND status='waiting_human'",
                (utc_now(), task_id),
            )
            self._event_in_transaction(
                conn, task_id, "task.closed_unresolved", "human_resolution", 100,
                "人工确认本次审阅无法判断", {"result_ref": review_id},
            )
        return len(rows)

    def _event(self, task_id: str, event_type: str, node: str, progress: int, message: str, data: dict[str, Any]) -> None:
        """按任务维度递增事件序号，保证增量轮询顺序稳定。"""

        with self.db.transaction() as conn:
            self._event_in_transaction(conn, task_id, event_type, node, progress, message, data)

    def _event_in_transaction(
        self,
        conn: Connection,
        task_id: str,
        event_type: str,
        node: str,
        progress: int,
        message: str,
        data: dict[str, Any],
        created_at: str | None = None,
    ) -> None:
        """Append one event using the same transaction as its state mutation."""

        row = conn.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 AS next FROM task_events WHERE task_id=?",
            (task_id,),
        ).fetchone()
        sequence = int(row["next"] if row else 1)
        conn.execute(
            "INSERT INTO task_events(event_id,task_id,sequence,type,node,progress,message,data,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                f"evt_{uuid4().hex}",
                task_id,
                sequence,
                event_type,
                node,
                progress,
                message,
                json_dumps(data),
                created_at or utc_now(),
            ),
        )

    def _mark_recovery_failed(self, task_id: str, exc: Exception) -> None:
        """Persist an actionable terminal state when a queued task cannot restart."""

        if isinstance(exc, LookupError):
            failure_code = "TASK_RECOVERY_UNSUPPORTED"
        elif isinstance(exc, (TypeError, ValueError)):
            failure_code = "TASK_RECOVERY_INVALID_PAYLOAD"
        else:
            failure_code = "TASK_RECOVERY_FAILED"
        changed = self.db.run(
            "UPDATE tasks SET status=?,current_node=?,failure_code=?,failure_message=?,updated_at=? "
            "WHERE task_id=? AND status='queued'",
            ("failed", "recovery", failure_code, str(exc)[:500], utc_now(), task_id),
        )
        if changed:
            self._event(
                task_id,
                "task.failed",
                "recovery",
                100,
                "Task recovery failed",
                {"code": failure_code},
            )

    def get(self, task_id: str) -> dict[str, Any] | None:
        """读取对外暴露的任务摘要，不返回内部数据库行对象。"""

        row = self.db.one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        if not row:
            return None
        return {
            "task_id": row["task_id"],
            "task_type": row["task_type"],
            "status": row["status"],
            "progress": row["progress"],
            "current_node": row["current_node"],
            "retry_count": row["retry_count"],
            "failure": ({"code": row["failure_code"], "message": row["failure_message"]} if row["failure_code"] else None),
            "result_ref": row["result_ref"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def owner_id(self, task_id: str) -> str | None:
        """读取任务归属供权限检查使用，不把内部字段暴露给 API。"""

        row = self.db.one("SELECT owner_id FROM tasks WHERE task_id=?", (task_id,))
        return str(row["owner_id"]) if row and row.get("owner_id") is not None else None

    def events(self, task_id: str, after: int = 0) -> list[dict[str, Any]]:
        """返回指定序号之后的事件，支持前端增量刷新时间线。"""

        rows = self.db.all("SELECT * FROM task_events WHERE task_id=? AND sequence>? ORDER BY sequence", (task_id, after))
        return [
            {
                "event_id": row["event_id"], "task_id": row["task_id"], "sequence": row["sequence"],
                "type": row["type"], "node": row["node"], "progress": row["progress"],
                "message": row["message"], "data": json.loads(row["data"] or "{}"), "created_at": row["created_at"],
            }
            for row in rows
        ]

    def close(self) -> None:
        """停止接收新任务并取消尚未开始的 future。"""

        self._stop_monitor.set()
        if self._monitor is not None:
            self._monitor.join(timeout=2)
        self.executor.shutdown(wait=False, cancel_futures=True)
