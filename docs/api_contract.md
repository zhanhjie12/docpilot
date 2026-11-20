# DocPilot 前后端与 Agent 接口约定

本文是前后端共同使用的 MVP 接口合同。后端响应、前端 TypeScript 类型和 Agent 节点
都以这里的字段语义为准。接口版本固定在 `/api/v1`；字段新增必须向后兼容，字段删除
或语义改变需要提高版本号。

当前筛选与审阅接口的字段、规则和结果均为合同语义。制度问答已有独立的受限版本接口；招标要求和跨文档核对仍需分别补充请求 Schema、Citation 和错误约定。

## 1. 通用约定

### 1.1 请求头

```http
Authorization: Bearer <token>       # AUTH_REQUIRED=true 时业务接口必填
X-Request-ID: <uuid>                # 客户端可传，服务端必须回传或生成
Idempotency-Key: <string>           # 上传、筛选、审阅等创建任务时使用
Content-Type: application/json
```

文件上传使用 `multipart/form-data`，不能同时把 JSON 作为请求体发送。

### 1.2 时间、空值和 ID

- 时间使用 UTC ISO 8601，例如 `2026-09-16T08:30:00Z`。
- ID 使用不透明字符串，前端不能依赖 UUID 格式或自行拼接 ID。
- “没有值”和“尚未分析”都使用 `null`，并通过 `status` 或 `missing_fields` 区分。
- 后端不得把金额、日期或风险等级编码成自由文本；前端按类型显示。

### 1.3 统一错误

所有 4xx/5xx 使用同一结构：

```json
{
  "error": {
    "code": "UNSUPPORTED_FILE",
    "message": "仅支持 PDF 或 DOCX",
    "details": {"allowed_types": ["application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"]},
    "retryable": false,
    "request_id": "req_123"
  }
}
```

建议错误码：`VALIDATION_ERROR`、`UNSUPPORTED_FILE`、`FILE_TOO_LARGE`、`DOCUMENT_NOT_FOUND`、
`TASK_NOT_FOUND`、`PARSE_FAILED`、`INDEX_FAILED`、`MODEL_OUTPUT_INVALID`、`EVIDENCE_MISSING`、
`REVIEW_CONFLICT`、`IDEMPOTENCY_CONFLICT`、`INTERNAL_ERROR`。

请求参数必须先通过 Schema 校验，校验失败统一返回 `422 VALIDATION_ERROR`。请求模型拒绝
未知字段，避免前端拼写错误被静默忽略。尚未支持的能力应返回稳定错误，不提供貌似成功的假结果。

当前已实现的参数边界：文档筛选的 `document_ids` 必须为 1--20 个不重复的非空 ID，
`query` 不能是空白字符串；问答必须提供 `document_ids` 或 `version_ids`，`question` 不能为空，
`top_k` 必须为正整数；审阅决策的 `decision` 为 `approved` 或 `rejected`，且必须携带不小于 1
的 `expected_version`；版本比较的两个版本 ID 必须不同。`Idempotency-Key` 和 `Last-Event-ID`
会校验为 1--128 个可见 ASCII 字符。上传、筛选和审阅创建接口按身份处理幂等键；任务事件支持按序号恢复读取。

审阅创建时，前端为一次逻辑操作生成一个新的 `Idempotency-Key`，在超时或网络失败重试时
复用该键。相同身份、相同键且文档/版本范围相同时，服务端返回首次创建的 `review_id` 和
`task_id`，不会再次启动 Agent；范围内 ID 的顺序不影响判定。相同键用于不同范围时返回
`409 IDEMPOTENCY_CONFLICT`。创建响应的 `status` 固定为首次受理时的 `queued`，前端随后通过
`GET /tasks/{task_id}` 查询实时状态。

## 2. 核心资源状态

### 2.1 文档状态

```text
uploaded → parsing → parsed → indexed
                     └──────→ failed
```

`failed` 必须包含 `failure_code` 和面向用户的 `failure_message`。重新处理创建新的任务，
但不覆盖原文档版本和历史结果。

