# DocPilot：10 个开发日详细实施日志

> 更新时间：2026-10-21  
> 计划状态：合同 Agent 可靠性链路已推进；真实中文业务验收仍待人工标注  
> 时间预算：10 个开发日，每日 4–6 小时，合计约 50 小时。  
> 本文件保留 development_plan_10_days.md 文件名，避免既有链接失效。

> 场景边界：本轮 10 天计划继续完成合同审阅的证据链和可靠性基线。企业业务文档的扩展顺序为招标要求清单、现行制度问答、跨文档一致性核对，见 [`docs/product_requirements.md`](docs/product_requirements.md)；新场景须另立数据与验收任务，不能按本轮合同测试判定完成。

## 0. 使用方法与记录规则

本文件既是开发计划，也是每日开发日志。每完成一天，在当天标题下更新实际日期、测试结果、实际耗时、阻塞项和产物路径。

| Day | 主题 | 初始状态 | 当日核心产物 |
| --- | --- | --- | --- |
| 1 | 评测数据地基 | ☑ 已完成 | ACORD 数据清单、校验器与数据卡 |
| 2 | 当前检索基线 | ◐ 进行中 | 词法、向量、RRF 指标报告 |
| 3 | 真实 Reranker | □ 待开始 | Cross-Encoder 重排与对比报告 |
| 4 | 审阅意图检索 | ◐ 基础完成 | 十类意图、检索追踪与范围测试；真实召回仍待评测 |
| 5 | 最小受控 LLM | ◐ 基础完成 | Mock、HTTP 适配器、错误边界；真实中转站尚未实测 |
| 6 | 结构化抽取与证据约束 | ◐ 进行中 | 模型建议 Schema、原文子串校验与降级；业务准确率待测 |
| 7 | Playbook v1 | ◐ 基础完成 | 五类可追溯规则；企业阈值和误报率待校准 |
| 8 | Checkpoint 与恢复 | ◐ 基础完成 | 审阅节点快照与过期租约恢复测试 |
| 9 | 任务租约与人工继续 | ◐ 基础完成 | Claim、Lease、Finding 人工决定、证据不足结案及页面刷新恢复；人工补证后重跑待补 |
| 10 | 全链路验收与演示 | ◐ 进行中 | 后端回归和前端构建通过；真实中文业务验收待执行 |

### 2026-10-21 实施记录

- `backend/app/playbooks/`：版本化合同规则；风险命中必须在验证节点重新计算。
- `backend/app/infrastructure/llm/`、`backend/app/agents/model_assist.py`：第三方兼容模型适配，模型字段和解释只作为待复核草稿，引用必须与原文匹配。默认 Mock，不会自动产生真实模型费用。
- `backend/app/agents/review_intents.py`：十类审阅意图检索；`api/routes/policies.py` 按生效日期与组织范围过滤制度版本；`api/routes/tenders.py` 分开检索招标和投标证据，不自动给合规结论。
- `backend/app/infrastructure/tasks.py`、`services/review_service.py`：租约心跳和过期恢复，审阅从已提交的 Agent 节点快照继续。非审阅任务中断后明确失败，需要重试上传或创建任务。
- `frontend/src/features/documents/components/BusinessAnalysisPanel.tsx`：提供制度与招投标的基础 Web 入口。
- 页面刷新后用当前用户的文档/审阅/任务 ID 向后端重新校验和读取进度；不在浏览器保存合同正文。
- `evals/scripts/evaluate_zh_business.py`：校验人工脱敏标注和中文检索指标。现有中文语料仍是合成冒烟集，没有可声称的真实业务准确率。
- 验证：后端 194 项、评测脚本 13 项通过；前端使用 `npm.cmd run build -- --configLoader native` 构建通过。完整人工业务验收和真实中转站联调尚未执行。

### 通用工程约束

1. 新建代码文件写模块说明；类、公共函数、关键数据结构和复杂业务分支写中文注释。
2. 模型只生成候选。字段、风险和答案必须经过 Schema、范围、Citation 与规则验证。
3. 不把 API Key、用户合同正文、完整 Prompt 或模型内部推理写入日志、任务事件、快照或测试夹具。
4. 原始评测数据只读；处理结果、报告和缓存只能写入约定的 processed 或 reports 目录。
5. 指标和测试结果必须来自实际运行。未执行的项目记录为“未执行”，不能补造数值。
6. 每天下班前运行当天测试与已有回归测试，记录命令、结果、失败原因和下一天阻塞项。

### 当前基线与边界

已有能力：React Web 前端、注册登录和资源隔离、PDF/DOCX 上传与索引、Chunk、词法检索、Embedding、RRF、Citation、固定 Agent 图、人工 Finding 决策、报告导出、SQLite 任务与基础幂等。

本轮优先补齐：评测集、真实 Reranker、受控 LLM、结构化候选、版本化 Playbook、Checkpoint、租约和 Human Gate。

本轮不实现：PostgreSQL、Redis、独立 Worker、Outbox、Kubernetes、OCR、Excel、自由编排多 Agent、Embedding 微调。只有评测证明正确条款经常未进入 Top 10 时，才评估 Embedding 模型更换或微调；已召回但排序靠后时，优先优化 Reranker。

---

# Day 1：评测数据地基

> 状态：☑ 已完成  
> 实际日期：2026-10-12  
> 实际耗时：本次会话未计时  
> 测试结果：校验器 12 项通过；真实数据校验通过  
> 阻塞项：无

## 当日目标

建立 ACORD 公开数据的原件层、处理层、来源记录和自动校验工具。后续检索、Reranker 和模型效果只能基于通过校验的数据集评测。

ACORD 主要用于英文合同检索工程基线，不代表中文合同理解能力，也不能单独用来选择中文 Embedding 模型。

## 前置条件

- 后端虚拟环境 backend/.venv 可以运行 Python。
- 已从 ACORD 官方来源下载数据，并确认许可证允许本地评测使用。
- 原始数据包含 corpus.jsonl、queries.jsonl、qrels/test.tsv 或等价的 BEIR 结构。
- 不把真实用户合同、密钥或内部资料放入 evals/raw。

## 文件清单

| 操作 | 文件或目录 | 实施内容 |
| --- | --- | --- |
| 新建 | evals/raw/acord/.gitkeep | 固定原始数据放置位置。 |
| 新建 | evals/processed/acord_v1/.gitkeep | 保存校验报告和后续转换结果。 |
| 新建 | evals/scripts/__init__.py | 使校验工具可被测试导入。 |
| 新建 | evals/scripts/validate_acord.py | 只读校验 ACORD，并输出机器可读报告。 |
| 新建 | evals/tests/test_validate_acord.py | 覆盖正常和异常数据。 |
| 新建 | evals/tests/fixtures/acord_valid | 最小合法 BEIR 样例。 |
| 新建 | evals/tests/fixtures/acord_invalid | 空文本、重复 ID、无效 qrels 等样例。 |
| 新建 | evals/dataset_card.md | 说明来源、用途、许可证、局限和中文集规划。 |
| 新建 | evals/manifest.json | 保存版本、下载日期、文件大小和 SHA-256。 |
| 修改 | evals/README.md | 写入目录约束、初始化和校验命令。 |
| 视情况修改 | .gitignore | 忽略大体积原始数据与临时处理文件，只保留目录占位符。 |

