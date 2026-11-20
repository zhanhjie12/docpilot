# DocPilot Agent 需求与实施边界

> 实施状态（2026-10-21）：合同固定图、十类审阅意图、五项版本化风险规则、
> 可选模型建议、原文验证和人工结案已具备基础实现。制度问答按人工确认的版本与日期
> 过滤；招标要求提取和投标证据对齐只输出待复核候选。下文仍保留目标需求，
> 不代表真实中文业务准确率、语义合规判断或企业级部署已经验收。

## 1. 结论先行

DocPilot 面向企业业务文档分析；当前 Agent 的主线是一个明确的合同业务闭环，后续扩展到招投标材料和内部制度。该闭环是：

> 采购或合同管理员上传一批供应商合同，系统先按条件筛选，再对命中文档做证据可追溯的条款抽取和风险提示，最后由人确认并导出报告。

这个闭环直接复用当前已经可用的文档解析、分块、Embedding、混合检索、筛选和规则审阅能力。Agent 负责调度、结构化抽取、证据校验和报告编排，不重复实现解析器、数据库或检索器。

## 2. 真实用户和使用场景

| 优先级 | 场景 | 触发方式 | 系统处理 | 用户得到的结果 | 业务价值 |
|---|---|---|---|---|---|
| P0 | 供应商合同批量初筛 | 采购上传不超过 20 份合同，输入“金额超过 100 万且付款期超过 60 天” | 解析查询条件，使用显式字段筛选；字段缺失单独标记；只对命中文档启动深度分析 | 匹配/未匹配/无法判断列表，金额、期限、风险和引用页码 | 快速找出需要法务介入的合同，降低人工通读量 |
| P0 | 签署前合同审阅 | 合同管理员选择一份合同并选择公司 Playbook | 检索相关条款，抽取付款、验收、责任上限、续约、保密、争议解决等字段；按版本化规则生成风险项 | 风险等级、规则命中、原文引用、修改建议、待确认项 | 把审阅重点标准化，减少漏看关键条款 |
| P1 | 版本变更审查 | 用户选择基准版本和目标版本 | 先按章节对齐，再识别新增、删除、修改和语义变化；两边分别保留引用 | 变更清单、业务影响、需要重新确认的风险 | 避免只看字面 diff 而漏掉实质变化 |
| P1 | 证据问答 | 用户在明确的文档范围内提问“验收后多久付款” | 在范围内检索少量证据，验证引用后生成答案；无证据则返回未知/待确认 | 简短答案、引用片段、页码和证据状态 | 让用户快速定位条款，不把问答变成无依据的法律结论 |
| P2 | 合同台账和续约提醒 | 用户查询未来 30/60/90 天到期合同 | 依赖稳定的日期抽取和任务调度，筛选即将到期合同并检查续约义务 | 到期清单、续约条款引用、待办负责人 | 从单次审阅扩展到合同运营 |

### 扩展场景：企业业务文档

这些是后续需求，不属于下文已实现的合同 Agent 节点。扩展顺序和验收指标以 [`product_requirements.md`](product_requirements.md) 为准。

| 优先级 | 场景 | 用户输入 | Agent 应完成的任务 | 人工核对点 |
| --- | --- | --- | --- | --- |
| B | 招标要求清单 | 一份招标文件及其附件 | 在限定文档内检索并抽取资格、交付、验收、付款和时间要求；逐项提供 Citation | 强制要求遗漏、跨页表格和模糊条件 |
| C | 现行制度问答 | 问题、授权的制度文档及适用日期 | 检索有效版本，给出有出处的答案；版本冲突时返回待确认 | 制度效力、适用组织和例外条款 |
| D | 跨文档一致性核对 | 招标要求与投标响应，或合同与制度 | 先匹配要求与响应，再判断已对应、缺失、偏离或无法判断；保留双侧 Citation | 语义偏离、未响应和规则冲突 |

扩展 Agent 可复用任务状态、检索、证据验证和人工确认；字段 Schema、规则和报告视图按场景独立定义。`review`、`screen_and_review` 等当前任务类型仍是合同语义；新增任务类型和 API 字段必须先写入接口文档并保持旧调用兼容。文档分类与场景选择需要用户可确认的结果，不能让低置信度分类自动触发错误的规则集。

### 不纳入首个 Agent 版本

