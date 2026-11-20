"""应用日志配置；不记录请求正文、合同原文或模型密钥。"""

import logging
from collections.abc import Mapping


LOG_CONTEXT_FIELDS = ("request_id", "task_id", "document_id")


def configure_logging() -> None:
    """只配置 DocPilot 的 logger，重复启动测试实例不重复添加 handler。"""
    logger = logging.getLogger("docpilot")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def log_request(
    logger: logging.Logger,
    *,
    request_id: str,
    method: str,
    status_code: int,
    duration_ms: float,
    state: Mapping[str, object] | None = None,
) -> None:
    """记录请求摘要和可选业务 ID，不接收也不记录请求正文。"""
    state = state or {}
    values = {
        field: state.get(field, "-")
        for field in LOG_CONTEXT_FIELDS[1:]
    }
    logger.info(
        "request_id=%s task_id=%s document_id=%s method=%s status=%s duration_ms=%.2f",
        request_id,
        values["task_id"],
        values["document_id"],
        method,
        status_code,
        duration_ms,
    )
