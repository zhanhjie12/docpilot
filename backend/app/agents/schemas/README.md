# Agent 输出 Schema

该目录使用 Pydantic 定义模型输出边界，建议所有模型输出使用 `extra="forbid"`。

计划模块：

```text
schemas/
├── extraction.py       # 字段候选、状态、标准化值和 Citation ID
├── answer.py           # 问答候选及逐项证据
└── explanation.py      # 风险解释和建议候选
```

模型返回的 Citation ID 必须在节点输入白名单中再次校验；Schema 验证不能替代原文验证。

