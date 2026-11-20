import type { AgentWorkflow, AuthResponse, ChatAnswer, ChatSession, Document, DocumentType, Finding, FindingDecision, PolicyAnswer, ReportFormat, ScreeningResult, TaskSummary, TenderCheckResult, TenderRequirementResult, User } from "../../types";

// API_BASE 指向 FastAPI 的版本化前缀；API_ROOT 用于访问不带 /api/v1 的就绪探针。
const API_BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "http://localhost:8000/api/v1";
const API_ROOT = API_BASE.replace(/\/api\/v1\/?$/, "");
const AUTH_TOKEN_KEY = "docpilot.access_token";

// 令牌只保存在浏览器本地，后续接入 SSO 时可替换为安全 Cookie 或统一身份 SDK。
export function getAccessToken(): string | null {
  return window.localStorage.getItem(AUTH_TOKEN_KEY);
}

export function clearAccessToken(): void {
  window.localStorage.removeItem(AUTH_TOKEN_KEY);
}

function saveAuth(response: AuthResponse): AuthResponse {
  window.localStorage.setItem(AUTH_TOKEN_KEY, response.access_token);
  return response;
}

export class ApiError extends Error {
  code: string;
  requestId?: string;
  constructor(message: string, code = "HTTP_ERROR", requestId?: string) {
    super(message);
    this.code = code;
    this.requestId = requestId;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  // 所有请求统一解析错误信封，页面只需要处理 ApiError.message。
  const accessToken = getAccessToken();
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
      ...(init?.headers ?? {}),
    },
  });
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const error = body?.error;
    throw new ApiError(error?.message ?? `请求失败（HTTP ${response.status}）`, error?.code, error?.request_id);
  }
  return body as T;
}

// 文档列表用于左侧工作区，并返回可继续分页的游标。
export async function listDocuments(): Promise<{ items: Document[]; next_cursor: string | null }> { return request("/documents"); }

export interface UploadResult extends Document { task_id: string | null; }

export async function registerUser(email: string, password: string, displayName: string): Promise<AuthResponse> {
  // 注册成功后直接保存令牌，让用户无需重复填写登录表单。
  const response = await request<AuthResponse>("/auth/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password, display_name: displayName }),
  });
  return saveAuth(response);
}

export async function loginUser(email: string, password: string): Promise<AuthResponse> {
  // 登录和注册共用统一响应结构，页面只需要更新一个 User 状态。
  const response = await request<AuthResponse>("/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  return saveAuth(response);
}

export async function getCurrentUser(): Promise<User> {
  const response = await request<User>("/auth/me");
  return response;
}

export async function checkHealth(): Promise<boolean> {
  try {
    // /ready checks storage, database, task manager, and enabled embedding resources.
    const response = await fetch(`${API_ROOT}/ready`, { headers: { Accept: "application/json" } });
    if (!response.ok) return false;
    const body = await response.json().catch(() => null);
    return body?.status === "ready";
  } catch {
    // A network error is represented as "not ready" so the header does not claim a false connection.
    return false;
  }
}

export async function uploadDocument(file: File, displayName?: string, documentType: DocumentType = "contract"): Promise<UploadResult> {
  // 文件使用 multipart 上传；幂等键避免用户重复点击产生重复任务。
  const form = new FormData(); form.append("file", file); form.append("document_type", documentType); if (displayName) form.append("display_name", displayName);
  return request("/documents", { method: "POST", body: form, headers: { "Idempotency-Key": crypto.randomUUID() } });
}

// 任务状态由页面定时轮询，直到进入终态。
export async function getTask(taskId: string): Promise<TaskSummary> { return request(`/tasks/${encodeURIComponent(taskId)}`); }

export async function screenDocuments(documentIds: string[], query: string, amount?: number, deepAnalysis = false): Promise<{ task_id: string }> {
  // amount 为空时不发送条件，避免把未填写的输入转换成错误的 0。
  const conditions = amount && amount > 0 ? { amount: { field: "amount", operator: "gt", value: amount } } : undefined;
  return request("/documents/screen", { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() }, body: JSON.stringify({ document_ids: documentIds, query, conditions, deep_analysis: deepAnalysis }) });
}

