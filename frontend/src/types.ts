// 这些类型镜像后端 API 契约，调整状态或字段时需同步检查接口文档。
export type DocumentStatus = "uploaded" | "parsing" | "parsed" | "indexed" | "failed";
export type DocumentType = "contract" | "tender" | "bid" | "policy" | "other";
export type TaskStatus = "queued" | "running" | "waiting_human" | "succeeded" | "failed" | "cancelled";

export interface Document {
  document_id: string; filename: string; display_name: string; mime_type: string; status: DocumentStatus;
  document_type: DocumentType;
  page_count: number | null; quality_warnings: string[];
  facts: { amount: number | null; duration_days: number | null; payment_days?: number | null; risk: string | null; missing_fields?: string[] };
  latest_version_id: string; created_at: string; updated_at: string;
}
export interface TaskSummary { task_id: string; task_type: string; status: TaskStatus; progress: number; current_node: string | null; retry_count: number; failure: { code: string; message: string } | null; result_ref: string | null; created_at: string; updated_at: string; }
export interface Citation { citation_id: string; document_id: string; version_id: string; page: number | null; section: string | null; quote: string; char_start: number | null; char_end: number | null; }
export interface ScreeningResult { task_id: string; status: TaskStatus; total: number; matched: number; items: Array<{ document_id: string; matched: boolean; matched_conditions: string[]; missing_fields: string[]; facts: Document["facts"]; review_id?: string; review_task_id?: string }>; }
export interface Finding {
  finding_id: string; document_id: string; version_id: string;
  severity: "high" | "medium" | "low"; rule_id?: string; rule_version?: string; playbook_id?: string;
  title: string; explanation: string; recommendation: string;
  citations: Citation[]; review_status: string; review_note?: string | null; version: number;
}
// Agent 快照只用于展示字段、节点错误和可回溯引用；报告仍由服务端核验后导出。
export interface AgentField { name: string; value: unknown; status: "found" | "missing" | "unknown" | "needs_review"; confidence: number; citations: Citation[]; reason?: string | null; document_id?: string | null; version_id?: string | null; }
export interface AgentWorkflow { status: TaskStatus; current_node: string | null; errors: string[]; extracted_fields: AgentField[]; node_results: Record<string, { status: string; error?: string | null }>; report: { fields: AgentField[] } | null; }
// 人工确认使用风险项自己的版本号，避免覆盖另一位审阅者的决定。
export interface FindingDecision {
  finding_id: string; review_status: "approved" | "rejected";
  review_note: string | null; version: number;
}
export type ReportFormat = "json" | "markdown" | "csv";
export interface ChatSession { session_id: string; document_ids: string[]; version_ids: string[]; }
export interface ChatAnswer { answer: string | null; status: "answered" | "insufficient_evidence"; needs_confirmation: boolean; citations: Citation[]; }
export interface PolicyAnswer extends ChatAnswer { policy_versions: Array<{ version_id: string; policy_family_id: string; issuer: string; org_scope: string; effective_from: string; effective_to: string | null }> }
export interface TenderCheckResult { tender_version_id: string; bid_version_id: string; needs_confirmation: boolean; items: Array<{ requirement_query: string; status: "requirement_unverified" | "bid_evidence_missing" | "needs_review"; tender_citations: Citation[]; bid_citations: Citation[] }> }
export interface TenderRequirementResult { version_id: string; needs_confirmation: boolean; truncated: boolean; items: Array<{ requirement_query: string; citation: Citation; status: "needs_review" }> }
export interface User { user_id: string; email: string; display_name: string; role: string; created_at: string; }
export interface AuthResponse { access_token: string; token_type: "bearer"; expires_in: number; user: User; }