- 泛化的“什么都能问”的法律聊天机器人。
- 无人工确认的自动法律结论、自动放行或自动拒签。
- 一开始支持扫描件 OCR、所有 Office 格式、跨租户权限和外部系统同步。
- 一开始做 DOCX 红线稿、自动谈判或复杂多 Agent 协商。
- 把招投标和制度分析伪装成已实现的合同审阅，或让一个通用 Prompt 对所有文档做风险结论。

这些能力可以在证据链闭环稳定、评测数据充足后再做。

## 3. Agent 与现有服务的边界

| 能力 | 负责模块 | Agent 的职责 |
|---|---|---|
| 文件接收、格式校验、版本保存 | `backend/app/services/document_service.py`、`backend/app/infrastructure/` | 调用并读取任务结果，不直接处理上传流 |
| PDF/DOCX 解析、页码和段落 | `backend/app/services/document_parser.py` | 检查解析质量，决定是否需要人工处理 |
| 分块、Embedding、混合检索 | `backend/app/rag/`、`backend/app/infrastructure/embedding.py` | 组织查询、限制范围和 Top-K，接收带 Citation 的证据 |
| 元数据筛选 | `backend/app/screening/` | 作为深度分析前的门控，避免对整批原文调用模型 |
| 规则命中和风险持久化 | `backend/app/services/review_service.py`、`backend/app/repositories/` | 组合规则结果、模型解释、证据和人工状态 |
| 工作流状态和任务事件 | `backend/app/infrastructure/tasks.py`、`backend/app/api/routes/tasks.py` | 产出节点事件、重试和 `waiting_human` 状态 |
| HTTP 请求和权限 | `backend/app/api/routes/` | 不允许 Agent 直接依赖 FastAPI Request 或路由实现 |

原则：Agent 节点只接收可序列化的任务上下文和引用，输出经过 Schema 校验的结构化结果；整份原文、完整思维链和未验证的自由文本不得进入持久化状态。

## 4. 首个闭环的工作流

```text
用户任务
  -> Supervisor 识别任务类型并选择固定流程
  -> Filter/Screening 先缩小文档范围
  -> RetrievalAgent 找到相关 Chunk 和 Citation
  -> ExtractionAgent 抽取字段、条款和置信度
  -> RiskAgent 按版本化 Playbook 生成候选风险
  -> VerificationAgent 检查结论与证据是否一一对应
  -> ReportAgent 输出 JSON/Markdown/CSV 报告
  -> 有冲突、低置信度或缺证据时进入 waiting_human
```

建议支持四种固定任务类型：

| `task_type` | 固定流程 | 是否允许模型生成 |
|---|---|---|
| `screen_and_review` | 条件解析 → 筛选 → 抽取 → 风险 → 验证 → 报告 | 仅用于不确定条件和解释补充 |
| `review` | 检索 → 抽取 → 规则风险 → 验证 → 报告 | 可用于字段抽取和风险说明，但必须有 Citation |
| `compare` | 两版本解析 → 章节对齐 → 变更识别 → 验证 → 报告 | 可用于语义影响摘要，不替代差异事实 |
| `qa` | 范围校验 → 检索 → 证据包 → 验证 → 回答 | 无证据时不得生成肯定答案 |

## 5. 节点职责和代码落点

以下是职责与实施顺序；部分文件现已投入运行，具体状态以开发日志和测试为准：

### P0：先完成可运行的审阅闭环

1. `state.py`：定义 `TaskState`、`NodeContext`、`NodeResult`、`EvidenceRef`、`ExtractedField`、`RiskFinding`。所有字段可 JSON 序列化，明确 `missing`、`unknown`、`needs_review`，不能用空字符串代替。
2. `supervisor.py`：只允许上表中的固定 `task_type`，设置最大步数、节点超时、重试次数和降级路径；不接受自由生成的下一步。
3. `retrieval_agent.py`：调用现有 RAG 检索服务，强制使用 `document_ids`/`version_ids` 范围，输出 Chunk、分数和 `Citation` 原料。
4. `extraction_agent.py`：针对 6～10 个 Playbook 字段输出类型化值、原文证据、置信度和缺失原因。模型输出必须经过 Pydantic 校验；失败时返回 `needs_review`。
5. `risk_agent.py`：先执行确定性规则，再保存模型解释；`rule_hit`、`explanation`、`citations`、`review_status` 分开存储。
6. `verification_agent.py`：检查每个字段、风险和答案至少有一个有效 Citation；页码、版本和文档范围必须一致。失败时重检索一次，仍失败则标记未知或转人工。
7. `report_agent.py`：只消费验证通过的结构化结果，输出稳定的审阅报告，不在此处解析、检索或写数据库。
8. `graph.py`：先实现一个轻量的顺序执行器；等状态和接口稳定后再考虑 LangGraph，不把框架作为前置条件。
9. `tests/test_agent_workflow.py`：覆盖正常流程、缺证据、冲突、超时、有限重试、幂等和人工暂停；LLM 使用 fake client，不依赖线上模型。

