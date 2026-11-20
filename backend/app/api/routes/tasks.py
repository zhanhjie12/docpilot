"""Task polling and durable event endpoints."""

import asyncio
import json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.api.deps import LastEventID, get_database, get_request_user, get_task_manager
from app.core.exceptions import AppError
from app.infrastructure.database import SQLiteDatabase, json_loads
from app.infrastructure.tasks import TaskManager

router = APIRouter()


def _task_or_404(tasks: TaskManager, task_id: str, owner_id: str | None = None) -> dict:
    """读取任务并校验用户归属，避免通过 task_id 越权读取进度。"""

    task = tasks.get(task_id)
    if not task or tasks.owner_id(task_id) != owner_id:
        raise AppError(code="TASK_NOT_FOUND", message="任务不存在", status_code=404)
    return task


@router.get("/{task_id}", summary="查询任务状态")
async def get_task(task_id: str, tasks: TaskManager = Depends(get_task_manager), current_user: dict[str, object] | None = Depends(get_request_user)):
    owner_id = str(current_user["user_id"]) if current_user else None
    return _task_or_404(tasks, task_id, owner_id)


@router.get("/{task_id}/events", summary="订阅任务事件")
async def stream_task_events(task_id: str, last_event_id: LastEventID = None, tasks: TaskManager = Depends(get_task_manager), current_user: dict[str, object] | None = Depends(get_request_user)):
    owner_id = str(current_user["user_id"]) if current_user else None
    _task_or_404(tasks, task_id, owner_id)
    after = 0
    if last_event_id and last_event_id.isdigit():
        after = int(last_event_id)

    async def event_stream():
        sent = after
        for _ in range(120):
            events = tasks.events(task_id, sent)
            for event in events:
                sent = event["sequence"]
                yield f"id: {sent}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            task = tasks.get(task_id)
            if task and task["status"] in {"succeeded", "failed", "cancelled", "waiting_human"}:
                break
            await asyncio.sleep(0.25)

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/{task_id}/screening-result", summary="查询筛选结果")
async def get_screening_result(task_id: str, tasks: TaskManager = Depends(get_task_manager), db: SQLiteDatabase = Depends(get_database), current_user: dict[str, object] | None = Depends(get_request_user)):
    owner_id = str(current_user["user_id"]) if current_user else None
    task = _task_or_404(tasks, task_id, owner_id)
    row = db.one("SELECT * FROM screening_results WHERE task_id=?", (task_id,))
    if not row:
        return {"task_id": task_id, "status": task["status"], "total": 0, "matched": 0, "items": []}
    return {
        "task_id": task_id, "status": task["status"], "query": row["query"],
        "conditions": json_loads(row["conditions"], {}), "total": row["total"], "matched": row["matched"],
        "items": json_loads(row["items"], []),
    }
