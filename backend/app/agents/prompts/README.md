# Prompt 目录

该目录保存可评测、可版本化的 Prompt。Prompt 不应散落在路由、Service 或节点代码中。

命名约定：

```text
<用途>_v<主版本>.py
```

例如：

```text
extraction_v1.py
answer_v1.py
risk_explanation_v1.py
```

每个模块至少公开 `PROMPT_NAME`、`PROMPT_VERSION` 和构建消息的纯函数。Prompt 只能引用
调用方明确传入的证据，不得自行扩大文档范围。

