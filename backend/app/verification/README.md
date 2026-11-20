# Verification

该目录逐层验证模型或规则生成的候选结论。

计划模块：

```text
verification/
├── __init__.py
├── source.py       # 文档、版本、页码、字符区间和原文引用
├── amount.py       # 金额与币种标准化
├── duration.py     # 日期、期限和起算条件
├── clause.py       # 否定、例外和前置条件
└── risk.py         # Playbook 输入及命中结果
```

验证结果应区分：

- `source_verified`：引用真实存在且属于当前范围。
- `claim_supported`：引用能够支持候选结论。
- `rule_verified`：风险规则使用了已验证事实并确实命中。
- `human_approved`：可复核问题已经由用户确认。

文档越权、伪造 Citation 和不存在的原文属于硬失败，不能通过人工审批改写为真实证据。

