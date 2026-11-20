"""路由通过 Depends 获取启动时创建的资源。"""

from typing import Annotated
from uuid import uuid4

from fastapi import Depends, Header, Request

from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.security import AuthenticationError, decode_access_token
from app.infrastructure.providers import EmbeddingProvider, LLMProvider
from app.infrastructure.storage import ObjectStorage
from app.infrastructure.database import SQLiteDatabase
from app.infrastructure.tasks import TaskManager


IdempotencyKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
        pattern=r"^[\x21-\x7E]+$",
        description="创建操作的幂等键；同一身份、同一操作和同一键会回放首次创建结果",
    ),
]

LastEventID = Annotated[
    str | None,
    Header(
        alias="Last-Event-ID",
        min_length=1,
        max_length=128,
        pattern=r"^[\x21-\x7E]+$",
        description="SSE断线恢复事件ID；当前事件服务尚未实现",
    ),
]


def get_storage(request: Request) -> ObjectStorage:
    """返回启动阶段创建的对象存储，否则用统一 503 错误阻止业务请求。"""

    storage = getattr(request.app.state, "storage", None)
    if storage is None or not request.app.state.ready:
        raise AppError(
            code="SERVICE_NOT_READY",
            message="对象存储尚未就绪",
            status_code=503,
            retryable=True,
        )
    return storage


def get_database(request: Request) -> SQLiteDatabase:
    """返回已初始化的数据库连接封装。"""

    database = getattr(request.app.state, "database", None)
    if database is None or not request.app.state.ready:
        raise AppError(code="SERVICE_NOT_READY", message="数据库尚未就绪", status_code=503, retryable=True)
    return database


def get_task_manager(request: Request) -> TaskManager:
    """返回任务管理器，确保任务接口只在应用 ready 后运行。"""

    manager = getattr(request.app.state, "task_manager", None)
    if manager is None or not request.app.state.ready:
        raise AppError(code="SERVICE_NOT_READY", message="任务服务尚未就绪", status_code=503, retryable=True)
    return manager


def get_embedding_provider(request: Request) -> EmbeddingProvider | None:
    """返回配置的向量提供方；None 表示使用纯词法检索。"""

    if not getattr(request.app.state, "ready", False):
        raise AppError(
            code="SERVICE_NOT_READY",
            message="Embedding 服务尚未就绪",
            status_code=503,
            retryable=True,
        )
    return getattr(request.app.state, "embedding_provider", None)


def get_llm_provider(request: Request) -> LLMProvider:
    """Return the configured model port after startup completes."""

    provider = getattr(request.app.state, "llm_provider", None)
    if provider is None or not getattr(request.app.state, "ready", False):
        raise AppError(code="SERVICE_NOT_READY", message="模型服务尚未就绪", status_code=503, retryable=True)
    return provider


def get_request_id(
    request: Request,
    request_id: Annotated[
        str | None,
        Header(alias="X-Request-ID", description="可选追踪ID；缺失或非法时生成UUID"),
    ] = None,
) -> str:
    # 外层 ASGI 中间件通常会先写入追踪 ID；这里保底生成，方便测试和内部挂载。
    current = getattr(request.state, "request_id", None)
    if not isinstance(current, str) or not current:
        current = str(uuid4())
        request.state.request_id = current
    return current


def _authorization_token(authorization: str | None) -> str | None:
    """解析标准 Bearer 头；其他认证方案统一视为无效。"""

    if not authorization:
        return None
    scheme, separator, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not separator or not token.strip():
        raise AppError(
            code="INVALID_TOKEN",
            message="登录凭证无效或已过期",
            status_code=401,
            retryable=False,
        )
    return token.strip()


def get_optional_current_user(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: SQLiteDatabase = Depends(get_database),
) -> dict[str, object] | None:
    """有令牌时验证并返回用户；开发环境无令牌时允许匿名访问。"""

    token = _authorization_token(authorization)
    if token is None:
        return None
    settings: Settings = request.app.state.settings
    try:
        payload = decode_access_token(token, settings.jwt_secret)
    except AuthenticationError as exc:
        raise AppError(
            code="INVALID_TOKEN",
            message="登录凭证无效或已过期",
            status_code=401,
            retryable=False,
        ) from exc
    user_id = str(payload["sub"])
    user = db.one(
        "SELECT user_id,email,display_name,role,created_at,is_active "
        "FROM users WHERE user_id=?",
        (user_id,),
    )
    if not user or not user.get("is_active"):
        raise AppError(
            code="INVALID_TOKEN",
            message="登录凭证无效或已过期",
            status_code=401,
            retryable=False,
        )
    # 业务路由通过 api_router 的全局依赖执行认证，这里把用户放入请求上下文，
    # 后续资源路由无需再次解析令牌或查询数据库。
    request.state.current_user = user
    return user


def get_request_user(request: Request) -> dict[str, object] | None:
    """读取当前请求已解析的用户；匿名开发请求返回 None。"""

    return getattr(request.state, "current_user", None)


def get_current_user(
    user: dict[str, object] | None = Depends(get_optional_current_user),
) -> dict[str, object]:
    """要求当前请求已经认证，供 /auth/me 和受保护业务路由使用。"""

    if user is None:
        raise AppError(
            code="AUTHENTICATION_REQUIRED",
            message="请先登录",
            status_code=401,
            retryable=False,
        )
    return user


def require_authenticated(
    request: Request,
    user: dict[str, object] | None = Depends(get_optional_current_user),
) -> dict[str, object] | None:
    """按 AUTH_REQUIRED 配置决定是否强制业务接口登录。"""

    if request.app.state.settings.auth_required and user is None:
        raise AppError(
            code="AUTHENTICATION_REQUIRED",
            message="请先登录",
            status_code=401,
            retryable=False,
        )
    return user