## 实施步骤

1. 创建 raw/acord、processed/acord_v1、scripts、tests 和 fixtures 目录。
2. 将下载内容原样放入 evals/raw/acord。保留官方文件名、ID、qrels 标签和切分；处理脚本不得写入该目录。
3. 编写 manifest.json，至少包含 dataset_id、source_url、source_revision、downloaded_at、license、files[].path、files[].bytes、files[].sha256。
4. 编写 dataset_card.md，明确 ACORD 的英文检索用途、已知局限、原件不可改写规则，以及中文脱敏集的授权与标注要求。
5. 在数据卡中固定第一批中文审阅意图：payment_terms、contract_term、renewal、acceptance、liability_cap、confidentiality、dispute_resolution、termination。它们是后续评测标签范围，不自动等同于法律风险。
6. 实现 validate_acord.py：
   - 严格以 UTF-8 读取 JSONL 与 TSV。
   - 检查 corpus、queries 的 ID 与文本非空。
   - 检查 corpus ID、query ID、query_id 与 corpus_id 对是否重复。
   - 检查 qrels 中的 query 和 corpus 引用存在，相关度为 ACORD 定义的 0–4 整数。
   - 计算 SHA-256，并与 manifest.json 对比。
   - 将 valid、计数、哈希和错误列表写入 processed/acord_v1/validation_report.json。
   - 每个错误包含文件、行号、记录 ID 和原因；失败进程退出码为 1。
7. 先使用 print-hashes 模式生成哈希，人工写入 manifest.json；再使用 verify-hashes 模式验证固定基线。
8. 编写最小合法样例和五类异常样例：非法 UTF-8、空文本、重复 ID、无效 qrels、哈希变化。

## 测试与命令

~~~powershell
$ProjectRoot = 'D:\go\docpilot'
$Python = "$ProjectRoot\backend\.venv\Scripts\python.exe"
& $Python -m pytest "$ProjectRoot\evals\tests\test_validate_acord.py" -q
& $Python "$ProjectRoot\evals\scripts\validate_acord.py" --raw-dir "$ProjectRoot\evals\raw\acord" --manifest "$ProjectRoot\evals\manifest.json" --print-hashes
& $Python "$ProjectRoot\evals\scripts\validate_acord.py" --raw-dir "$ProjectRoot\evals\raw\acord" --manifest "$ProjectRoot\evals\manifest.json" --report "$ProjectRoot\evals\processed\acord_v1\validation_report.json" --verify-hashes
~~~

## 验收与日志

- validation_report.json 的 valid 为 true。
- manifest.json 的每个原始文件都记录 SHA-256。
- 校验器能精确定位空文本、重复 ID、无效 qrels 与哈希变化。
- 校验前后 raw/acord 的哈希不变。
- README 有可复制的初始化与日常校验命令。

| 工作 | 预计时间 |
| --- | ---: |
| 目录、忽略规则、数据放置 | 0.5 小时 |
| 数据卡和来源清单 | 0.75 小时 |
| 校验脚本 | 2 小时 |
| 夹具和自动化测试 | 1 小时 |
| 固化哈希、报告和 README | 0.5 小时 |
| 合计 | 4.75 小时 |

实际记录：从发布方 Hugging Face 获取 ACORD 原始压缩包，保留原包并原样解压；压缩包 SHA-256 为 `c9e851456a14aed161033b856e9f40f3cc9f46cf7282c2007b143420e93a65c3`。包内 README 确认 CC BY 4.0。`manifest.json` 固定 9 个文件的哈希；严格校验结果 `valid=true`、corpus 3,931、queries 114、qrels 126,659，详见 `evals/processed/acord_v1/validation_report.json`。校验器测试 12 项通过。第 2 天以 `test` 切分和当前 manifest 作为固定输入。

---

# Day 2：当前检索基线

> 状态：◐ 进行中  
> 实际日期：2026-10-13 开始  
> 实际耗时：待填写  
> 测试结果：ACORD test 57 条查询完成词法、向量、RRF 实测；生产词法路径烟测一致；持久化向量检索定向测试 1 项通过  
> 待完成：计划中的独立数据加载/指标模块、系统性单元测试、BM25 与英文模型对比

## 当日目标

对当前词法检索、向量检索和 RRF 混合检索建立可重复的离线基线，为 Day 3 是否值得接入 Reranker 提供证据。

## 前置条件

- Day 1 校验报告通过，manifest 哈希已固定。
- 当前 Embedding Provider 可加载或已配置可用 API。
- 一次评测只使用一个模型、一个设备、一个批大小和一组固定参数。
- 报告标记 ACORD 为英文工程回归基线；中文模型选择需要后续中文脱敏评测集验证。

## 文件清单

| 操作 | 文件 | 实施内容 |
| --- | --- | --- |
| 新建 | evals/__init__.py | 允许以 Python 模块方式运行评测。 |
| 新建 | evals/datasets/__init__.py | 数据加载模块入口。 |
| 新建 | evals/datasets/acord.py | 读取 BEIR corpus、queries、qrels，保留原始 ID。 |
| 新建 | evals/metrics/__init__.py | 指标模块入口。 |
| 新建 | evals/metrics/retrieval.py | Recall、Hit Rate、MRR、nDCG、延迟分位数。 |
| 新建 | evals/scripts/run_retrieval_eval.py | 运行词法、向量和 RRF 的命令行工具。 |
| 新建 | evals/tests/test_acord_loader.py | 覆盖正常加载、空数据、无效 qrels。 |
| 新建 | evals/tests/test_retrieval_metrics.py | 覆盖公式、并列分数和空结果。 |
| 修改 | backend/tests/test_retrieval.py | 从说明文件升级为生产检索原语回归测试。 |
| 修改 | evals/README.md | 说明参数、指标和报告解释。 |
| 生成 | evals/reports/acord_baseline.json | 机器可读基线。 |
| 生成 | evals/reports/acord_baseline.md | 人工可读摘要。 |

## 实施步骤

1. 在 acord.py 读取 corpus、queries 与 `test` qrels，保留官方 corpus_id 与 query_id 和 0–4 分标签；二值 Recall、HitRate 和 MRR 明确以分数 ≥2 为相关，nDCG 使用原始分级分数，发现无效引用立即报错。
2. 将 corpus 适配为现有 Chunk：chunk_id 使用 corpus_id，document_id 为 acord，version_id 为 acord_v1，文本为 title 加换行加 text。此过程仅在内存运行，不写入业务 SQLite。
3. 复用 backend/app/rag/retriever.py 的词法检索、向量检索和 reciprocal_rank_fusion，不复制一套生产算法。
4. 在 retrieval.py 实现并单测：
   - Recall@5、Recall@10：已召回相关条款占全部相关条款的比例。
   - HitRate@5、HitRate@10：至少命中一条相关条款的查询比例。
   - MRR@10：第一条相关结果倒数排名的均值。
   - nDCG@10：使用 qrels 相关度衡量排序质量。
   - P50、P95：每个查询的完整检索耗时，模型冷启动单独记录。
