# Agent Runtime

该目录承载与具体合同业务无关的工作流执行能力。

计划模块：

```text
runtime/
├── __init__.py
├── context.py       # 数据库、模型、事件和取消信号等运行依赖
├── node.py          # NodeSpec、节点协议和工作流定义
├── executor.py      # 超时、重试、退避和节点调用
├── checkpoint.py    # 状态快照的保存与恢复
└── errors.py        # 节点错误分类
```

依赖规则：

- Runtime 可以依赖 `agents.state` 和基础设施协议。
- Runtime 不得包含合同字段、风险规则或 Prompt。
- 节点不得依赖 FastAPI `Request` 或直接创建供应商客户端。
- 每次节点尝试应隔离状态；只有成功结果可以提交到主状态。

