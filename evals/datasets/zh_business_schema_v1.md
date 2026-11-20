# 中文业务文档人工评测格式 v1

评测数据使用 JSON，顶层为 `schema_version: 1` 和 `cases` 数组。每个 case 至少包含：

```json
{
  "case_id": "case_001",
  "source_id": "project_001",
  "split": "test",
  "document_type": "contract",
  "source_url": "https://发布单位/原件",
  "license_note": "来源与使用限制的人工记录",
  "redaction_note": "脱敏方法与人工复核记录",
  "annotator": "标注员代号",
  "reviewer": "复核员代号",
  "redacted_text": "合同签订后30日内付款。",
  "text_sha256": "脱敏文本 UTF-8 SHA-256",
  "queries": [
    {
      "query_id": "q_001",
      "question": "付款期限是多少？",
      "answerable": true,
      "gold_spans": [{"start": 0, "end": 12, "quote": "合同签订后30日内付款。"}]
    }
  ]
}
```

`start` 包含、`end` 排除。标注片段须与脱敏文本的字符切片完全一致；无法回答时
`answerable=false` 且 `gold_spans=[]`。同一项目的合同、招标、投标和制度文件使用同一个
`source_id` 并置于同一 `split`，避免训练/测试泄漏。建议分别统计合同、招投标和制度场景。

运行：

```powershell
.\backend\.venv\Scripts\python.exe .\evals\scripts\evaluate_zh_business.py --dataset <已授权脱敏并复核的数据.json> --output .\evals\reports\zh_business_v1.json
```

脚本校验来源记录、双人标注、文本哈希、中文内容、ID 唯一性、数据切分与原文坐标，
再计算词法检索 Recall@K、不可回答问题的误检率和引用回放率。报告只含 ID、指标与
哈希，不复制原文。此脚本尚不能测量风险结论准确率或 LLM 解释质量；这些需要单独的
人工风险标签与复核流程。
