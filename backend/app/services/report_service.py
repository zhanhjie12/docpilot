"""把确定性的审阅结果导出为 JSON、Markdown 或 CSV。"""

from __future__ import annotations

import csv
import io


def render_report(review: dict, findings: list[dict], output_format: str, agent_report: dict | None = None) -> tuple[str, str]:
    """导出人工决定和通过核验的 Agent 字段，保留可回溯引用。"""

    payload = {"review": review, "findings": findings, "agent": agent_report}
    if output_format == "json":
        import json
        return json.dumps(payload, ensure_ascii=False, indent=2), "application/json"
    if output_format == "markdown":
        lines = [f"# 文档审阅报告\n\n- 审阅 ID：{review['review_id']}\n- 状态：{review['status']}\n"]
        if agent_report:
            lines.append("## 已核验字段\n")
            for field in agent_report.get("fields", []):
                lines.append(f"- {field['name']}：{field.get('value')}（{field['status']}）")
        lines.append("## 风险发现\n")
        for item in findings:
            lines.append(f"- **{item['severity']}** {item['title']}（{item['review_status']}）：{item['explanation']} 建议：{item['recommendation']}")
            for citation in item.get("citations", []):
                lines.append(
                    f"  - 证据：{citation['document_id']} / {citation['version_id']} "
                    f"第 {citation.get('page') or '未知'} 页：{citation['quote']}"
                )
        return "\n".join(lines) + "\n", "text/markdown; charset=utf-8"
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["finding_id", "severity", "rule_id", "title", "review_status", "explanation", "recommendation"])
    writer.writeheader()
    writer.writerows({key: item.get(key) for key in writer.fieldnames} for item in findings)
    return output.getvalue(), "text/csv; charset=utf-8"
