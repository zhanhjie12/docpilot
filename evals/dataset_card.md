# ACORD 评测数据卡

## 数据来源与状态

- 名称：Atticus Clause Retrieval Dataset（ACORD）。
- 官方仓库：[The Atticus Project / acord](https://github.com/The-Atticus-Project/acord)。
- 当前状态：已于 2026-10-12 从发布方的 Hugging Face 数据页下载原始压缩包，保留压缩包和原样解压的 8 个文件。下载地址、压缩包 SHA-256 与各文件哈希见 `manifest.json`。
- 许可证：官方 README 与本次下载包内的 `ACORD readme.docx` 均声明数据集为 CC BY 4.0；仓库顶层另显示 MIT 许可证，不将代码许可证当成数据许可证。使用数据时保留来源署名。

## 结构和用途

官方提供 BEIR 格式：`corpus.jsonl` 是合同条款，`queries.jsonl` 是检索问题，`qrels/train.tsv`、`valid.tsv`、`test.tsv` 是分割后的相关度标签。相关度分数为 0–4；0 表示明确的不相关标注，因此校验器必须保留 0。原件 ID、评分和切分都不得改写。

本次下载的原件经结构校验包含 3,931 条 corpus、114 条 query 和 126,659 条 qrels 记录；其中 test 61,988 条、train 58,529 条、valid 6,142 条。计数以当前哈希固定的版本为准。

本项目用它建立**英文合同条款检索与重排**的工程基线。ACORD 不足以证明中文合同抽取、中文 Embedding、内部 Playbook 或法律风险判断的效果。第 2 天评测固定使用 `test` 切分；训练和验证切分不得混入测试指标。

## 数据保护与处理边界

1. 将官方数据原样放在 `evals/raw/acord/`。脚本只读取该目录。
2. 数据适配、校验报告与临时索引写入 `evals/processed/acord_v1/`；评测摘要写入 `evals/reports/`。
3. 在 `manifest.json` 固定来源 revision、下载日期、每个原件的相对路径、字节数和 SHA-256。哈希改变需重新说明来源，不直接覆盖既有基线。
4. 不在评测报告和任务日志中复制合同全文。不把未授权的用户合同加入公开数据层。

## 中文脱敏集规划

中文集将另建数据卡和来源清单；每份合同记录使用授权、脱敏方式、标注人、标注日期和原文引用位置。第一批标注以下八类审阅意图：

目前先有 [`datasets/zh_contract_smoke_v1.json`](datasets/zh_contract_smoke_v1.json) 自编合成条款冒烟集（32 条短条款、24 个问题）。它不属于下述真实脱敏集，也不作为中文生产质量结论。

| ID | 意图 |
| --- | --- |
| `payment_terms` | 付款条件、账期和节点 |
| `contract_term` | 生效、期限和到期 |
| `renewal` | 续约和通知期限 |
| `acceptance` | 验收标准和期限 |
| `liability_cap` | 责任上限和赔偿范围 |
| `confidentiality` | 保密义务和例外 |
| `dispute_resolution` | 仲裁和法院管辖 |
| `termination` | 解除和终止条件 |

意图未命中只表示当前证据不足，不能直接判定合同缺少条款。