5. 评测命令支持 mode、top-k、query-limit、seed、output 和 Embedding 配置；报告中记录数据集 ID、manifest 哈希、query 数量、模型、设备、批大小、RRF 参数、运行时长和是否抽样。
6. 先对固定 20 条 test query 做冒烟，再对完整 test 切分生成基线。ACORD 全集只有 114 条 query，不能安排 200 条抽样。
7. 报告只保存统计数据、配置和必要 ID，不复制合同正文。

## 测试与命令

~~~powershell
$ProjectRoot = 'D:\go\docpilot'
$Python = "$ProjectRoot\backend\.venv\Scripts\python.exe"
& $Python -m pytest "$ProjectRoot\evals\tests\test_acord_loader.py" "$ProjectRoot\evals\tests\test_retrieval_metrics.py" -q
Push-Location "$ProjectRoot\backend"; & .\.venv\Scripts\python.exe -m pytest tests\test_retrieval.py tests\test_embeddings.py -q; Pop-Location
$env:PYTHONPATH = "$ProjectRoot\backend"
& $Python -m evals.scripts.run_retrieval_eval --dataset acord --raw-dir "$ProjectRoot\evals\raw\acord" --split test --mode all --top-k 5 10 --query-limit 20 --seed 20261012 --output "$ProjectRoot\evals\reports\acord_smoke.json"
& $Python -m evals.scripts.run_retrieval_eval --dataset acord --raw-dir "$ProjectRoot\evals\raw\acord" --split test --mode all --top-k 5 10 --output "$ProjectRoot\evals\reports\acord_baseline.json"
~~~

## 验收与日志

- acord_baseline.json 同时包含 lexical、vector、hybrid 三组结果。
- 相同 manifest、模型、参数和 seed 下，重复结果可比较。
- 20 条 query 冒烟能在本机完成。
- 指标对全错、全对、多个相关文本和并列分数有单元测试。
- 不根据 ACORD 上的一次结果替换中文 Embedding 模型。

| 工作 | 预计时间 |
| --- | ---: |
| ACORD 加载与 Chunk 适配 | 1 小时 |
| 指标实现和单元测试 | 1.5 小时 |
| 运行器和报告格式 | 1.5 小时 |
| 冒烟、基线运行和 README | 1 小时 |
| 合计 | 5 小时 |

记录模板：数据集哈希 ____；模型/设备 ____；20 条冒烟结果 ____；完整 test 基线报告 ____；是否进入 Day 3 ____。

本轮实际记录：复用 `backend/app/rag/retriever.py` 建立 [`evals/scripts/run_acord_retrieval_eval.py`](evals/scripts/run_acord_retrieval_eval.py)，先校验 manifest 中三个输入文件的哈希，再对 3,931 条条款和 57 条 test 查询计算词法、`BAAI/bge-small-zh-v1.5` 本地向量及 RRF 指标。完整结果见 [`evals/reports/acord_retrieval_summary.md`](evals/reports/acord_retrieval_summary.md) 和同目录 JSON。HitRate@5 分别为 14.04%、31.58%、22.81%；测试模型为中文模型，ACORD 为英文数据，不能据此决定中文生产模型。独立 SQLite 导入 3,931 条后，`search_chunks` 示例查询前 10 ID 与内存词法结果一致。当前仍是检索原语测试，尚未完成计划中的正式 Day 2 验收。

中文语料补充记录：建立自编合成 [`evals/datasets/zh_contract_smoke_v1.json`](evals/datasets/zh_contract_smoke_v1.json)，覆盖八类意图的 32 条短条款、24 个问题；词法、向量、RRF 的 HitRate@5 均为 100%，排序差异和误排案例见 [`evals/reports/zh_contract_smoke_v1.md`](evals/reports/zh_contract_smoke_v1.md)。隔离 SQLite 中生产 `search_chunks` 的词法和融合前 10 ID 与内存评测各 24/24 一致。这只能证明小型中文检索链路可运行，不能作为真实中文合同的质量验收；仍需授权脱敏合同与独立人工标签。

---

# Day 3：接入真实 Reranker

> 状态：□ 待开始  
> 实际日期：待填写  
> 实际耗时：待填写  
> 测试结果：待填写  
> 阻塞项：待填写

## 当日目标

把当前按检索分数排序的 reranker.py 升级为真实 Cross-Encoder 重排：先召回 Top 30 候选，再重排，最后返回 Top 5，同时保持关闭和故障时的安全回退。

OpenAI-compatible 聊天规范没有统一的 rerank 接口，因此本日只实现本地 Cross-Encoder。未来接入第三方 rerank 服务时，单独实现其明确的私有协议。

## 前置条件

- Day 2 已保存 acord_baseline.json 和固定 query 集。
- 已选择可本地运行的 Cross-Encoder，记录模型版本、缓存容量和设备。
- sentence-transformers 可选依赖已安装。
- 模型应懒加载，FastAPI 启动阶段不得自动下载或占用显存。

## 文件清单

| 操作 | 文件 | 实施内容 |
| --- | --- | --- |
| 修改 | backend/app/infrastructure/providers.py | 补全 Reranker 协议、模型元数据和返回约束。 |
| 新建 | backend/app/infrastructure/reranking.py | LocalCrossEncoderReranker、异常类型和工厂。 |
| 修改 | backend/app/core/config.py | 增加 provider、模型、设备、批大小和候选数配置。 |
| 修改 | .env.example | 保留默认关闭，增加本地启用示例。 |
| 修改 | backend/app/main.py | 生命周期创建、注入和关闭 Reranker。 |
| 修改 | backend/app/api/deps.py | 增加 get_reranker 依赖。 |
| 修改 | backend/app/services/task_dispatcher.py | 恢复任务时注入 Reranker。 |
| 修改 | backend/app/services/review_service.py | 将 Reranker 传至 Agent Graph。 |
| 修改 | backend/app/agents/graph.py | 组装检索节点时传递 Reranker。 |
| 修改 | backend/app/agents/retrieval_agent.py | 审阅和问答均可使用重排。 |
| 修改 | backend/app/api/routes/chat.py | 问答路由注入 Reranker。 |
| 修改 | backend/app/api/routes/reviews.py | 创建审阅任务时注入 Reranker。 |
| 修改 | backend/app/rag/retriever.py | 结果保留初始分数、Rerank 分数与召回来源。 |
| 修改 | backend/app/rag/reranker.py | 候选校验、批量重排、稳定排序与回退。 |
| 修改 | backend/app/rag/search.py | RRF 后对 Top 30 候选执行重排。 |
| 新建 | backend/tests/test_reranker.py | 开启、关闭、异常回退和元数据测试。 |
| 修改 | backend/tests/test_embeddings.py | 混合检索加 Reranker 集成测试。 |
| 修改 | backend/tests/test_agent_nodes.py | Agent 引用仍隔离用户、文档与版本。 |
| 修改 | evals/scripts/run_retrieval_eval.py | 支持 rerank 参数及报告记录。 |
| 生成 | evals/reports/acord_rerank_v1.json | 与 Day 2 同一输入的对比报告。 |

## 实施步骤