### 2.2 任务状态

```text
queued → running → waiting_human → succeeded
                  └──────────────→ failed
                  └──────────────→ cancelled
```

`waiting_human` 表示工作流暂停等待审核，不是失败。

## 3. 文档 API

### 3.1 上传文档

```http
POST /api/v1/documents
Content-Type: multipart/form-data
```

表单字段：`file`（必填）、`display_name`（可选）、`document_group_id`（可选）、`document_type`（可选，默认 `contract`；支持 `contract`、`tender`、`bid`、`policy`、`other`）。

`file` 通过 `multipart/form-data` 声明为文件字段；`display_name` 和 `document_group_id`
是普通表单字段，不能把 JSON Body 和文件放在同一个请求中。当前路由会校验文件类型、
大小和内容签名，计算 SHA-256，写入本地对象存储并异步解析与索引。

成功返回 `202 Accepted`：

```json
{
  "document_id": "doc_001",
  "version_id": "ver_001",
  "task_id": "task_parse_001",
  "status": "uploaded",
  "filename": "supplier-contract-v1.pdf",
  "mime_type": "application/pdf",
  "size_bytes": 182736,
  "content_sha256": "...",
  "created_at": "2026-09-16T08:30:00Z"
}
```

后端必须校验扩展名、真实 MIME、大小、空文件和 SHA-256。相同内容重复上传应返回已有
版本或明确的幂等结果，不能静默创建无限重复版本。

### 3.2 查询文档

```http
GET /api/v1/documents/{document_id}
GET /api/v1/documents?status=parsed&limit=20&cursor=...
GET /api/v1/documents/{document_id}/versions
```

文档详情至少包含：`document_id`、`filename`、`status`、`page_count`、`parser_name`、
`parser_version`、`quality_warnings`、`latest_version_id`、`created_at`、`updated_at`。

### 3.3 重新索引

```http
POST /api/v1/documents/{document_id}/reindex
```

返回 `{ "task_id": "...", "status": "queued" }`。只能对已成功上传的版本执行，任务必须幂等。

## 4. 任务与事件 API

### 4.1 查询任务

```http
GET /api/v1/tasks/{task_id}
```

```json
{
  "task_id": "task_review_001",
  "task_type": "screen_and_review",
  "status": "running",
  "progress": 45,
  "current_node": "extract",
  "retry_count": 0,
  "failure": null,
  "result_ref": null,
  "created_at": "2026-09-16T08:30:00Z",
  "updated_at": "2026-09-16T08:31:12Z"
}
```

### 4.2 SSE 事件

```http
GET /api/v1/tasks/{task_id}/events
Accept: text/event-stream
```

每条事件的 `data` 是 JSON：

```json
{
  "event_id": "evt_009",
  "task_id": "task_review_001",
  "sequence": 9,
  "type": "node.completed",
  "node": "extract",
  "progress": 45,
  "message": "已完成关键字段抽取",
  "created_at": "2026-09-16T08:31:12Z",
  "data": {"field_count": 8}
}
```

允许的事件类型：`task.queued`、`task.recovered`、`task.started`、`node.started`、`node.completed`、
`node.retrying`、`node.failed`、`human_review.required`、`task.succeeded`、`task.failed`。

前端按 `sequence` 去重；断线后使用 `Last-Event-ID` 或重新查询任务状态恢复，不依赖事件一定不丢失。

## 5. 多文档筛选 API

### 5.1 创建筛选任务

```http
POST /api/v1/documents/screen
```

请求：

```json
{
  "document_ids": ["doc_001", "doc_002"],
  "query": "金额超过100万元、期限超过一年且风险较高",
  "conditions": {
    "amount": {"field": "amount", "operator": "gt", "value": 1000000},
    "duration_days": {"field": "duration_days", "operator": "gt", "value": 365},
    "risk": "high"
  },
  "deep_analysis": true
}
```

