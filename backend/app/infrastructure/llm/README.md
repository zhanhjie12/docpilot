# LLM 适配层

该目录用于隔离第三方模型协议和 SDK，避免供应商实现进入 Agent、领域或 API 层。

计划模块：

```text
llm/
├── __init__.py
├── models.py              # 统一请求、响应和 Token 用量
├── errors.py              # 可重试与不可重试的供应商错误
├── mock.py                # 不依赖网络的测试实现
├── openai_compatible.py   # 第三方中转站和 vLLM 适配器
└── factory.py             # 根据 Settings 创建 Provider
```

依赖规则：

- 可以依赖 `core.config` 和供应商 HTTP/SDK。
- 必须实现 `infrastructure.providers` 中的协议。
- 不得依赖 FastAPI 路由、Agent 节点或业务 Service。
- 不得在日志和异常中输出 API Key 或完整敏感 Prompt。