1. 新增配置：RERANK_PROVIDER 为 disabled 或 local，RERANK_MODEL、RERANK_DEVICE、RERANK_BATCH_SIZE、RERANK_CANDIDATE_LIMIT。默认 disabled。
2. 实现 LocalCrossEncoderReranker：使用 sentence_transformers.CrossEncoder，批量打分 query 与 passage 对，只返回有限数值；输入和输出数量必须一致。
3. 扩展 RetrievedChunk，保留 lexical_score、vector_score、initial_score、rerank_score、sources 和最终 score。
4. 修改 search_chunks：词法与向量先召回，RRF 后保留最多 30 条候选；Reranker 可用时重排；按稳定的 tie break 返回前 5 条。
5. 当模型加载、超时、分数长度或数值异常时，写入不含正文的降级信息，返回原始混合排序。关闭 Reranker 时，结果顺序兼容 Day 2。
6. 将依赖经 lifespan、app.state、Depends、TaskRunnerFactory、Review Runner、Agent Graph、Chat 路由完整传递，避免只在问答中生效。
7. 评测运行器增加 rerank、rerank-provider、rerank-model、candidate-limit 参数，并记录启用状态、候选数和降级次数。
8. 使用 Day 2 的相同 manifest、seed、query 数量和 top-k 生成前后报告。

## 测试与命令

~~~powershell
Set-Location D:\go\docpilot\backend
.\.venv\Scripts\python.exe -m pytest tests\test_reranker.py tests\test_embeddings.py tests\test_agent_nodes.py -q
.\.venv\Scripts\python.exe -m pytest -q
$ProjectRoot = 'D:\go\docpilot'
$Python = "$ProjectRoot\backend\.venv\Scripts\python.exe"
$env:PYTHONPATH = "$ProjectRoot\backend"
& $Python -m evals.scripts.run_retrieval_eval --dataset acord --raw-dir "$ProjectRoot\evals\raw\acord" --split test --mode hybrid --rerank --top-k 5 10 --candidate-limit 30 --output "$ProjectRoot\evals\reports\acord_rerank_v1.json"
~~~

## 验收与日志

- RERANK_PROVIDER=disabled 时，检索与 Citation 链路保持兼容。
- 启用后每个结果含召回来源、初始分数和 Rerank 分数。
- Reranker 失败不返回 500，不阻断问答、审阅和 Citation。
- 对比报告与 Day 2 使用同一评测输入。
- 同时比较 Recall、MRR、nDCG、P50、P95 和降级次数。

| 工作 | 预计时间 |
| --- | ---: |
| 配置、协议和本地适配器 | 1.25 小时 |
| RAG 元数据与重排逻辑 | 1.25 小时 |
| 应用、任务和 Agent 注入 | 0.75 小时 |
| 单元和回归测试 | 1 小时 |
| 评测、报告和 README | 0.75 小时 |
| 合计 | 5 小时 |

记录模板：模型版本 ____；候选数 ____；故障回退测试 ____；指标差异 ____；是否保留该模型 ____。

---

# Day 4：审阅意图检索

> 状态：□ 待开始  
> 实际日期：待填写  
> 实际耗时：待填写  
> 测试结果：待填写  
> 阻塞项：待填写

## 当日目标

将 review 任务中“空请求后扫描固定关键词”的主路径替换为版本化意图检索。每个意图都要返回已验证候选或 evidence_insufficient，不能把未命中伪装为合同不存在该条款。

## 前置条件

- Day 2 有可运行基线，Day 3 的 search_chunks 已支持混合召回、重排和故障回退。
- 已索引文档具备 chunks、页码、字符范围和所有者信息。
- Embedding 不可用时，词法检索仍能在严格范围内返回候选。

## 文件清单

| 操作 | 文件 | 实施内容 |
| --- | --- | --- |
| 新建 | backend/app/agents/review_intents.py | 不可变 ReviewIntent、REVIEW_INTENT_VERSION 和六类意图。 |
| 修改 | backend/app/agents/retrieval_agent.py | 按意图检索、去重、写 trace，停用空审阅主路径的关键词扫描。 |
| 修改 | backend/app/agents/state.py | 增加可序列化 RetrievalTrace 和 retrieval_traces。 |
| 修改 | backend/app/rag/search.py | 分离 candidate_k 与 top_k，保留各阶段分数。 |
| 修改 | backend/app/agents/graph.py | 注入 Reranker 至 retrieve_node。 |
| 修改 | backend/app/services/review_service.py | 将依赖贯通至审阅 Runner。 |
| 修改 | backend/app/services/task_dispatcher.py | 恢复任务时传递 Reranker。 |
| 修改 | backend/app/main.py | 仅在前述依赖尚未贯通时补齐。 |
| 新建 | backend/tests/test_review_intents.py | 意图、范围、空证据和兼容性测试。 |
| 修改 | backend/tests/test_agent_nodes.py | 节点回归。 |
| 修改 | backend/tests/test_agent_graph.py | Graph 依赖贯通测试。 |
| 修改 | docs/development_chain.md | 写明 review_intents_v1 与证据不足语义。 |

## 实施步骤

1. 在 review_intents.py 固定六个 ID：payment_terms、contract_term、renewal、liability_cap、confidentiality、dispute_resolution。每个对象含版本、中文和英文检索 query、目标字段和默认候选数。
2. 在 TaskState 定义 RetrievalTrace，字段包括 intent_id、intent_version、citation_id、rank、lexical_score、vector_score、rerank_score、status。Trace 只保存索引 ID、分数和状态，不重复保存合同全文。
3. review 且 request 为空时，对每个合法 document_id 与 version_id 执行六个意图检索。QA 继续使用用户 request 的单查询路径。
4. 每次检索都显式传入 version_id 和 owner 范围；结果再次断言 document_id 与 version_id 一致，再转换为 EvidenceRef。
5. 按 citation_id 去重 EvidenceRef，但允许同一证据保留多个意图 Trace。
6. 单个意图没有候选时记录 evidence_insufficient；不得用另一个意图的片段补足，也不得生成“条款缺失”结论。整份范围无任何证据时才返回 waiting_human。
7. NodeResult 仅输出 scope_count、evidence_count、intent_summary、unresolved_intent_ids，事件不写原文。
8. 保持现有范围、页码、字符区间和所有者验证；不新增跨用户或跨历史版本兜底查询。

## 测试与命令

~~~powershell
Set-Location D:\go\docpilot
.\backend\.venv\Scripts\python.exe -m pytest backend/tests/test_review_intents.py backend/tests/test_agent_nodes.py backend/tests/test_agent_graph.py -q
~~~

## 验收与日志

- 六个意图各自有命中证据或 evidence_insufficient 状态。
- 缺少争议解决条款时，不借用付款条款，不生成虚构 Citation。
- 多用户、多文档、多版本输入时，引用不越界。
- QA 有 request 的路径与原有行为兼容。
- Embedding 或 Reranker 故障时仍可获得受范围约束的词法结果。

| 工作 | 预计时间 |
| --- | ---: |
| 意图契约与样例 | 0.5 小时 |
| 状态与检索实现 | 2 小时 |
| 依赖贯通 | 1 小时 |
| 测试 | 1 小时 |
| 文档和回归 | 0.5 小时 |
| 合计 | 5 小时 |

记录模板：意图版本 ____；未解决意图 ____；范围隔离测试 ____；生产检索行为差异 ____。

---

# Day 5：最小受控 LLM 接入