// 只有筛选任务成功后才读取结果，后端会校验任务类型和状态。
export async function getScreeningResult(taskId: string): Promise<ScreeningResult> { return request(`/tasks/${encodeURIComponent(taskId)}/screening-result`); }
// 创建审阅任务并返回 review_id/task_id，详情结果在任务完成后获取。
export async function createReview(documentIds: string[]): Promise<{ review_id: string; task_id: string }> { return request("/reviews", { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() }, body: JSON.stringify({ document_ids: documentIds }) }); }
// 风险发现和问答会话都以当前文档 ID 作为边界。
export async function getFindings(reviewId: string): Promise<{ items: Finding[] }> { return request(`/reviews/${encodeURIComponent(reviewId)}/findings`); }
// 返回已持久化的 Agent 节点快照，页面刷新后也能展示字段与待确认原因。
export async function getReviewWorkflow(reviewId: string): Promise<{ state: AgentWorkflow | null; status: string }> { return request(`/reviews/${encodeURIComponent(reviewId)}/workflow`); }
export async function closeUnresolvedReview(reviewId: string, note: string): Promise<{ status: "closed_unresolved"; version: number }> {
  const review = await request<{ version: number }>(`/reviews/${encodeURIComponent(reviewId)}`);
  return request(`/reviews/${encodeURIComponent(reviewId)}/close-unresolved`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ note, expected_version: review.version }),
  });
}
// 人工确认必须携带当前风险项版本；后端冲突响应由统一错误处理传给页面。
export async function decideFinding(reviewId: string, finding: Finding, decision: "approved" | "rejected", note: string): Promise<FindingDecision> {
  return request(`/reviews/${encodeURIComponent(reviewId)}/findings/${encodeURIComponent(finding.finding_id)}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision, note: note.trim() || null, expected_version: finding.version }),
  });
}

// 报告接口返回 JSON 或纯文本；下载时保留原始字节，避免再次序列化破坏格式。
export async function getReviewReport(reviewId: string, format: ReportFormat): Promise<Blob> {
  const accessToken = getAccessToken();
  const response = await fetch(`${API_BASE}/reviews/${encodeURIComponent(reviewId)}/report?format=${format}`, {
    headers: {
      Accept: format === "json" ? "application/json" : "text/plain",
      ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
    },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new ApiError(body?.error?.message ?? `报告下载失败（HTTP ${response.status}）`, body?.error?.code, body?.error?.request_id);
  }
  return response.blob();
}
export async function createChatSession(documentId: string): Promise<ChatSession> { return request("/chat/sessions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ document_ids: [documentId] }) }); }
// 问题提交后由后端检索证据，返回值包含 answer 和 citations。
export async function askQuestion(sessionId: string, documentId: string, question: string): Promise<ChatAnswer> { return request(`/chat/${encodeURIComponent(sessionId)}/ask`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ document_ids: [documentId], question, top_k: 5 }) }); }

export async function setPolicyMetadata(versionId: string, metadata: { policy_family_id: string; issuer: string; org_scope: string; effective_from: string; effective_to: string | null; status: "active" | "draft" | "retired" }): Promise<void> {
  await request(`/policies/versions/${encodeURIComponent(versionId)}/metadata`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(metadata) });
}

export async function askPolicy(documentId: string, orgScope: string, asOfDate: string, question: string): Promise<PolicyAnswer> {
  return request("/policies/ask", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ document_ids: [documentId], org_scope: orgScope, as_of_date: asOfDate, question }) });
}

export async function checkTender(tenderVersionId: string, bidVersionId: string, requirementQueries: string[]): Promise<TenderCheckResult> {
  return request("/tenders/check", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ tender_version_id: tenderVersionId, bid_version_id: bidVersionId, requirement_queries: requirementQueries }) });
}

export async function listTenderRequirements(versionId: string): Promise<TenderRequirementResult> {
  return request(`/tenders/versions/${encodeURIComponent(versionId)}/requirements`);
}
