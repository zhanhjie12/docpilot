"""请求追踪与跨域配置，兼容流式响应，不读取请求或响应正文。"""

import logging
import re
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Request
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.exceptions import make_error_response
from app.core.logging import log_request


logger = logging.getLogger("docpilot.http")
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}")


class RequestIDMiddleware:
    """回传合法请求 ID；缺失、过长或含控制字符时生成新 ID。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        supplied = Headers(scope=scope).get("X-Request-ID", "")
        request_id = supplied if REQUEST_ID_PATTERN.fullmatch(supplied) else str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        started = perf_counter()
        status_code = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception as exc:
            # Starlette 在发送 500 后仍会抛出异常，Uvicorn 会记录其文本。
            # 保留失败传播（包括 SSE 中途失败），但不让服务器日志写入原始文档。
            raise RuntimeError(
                f"Unhandled HTTP request request_id={request_id} "
                f"exception_type={type(exc).__name__}"
            ) from None
        finally:
            log_request(
                logger,
                request_id=request_id,
                method=scope["method"],
                status_code=status_code,
                duration_ms=(perf_counter() - started) * 1000,
                state=scope.get("state"),
            )


class ContractCORSMiddleware(CORSMiddleware):
    """预检拒绝也使用接口契约的错误格式，保留 CORS 响应头。"""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = Headers(scope=scope)
            if (
                scope["method"] == "OPTIONS"
                and "origin" in headers
                and "access-control-request-method" in headers
            ):
                response = self.preflight_response(headers)
                if response.status_code == 400:
                    response = make_error_response(
                        Request(scope),
                        status_code=400,
                        code="CORS_REJECTED",
                        message="跨域请求的来源、方法或请求头不被允许",
                        headers={
                            key: value for key, value in response.headers.items()
                            if key.lower() not in {"content-length", "content-type"}
                        },
                    )
                await response(scope, receive, send)
                return
        await super().__call__(scope, receive, send)


def wrap_http_app(application: FastAPI) -> ASGIApp:
    """请求 ID → CORS → FastAPI；包括未处理的 500 和 OPTIONS。"""
    settings = application.state.settings
    cors_app = ContractCORSMiddleware(
        application,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=[
            "Authorization", "Content-Type", "X-Request-ID",
            "Idempotency-Key", "Last-Event-ID",
        ],
        expose_headers=["X-Request-ID"],
    )
    return RequestIDMiddleware(cors_app)