> 状态：□ 待开始  
> 实际日期：待填写  
> 实际耗时：待填写  
> 测试结果：待填写  
> 阻塞项：待填写

## 当日目标

将当前仅有 Protocol 和目录说明的 LLM 边界落实为可替换的 OpenAI-compatible HTTP 适配器和完全离线 Mock。本日只完成传输、配置、错误和脱敏，不让 LLM 直接修改审阅结果。

## 前置条件

- Settings 中已有 LLM_PROVIDER、LLM_API_KEY、LLM_BASE_URL、LLM_MODEL、LLM_TIMEOUT_SECONDS 校验。
- 真实中转站 Key 仅放在本机 .env，不写入 .env.example、数据库、任务 payload 或测试夹具。
- Day 4 的确定性检索和节点测试通过。

## 文件清单

| 操作 | 文件 | 实施内容 |
| --- | --- | --- |
| 新建 | backend/app/infrastructure/llm/models.py | 请求、响应、Usage 与稳定错误模型。 |
| 新建 | backend/app/infrastructure/llm/mock.py | 无网络、确定性 MockLLMProvider。 |
| 新建 | backend/app/infrastructure/llm/openai_compatible.py | chat/completions HTTP 客户端。 |
| 新建 | backend/app/infrastructure/llm/factory.py | 基于 Settings 创建 Provider。 |
| 修改 | backend/app/infrastructure/llm/__init__.py | 仅导出稳定公共接口。 |
| 修改 | backend/app/infrastructure/providers.py | Protocol 返回结构化响应，不返回裸字符串。 |
| 修改 | backend/app/core/config.py | 约束 provider 白名单、重试、最大输出 token。 |
| 修改 | .env.example | 中文说明和空示例，不填真实 Key。 |
| 修改 | backend/app/main.py | lifespan 创建、保存和关闭 Provider。 |
| 修改 | backend/app/services/task_dispatcher.py | 预留 Provider 注入。 |
| 修改 | backend/app/services/review_service.py | 预留 Provider 注入。 |
| 视实现修改 | backend/pyproject.toml | 将运行时使用的 httpx 放入正式依赖或 llm-api extra。 |
| 新建 | backend/tests/test_llm_provider.py | Mock、HTTP fake、错误和泄密测试。 |
| 视实现修改 | backend/tests/test_main.py | 生命周期测试。 |

## 实施步骤

1. 定义内部契约：请求含 messages、response_schema、temperature=0、max_output_tokens；响应含 content、model、usage、latency_ms、provider_request_id。
2. MockLLMProvider 返回固定合法 JSON 与可预测 usage，使开发和测试无需网络和真实 Key。
3. OpenAICompatibleLLMProvider 向 base_url 加 chat/completions 发送 Bearer Key、model、messages；使用显式 connect/read timeout。
4. 仅重试短暂 429 和 5xx；401、403 不重试。将超时、鉴权、限流、上游不可用、响应格式错误转成稳定错误码。
5. 固定错误码：LLM_CONFIGURATION_ERROR、LLM_AUTH_FAILED、LLM_RATE_LIMITED、LLM_UPSTREAM_UNAVAILABLE、LLM_TIMEOUT、LLM_INVALID_RESPONSE。
6. 日志仅记录 provider、model、HTTP 状态、耗时和 request_id；不得记录 Authorization、Key、完整 Prompt、完整合同。
7. factory 只接受 mock 和 openai_compatible；未知值在启动阶段失败。lifespan 创建 Provider 后安全关闭。
8. 将 Provider 注入 Review Runner 和 Graph，但此日仍不让确定性 extract、risk、verify 的结果依赖模型。

## 测试与命令

~~~powershell
Set-Location D:\go\docpilot
.\backend\.venv\Scripts\python.exe -m pytest backend/tests/test_llm_provider.py backend/tests/test_main.py -q
.\backend\.venv\Scripts\python.exe -m pytest backend/tests -q
~~~

## 验收与日志

- Mock 未配置 Key 仍可稳定返回合法响应。
- HTTP fake 覆盖 200、401、429、500、超时、缺 choices、非法 content、缺 usage。
- 真实 Provider 缺 key、base URL 或 model 时在启动前失败；mock 正常。
- 测试断言 Key 和 Prompt 片段不出现在异常或日志。
- 本日不产生任何由模型直接写入报告的字段或 Finding。

| 工作 | 预计时间 |
| --- | ---: |
| 契约 | 0.5 小时 |
| 适配器与错误映射 | 2 小时 |
| 工厂和生命周期注入 | 0.75 小时 |
| HTTP fake 与泄密测试 | 1.25 小时 |
| 回归和文档 | 0.5 小时 |
| 合计 | 5 小时 |

记录模板：Provider 模式 ____；Mock 测试 ____；错误码覆盖 ____；真实中转站 smoke test 是否已单独执行 ____。

---

# Day 6：结构化抽取与证据约束

> 状态：□ 待开始  
> 实际日期：待填写  
> 实际耗时：待填写  
> 测试结果：待填写  
> 阻塞项：待填写

## 当日目标

LLM 只基于 Day 4 检索到的 Evidence 生成候选字段。所有候选必须通过 Pydantic、Citation 回放和确定性冲突判定，才能进入报告。

## 前置条件

- Day 4 已产生 retrieval_traces 与严格范围内的 Evidence。
- Day 5 Provider 与稳定错误码可用。
- extraction_agent.py 的确定性抽取、verification_agent.py 的 Citation 验证测试均通过。

## 文件清单

| 操作 | 文件 | 实施内容 |
| --- | --- | --- |
| 新建 | backend/app/agents/prompts/extraction_v1.py | Prompt 名称、版本和纯函数构造器。 |
| 新建 | backend/app/agents/schemas/extraction.py | extra=forbid 的候选 Pydantic Schema。 |
| 修改 | backend/app/agents/prompts/__init__.py | 导出稳定 Prompt 接口。 |
| 修改 | backend/app/agents/schemas/__init__.py | 导出稳定 Schema 接口。 |
| 修改 | backend/app/agents/extraction_agent.py | 保留规则抽取，增加 LLM 候选合并。 |
| 修改 | backend/app/agents/verification_agent.py | 校验候选引用、范围和文本支持关系。 |
| 修改 | backend/app/agents/state.py | ExtractedField 增加 source 与 prompt_version，保持旧快照兼容。 |
| 修改 | backend/app/agents/graph.py | 将 Provider 注入 extract 节点。 |
| 修改 | backend/app/services/review_service.py | 贯通 Provider。 |
| 修改 | backend/app/services/task_dispatcher.py | 恢复任务时贯通 Provider。 |
| 修改 | backend/app/main.py | 仅在依赖未贯通时补齐。 |
| 新建 | backend/tests/test_llm_extraction.py | 正常、伪造、冲突和降级测试。 |
| 修改 | backend/tests/test_agent_nodes.py | 节点回归。 |
| 修改 | backend/tests/test_agent_workflow.py | 工作流回归。 |

## 实施步骤