### P1：再做版本对比和可配置规则

- `comparison_agent.py`：章节/语义对齐、变更分类、双版本 Citation、业务影响摘要。
- `domain/schemas.py` 或独立 `domain/agent_schemas.py`：补充 `ClauseChange`、`PlaybookRule`、`ReviewReport`。
- `services/review_service.py`：把规则从代码常量迁移为带 `rule_version` 的配置，保留历史结果可重放。

### P2：最后扩展能力

- `parser_agent.py`：只做解析质量检查和异常页报告，不重新实现解析。
- Query Analyzer、分类器、续约提醒、权限过滤、外部系统同步。

## 6. 证据、置信度和人工确认规则

1. 结论必须能回到 `document_id`、`version_id`、页码、章节和原文片段；不能只保存一段模型解释。
2. `missing` 表示原文没有找到字段，`unknown` 表示证据不足或冲突，二者都不能当作满足筛选条件。
3. 下列情况必须进入 `waiting_human`：关键字段冲突、金额/日期无法归一化、规则命中但证据不完整、模型输出 Schema 校验失败、引用页码不存在。
4. 人工确认只改变审核状态和备注，不覆盖原始证据、规则版本或模型输出。
5. 报告明确区分“规则命中”“模型解释”“AI 建议”“人工已确认”，不宣称法律结论。

## 7. 可验收标准

首个 Agent 版本完成前，应满足以下条件：

- 对 20 份以内合同执行 `screen_and_review`，只有筛选命中文档进入深度抽取。
- 每个关键字段和风险项都有有效 Citation；缺证据的结论会被 Verification 拦截。
- 同一 `version_id + task_type + rule_version` 重复运行不会产生重复结果，历史结果可查询。
- 节点失败最多按配置重试，超过上限进入 `failed`；等待人工时为 `waiting_human` 而不是失败。
- 前端能展示当前节点、进度、风险清单、证据侧栏和人工决策，不能只展示一段聊天文本。
- 在 30～100 份脱敏合同上记录筛选 F1、字段准确率、风险误报/漏报、Citation 页码准确率、P50/P95 延迟和单份成本。

## 8. 需要产品方尽早确定的事项

- 第一版 Playbook 的 6～10 条规则、严重等级和例外情形。
- 金额、期限、币种、日期冲突时的归一化约定。
- 哪些风险可以由采购负责人确认，哪些必须由法务确认。
- 报告需要 JSON、Markdown 还是 CSV，是否需要下载审阅历史。
- 第三方中转站的模型协议、超时、费用和敏感合同脱敏策略。

## 9. 当前代码落地状态

`backend/app/agents/` 已完成首个不依赖 LangGraph 的 P0 执行内核：

- `state.py` 提供可 JSON 序列化的任务、证据、抽取字段、风险和事件模型；
- `supervisor.py` 只允许四种固定任务类型，并设置最大步骤和重试上限；
- `retrieval_agent.py`、`extraction_agent.py`、`risk_agent.py`、
  `verification_agent.py`、`report_agent.py` 依次组成证据优先链路；
- `graph.py` 负责顺序执行、有限重试和 `waiting_human` 安全暂停；
- `parser_agent.py` 与 `comparison_agent.py` 提供解析质量检查和双版本事实 diff。

当前 `POST /reviews` 已通过服务层运行上述 Agent 图，状态写入 `agent_runs`，并经
`GET /reviews/{review_id}/workflow` 返回。筛选设为 `deep_analysis=true` 时，为命中的
每份合同创建独立审阅任务；比较接口使用已验证的双版本引用。前端已展示节点进度、
抽取字段、风险证据和人工决定。`tests/test_agent_workflow.py`、`test_agent_nodes.py`、
`test_agent_graph.py` 和 `test_agent_review_api.py` 覆盖工作流与 HTTP 集成。

当前规则只确认原文明确出现的高风险标记；完整 Playbook、第三方 LLM 解释、自然语言
筛选解析和评测基线仍待完成。`LLM_*` 配置不表示已有模型调用。
