# DocPilot 目录与依赖约定

## 结构调整结论

现有 API、领域、RAG、Agent、Service 和基础设施分层已经可以支撑本地 MVP。本阶段不搬动
已运行的 Agent 节点，而是在原结构中增加六个明确边界：模型适配、运行时、Prompt、模型输出
Schema、Playbook 和确定性验证。这样可以逐步迁移，并保持现有接口和测试稳定。

## 当前目标结构

项目是浏览器 Web 应用。`backend/app/` 中的 `app` 是 Python 包名；FastAPI 提供 HTTP/SSE
接口，页面代码位于 `frontend/`。

```text
backend/
├── app/
│   ├── api/                    # HTTP 路由与依赖注入
│   ├── agents/
│   │   ├── prompts/            # 版本化 Prompt
│   │   ├── runtime/            # 节点执行、重试、恢复和 Checkpoint
│   │   ├── schemas/            # 模型结构化输出 Schema
│   │   ├── *_agent.py          # 当前有界业务节点，暂不移动
│   │   ├── graph.py            # 当前工作流装配入口
│   │   ├── state.py            # 可序列化任务状态
│   │   └── supervisor.py       # 固定计划和范围门禁
│   ├── core/                   # 配置、错误、日志和安全
│   ├── domain/                 # 与框架无关的实体、枚举和 API Schema
│   ├── infrastructure/
│   │   ├── llm/                # Mock、OpenAI Compatible 和未来 vLLM 适配
│   │   ├── providers.py        # Agent 使用的模型、Embedding 和 Reranker 协议
│   │   ├── database.py         # 当前 SQLite 适配器
│   │   ├── embedding.py        # Embedding 适配器
│   │   ├── storage.py          # 对象存储适配器
│   │   └── tasks.py            # 当前进程内任务执行器
│   ├── playbooks/              # 版本化合同风险规则
│   │   └── rules/              # YAML 等受审查规则文件
│   ├── rag/                    # Chunk、索引、检索和 Citation
│   ├── repositories/           # 持久化访问
│   ├── screening/              # 多文档确定性筛选
│   ├── services/               # 用例编排
│   ├── verification/           # 字段、证据和规则的纯验证器
│   └── workers/                # 后台任务入口
├── tests/
└── pyproject.toml

frontend/src/
├── features/
│   ├── auth/
│   ├── documents/
│   └── reviews/
└── shared/
```

## Agent Workflow 目标链路

```text
API
  → Service
  → Task / Worker
  → Agent Runtime
      → Retrieve
      → Rule Extract
      → LLM Extract
      → Merge Fields
      → Playbook
      → Verify
      → Human Gate
      → Report
```

模型只生成候选字段、解释或建议。文档范围、Citation、数值、风险规则和最终发布条件由代码
校验。高风险、冲突和低置信结果在 `Human Gate` 暂停；人工输入写入状态后从 Checkpoint 恢复。

## 依赖方向

```text
api → services → agents → domain / rag / playbooks / verification
                         ↓
                  infrastructure protocols
                         ↑
                  infrastructure adapters
```

具体约束：

1. `api` 只做鉴权、输入校验和响应转换，不实现检索或 Agent 业务。
2. `agents` 通过运行上下文使用数据库、模型和事件服务，不直接创建供应商客户端。
3. `agents/prompts` 不依赖数据库和 FastAPI，只负责构建版本化模型消息。
4. `agents/schemas` 只验证模型候选输出；Schema 通过不代表事实已经验证。
5. `agents/runtime` 不包含合同字段和风险规则，只处理执行语义。
6. `playbooks` 使用白名单操作符确定性求值，禁止执行规则文件中的代码。
7. `verification` 实现纯验证逻辑，`verification_agent.py` 负责工作流编排。
8. `infrastructure/llm` 可以依赖 HTTP/供应商 SDK，但不能依赖业务 Agent。
9. `domain` 不依赖 FastAPI、React、模型 SDK 或具体数据库。

## 渐进迁移顺序

本次只建立包边界，不改变运行链路。后续按以下顺序迁移：

1. 在 `infrastructure/llm` 实现模型请求、响应、错误、Mock 和 OpenAI Compatible Adapter。
2. 在 `agents/schemas` 和 `agents/prompts` 建立结构化抽取契约。
3. 在 `agents/runtime` 实现统一上下文、Executor、超时、重试和 Checkpoint。
4. 将现有同步节点通过适配器接入 Runtime，保持 API 契约不变。
5. 将字段验证逐步下沉到 `verification`，保留 Agent 编排层。
6. 用 `playbooks` 替换 `risk_agent.py` 中的单一演示规则。
7. Runtime 稳定后再迁移 PostgreSQL、Redis Worker、Model Gateway 和 Kubernetes。

## 为什么不立即移动现有 Agent 文件

当前测试和 Service 已直接导入 `agents/*.py`。一次性移动会制造大量导入变化，却不会改善运行
能力，还会混合“目录重构”和“异步 Runtime 改造”两类风险。新增包允许旧流程继续运行，新模块
可以按测试驱动逐步接管，最终再删除已经没有引用的兼容层。

