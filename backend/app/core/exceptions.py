"""统一业务异常、HTTP 错误响应和 OpenAPI 错误模型。"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from http import HTTPStatus
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, JsonValue
from starlette.exceptions import HTTPException

logger = logging.getLogger("docpilot.errors")


class ErrorBody(BaseModel):
    """与 docs/api_contract.md 的统一错误字段保持一致。"""

    code: str
    message: str
    details: dict[str, JsonValue] = Field(default_factory=dict)
    retryable: bool = False
    request_id: str


class ErrorResponse(BaseModel):
    error: ErrorBody


COMMON_ERROR_RESPONSES = {
    status_code: {"model": ErrorResponse, "description": description}
    for status_code, description in {
        400: "请求不符合业务要求",
        401: "需要身份验证",
        403: "无权访问此资源",
        404: "资源不存在",
        405: "请求方法不支持",
        409: "资源状态冲突",
        413: "请求内容过大",
        415: "文件或内容类型不支持",
        422: "请求参数校验失败",
        500: "服务器内部错误",
        501: "接口尚未实现",
        503: "依赖服务暂不可用",
    }.items()
}


def _json_details(details: dict[str, JsonValue] | None) -> dict[str, JsonValue]:
    """复制公开详情，并在生成响应前排除非 JSON 对象和非有限数值。"""

    if details is None:
        return {}
    if not isinstance(details, dict):
        raise TypeError("Error details must be a JSON object")
    return json.loads(json.dumps(details, ensure_ascii=False, allow_nan=False))


class AppError(Exception):
    """预期的业务失败；message 和 details 必须由调用方确认可以公开。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: dict[str, JsonValue] | None = None,
        retryable: bool = False,
    ) -> None:
        if not 400 <= status_code <= 599:
            raise ValueError("AppError status_code must be between 400 and 599")
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = _json_details(details)
        self.retryable = retryable


def _request_id(request: Request) -> str:
    request_id = getattr(request.state, "request_id", None)
    if not isinstance(request_id, str) or not request_id:
        request_id = str(uuid4())
        request.state.request_id = request_id
    return request_id


def make_error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    *,
    details: dict[str, JsonValue] | None = None,
    retryable: bool = False,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    request_id = _request_id(request)
    body = ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message,
            details=_json_details(details),
            retryable=retryable,
            request_id=request_id,
        )
    )
    response = JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
        headers=headers,
    )
    response.headers["X-Request-ID"] = request_id
    return response


async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return make_error_response(
        request,
        exc.status_code,
        exc.code,
        exc.message,
        details=exc.details,
        retryable=exc.retryable,
    )


async def _http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    code = {
        404: "NOT_FOUND",
        405: "METHOD_NOT_ALLOWED",
        501: "NOT_IMPLEMENTED",
    }.get(exc.status_code, f"HTTP_{exc.status_code}")
    try:
        default_message = HTTPStatus(exc.status_code).phrase
    except ValueError:
        default_message = "HTTP request failed"
    # HTTPException.detail 是接口主动声明的公开消息；结构化业务详情使用 AppError。
    message = exc.detail if isinstance(exc.detail, str) else default_message
    return make_error_response(
        request,
        exc.status_code,
        code,
        message,
        headers=exc.headers,
    )


async def _validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    fields = [
        {
            "location": [
                part if isinstance(part, (str, int)) else "field"
                for part in error.get("loc", ())
            ],
            "type": error.get("type", "validation_error"),
            "message": "字段格式或取值不符合要求",
        }
        for error in exc.errors()
    ]
    return make_error_response(
        request,
        422,
        "VALIDATION_ERROR",
        "请求参数校验失败",
        details={"fields": fields},
    )


async def _unexpected_error_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    # 不记录异常文本、请求正文或完整 traceback，避免将合同内容写入日志。
    logger.error(
        "Unhandled request error request_id=%s exception_type=%s",
        _request_id(request),
        type(exc).__name__,
    )
    return make_error_response(
        request,
        500,
        "INTERNAL_ERROR",
        "服务器内部错误",
        retryable=False,
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(HTTPException, _http_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(Exception, _unexpected_error_handler)
