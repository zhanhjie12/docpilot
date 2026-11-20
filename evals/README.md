# 离线评测

此目录存放公开或已授权脱敏的固定评测集、校验脚本和真实运行报告。原件与可再生结果分开保存。

中文采购原件的首批技术核验见 [`zh_procurement_pilot_v1.md`](zh_procurement_pilot_v1.md)：10 份合同候选和 1 份同项目招标文件已下载、计算 SHA-256，并用项目 PDF 解析器检查可选文本。原件位于被忽略的 `raw/zh_procurement/`；仍须复核使用条件、脱敏和人工标注，不能据此宣称真实业务准确率。

```text
evals/
├── raw/acord/                 # ACORD 官方原件；脚本只读，不提交大文件
├── processed/acord_v1/        # 校验报告、转换结果和临时索引
├── scripts/validate_acord.py  # UTF-8、ID、qrels 和 SHA-256 校验
├── tests/                     # 校验器的最小有效与错误样例
├── dataset_card.md            # 来源、用途、许可证和局限
└── manifest.json              # 固定 revision、日期、字节数和哈希
```

## 首次准备 ACORD

1. 本仓库已在本机下载并解压 ACORD 原件。其他开发机可从 [发布方仓库](https://github.com/The-Atticus-Project/acord) 或 `manifest.json` 中固定的下载地址获取相同压缩包，把 `corpus.jsonl`、`queries.jsonl` 和完整 `qrels/` 原样放入 `evals/raw/acord/`；不要改 ID、分数和切分。
2. 核对压缩包与各文件的哈希、数据许可和 `manifest.json` 中记录的版本；本机已固定 2026-10-12 下载的压缩包 SHA-256。
3. 如需重新导入，从项目根目录运行以下命令，得到每个文件的 `path`、`bytes` 和 `sha256`，再更新 `manifest.json` 的 `files` 数组。

```powershell
$ProjectRoot = 'D:\go\docpilot'
$Python = "$ProjectRoot\backend\.venv\Scripts\python.exe"
& $Python "$ProjectRoot\evals\scripts\validate_acord.py" --raw-dir "$ProjectRoot\evals\raw\acord" --manifest "$ProjectRoot\evals\manifest.json" --print-hashes
```

4. 固定清单后运行严格校验，报告输出到处理目录：

```powershell
& $Python "$ProjectRoot\evals\scripts\validate_acord.py" --raw-dir "$ProjectRoot\evals\raw\acord" --manifest "$ProjectRoot\evals\manifest.json" --report "$ProjectRoot\evals\processed\acord_v1\validation_report.json" --verify-hashes
```

成功时退出码为 0，报告 `valid=true`；失败时退出码为 1，错误包含文件、行号、记录 ID 和原因。校验器会检查全部找到的 `qrels/*.tsv`，并要求 `qrels/test.tsv` 存在。报告路径不能位于 `raw/acord/`。

## 测试与后续指标

```powershell
& $Python -m pytest "$ProjectRoot\evals\tests\test_validate_acord.py" -q
```

第 2 天在通过校验的数据上运行词法、向量和 RRF 基线，记录 Recall@K、MRR、nDCG、P50/P95 延迟及模型配置。ACORD 是英文合同检索集；中文合同效果另用有授权和脱敏记录的中文集评测。指标和测试结果只记录实际运行值。

当前已完成一轮 ACORD `test` 切分的检索原语基线，结果和复现命令见 [`reports/acord_retrieval_summary.md`](reports/acord_retrieval_summary.md)。这轮脚本不经过 FastAPI 和业务数据库，也不替代第 2 天计划的正式多模型基线与生产链路测试。

## 中文条款功能冒烟

自编合成中文语料和边界见 [`datasets/README.md`](datasets/README.md)，本地模型实测见 [`reports/zh_contract_smoke_v1.md`](reports/zh_contract_smoke_v1.md)。可运行：

```powershell
.\backend\.venv\Scripts\python.exe .\evals\scripts\run_zh_retrieval_smoke.py --output .\evals\reports\zh_contract_smoke_v1.json
```

该小集合只检验中文检索链路，不用于宣称真实合同审阅效果。正式中文质量基线仍需按 `dataset_card.md` 的授权、脱敏和人工标注要求建立。

公开中文采购合同与同项目招标文件的首批候选来源见 [`chinese_procurement_source_shortlist.md`](chinese_procurement_source_shortlist.md)。该清单只有来源和筛选理由，尚未形成可评分的真实合同数据集。

真实中文业务集采用 [`datasets/zh_business_schema_v1.md`](datasets/zh_business_schema_v1.md)
记录来源、授权/使用限制、脱敏、双人标注、项目切分与原文坐标。标注完成后用
`scripts/evaluate_zh_business.py` 校验并评测；在此之前不报告真实中文合同准确率。