1. Schema 只允许固定字段白名单。每项候选至少有 name、value、confidence、citation_ids、reason；citation_ids 非空且最多三项。
2. 拒绝模型提交 document_id、version_id、自造 quote、未知字段或额外 JSON 字段。归属只能从已有 Evidence 推导。
3. Prompt 将合同文本标记为不可信数据，明确“只选择给定 Citation ID；无法确认时返回空 candidates”。仅传 citation_id、quote、page、section，不传 Key、全库数据和隐藏规则。
4. extract_node 先执行现有确定性规则，再在 Provider 和 Evidence 均存在时请求 LLM。非法 JSON、超时或模型错误时保留确定性结果，不能使任务崩溃。
5. 规则与模型值相同则合并可验证 Citation；冲突则标记 needs_review，不选择任一值；仅 LLM 发现的值也需通过后续验证。
6. verify_node 复用原文回放：每个引用必须是 state.evidence 的同一对象、同文档同版本，且 value 能被 quote 的确定性提取函数支持。
7. 高风险、低置信度、冲突、无证据、引用无效均进入 waiting_human。模型不可用而规则结果充分时可安全继续。
8. Prompt 版本与字段 source 写入审计元数据；API 和日志不展示完整 Prompt。

## 测试与命令

~~~powershell
Set-Location D:\go\docpilot
.\backend\.venv\Scripts\python.exe -m pytest backend/tests/test_llm_extraction.py backend/tests/test_agent_nodes.py backend/tests/test_agent_workflow.py -q
.\backend\.venv\Scripts\python.exe -m pytest backend/tests -q
~~~

## 验收与日志

- 合法 Mock 候选仅在已有 Citation、同 scope、值与 quote 一致时通过。
- 伪造 Citation、跨版本引用、空引用、额外字段、未知字段、错误值、非法 JSON、低置信度、规则冲突都不能写入报告。
- Provider 超时、429、5xx 时，确定性字段保留且不生成猜测结论。
- 每个 LLM 来源的 found 字段都可回放至数据库 chunk。

| 工作 | 预计时间 |
| --- | ---: |
| Schema 与 Prompt | 0.75 小时 |
| Extract 与合并 | 2 小时 |
| Verification 与依赖贯通 | 1 小时 |
| 对抗测试 | 1.25 小时 |
| 回归和文档 | 0.5 小时 |
| 合计 | 5.5 小时 |

记录模板：Prompt 版本 ____；Schema 拒绝案例 ____；降级案例 ____；验证通过率 ____。

---

# Day 7：Playbook v1

> 状态：□ 待开始  
> 实际日期：待填写  
> 实际耗时：待填写  
> 测试结果：待填写  
> 阻塞项：待填写

## 当日目标

以版本化、声明式 Playbook 驱动风险结论。先实现付款期限、自动续约、无限或异常责任、争议解决四类有正向证据的风险。

“未找到责任上限”等缺失类结论只转人工，不能直接成为最终风险。

## 前置条件

- Day 4 的检索能返回指定 document_id 和 version_id 范围内的 Citation。
- Day 6 的字段通过 Schema，并包含字段名、值、归属与 Citation。
- 现有 Finding 的 rule_id、rule_version、citation 和人工 decision 链路可用。

## 文件清单

| 操作 | 文件 | 实施内容 |
| --- | --- | --- |
| 新建 | backend/app/playbooks/models.py | Playbook、规则、条件和风险模板 Schema。 |
| 新建 | backend/app/playbooks/loader.py | 限制加载目录、校验与不可变缓存。 |
| 新建 | backend/app/playbooks/evaluator.py | 白名单条件求值与 Finding 生成。 |
| 新建 | backend/app/playbooks/rules/contract_v1.yaml | 四条可审查的声明式规则。 |
| 新建 | backend/app/verification/source.py | Citation 来源与范围验证纯函数。 |
| 新建 | backend/app/verification/amount.py | 确定性金额解析。 |
| 新建 | backend/app/verification/duration.py | 确定性时长、通知期限解析。 |
| 新建 | backend/app/verification/risk.py | 字段、证据和规则命中验证。 |
| 修改 | backend/app/agents/state.py | RiskFinding 增加 playbook_id 等审计字段。 |
| 修改 | backend/app/agents/risk_agent.py | 使用 Playbook evaluator 替代演示规则。 |
| 修改 | backend/app/agents/verification_agent.py | 未满足证据条件时阻断 Finding。 |
| 修改 | backend/app/services/review_service.py | 只持久化已验证 Finding。 |
| 修改 | backend/app/infrastructure/database.py | findings 表增加兼容字段与迁移。 |
| 修改 | backend/app/api/routes/reviews.py | 对外返回 playbook_id。 |
| 修改 | backend/app/domain/schemas.py | OpenAPI Schema 同步 Finding 字段。 |
| 修改 | backend/pyproject.toml | 增加受限 YAML 解析依赖，例如 PyYAML。 |
| 新建 | backend/tests/test_playbooks.py | 规则、安全和幂等测试。 |
| 修改 | backend/tests/test_agent_nodes.py | 风险节点回归。 |
| 修改 | backend/tests/test_agent_review_api.py | API 契约回归。 |

## 实施步骤

1. 使用 extra=forbid 定义 Playbook、PlaybookRule、Condition、RiskTemplate；规则必须具备 playbook_id、version、rule_id、severity、required_fields、conditions、title、recommendation。
2. YAML 只允许数据声明，拒绝 Python 表达式、Jinja、导入路径和可执行字符串。白名单操作符仅包含 eq、ne、gt、gte、lt、lte、contains_any、all、any。
3. contract_v1.yaml 定义四条小而明确的规则：payment_days_gt_60、auto_renewal_short_notice、unlimited_liability、unfavorable_dispute_resolution。
4. loader 只从包内 playbooks/rules 加载；未知键、未知操作符、重复 rule_id、空版本都在加载阶段报错。
5. verification 模块只处理确定性事实和 Citation 约束；模型解释不能作为规则事实输入。
6. evaluator 生成 RiskFinding 时，finding_id 基于 task_id、document_id、version_id、rule_id、playbook_version 确定性生成，保证恢复后不重复。
7. 给 findings 表增加 playbook_id，使用兼容迁移，保持已有 rule_id 与 rule_version 不变。
8. 字段缺失、低置信度、无 Citation、跨版本 Citation 或缺失条款进入 unresolved_fields 和 waiting_human，不进入报告风险项。
9. 仅让 Verification 放行的 Finding 由 review_service 持久化。

## 测试与命令

~~~powershell
Set-Location D:\go\docpilot\backend
.\.venv\Scripts\python.exe -m pytest tests\test_playbooks.py tests\test_agent_nodes.py tests\test_agent_review_api.py -q
~~~

## 验收与日志

- YAML 正常加载，未知操作符、重复 rule_id、空版本均失败。
- 四类规则各自生成正确 Finding。
- 缺 Citation 或字段跨版本时不生成 Finding。
- 同一 task 重跑不重复写入 Finding。
- playbook_id、rule_id、rule_version 和 Citation 能经 API 与报告读回。

| 工作 | 预计时间 |
| --- | ---: |
| Schema 与 YAML 规则 | 1 小时 |
| Loader 与安全校验 | 1 小时 |
| Evaluator、Risk Agent 和持久化 | 1.5 小时 |
| 测试 | 1 小时 |
| 文档和回归 | 0.5 小时 |
| 合计 | 5 小时 |

记录模板：Playbook 版本 ____；规则测试 ____；阻断案例 ____；API 字段兼容性 ____。