当前版本还没有 Query Analyzer。`conditions` 至少要包含一个明确条件；只传 `query`
会返回 `422 SCREENING_CONDITIONS_REQUIRED`，不会把所有文档误判为命中。
`deep_analysis=true` 会在筛选完成后为每份命中文档创建独立 Agent 审阅任务，结果项返回
`review_id` 和 `review_task_id`；未命中文档不启动审阅。

成功返回 `202 Accepted`：

```json
{
  "task_id": "task_screen_001",
  "status": "queued",
  "query": "金额超过100万元、期限超过一年且风险较高",
  "conditions": {"...": "..."},
  "document_count": 2
}
```

### 5.2 查询筛选结果

```http
GET /api/v1/tasks/{task_id}/screening-result
```

```json
{
  "task_id": "task_screen_001",
  "status": "succeeded",
  "total": 2,
  "matched": 1,
  "items": [
    {
      "document_id": "doc_001",
      "matched": true,
      "matched_conditions": ["amount", "duration_days", "risk"],
      "missing_fields": [],
      "facts": {"amount": 2300000, "duration_days": 730, "risk": "high"},
      "review_id": "review_001",
      "review_task_id": "task_review_001"
    }
  ]
}
```

## 6. 问答 API

```http
POST /api/v1/chat/sessions
POST /api/v1/chat/{session_id}/ask
GET  /api/v1/chat/{session_id}/messages
```

请求必须指定 `document_ids` 或 `version_ids`，不能默认搜索所有文档：

```json
{
  "question": "付款条件是什么？",
  "document_ids": ["doc_001"],
  "top_k": 5
}
```

回答：

```json
{
  "answer": "合同约定验收合格后 30 日内付款。",
  "status": "answered",
  "needs_confirmation": false,
  "citations": [
    {
      "citation_id": "cite_001",
      "document_id": "doc_001",
      "version_id": "ver_001",
      "page": 12,
      "section": "付款条款",
      "quote": "验收合格后30日内支付...",
      "char_start": 1842,
      "char_end": 1858
    }
  ]
}
```

无证据时返回 `status: "insufficient_evidence"`，`answer` 可以是空值，不能生成肯定结论。

当前问答路由已校验文档范围、问题和 `top_k`，并支持检索、消息保存和 Citation 返回。
回答为已检索原文摘录；第三方 LLM 生成尚未接入。

## 7. 用户注册与登录 API

```http
POST /api/v1/auth/register
POST /api/v1/auth/login
GET  /api/v1/auth/me
```

注册请求：

```json
{
  "email": "user@example.com",
  "password": "至少 8 位的密码",
  "display_name": "合同管理员"
}
```

注册和登录成功后返回：

```json
{
  "access_token": "<signed-token>",
  "token_type": "bearer",
  "expires_in": 3600,
  "user": {
    "user_id": "user_...",
    "email": "user@example.com",
    "display_name": "合同管理员",
    "role": "user",
    "created_at": "2026-09-23T00:00:00Z"
  }
}
```

后续业务请求可携带：

```http
Authorization: Bearer <access_token>
```

`GET /api/v1/auth/me` 用于前端刷新页面后恢复登录状态。开发环境默认允许匿名使用
业务接口；部署环境设置 `AUTH_REQUIRED=true` 后，文档、筛选、问答和审阅接口必须登录。
认证请求创建的文档、任务、审阅和问答会话会记录 `owner_id`，其他用户只能看到自己的资源。
匿名开发请求仍可访问历史匿名资源；切换到强制认证前应先为已有数据分配归属。

## 8. 审阅、人工确认与报告 API

```http
POST /api/v1/reviews
GET  /api/v1/reviews/{review_id}
GET  /api/v1/reviews/{review_id}/workflow
GET  /api/v1/reviews/{review_id}/findings
POST /api/v1/reviews/{review_id}/findings/{finding_id}/decision
POST /api/v1/reviews/{review_id}/compare
GET  /api/v1/reviews/{review_id}/report?format=json|markdown|csv
```

风险项结构：

