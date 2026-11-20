# ACORD 检索模块基线（2026-10-13）

## 测试范围

- 输入：官方 ACORD `corpus.jsonl` 的 3,931 条英文条款，`test` 切分的 57 条查询和对应 `qrels/test.tsv`；原始压缩包 SHA-256 为 `c9e851456a14aed161033b856e9f40f3cc9f46cf7282c2007b143420e93a65c3`。
- 实现：直接调用 `backend/app/rag/retriever.py` 的词法检索、余弦向量检索和 RRF；每条 ACORD 条款对应一个内存 Chunk，候选池 40，最终取前 5/10。
- 向量模型：本机已缓存的 `BAAI/bge-small-zh-v1.5`，CPU，最小余弦分数 0.25。条款和查询都是英文，此模型的语言适配并非本测试的理想条件。
- 相关判定：ACORD 分数 `>=2` 为相关；nDCG 使用原始 0–4 分。未出现在 qrels 的结果按 0 分处理，并单独记录前 10 结果的显式评分比例。
- 耗时：查询 P50/P95 包含单条查询的向量编码和内存扫描；不包含一次性语料向量化、SQLite、HTTP 或前端耗时。语料向量化耗时约 390.2 秒。

## 实测结果

| 模式 | Recall@5 | Recall@10 | HitRate@5 | MRR@10 | nDCG@10 | 前 10 显式评分比例 | P50 | P95 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 词法 | 1.91% | 2.80% | 14.04% | 0.0938 | 0.0399 | 18.77% | 26.5 ms | 62.6 ms |
| 向量 | 3.71% | 5.67% | 31.58% | 0.1981 | 0.0678 | 18.60% | 907.2 ms | 1495.5 ms |
| RRF 融合 | 2.87% | 5.32% | 22.81% | 0.1880 | 0.0680 | 18.95% | 924.4 ms | 1522.9 ms |

57 条查询中，词法、向量和融合在前 10 条至少找到一个相关结果的查询数分别为 13、24、24；三种模式都没有空结果。完整逐查询 ID 和指标分别在 `acord_retrieval_lexical.json`、`acord_retrieval_all.json`，报告不包含合同正文。

## 结果解释与后续动作

1. 现有词法器是英文词出现次数之和，没有 BM25 的文档频率或长度归一化；在 ACORD 上作为基线较弱。向量模式提高了命中率，但绝对指标仍低。
2. 当前等权 RRF 的 HitRate@5 低于单独向量检索，说明不能假定两路融合自然更好。下一步在 `valid` 切分比较 BM25、英文 Embedding、候选池和融合权重，再锁定参数后只在 `test` 复测一次。
3. 前 10 候选仅约 19% 有显式 qrels 分数。本报告按未评分为 0 计算指标，不能把单次低分直接解释为人工确认的错误条款比例。
4. 用独立 SQLite 库导入同一批 3,931 条条款后，生产 `search_chunks` 的示例查询 `England Governing Law` 前 10 ID 与内存词法检索完全一致。该检查只验证词法生产调用；全量指标没有经过上传、解析、数据库向量索引、FastAPI、Agent 或前端。
5. ACORD 是英文条款检索数据。中文合同 Agent 的条款发现、证据引用、风险判断和端到端效果仍需独立中文脱敏集评测。

## 复现

在项目根目录执行：

```powershell
.\backend\.venv\Scripts\python.exe .\evals\scripts\run_acord_retrieval_eval.py --output .\evals\reports\acord_retrieval_lexical.json
.\backend\.venv\Scripts\python.exe .\evals\scripts\run_acord_retrieval_eval.py --embedding-model BAAI/bge-small-zh-v1.5 --output .\evals\reports\acord_retrieval_all.json
```

第二条命令只读取已缓存模型，不下载模型，也不调用第三方 API；CPU 全量语料编码需数分钟。运行前先用 `evals/scripts/validate_acord.py --verify-hashes` 确认原件与 manifest 一致。
