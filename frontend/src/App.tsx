// 页面容器负责把上传、任务轮询、筛选、审阅和证据问答串成一条工作流。
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { AgentWorkflow, Citation, Document, Finding, ReportFormat, ScreeningResult, TaskSummary, User } from "./types";
import { AuthPanel } from "./features/auth/AuthPanel";
import { DocumentWorkspace } from "./features/documents/pages/DocumentWorkspace";
import { BusinessAnalysisPanel } from "./features/documents/components/BusinessAnalysisPanel";
import type { UploadedDocument } from "./features/documents/components/DocumentUploader";
import { ReviewWorkspace } from "./features/reviews/pages/ReviewWorkspace";
import {
  askQuestion,
  checkHealth,
  clearAccessToken,
  closeUnresolvedReview,
  createChatSession,
  createReview,
  decideFinding,
  getFindings,
  getAccessToken,
  getCurrentUser,
  getReviewReport,
  getReviewWorkflow,
  getScreeningResult,
  getTask,
  listDocuments,
  screenDocuments,
  ApiError,
} from "./shared/services/api";
import { EvidencePanel } from "./shared/components/EvidencePanel";
import { WorkflowTimeline } from "./shared/components/WorkflowTimeline";

// 终态任务不再轮询；其余状态仍由后端任务接口推进。
const terminalStatuses = new Set(["succeeded", "failed", "cancelled", "waiting_human"]);
const activeReviewKey = "docpilot.active_review";
const documentStatus: Record<Document["status"], string> = {
  uploaded: "已上传",
  parsing: "解析中",
  parsed: "已解析",
  indexed: "已建立索引",
  failed: "处理失败",
};
const riskLabel: Record<string, string> = { high: "高风险", medium: "中风险", low: "低风险" };
const screeningFieldLabel: Record<string, string> = { amount: "合同金额", duration_days: "合同期限", risk: "风险等级" };
type HealthState = "checking" | "connected" | "disconnected";

function errorMessage(error: unknown, fallback: string) {
  // API 层已经把服务端错误转换为 Error，这里只负责提供安全的兜底文案。
  return error instanceof Error ? error.message : fallback;
}