```json
{
  "finding_id": "finding_001",
  "document_id": "doc_001",
  "version_id": "ver_001",
  "severity": "high",
  "rule_id": "liability_cap",
  "rule_version": "2026-09-01",
  "title": "责任上限缺失",
  "rule_hit": true,
  "explanation": "条款未发现明确的责任上限。",
  "recommendation": "要求补充责任上限并由法务确认例外情形。",
  "citations": [{"citation_id": "cite_001", "document_id": "doc_001", "version_id": "ver_001", "page": 2, "section": "责任条款", "quote": "原文片段", "char_start": 30, "char_end": 34}],
  "review_status": "needs_review",
  "review_note": null
}
```

人工决策请求：

```json
{
  "decision": "approved",
  "note": "接受该风险，采购负责人已确认。",
  "expected_version": 2
}
```

`expected_version` 不一致时返回 `409 REVIEW_CONFLICT`，前端必须重新拉取后再提交。

审阅创建后返回 `review_id` 和 `task_id`。任务经过 retrieve → extract → risk → verify → report；
`GET /workflow` 返回节点状态、核验结果和结构化引用，`GET /findings` 仅返回验证通过的风险项。
有待人工处理的风险时，任务状态为 `waiting_human`；所有风险项完成决定后为 `succeeded`。
报告支持 JSON、Markdown 和 CSV。版本比较只允许审阅范围内、当前身份拥有的版本。
当前 Agent 使用确定性抽取与规则，第三方 LLM 解释器尚未接入。

Review、Task 与初始 `task.queued` 事件在同一数据库事务中创建；事务提交后才投递后台 runner。
应用启动时会重新投递仍为 `queued` 的持久任务并写入 `task.recovered` 事件。`running` 任务不自动
重跑，因为它可能已经调用模型或写入部分结果；恢复运行中任务需要后续的 checkpoint 与租约机制。

## 9. Agent 节点接口

Agent 节点不直接接收 HTTP Request，也不返回自然语言自由格式。节点读取并更新
`backend/app/agents/state.py` 中的 `TaskState`：

```python
state.task_id               # 任务 ID
state.task_type             # review / qa / compare / screen_and_review
state.user_id               # 文档归属
state.document_ids          # 显式文档范围
state.version_ids           # 显式版本范围
state.evidence              # 结构化 Citation，不保存完整合同
state.extracted_fields      # 每个文档版本的字段和状态
state.risks                 # 待人工确认的规则命中
state.node_results          # 各节点执行结果
```

统一节点结果：

```json
{
  "node": "verify",
  "status": "succeeded|waiting_human|failed",
  "output": {},
  "error": null,
  "attempts": 1
}
```

节点职责：

| 节点 | 输入 | 输出 |
|---|---|---|
| Parser | 已解析文档结构 | 页面和质量告警；当前上传链负责实际解析 |
| Classifier | 文档结构 | 后续增加文档类型和置信度 |
| Extractor | 相关 Chunk | 十类合同字段、字段状态和 Citation |
| Query Analyzer | 用户筛选语句 | 后续增加；当前必须传结构化 `conditions` |
| Filter | 字段和条件 | 命中文档、缺失字段 |
| Retriever | 文档范围和问题 | Chunk、分数、Citation 原料 |
| Risk/Review | 条款、规则和证据 | `RiskFinding` |
| Comparator | 两个已索引版本 | Diff、变更条目和双侧 Citation |
| Verification | 结构化结论和 Citation | 已验证或待确认结果 |
| Report | 已验证结果 | JSON/Markdown/CSV 报告 |

节点不得保存完整思维链。当前任务事件记录节点、进度、重试和脱敏摘要；模型版本、Token 和费用统计需在模型接入后增加。

## 10. 前端 TypeScript 映射

前端类型应与后端 Schema 一一对应，禁止使用 `any`：

