import { useState } from "react";

import type { Citation, Finding } from "../../../types";

interface RiskFindingListProps {
  findings: Finding[];
  decidingId: string | null;
  onDecision: (finding: Finding, decision: "approved" | "rejected", note: string) => void;
  onShowEvidence: (citations: Citation[], selectedId: string) => void;
}

const severityLabel = { high: "高风险", medium: "中风险", low: "低风险" } as const;
const reviewStatus: Record<string, string> = {
  pending: "待人工确认",
  needs_review: "待人工确认",
  approved: "风险已确认",
  rejected: "已驳回",
  confirmed: "已确认",
  dismissed: "已忽略",
  completed: "已完成",
};

export function RiskFindingList({ findings, decidingId, onDecision, onShowEvidence }: RiskFindingListProps) {
  // 每条风险保留自己的备注，切换决定时不会把其他风险的说明带过来。
  const [notes, setNotes] = useState<Record<string, string>>({});
  if (!findings.length) return <div className="empty-state">暂无风险发现。文档解析完成后可启动审阅。</div>;

  return <div className="finding-list">{findings.map((finding) => {
    const pending = finding.review_status === "pending" || finding.review_status === "needs_review";
    const saving = decidingId === finding.finding_id;
    return <article className="finding-card" key={finding.finding_id}>
      <div className="finding-top">
        <span className={`severity ${finding.severity}`}>{severityLabel[finding.severity]}</span>
        <span className="finding-status">{reviewStatus[finding.review_status] ?? finding.review_status}</span>
      </div>
      <h3>{finding.title}</h3>
      {finding.rule_id && <small className="finding-rule">规则 {finding.rule_id}{finding.rule_version ? ` · ${finding.rule_version}` : ""}</small>}
      <p>{finding.explanation}</p>
      <p className="recommendation"><strong>建议：</strong>{finding.recommendation}</p>
      {finding.citations.length ? <div className="finding-evidence">
        <span>原文证据</span>
        {finding.citations.map((citation, index) => <button key={`${citation.citation_id}-${index}`} type="button" onClick={() => onShowEvidence(finding.citations, citation.citation_id)}>
          查看引用 {index + 1}{citation.page ? ` · 第 ${citation.page} 页` : ""}
        </button>)}
      </div> : <p className="finding-evidence-warning">暂无可定位的原文引用，请人工核对。</p>}
      {finding.review_note && <p className="finding-note"><strong>审核备注：</strong>{finding.review_note}</p>}
      {pending && <div className="finding-decision">
        <label htmlFor={`note-${finding.finding_id}`}>人工确认备注</label>
        <textarea id={`note-${finding.finding_id}`} value={notes[finding.finding_id] ?? ""} onChange={(event) => setNotes((current) => ({ ...current, [finding.finding_id]: event.target.value }))} placeholder="记录批准或驳回的原因（可选）" rows={2} />
        <div className="finding-actions">
          <button type="button" disabled={decidingId !== null} onClick={() => onDecision(finding, "approved", notes[finding.finding_id] ?? "")}>{saving ? "保存中……" : "确认风险"}</button>
          <button type="button" className="danger-action" disabled={decidingId !== null} onClick={() => onDecision(finding, "rejected", notes[finding.finding_id] ?? "")}>{saving ? "保存中……" : "驳回发现"}</button>
        </div>
      </div>}
    </article>;
  })}</div>;
}
