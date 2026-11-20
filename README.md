# DocPilot：企业业务文档分析 Agent

> 当前仓库包含可运行的本地 MVP：文档解析、RAG、固定 Agent 审阅、前端工作台以及基础用户认证。

## 项目目标

项目目标覆盖中文合同、招投标材料和内部制度的证据检索、结构化抽取、审阅与跨文档核对。当前合同主线支持按明确条件筛选、风险审阅、版本比较、引用和导出；制度场景支持经人工确认的生效版本问答，招投标场景支持逐条双侧证据对齐。招投标合规判断、合同与制度冲突判断以及自然语言筛选解析仍待完善。范围与顺序见 [docs/product_requirements.md](docs/product_requirements.md)。

项目形态是浏览器 Web 应用：`frontend/` 提供网页界面，`backend/app/` 提供 FastAPI
接口；其中 `app` 是 Python 包名，不是移动端应用。

```text
上传多份 PDF / DOCX
  -> 异步解析文本型 PDF / DOCX
  -> 统一文档结构、Chunk 与 Metadata
  -> Metadata / 规则筛选候选文档
  -> 对候选文档执行抽取、问答、版本对比和风险审阅
  -> Verification Agent 校验每项结论的原文证据
  -> 返回带文件名、页码、原文片段的报告与待办
```

## 目录说明

```text
docpilot/
├── backend/                 # Python + FastAPI 后端与 Agent 核心
├── frontend/                # React + TypeScript 前端骨架
├── infra/                   # Docker、反向代理与部署配置说明
├── docs/                    # 架构、开发链路与接口约定
├── evals/                   # 离线评测集、评测脚本与指标说明
└── data/samples/            # 脱敏或公开的样例文档；不得放真实敏感文件
```

后端与前端的详细目录边界、依赖方向和本次结构审查见
[`docs/architecture.md`](docs/architecture.md)。
产品范围、功能需求和评测基线见 [`docs/product_requirements.md`](docs/product_requirements.md)。
前后端与 Agent 的字段、状态和事件约定见 [`docs/api_contract.md`](docs/api_contract.md)。

## 推荐开发链路

1. **先完成单文档闭环**：`上传 -> PDF/DOCX 文本提取 -> SQLite 记录 -> 文档状态查询`。此阶段不接入 LLM。
2. **建立可追溯 RAG**：保留 `document_id`、文件名、页码、标题、字符区间、表格坐标等 Metadata；先做检索，再让 LLM 回答。
3. **加入结构化抽取**：用 Pydantic Schema 约束合同主体、金额、日期、付款节点、条款、待办等输出，失败时有限次重试和降级。
4. **实现确定性规则审阅**：把公司制度或审阅 Playbook 写成版本化规则；先由规则筛查，再让模型解释风险，避免所有判断都依赖提示词。
5. **再加 Agent 工作流**：先实现明确的状态机，再引入 Supervisor 路由 Parser、Retriever、Extractor、Comparison、Risk、Verifier 等专家。
6. **补前端、评测与部署**：展示解析进度、检索证据和任务轨迹；用固定评测集验证引用正确率、抽取准确率与耗时。

## MVP 边界

第一版支持 PDF/DOCX、基础用户认证、按用户隔离文档、单文档或两版本对比、文本型 PDF；扫描件 OCR、Excel、组织级角色、实时协作放到后续版本。这样能先完成一个可演示且可测的闭环。

## 项目亮点应如何落地

- **证据优先**：任何风险或回答都必须返回 `Citation`，无证据则明确拒答或标记待人工确认。
- **结构化而非纯聊天**：所有抽取、审阅和待办都有 Schema，可导出 JSON/CSV/Markdown。
- **跨文档能力**：当前比较合同 v1/v2；后续核对招标要求与投标响应、合同与制度的对应关系，并保留双侧原文证据。
- **可观测 Agent**：记录每个阶段的输入摘要、输出、耗时、模型、Token、错误和重试，不记录敏感原文。
- **人工兜底**：风险结论区分模型建议与已确认结果，支持审核后导出报告。

详细的阶段计划见 [docs/development_chain.md](docs/development_chain.md)。

基于当前项目状态的 10 个开发日高价值迭代计划见 [development_plan_10_days.md](development_plan_10_days.md)。

后端已经建立 `agents/runtime`、`agents/prompts`、`agents/schemas`、`infrastructure/llm`、
`playbooks` 和 `verification` 包边界；这些目录当前用于渐进迁移，不代表对应生产能力已经接入运行链路。
## 当前实现

`POST /api/v1/reviews` 已接入固定的 Agent 工作流：按审阅意图检索、十类合同字段抽取、
版本化 Playbook 风险规则、数据库原文引用核验和报告编排。任务及节点状态可通过
`/api/v1/tasks/{task_id}` 和 `/api/v1/reviews/{review_id}/workflow` 查看；风险项可人工批准或驳回。
批量筛选设置 `deep_analysis=true` 时，只为命中文档创建独立审阅任务。前端可打开审阅、
查看原文、处理风险并下载 JSON、Markdown 或 CSV 报告。
证据不足且没有风险项时，可填写人工备注，将审阅以“无法判断”结案；任务不会被记为审阅成功。
招标文件可提取带原文引用的要求候选，编辑后与投标文件逐项对齐候选证据。

`LLM_PROVIDER=openai_compatible` 可接第三方兼容中转站；模型仅产生可回放原文的字段建议与待复核风险解释，问答也只选原文片段。默认 `mock`，真实中转站尚未联调。自然语言筛选解析、OCR、企业真实 Playbook 阈值校准和真实中文质量验收尚未完成。只传筛选 `query` 会返回明确错误，避免误判所有合同命中。
启动方法见 [backend/README.md](backend/README.md) 和 [frontend/README.md](frontend/README.md)。
