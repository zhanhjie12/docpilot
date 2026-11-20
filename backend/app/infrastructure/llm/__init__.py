"""LLM 供应商适配器包。

该包只负责模型请求、响应、错误转换和客户端生命周期。Agent 节点应依赖
``app.infrastructure.providers.LLMProvider`` 协议，不应直接导入供应商 SDK。
"""