---

# Day 8：Checkpoint 与恢复

> 状态：□ 待开始  
> 实际日期：待填写  
> 实际耗时：待填写  
> 测试结果：待填写  
> 阻塞项：待填写

## 当日目标

让 Agent 在节点边界中断后，从最近成功节点继续，并保持 Citation、Finding 和人工决策的幂等性。

## 前置条件

- Day 7 的 Playbook 和 RiskFinding 结构稳定、测试通过。
- 现有 agent_runs 已保存状态快照；AgentGraph 已可依据已成功 node_results 跳过节点。
- 每个图节点的状态可 JSON 序列化。

## 文件清单

| 操作 | 文件 | 实施内容 |
| --- | --- | --- |
| 新建 | backend/app/agents/runtime/checkpoint.py | CheckpointStore、CheckpointRecord 和恢复验证。 |
| 修改 | backend/app/agents/runtime/__init__.py | 导出 Runtime 公共接口。 |
| 修改 | backend/app/agents/graph.py | 成功节点后保存 Checkpoint。 |
| 修改 | backend/app/agents/state.py | 状态版本与安全序列化。 |
| 修改 | backend/app/services/review_service.py | 从 Checkpoint 新建或恢复状态。 |
| 修改 | backend/app/infrastructure/database.py | agent_runs 兼容迁移和索引。 |
| 修改 | backend/app/infrastructure/tasks.py | 事件与恢复边界对接。 |
| 新建 | backend/tests/test_checkpoint.py | 中断、恢复、指纹不匹配测试。 |
| 修改 | backend/tests/test_agent_workflow.py | 图恢复回归。 |
| 修改 | backend/tests/test_task_recovery.py | 任务恢复回归。 |
| 修改 | backend/tests/test_agent_review_api.py | API 级幂等性回归。 |

## 实施步骤

1. 在 CheckpointRecord 中保存 task_id、review_id、input_fingerprint、state_schema_version、checkpoint_sequence、last_successful_node、最小 TaskState JSON、创建和更新时间。
2. 使用稳定 JSON 加 SHA-256 计算 input_fingerprint，输入包括任务类型、owner、document/version scope、request 或 conditions、Playbook 版本、Prompt 或模型版本；不写合同全文和 API Key。
3. 每个节点成功写入 state.node_results 后，保存 Checkpoint；保存点只出现在节点边界。
4. 使用 SQLite 原子事务完成：验证输入指纹、更新 agent_runs、更新 tasks.current_node、追加只含节点名、序号和哈希的 agent.checkpointed 事件。
5. review_runner 优先读取 task_id 的 Checkpoint；无快照才创建状态。有快照时执行 TaskState 反序列化、指纹、scope 和版本校验。
6. 指纹不匹配时失败为 CHECKPOINT_INPUT_MISMATCH。损坏 JSON 或旧快照也返回稳定、可解释的失败码。
7. waiting_human、failed、succeeded 快照不自动重跑；只恢复最后一个成功节点之后的部分。
8. _store_findings 保持幂等，使用确定性 finding_id 和冲突忽略逻辑，恢复不得覆盖已有人工 review_status 或 review_note。

## 测试与命令

~~~powershell
Set-Location D:\go\docpilot\backend
.\.venv\Scripts\python.exe -m pytest tests\test_checkpoint.py tests\test_agent_workflow.py tests\test_task_recovery.py tests\test_agent_review_api.py -q
~~~

## 验收与日志

- retrieve 成功后模拟中断，恢复时只执行 extract、risk、verify、report。
- 中断前后 Citation 与 Finding ID 一致。
- 同一任务重复恢复不写重复 Finding。
- scope、Playbook 版本或输入指纹变化时拒绝恢复。
- waiting_human 不被后台自动重跑。

| 工作 | 预计时间 |
| --- | ---: |
| 快照模型和迁移 | 1 小时 |
| Graph 和 Runner 恢复逻辑 | 1.5 小时 |
| 原子事件与幂等性 | 0.75 小时 |
| 中断和损坏数据测试 | 1.25 小时 |
| 回归和文档 | 0.5 小时 |
| 合计 | 5 小时 |

记录模板：Checkpoint schema 版本 ____；中断节点 ____；恢复路径 ____；重复执行结果 ____。

---

# Day 9：任务租约、人工继续与页面恢复

> 状态：□ 待开始  
> 实际日期：待填写  
> 实际耗时：待填写  
> 测试结果：待填写  
> 阻塞项：待填写

## 当日目标

补齐 SQLite 单机模式下的任务认领、租约、心跳、过期回收、Human Gate 和页面刷新恢复，避免同一任务被两个执行器并行处理。

## 前置条件

- Day 8 的 Checkpoint 可验证恢复，并持有 input_fingerprint。
- 当前 TaskManager、tasks 表和 Finding 的批准/驳回逻辑可用。
- 前端能读取 TaskSummary、工作流和任务事件。

## 文件清单

| 操作 | 文件 | 实施内容 |
| --- | --- | --- |
| 修改 | backend/app/infrastructure/database.py | tasks 租约字段、迁移和索引。 |
| 修改 | backend/app/infrastructure/tasks.py | claim、heartbeat、release、reclaim。 |
| 修改 | backend/app/domain/schemas.py | 人工动作请求与任务响应 Schema。 |
| 修改 | backend/app/domain/enums.py | 状态和人工动作枚举。 |
| 修改 | backend/app/api/routes/tasks.py | 人工继续、终止和安全任务摘要接口。 |
| 修改 | backend/app/api/routes/reviews.py | 与 Finding 决策、恢复行为协调。 |
| 修改 | backend/app/services/review_service.py | 从 Checkpoint 继续审阅。 |
| 修改 | backend/app/main.py | 启动时回收过期任务。 |
| 修改 | frontend/src/types.ts | lease、checkpoint、人工动作类型。 |
| 修改 | frontend/src/shared/services/api.ts | continueTask、terminateTask 和刷新接口。 |
| 修改 | frontend/src/shared/components/WorkflowTimeline.tsx | 当前节点、失败和待人工动作提示。 |
| 修改 | frontend/src/features/reviews/pages/ReviewWorkspace.tsx | 继续、终止按钮与加载状态。 |
| 修改 | frontend/src/App.tsx | 刷新后加载 Task 与 Workflow 快照。 |
| 新建 | backend/tests/test_task_lease.py | 并发认领、心跳、过期和取消测试。 |
| 修改 | backend/tests/test_task_recovery.py | Checkpoint 恢复回归。 |
| 修改 | backend/tests/test_agent_review_api.py | API、权限和乐观锁回归。 |

## 实施步骤

