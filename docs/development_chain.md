# 开发链路与技术建议

## 阶段 1：文档基础设施

- 使用 FastAPI 建立上传、查询状态、下载报告三个基础接口。
- 文件落对象存储或本地受控目录；数据库只保存文件元信息和状态，不直接把大文件塞进 SQLite/PostgreSQL。
- 抽象 `DocumentParser` 接口，初版用 PyMuPDF、python-docx；复杂版可替换为 Docling。
- 将解析任务放入后台 Worker，状态机使用 `uploaded / parsing / indexed / failed`。

验收：上传一份 PDF 后，能查询到页数、解析文本、失败原因和处理状态。

## 阶段 2：可追溯检索

- 按标题、段落、表格而不是固定字符数优先切块。
- 每个 Chunk 保存 `document_id`、版本、页码、标题路径、位置、内容哈希和解析质量。
- 先做 BM25，再叠加向量检索；使用 RRF 融合，候选结果再 Rerank。
- 回答上下文只传入少量高质量 Chunk，最终答案的每条结论绑定 Citation。

验收：准备 20 个带标准页码的问题，统计 Recall@K 和引用页码正确率。

## 阶段 3：结构化抽取与规则审阅

- 定义 `Clause`、`ExtractedField`、`RiskFinding` 等 Pydantic Schema。
- 对模型输出做 JSON 校验、重试与降级；不要将未经校验的字符串直接入库。
- 制度规则以 JSON/YAML 或数据库表维护，包含规则版本、严重等级、触发条件、处理建议。
- 风险结果同时保存“规则命中”“模型解释”“证据”“人工审核状态”。

验收：对合同样例抽取金额、日期、付款节点；针对缺失条款和冲突条款输出可定位的风险项。

## 阶段 4：Agent 工作流

首个目标是合同筛选和审阅闭环，详细需求见 [`agent_requirements.md`](agent_requirements.md)。
不要让多个 Agent 自由对话。先使用轻量有界状态机，等状态和节点 Schema 稳定后再考虑 LangGraph：

```text
输入任务
  -> Planner/Supervisor 选择流程
  -> Parser / Retriever / Extractor / Comparator 并行或顺序执行
  -> Risk Agent 依据规则和证据生成候选风险
  -> Verification Agent 检查“结论-证据”对应关系
  -> Report Agent 输出结构化报告
```

每个节点必须有输入 Schema、输出 Schema、超时、最大重试次数和可记录的事件。

建议实现顺序：`state.py`/`supervisor.py` → `retrieval_agent.py` → `extraction_agent.py` →
`risk_agent.py` → `verification_agent.py` → `report_agent.py` → `graph.py` →
`tests/test_agent_workflow.py`。`comparison_agent.py`、解析质量检查和可配置 Playbook 放到首个闭环通过评测后。

## 阶段 5：产品化与可靠性

- 前端以“任务进度、证据侧栏、风险列表、审核动作”为主，不要只做聊天窗口。
- 加用户与文档权限，检索阶段就过滤无权限文档，而不是生成后再隐藏。
- 建立日志、Trace、模型成本和延迟指标；敏感内容脱敏后再进入日志。
- Docker 化部署；开发期 SQLite，演示或多人环境迁移 PostgreSQL + pgvector/独立向量库。

## 阶段 6：企业业务文档场景扩展

合同证据链和评测基线稳定后，按 [`product_requirements.md`](product_requirements.md) 的 B → C → D 顺序扩展：

1. **招标要求清单**：确认文档类型，定义资格、交付、验收、付款和时间要求 Schema；处理表格与附件，逐项输出 Citation 和待确认状态。用人工标注的强制要求检查遗漏。
2. **现行制度问答**：记录组织权限、制度版本和生效日期；检索限定在适用版本，过期或冲突时暂停给出肯定结论。单独评测拒答和版本误用。
3. **跨文档一致性核对**：建立招标要求到投标响应、合同条款到制度规则的对应关系；每项差异保留双侧 Citation，区分缺失、偏离与无法判断。

复用解析、RAG、任务状态、权限和 Citation 验证；为每个场景新增领域 Schema、规则、数据卡、评测集和前端视图。新增接口先更新 [`api_contract.md`](api_contract.md)，现有合同任务保持兼容。现阶段的 `document_classifier.py` 只是占位，不能据此宣称已支持自动分类。

## 不建议一开始做的内容

- 不要一开始上 Kubernetes、微服务、复杂消息队列。
- 不要为“多 Agent”而多 Agent；单文档 Q&A 与确定性抽取用普通服务即可。
- 不要宣称法律结论；项目定位为“风险提示与人工审阅辅助”。
