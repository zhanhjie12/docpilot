import { useState } from "react";
import type { AgentField, AgentWorkflow, Citation, Finding, ReportFormat } from "../../../types";
import { RiskFindingList } from "../components/RiskFindingList";

interface ReviewWorkspaceProps {
  findings: Finding[];
  reviewId: string | null;
  workflow: AgentWorkflow | null;
  decidingId: string | null;
  exporting: boolean;
  closingUnresolved: boolean;
  closedUnresolved: boolean;
  onDecision: (finding: Finding, decision: "approved" | "rejected", note: string) => void;
  onExport: (format: ReportFormat) => void;
  onShowEvidence: (citations: Citation[], selectedId: string) => void;
  onCloseUnresolved: (note: string) => void;
}

const fieldLabel: Record<string, string> = {
  amount: "合同金额", duration_days: "合同期限", payment_days: "付款期限",
  payment_terms: "付款条款", acceptance: "验收条款", liability_cap: "责任上限",
  renewal: "续约条款", confidentiality: "保密条款", dispute_resolution: "争议解决", risk: "风险标记",
};

function fieldText(field: AgentField): string {
  // 未找到原文时显示证据状态，不把 null 或候选值包装成已确认的结论。
  if (field.status !== "found") return field.reason || "当前证据不足，需人工核对";
  if (typeof field.value === "number") return field.value.toLocaleString("zh-CN");
  return String(field.value ?? "待确认");
}

export function ReviewWorkspace({ findings, reviewId, workflow, decidingId, exporting, closingUnresolved, closedUnresolved, onDecision, onExport, onShowEvidence, onCloseUnresolved }: ReviewWorkspaceProps) {
  const [resolutionNote, setResolutionNote] = useState("");
  // 报告只在已创建审阅后开放；风险列表可继续显示等待人工审核的结果。
  const fields = workflow?.report?.fields ?? workflow?.extracted_fields ?? [];
  const nodeError = workflow?.current_node ? workflow.node_results[workflow.current_node]?.error : null;
  return <section className="review-panel">
    {workflow && <div className="agent-fields">
      <div className="section-heading"><div><span className="eyebrow">Agent 分析</span><h2>{workflow.report ? "已核验字段" : "待核验字段"}</h2></div></div>
      {nodeError && <p className="agent-warning">{nodeError}</p>}
      <div className="agent-field-list">{fields.map((field, index) => <div className="agent-field" key={`${field.document_id}-${field.version_id}-${field.name}-${index}`}>
        <strong>{fieldLabel[field.name] ?? field.name}</strong>
        <span>{fieldText(field)}</span>
        {field.citations.length > 0 && <button type="button" onClick={() => onShowEvidence(field.citations, field.citations[0].citation_id)}>查看原文</button>}
      </div>)}</div>
    </div>}
    <div className="section-heading"><div><span className="eyebrow">规则审阅</span><h2>风险发现</h2></div><span className="count-pill">{findings.length}</span></div>
    {reviewId && <div className="report-actions" aria-label="导出审阅报告">
      <span>导出报告</span>
      <button type="button" disabled={exporting} onClick={() => onExport("json")}>JSON</button>
      <button type="button" disabled={exporting} onClick={() => onExport("markdown")}>Markdown</button>
      <button type="button" disabled={exporting} onClick={() => onExport("csv")}>CSV</button>
    </div>}
    {reviewId && findings.length === 0
      ? <div className="empty-state">{workflow?.status === "waiting_human" ? "证据不足或字段冲突，请人工核对上方字段及原文。" : "本次审阅没有发现已核验的高风险项。"}</div>
      : <RiskFindingList findings={findings} decidingId={decidingId} onDecision={onDecision} onShowEvidence={onShowEvidence} />}
    {closedUnresolved && <p className="agent-warning">本次审阅已由人工标记为无法判断并结案。</p>}
    {reviewId && workflow?.status === "waiting_human" && findings.length === 0 && !closedUnresolved && <div className="form-row"><input aria-label="无法判断结案备注" placeholder="说明线下确认原因或后续处理人" value={resolutionNote} onChange={(event) => setResolutionNote(event.target.value)} /><button type="button" disabled={closingUnresolved || !resolutionNote.trim()} onClick={() => onCloseUnresolved(resolutionNote.trim())}>标记无法判断并结案</button></div>}
  </section>;
}