```ts
export type TaskStatus =
  | "queued" | "running" | "waiting_human"
  | "succeeded" | "failed" | "cancelled";

export interface TaskSummary {
  task_id: string;
  task_type: string;
  status: TaskStatus;
  progress: number;
  current_node: string | null;
  retry_count: number;
  failure: { code: string; message: string } | null;
  result_ref: string | null;
  created_at: string;
  updated_at: string;
}

export interface Citation {
  citation_id: string;
  document_id: string;
  version_id: string;
  page: number | null;
  section: string | null;
  quote: string;
  char_start: number | null;
  char_end: number | null;
}
```

`shared/services/api.ts` 统一处理 Base URL、超时、错误解析、上传、轮询和 SSE；页面组件只调用
类型化方法，不直接拼接 URL 或解析后端原始错误。

## 11. 实现顺序和兼容规则

1. 先实现文档上传、任务查询和 Citation 类型。
2. 再实现筛选请求、筛选结果和审阅风险项。
3. 最后实现问答流式事件和人工审核冲突处理。
4. 后端先返回完整字段，前端未知字段必须忽略，不能因新增字段崩溃。
5. 对于尚未实现的接口，可以返回统一的 `501 NOT_IMPLEMENTED`，不要返回结构不稳定的临时 JSON。

## 12. 当前实现状态

### 制度版本确认与问答（独立场景）

```http
PUT /api/v1/policies/versions/{version_id}/metadata
POST /api/v1/policies/ask
```

`PUT` 请求包含 `policy_family_id`、`issuer`、`org_scope`、`effective_from`、可空的
`effective_to` 和 `status`（`draft|active|retired`）。日期为 ISO 日期；`effective_to`
为排他上界。该操作必须属于当前用户已上传的 `policy` 文档版本。

`POST` 请求包含显式 `document_ids`、`org_scope`、`as_of_date`、`question` 和可选
`top_k`。服务端先验证归属及文档类型，再筛选已索引、已确认生效的版本。没有适用版本时
返回 `insufficient_evidence`；同一制度系列同时适用多个版本时返回
`409 POLICY_VERSION_CONFLICT`。成功结果包含 `answer`、`citations` 和
`policy_versions`，模型只能选择可回放的原文片段。

### 招标要求与投标证据对齐

```http
POST /api/v1/tenders/check
GET /api/v1/tenders/versions/{version_id}/requirements
```

请求体包含 `tender_version_id`、`bid_version_id`、1–20 条
`requirement_queries`，以及可选的 `top_k`（1–5）。两个版本必须分别属于当前用户的
已索引 `tender` 与 `bid` 文档。每条结果独立返回 `tender_citations`、
`bid_citations` 和 `status`：`requirement_unverified`、`bid_evidence_missing` 或
`needs_review`。这些状态表示证据检索情况；接口始终返回
`needs_confirmation: true`，不直接断言投标合规。

`GET` 要求已索引且属于当前用户的招标版本，从原文中提取最多 50 条带引用的要求句。
每条仍是 `needs_review` 候选；系统不会把关键词匹配当成正式招标要求。

### 证据不足的人工结案

```http
POST /api/v1/reviews/{review_id}/close-unresolved
```

请求体为 `note` 和 `expected_version`。仅当审阅处于 `needs_review` 且没有待处理
风险项时可使用。服务端以版本号乐观锁在同一事务写入人工备注，将审阅置为
`closed_unresolved`、任务置为 `cancelled`，不把证据不足伪装成审阅成功。


The local backend implements the document, task, screening, chat, review, policy and tender
paths described above. These paths return persisted results when the service
starts with its lifespan (`uvicorn app.main:app`).

The bounded Agent workflow is connected to the existing review HTTP API.
`POST /reviews` creates a task, `/reviews/{review_id}/workflow` returns the
persisted node snapshot, and `/reviews/{review_id}/findings` returns verified
risks. Screening can create child review tasks for matched documents. Optional
model assistance appears only as `model_assistance` drafts in the verified
report; it does not change a Playbook hit or severity. Natural-language
condition parsing and real Chinese business-quality acceptance remain later work.
