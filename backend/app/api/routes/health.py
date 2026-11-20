"""提供进程存活检查和依赖资源就绪检查。"""

import sqlite3

from fastapi import APIRouter, Request

from app.core.exceptions import AppError

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """只检查 API 进程是否能响应，不触碰外部依赖。"""

    return {"status": "ok"}


@router.get("/ready")
def ready(request: Request) -> dict[str, str]:
    """检查接收文档任务所需的存储、数据库、任务和向量资源。"""
    state = request.app.state
    storage = getattr(state, "storage", None)
    database = getattr(state, "database", None)
    task_manager = getattr(state, "task_manager", None)
    settings = getattr(state, "settings", None)
    embedding_provider = getattr(state, "embedding_provider", None)
    embedding_required = (
        settings is not None and settings.embedding_provider != "disabled"
    )
    # 先检查应用生命周期是否已完成，以及启动阶段挂载的资源是否存在。
    if (
        not getattr(state, "ready", False)
        or storage is None
        or database is None
        or task_manager is None
        or (embedding_required and embedding_provider is None)
    ):
        raise AppError(
            code="SERVICE_NOT_READY",
            message="服务资源尚未就绪",
            status_code=503,
            retryable=True,
        )
    # 该同步路由由 FastAPI 放入线程池执行，避免磁盘 I/O 阻塞事件循环。
    # 每次请求都会重新探测，因此临时故障恢复后可以自动恢复 ready 状态。
    try:
        storage.check_ready()
        # SELECT 1 同时验证数据库连接和最基本的查询能力。
        probe = database.one("SELECT 1 AS ready")
        if probe != {"ready": 1}:
            raise sqlite3.DatabaseError("database readiness probe returned no row")
    except (OSError, sqlite3.Error) as exc:
        raise AppError(
            code="SERVICE_NOT_READY",
            message="持久化资源暂不可用",
            status_code=503,
            retryable=True,
        ) from exc
    return {"status": "ready"}
