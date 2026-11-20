"""FastAPI 应用装配入口：配置、路由、错误处理和资源生命周期。

从 backend 目录运行：python -m uvicorn app.main:app --reload
api_app 是 FastAPI 实例；app 是带请求 ID 和 CORS 的 ASGI 服务入口。
解析、检索和 Agent 业务分别放在 services、rag 和 agents 中。
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool

from app.api.router import api_router
from app.api.routes.auth import router as auth_router
from app.api.routes.health import router as health_router
from app.core.config import BACKEND_ROOT, Settings, get_settings
from app.core.exceptions import COMMON_ERROR_RESPONSES, register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import wrap_http_app
from app.infrastructure.embedding import create_embedding_provider
from app.infrastructure.llm.client import create_llm_provider
from app.infrastructure.storage import LocalObjectStorage
from app.infrastructure.database import SQLiteDatabase
from app.infrastructure.tasks import TaskManager
from app.services.task_dispatcher import TaskRunnerFactory


OPENAPI_TAGS = [
    {"name": "health", "description": "服务存活及已启用资源的就绪检查"},
    {"name": "auth", "description": "用户注册、登录和当前用户信息"},
    {"name": "documents", "description": "文档上传、查询和版本管理"},
    {"name": "tasks", "description": "任务状态、执行事件和筛选结果"},
    {"name": "screening", "description": "多文档条件筛选"},
    {"name": "chat", "description": "基于文档证据的问答"},
    {"name": "reviews", "description": "合同审阅、人工确认和报告"},
    {"name": "policies", "description": "制度版本确认与有范围问答"},
    {"name": "tenders", "description": "招标要求与投标文件证据对齐"},
]


@asynccontextmanager
async def lifespan(application: FastAPI):
    """初始化已实现的资源；未来的数据库/模型/队列适配器在此接入。"""
    settings = application.state.settings
    application.state.ready = False
    configure_logging()

    # 相对路径固定以 backend 为基准，不受终端工作目录影响。
    upload_dir = settings.upload_dir
    if not upload_dir.is_absolute():
        upload_dir = BACKEND_ROOT / upload_dir
    upload_dir = upload_dir.resolve()
    upload_dir.mkdir(parents=True, exist_ok=True)
    storage = LocalObjectStorage(upload_dir)
    await run_in_threadpool(storage.check_ready)
    application.state.storage = storage
    database = SQLiteDatabase.from_settings(settings)
    task_manager = None
    embedding_provider = None
    llm_provider = None
    try:
        database.initialize()
        application.state.database = database
        task_manager = TaskManager(database, settings.task_workers)
        application.state.task_manager = task_manager
        embedding_provider = create_embedding_provider(settings)
        application.state.embedding_provider = embedding_provider
        llm_provider = create_llm_provider(settings)
        application.state.llm_provider = llm_provider
        # Task rows persist only task_type and JSON payload. Rebuild their
        # runners before accepting HTTP traffic so a process crash after a
        # durable queue write does not leave work permanently queued.
        runner_factory = TaskRunnerFactory(
            database,
            storage,
            settings,
            embedding_provider,
            llm_provider,
        )
        await run_in_threadpool(task_manager.recover_queued, runner_factory.resolve)
    except Exception:
        if task_manager is not None:
            task_manager.close()
        if embedding_provider is not None:
            close = getattr(embedding_provider, "close", None)
            if callable(close):
                close()
        if llm_provider is not None:
            close = getattr(llm_provider, "close", None)
            if callable(close):
                close()
        database.close()
        for attribute in ("llm_provider", "embedding_provider", "task_manager", "database", "storage"):
            if hasattr(application.state, attribute):
                delattr(application.state, attribute)
        raise

    # 当前只有本地存储有具体实现。不能把 Protocol 或 None 当作已就绪资源。
    # 后续创建连接池、模型客户端和任务执行器时，用 AsyncExitStack 管理关闭。
    try:
        application.state.ready = True
        yield
    finally:
        application.state.ready = False
        # LocalObjectStorage 不持有连接；文件句柄由使用方通过 with 关闭。
        if embedding_provider is not None:
            close = getattr(embedding_provider, "close", None)
            if callable(close):
                close()
        if llm_provider is not None:
            close = getattr(llm_provider, "close", None)
            if callable(close):
                close()
        task_manager.close()
        database.close()
        del application.state.embedding_provider
        del application.state.llm_provider
        del application.state.task_manager
        del application.state.database
        del application.state.storage


def create_app(settings: Settings | None = None) -> FastAPI:
    """创建独立 FastAPI 实例；测试可以显式注入配置。"""
    application = FastAPI(
        title="DocPilot",
        version="0.1.0",
        description="文档筛选与合同审阅 Web API，返回可追溯的原文证据。",
        openapi_tags=OPENAPI_TAGS,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
        responses=COMMON_ERROR_RESPONSES,
    )
    application.state.settings = settings if settings is not None else get_settings()
    application.state.ready = False

    register_exception_handlers(application)
    application.include_router(health_router)
    # 注册/登录不依赖已登录用户，因此放在受保护的业务路由之外。
    application.include_router(auth_router, prefix="/api/v1/auth", tags=["auth"])
    application.include_router(api_router, prefix="/api/v1")
    return application


api_app = create_app()
# 外层中间件也覆盖 FastAPI 生成的 500 响应，不缓存 SSE 响应体。
app = wrap_http_app(api_app)