export default function App() {
  const [documents, setDocuments] = useState<Document[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [task, setTask] = useState<TaskSummary | null>(null);
  const [findings, setFindings] = useState<Finding[]>([]);
  const [reviewId, setReviewId] = useState<string | null>(null);
  const [workflow, setWorkflow] = useState<AgentWorkflow | null>(null);
  const [decidingId, setDecidingId] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const [closingUnresolved, setClosingUnresolved] = useState(false);
  const [closedUnresolved, setClosedUnresolved] = useState(false);
  const [screening, setScreening] = useState<ScreeningResult | null>(null);
  const [screenDocumentIds, setScreenDocumentIds] = useState<string[]>([]);
  const [citations, setCitations] = useState<Citation[]>([]);
  const [selectedCitationId, setSelectedCitationId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [amount, setAmount] = useState("");
  const [deepAnalysis, setDeepAnalysis] = useState(false);
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [health, setHealth] = useState<HealthState>("checking");
  const [user, setUser] = useState<User | null>(null);
  const [authReady, setAuthReady] = useState(false);
  const [accountKey, setAccountKey] = useState(0);
  // 异步请求开始时记录视图与账户版本，切换账号或文档后丢弃过期响应。
  const accountEpoch = useRef(0);
  const viewEpoch = useRef(0);

  const selected = useMemo(
    () => documents.find((item) => item.document_id === selectedId) ?? null,
    [documents, selectedId],
  );
  const taskRunning = Boolean(task && !terminalStatuses.has(task.status));
  const currentScreenDocumentIds = screenDocumentIds.length ? screenDocumentIds : selected ? [selected.document_id] : [];

  const refresh = useCallback(async (preferredId?: string) => {
    // 刷新列表时优先保留当前文档，避免异步任务完成后切换到另一项。
    const epoch = accountEpoch.current;
    const token = getAccessToken();
    try {
      const result = await listDocuments();
      if (epoch !== accountEpoch.current || token !== getAccessToken()) return;
      setDocuments(result.items);
      setSelectedId((current) => {
        const target = preferredId ?? current;
        if (target && result.items.some((item) => item.document_id === target)) return target;
        return result.items[0]?.document_id ?? null;
      });
    } catch (loadError) {
      if (epoch !== accountEpoch.current || token !== getAccessToken()) return;
      // 强制认证模式下的匿名 401 只说明需要登录，不是页面故障。
      if (!(loadError instanceof ApiError && loadError.code === "AUTHENTICATION_REQUIRED")) {
        setError(errorMessage(loadError, "无法加载文档列表"));
      }
    }
  }, []);

  function clearDocumentResults() {
    // 切换文档必须清空旧结果，避免上一份合同的证据泄漏到当前详情页。
    viewEpoch.current += 1;
    setAnswer(null);
    setCitations([]);
    setSelectedCitationId(null);
    setFindings([]);
    setReviewId(null);
    setWorkflow(null);
    setDecidingId(null);
    setExporting(false);
    setClosingUnresolved(false);
    setClosedUnresolved(false);
    setScreening(null);
    setTask(null);
    setError("");
  }

  function resetWorkspace() {
    // 账号切换时立即清空全部业务状态，并让旧账号的异步响应失效。
    accountEpoch.current += 1;
    clearDocumentResults();
    setDocuments([]);
    setSelectedId(null);
    setScreenDocumentIds([]);
    setQuery("");
    setAmount("");
    setDeepAnalysis(false);
    setQuestion("");
    setBusy(false);
  }

  function authenticated(nextUser: User) {
    resetWorkspace();
    setUser(nextUser);
    setAccountKey((current) => current + 1);
  }

  function loggedOut() {
    window.sessionStorage.removeItem(activeReviewKey);
    resetWorkspace();
    setUser(null);
    setAccountKey((current) => current + 1);
  }

  function selectDocument(documentId: string) {
    if (documentId === selectedId) return;
    window.sessionStorage.removeItem(activeReviewKey);
    clearDocumentResults();
    setSelectedId(documentId);
    setScreenDocumentIds([documentId]);
  }

  async function handleUploaded(result: UploadedDocument) {
    // 重复上传可能没有 taskId，此时服务端已有索引，直接刷新文档即可。
    clearDocumentResults();
    window.sessionStorage.removeItem(activeReviewKey);
    setSelectedId(result.documentId);
    setScreenDocumentIds([result.documentId]);
    const epoch = viewEpoch.current;
    await refresh(result.documentId);
    if (!result.taskId) return;
    try {
      const uploadedTask = await getTask(result.taskId);
      if (epoch === viewEpoch.current) setTask(uploadedTask);
    } catch (taskError) {
      if (epoch === viewEpoch.current) setError(`上传成功，但读取处理任务失败：${errorMessage(taskError, "未知错误")}`);
    }
  }

  // 启动时恢复本地令牌；令牌失效时清除它并回到未登录状态。
  useEffect(() => {
    const token = getAccessToken();
    if (!token) { setAuthReady(true); return; }
    let disposed = false;
    void getCurrentUser()
      .then((current) => { if (!disposed && token === getAccessToken()) setUser(current); })
      .catch(() => { if (!disposed && token === getAccessToken()) clearAccessToken(); })
      .finally(() => { if (!disposed) setAuthReady(true); });
    return () => { disposed = true; };
  }, []);

  // 先恢复身份，再加载该身份可见的文档；登录和退出都会触发重新读取。
  useEffect(() => { if (authReady) void refresh(); }, [authReady, accountKey, refresh]);

  // Restore only opaque IDs, then ask the server to enforce ownership again.
  useEffect(() => {
    if (!authReady) return;
    const raw = window.sessionStorage.getItem(activeReviewKey);
    if (!raw) return;
    let pointer: { userId: string; documentId: string; reviewId: string; taskId: string };
    try { pointer = JSON.parse(raw); } catch { window.sessionStorage.removeItem(activeReviewKey); return; }
    if (pointer.userId !== (user?.user_id ?? "anonymous")) return;
    let disposed = false;
    void Promise.all([listDocuments(), getTask(pointer.taskId)])
      .then(([result, nextTask]) => {
        if (disposed || !result.items.some((item) => item.document_id === pointer.documentId)
          || nextTask.task_type !== "review") return;
        setDocuments(result.items);
        setSelectedId(pointer.documentId);
        setScreenDocumentIds([pointer.documentId]);
        setReviewId(pointer.reviewId);
        setTask(nextTask);
      })
      .catch(() => { if (!disposed) window.sessionStorage.removeItem(activeReviewKey); });
    return () => { disposed = true; };
  }, [authReady, user?.user_id, accountKey]);

  // 定时读取 /ready，让页面顶部的连接状态反映真实后端依赖状态。
  useEffect(() => {
    let disposed = false;
    const probe = async () => {
      const connected = await checkHealth();
      if (!disposed) setHealth(connected ? "connected" : "disconnected");
    };
    void probe();
    const timer = window.setInterval(() => { void probe(); }, 15_000);
    return () => { disposed = true; window.clearInterval(timer); };
  }, []);

  // 轮询当前任务；清理旧计时器可以避免切换文档后写错任务状态。
  useEffect(() => {
    if (!task || terminalStatuses.has(task.status)) return;
    const taskId = task.task_id;
    const epoch = viewEpoch.current;
    let disposed = false;
    const timer = window.setInterval(async () => {
      try {
        const next = await getTask(taskId);
        if (!disposed && epoch === viewEpoch.current) setTask((current) => current?.task_id === taskId ? next : current);
      } catch (pollError) {
        if (!disposed && epoch === viewEpoch.current) setError(`任务状态更新失败：${errorMessage(pollError, "请检查后端服务")}`);
      }
    }, 700);
    return () => { disposed = true; window.clearInterval(timer); };
  }, [task?.task_id, task?.status, accountKey]);

  // 任务结束后按类型读取筛选、审阅或解析结果；失败时显示后端错误。
  useEffect(() => {
    if (!task) return;
    let disposed = false;
    const epoch = viewEpoch.current;
    if (task.status === "failed") {
      const detail = task.failure ? `${task.failure.message}（${task.failure.code}）` : "后台任务执行失败";
      setError(detail);
      if (task.task_type === "parse_and_index") void refresh(selectedId ?? undefined);
      return;
    }
    if (task.status !== "succeeded" && task.status !== "waiting_human" && task.status !== "cancelled") return;
    const loadResult = async () => {
      try {
        if (task.task_type === "screen") {
          const result = await getScreeningResult(task.task_id);
          if (!disposed && epoch === viewEpoch.current) setScreening(result);
        } else if (task.task_type === "review" && (reviewId || task.result_ref)) {
          const currentReviewId = reviewId || task.result_ref;
          const [result, snapshot] = await Promise.all([getFindings(currentReviewId!), getReviewWorkflow(currentReviewId!)]);
          if (!disposed && epoch === viewEpoch.current) {
            setReviewId(currentReviewId);
            setFindings(result.items);
            setWorkflow(snapshot.state);
            setClosedUnresolved(snapshot.status === "closed_unresolved");
          }
        } else if (task.task_type === "parse_and_index") {
          await refresh(selectedId ?? undefined);
        }
      } catch (resultError) {
        if (!disposed && epoch === viewEpoch.current) setError(`读取任务结果失败：${errorMessage(resultError, "未知错误")}`);
      }
    };
    void loadResult();
    return () => { disposed = true; };
  }, [task?.status, task?.task_id, task?.task_type, task?.result_ref, reviewId, refresh, selectedId, accountKey]);

  async function runScreen() {
    // 筛选先创建异步任务，进度由上面的统一任务轮询逻辑更新。
    if (!selected || taskRunning || busy) return;
    const threshold = Number(amount);
    if (!Number.isFinite(threshold) || threshold <= 0) {
      setError("请先填写大于 0 的合同金额。当前筛选仅支持明确的金额条件。");
      return;
    }
    const epoch = viewEpoch.current;
    setBusy(true); setError("");
    try {
      const result = await screenDocuments(currentScreenDocumentIds, query.trim() || `合同金额大于 ${threshold} 元`, threshold, deepAnalysis);
      const nextTask = await getTask(result.task_id);
      if (epoch === viewEpoch.current) { setScreening(null); setTask(nextTask); }
    } catch (screenError) {
      if (epoch === viewEpoch.current) setError(errorMessage(screenError, "合同筛选失败"));
    } finally { if (epoch === viewEpoch.current) setBusy(false); }
  }

  async function openScreenReview(item: ScreeningResult["items"][number]) {
    // 打开筛选任务自动创建的独立审阅，复用现有任务轮询和风险展示流程。
    if (!item.review_id || !item.review_task_id) return;
    clearDocumentResults();
    setSelectedId(item.document_id);
    setScreenDocumentIds([item.document_id]);
    const epoch = viewEpoch.current;
    try {
      const nextTask = await getTask(item.review_task_id);
      if (epoch === viewEpoch.current) {
        setReviewId(item.review_id);
        setTask(nextTask);
        window.sessionStorage.setItem(activeReviewKey, JSON.stringify({ userId: user?.user_id ?? "anonymous", documentId: item.document_id, reviewId: item.review_id, taskId: item.review_task_id }));
      }
    } catch (loadError) {
      if (epoch === viewEpoch.current) setError(errorMessage(loadError, "无法打开审阅任务"));
    }
  }

  async function runReview() {
    // 审阅与筛选共享任务生命周期，成功后再读取风险发现。
    if (!selected || taskRunning || busy) return;
    const epoch = viewEpoch.current;
    setBusy(true); setError("");
    try {
      const result = await createReview([selected.document_id]);
      const nextTask = await getTask(result.task_id);
      if (epoch === viewEpoch.current) {
        setReviewId(result.review_id);
        setFindings([]);
        setWorkflow(null);
        setTask(nextTask);
        window.sessionStorage.setItem(activeReviewKey, JSON.stringify({ userId: user?.user_id ?? "anonymous", documentId: selected.document_id, reviewId: result.review_id, taskId: result.task_id }));
      }
    } catch (reviewError) {
      if (epoch === viewEpoch.current) setError(errorMessage(reviewError, "合同审阅失败"));
    } finally { if (epoch === viewEpoch.current) setBusy(false); }
  }

  async function decide(finding: Finding, decision: "approved" | "rejected", note: string) {
    if (!reviewId || decidingId) return;
    const epoch = viewEpoch.current;
    setDecidingId(finding.finding_id);
    setError("");
    try {
      const updated = await decideFinding(reviewId, finding, decision, note);
      if (epoch !== viewEpoch.current) return;
      setFindings((items) => items.map((item) => item.finding_id === updated.finding_id
        ? { ...item, review_status: updated.review_status, review_note: updated.review_note, version: updated.version }
        : item));
    } catch (decisionError) {
      if (epoch !== viewEpoch.current) return;
      if (decisionError instanceof ApiError && decisionError.code === "REVIEW_CONFLICT") {
        // 并发修改时重新读取风险项，用户可在看到新版本后再提交。
        const latest = await getFindings(reviewId).catch(() => null);
        if (latest && epoch === viewEpoch.current) setFindings(latest.items);
      }
      setError(errorMessage(decisionError, "人工确认失败"));
    } finally { if (epoch === viewEpoch.current) setDecidingId(null); }
  }

  async function closeUnresolved(note: string) {
    if (!reviewId || closingUnresolved) return;
    const epoch = viewEpoch.current;
    setClosingUnresolved(true); setError("");
    try {
      await closeUnresolvedReview(reviewId, note);
      if (epoch === viewEpoch.current) {
        setClosedUnresolved(true);
        setTask((current) => current && current.result_ref === reviewId
          ? { ...current, status: "cancelled", current_node: "human_resolution" } : current);
      }
    } catch (closeError) {
      if (epoch === viewEpoch.current) setError(errorMessage(closeError, "结案失败"));
    } finally { if (epoch === viewEpoch.current) setClosingUnresolved(false); }
  }

  async function exportReport(format: ReportFormat) {
    if (!reviewId || exporting) return;
    const epoch = viewEpoch.current;
    setExporting(true);
    setError("");
    try {
      const report = await getReviewReport(reviewId, format);
      if (epoch !== viewEpoch.current) return;
      const extension = format === "markdown" ? "md" : format;
      const url = URL.createObjectURL(report);
      const link = document.createElement("a");
      link.href = url;
      link.download = `docpilot-${reviewId}.${extension}`;
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1_000);
    } catch (reportError) {
      if (epoch === viewEpoch.current) setError(errorMessage(reportError, "报告导出失败"));
    } finally { if (epoch === viewEpoch.current) setExporting(false); }
  }

  function showEvidence(items: Citation[], selectedCitation: string) {
    // 点击风险引用时把整组证据交给侧栏，并突出显示被点选的片段。
    setCitations(items);
    setSelectedCitationId(selectedCitation);
    if (window.innerWidth <= 1120) {
      window.requestAnimationFrame(() => document.querySelector(".evidence-panel")?.scrollIntoView({ behavior: "smooth", block: "start" }));
    }
  }

  async function ask() {
    // 问答接口返回可追溯引用，页面同时保存答案和引用列表。
    if (!selected || !question.trim()) return;
    const epoch = viewEpoch.current;
    setBusy(true); setError("");
    try {
      const session = await createChatSession(selected.document_id);
      const result = await askQuestion(session.session_id, selected.document_id, question.trim());
      if (epoch === viewEpoch.current) {
        setAnswer(result.answer ?? "当前文档中没有足够证据回答这个问题，请补充信息或调整问题。");
        setCitations(result.citations);
        setSelectedCitationId(result.citations[0]?.citation_id ?? null);
      }
    } catch (questionError) {
      if (epoch === viewEpoch.current) setError(errorMessage(questionError, "问题提交失败"));
    } finally { if (epoch === viewEpoch.current) setBusy(false); }
  }

  const healthText = health === "connected" ? "后端已就绪" : health === "disconnected" ? "后端未就绪" : "正在检查后端";
  const actionDisabled = busy || taskRunning || selected?.status !== "indexed";

  return <div className="app-shell">
    <header className="topbar">
      <div className="brand"><span className="brand-mark">D</span><span>DocPilot</span></div>
      <span className="environment">本地合同智能审阅工作台</span>
      <div className={`health ${health}`} aria-live="polite"><span />{healthText}</div>
    </header>
    <main className="page">
      <div className="hero"><div><span className="eyebrow">合同智能分析工作台</span><h1>让每项合同结论都有<em>原文依据</em></h1><p>上传合同，等待系统完成解析和索引，即可在同一页面进行筛选、问答和风险审阅。</p></div></div>
      {error && <div className="error-banner" role="alert"><span>{error}</span><button onClick={() => setError("")} aria-label="关闭错误提示">×</button></div>}
      <AuthPanel user={user} onAuthenticated={authenticated} onLoggedOut={loggedOut} />
      <div className="main-grid">
        <DocumentWorkspace documents={documents} selectedId={selectedId} onSelect={selectDocument} onUploaded={handleUploaded} uploadDisabled={taskRunning} />
        <div className="detail-column">
          {selected ? <>
            <section className="detail-card"><div className="detail-header"><div><span className="eyebrow">当前文档</span><h2>{selected.display_name}</h2><p>{selected.filename} · {documentStatus[selected.status]}</p></div><span className={`large-status ${selected.status}`}>{documentStatus[selected.status]}</span></div>{selected.document_type === "contract" && <div className="fact-grid"><div><small>合同金额</small><strong>{selected.facts.amount ? selected.facts.amount.toLocaleString("zh-CN") : "未提取"}</strong></div><div><small>合同期限</small><strong>{selected.facts.duration_days ? `${selected.facts.duration_days} 天` : "未提取"}</strong></div><div><small>付款期限</small><strong>{selected.facts.payment_days ? `${selected.facts.payment_days} 天` : "未提取"}</strong></div><div><small>风险等级</small><strong>{selected.facts.risk ? (riskLabel[selected.facts.risk] ?? selected.facts.risk) : "未判断"}</strong></div></div>}</section>
            {selected.document_type === "contract" && <><section className="tool-card">
              <div className="section-heading"><div><span className="eyebrow">合同筛选</span><h2>检查是否符合筛选条件</h2></div></div>
              <div className="form-row"><input aria-label="筛选说明" placeholder="筛选说明（可选；金额条件请在右侧填写）" value={query} onChange={(event) => setQuery(event.target.value)} /><input aria-label="最低合同金额" type="number" min="0" placeholder="金额大于（元）" value={amount} onChange={(event) => setAmount(event.target.value)} /><button onClick={runScreen} disabled={actionDisabled}>开始筛选</button></div>
              <label className="deep-analysis-option"><input type="checkbox" checked={deepAnalysis} onChange={(event) => setDeepAnalysis(event.target.checked)} />命中后自动进行 Agent 审阅</label>
              <details className="screen-scope"><summary>筛选文档（已选 {currentScreenDocumentIds.length} 份，最多 20 份）</summary>
                <div className="screen-scope-list">{documents.filter((item) => item.status === "indexed").map((item) => <label key={item.document_id}>
                  <input type="checkbox" checked={currentScreenDocumentIds.includes(item.document_id)} onChange={() => {
                    // 没有显式选择时从当前文档开始，再应用本次勾选变化。
                    const current = currentScreenDocumentIds;
                    const next = current.includes(item.document_id) ? current.filter((id) => id !== item.document_id) : [...current, item.document_id];
                    if (next.length > 0 && next.length <= 20) setScreenDocumentIds(next);
                  }} />{item.display_name}
                </label>)}</div>
              </details>
              {screening && <div className="screen-result"><div className="result-strip">共 {screening.total} 份文档，{screening.matched} 份符合条件</div>
                {screening.items.map((item) => <div className="screen-result-item" key={item.document_id}>
                  <strong>{documents.find((documentItem) => documentItem.document_id === item.document_id)?.display_name ?? item.document_id}</strong>
                  <span>{item.matched ? "符合条件" : "未符合条件"}</span>
                  <small>{item.missing_fields.length ? `缺少字段：${item.missing_fields.map((field) => screeningFieldLabel[field] ?? field).join("、")}` : item.matched_conditions.length ? `命中条件：${item.matched_conditions.map((field) => screeningFieldLabel[field] ?? field).join("、")}` : "没有满足筛选条件"}</small>
                  {item.review_task_id && <button type="button" onClick={() => void openScreenReview(item)}>打开审阅</button>}
                </div>)}
              </div>}
            </section>
            <section className="tool-card"><div className="section-heading"><div><span className="eyebrow">风险审阅</span><h2>按规则检查合同风险</h2></div><button onClick={runReview} disabled={actionDisabled}>开始审阅</button></div><ReviewWorkspace findings={findings} reviewId={reviewId} workflow={workflow} decidingId={decidingId} exporting={exporting} closingUnresolved={closingUnresolved} closedUnresolved={closedUnresolved} onDecision={(finding, decision, note) => void decide(finding, decision, note)} onExport={(format) => void exportReport(format)} onShowEvidence={showEvidence} onCloseUnresolved={(note) => void closeUnresolved(note)} /></section></>}
            <section className="tool-card"><div className="section-heading"><div><span className="eyebrow">证据问答</span><h2>询问当前文档</h2></div></div><div className="form-row"><input aria-label="文档问题" placeholder="例如：付款条件是什么？" value={question} onChange={(event) => setQuestion(event.target.value)} onKeyDown={(event) => event.key === "Enter" && void ask()} /><button onClick={ask} disabled={busy || selected.status !== "indexed"}>提问</button></div>{answer && <p className="answer">{answer}</p>}</section>
            <BusinessAnalysisPanel key={selected.document_id} documents={documents} selected={selected} onShowEvidence={showEvidence} />
          </> : <div className="empty-detail">请选择一份文档查看提取结果。</div>}
          <WorkflowTimeline task={task} />
        </div>
        <EvidencePanel citations={citations} selectedCitationId={selectedCitationId} />
      </div>
    </main>
  </div>;
}