1. 对 tasks 做兼容迁移：lease_owner、lease_expires_at、heartbeat_at、attempt、checkpoint_version、human_action_required；为可认领查询建立索引。
2. 在 TaskManager 实现 claim、heartbeat、release_or_finish、reclaim_expired。claim 使用 SQLite 的立即写事务，只有 queued 或租约到期的 running 任务能被认领。
3. 执行器身份使用进程启动时生成的 UUID，不能使用用户名或固定字符串。
4. TaskContext.update 和图节点事件触发 heartbeat。任务成功、失败、等待人工或取消时释放租约。
5. 应用启动时先回收过期租约，再由 resolver 从 Checkpoint 继续可恢复任务，不创建新 task，也不从头重复已完成节点。
6. 新增任务级人工动作接口，例如 POST /tasks/{task_id}/human-actions，严格接收 action=continue 或 terminate、note、expected_checkpoint_version。
7. continue 只将 waiting_human 任务置为 queued 并从 Checkpoint 继续；terminate 原子变为 cancelled，之后 claim 和 resume 都拒绝。
8. 现有 Finding approved 或 rejected 接口继续负责单条风险决定；任务级 continue 不等于批准模型结论。
9. GET /tasks/{task_id} 返回 lease/heartbeat 的安全摘要、checkpoint_version、human_action_required，不暴露内部 payload。
10. 前端刷新时先请求任务与工作流快照，再订阅事件；按钮请求期间禁用，遇到 409 后重新获取后端状态。

## 测试与命令

~~~powershell
Set-Location D:\go\docpilot\backend
.\.venv\Scripts\python.exe -m pytest tests\test_task_lease.py tests\test_task_recovery.py tests\test_agent_review_api.py -q
Set-Location D:\go\docpilot\frontend
npm run build
~~~

## 验收与日志

- 两个 TaskManager 指向同一数据库时，只有一个能 claim 同一任务。
- 正常 heartbeat 不被回收；过期租约只恢复一次并从 Checkpoint 继续。
- waiting_human 不被 worker 自动认领。
- continue、terminate 的 owner、状态、乐观锁和审计事件正确。
- cancelled 任务不可恢复。
- 前端构建通过，刷新后能显示节点、失败原因和待处理动作。

| 工作 | 预计时间 |
| --- | ---: |
| 数据库迁移与 Claim | 1.25 小时 |
| 心跳、回收和 Runner 集成 | 1.25 小时 |
| API 和审计事件 | 1 小时 |
| 前端快照与 Human Gate | 0.75 小时 |
| 并发和构建测试 | 1 小时 |
| 合计 | 5.25 小时 |

记录模板：lease 时长 ____；并发 claim 结果 ____；过期恢复结果 ____；前端刷新验证 ____。

---

# Day 10：全链路验收、演示与交付记录

> 状态：□ 待开始  
> 实际日期：待填写  
> 实际耗时：待填写  
> 测试结果：待填写  
> 阻塞项：待填写

## 当日目标

固定一条可重复演示的合同审阅链路，留下质量报告、恢复记录、运行手册和已知限制。

## 前置条件

- Day 1–9 的测试通过。
- 至少有一份公开或脱敏的演示合同，以及固定的评测数据版本。
- 环境可选择 Mock LLM；真实 Key 不写入文档、测试、日志或报告。

## 文件清单

| 操作 | 文件 | 实施内容 |
| --- | --- | --- |
| 修改 | README.md | 最短启动、测试、Mock/真实 Provider 切换和演示前检查。 |
| 修改 | docs/architecture.md | 单机边界、Checkpoint、Lease、Human Gate 状态转换。 |
| 修改 | docs/api_contract.md | Playbook 字段、任务人工动作和状态定义。 |
| 修改 | evals/README.md | 数据准备、评测和报告解释。 |
| 新建 | docs/demo_runbook.md | 固定样本、预期结果、演示步骤、回滚方式。 |
| 新建 | evals/reports/final_v1.md | 真实指标、配置、日期和限制。 |
| 新建或修改 | infra/README.md | PostgreSQL、Redis/Outbox、独立 Worker、观测迁移点。 |
| 新建 | backend/tests/test_e2e_review_flow.py | HTTP 端到端审阅流程。 |
| 视需要修改 | backend/tests/test_agent_review_api.py | 拆分或补充 API 回归。 |
| 可选新建 | scripts/demo_e2e.ps1 | 本地演示启动与校验，不包含密钥。 |

## 实施步骤

1. 固定脱敏或公开演示样本：文件名、预期可检索条款、预期至少一个 Playbook Finding、预期 Citation、预期人工动作。
2. 编写 HTTP E2E：注册或登录、上传或 seed 已索引样本、等待 parse/index、创建 review、轮询 waiting_human 或 succeeded、校验 Citation scope 和 Playbook 元数据、完成人工 decision 或任务级 continue/terminate、导出 JSON、Markdown、CSV。
3. 异步等待必须有超时和可读失败信息；E2E 不依赖真实 LLM Key，默认使用 Mock。
4. 运行 Day 2 和 Day 3 评测，写 final_v1.md：manifest 版本、检索配置、Reranker 开关、Recall@5/10、MRR、nDCG、P50/P95、硬件、模型和运行时间。未执行或不可比较的结果明确标注。
5. README 固化后端、前端启动命令、Mock 与真实 Provider 切换、测试命令、数据目录和演示前检查。
6. architecture.md 说明 SQLite、本地对象存储、进程内 Worker 的限制；infra/README 记录迁移到 PostgreSQL、Redis/Outbox、独立 Worker、可观测性和模型治理的边界。
7. 使用干净的本地数据目录完成一次浏览器流程：登录、上传、任务进度、证据面板、人工处理、报告下载；把实际失败项和已知限制写入 demo_runbook 和 final_v1。

## 测试与命令

~~~powershell
Set-Location D:\go\docpilot\backend
.\.venv\Scripts\python.exe -m pytest -q
Set-Location D:\go\docpilot\frontend
npm run build
Set-Location D:\go\docpilot
$env:PYTHONPATH = "$PWD\backend"
.\backend\.venv\Scripts\python.exe -m evals.scripts.run_retrieval_eval --dataset acord --raw-dir .\evals\raw\acord --split test --mode all --top-k 5 10 --output .\evals\reports\acord_final_recheck.json
~~~

手工演示启动命令：

~~~powershell
Set-Location D:\go\docpilot\backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
Set-Location D:\go\docpilot\frontend
npm run dev
~~~

## 验收与日志

- test_e2e_review_flow.py 通过，后端全量测试与前端构建通过。
- demo_runbook 能让另一名开发者完成固定演示。
- final_v1.md 记录真实质量指标、配置、日期和限制。
- README、架构和 API 文档与运行代码一致。
- 完成状态仅表示可演示，不代表已具备生产级多实例能力。

| 工作 | 预计时间 |
| --- | ---: |
| E2E 与演示样本固定 | 1.5 小时 |
| 评测与最终报告 | 1 小时 |
| README、架构、API、Infra 文档 | 1 小时 |
| 干净环境手工验收 | 1 小时 |
| 合计 | 4.5 小时 |

记录模板：全量测试 ____；前端构建 ____；E2E 结果 ____；演示结果 ____；遗留限制 ____。

---

## 第 10 天后的演进顺序

1. PostgreSQL、独立 Worker、Redis 或 Outbox，替换 SQLite 和进程内 ThreadPoolExecutor。
2. 对象存储、安全扫描、限流、审计和密钥治理。
3. OpenTelemetry、Prometheus、压测、成本与模型调用观测。
4. 模型路由、Fallback、缓存和配额控制。
5. 基于中文脱敏评测集确认存在召回缺口后，再评估 Embedding 或 Reranker 微调。

这条顺序先保证证据正确、质量可测、状态可恢复，再投入生产基础设施和模型优化。

