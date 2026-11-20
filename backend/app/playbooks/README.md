# 合同审阅 Playbook

该目录保存风险规则的数据模型、加载器、确定性求值器和版本化规则文件。

计划结构：

```text
playbooks/
├── __init__.py
├── models.py
├── loader.py
├── evaluator.py
└── rules/
    └── contract_v1.yaml
```

只允许实现明确的操作符，例如 `equals`、`contains`、`greater_than` 和 `missing`；禁止使用
Python `eval()` 执行规则文件。每个 Finding 必须记录 Playbook、Rule ID 和规则版本。

