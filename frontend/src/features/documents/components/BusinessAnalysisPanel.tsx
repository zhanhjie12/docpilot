// Enterprise document tools keep every query inside selected owned versions.
import { useState } from "react";
import type { Citation, Document, PolicyAnswer, TenderCheckResult } from "../../../types";
import { askPolicy, checkTender, listTenderRequirements, setPolicyMetadata } from "../../../shared/services/api";

export function BusinessAnalysisPanel({ documents, selected, onShowEvidence }: {
  documents: Document[];
  selected: Document;
  onShowEvidence: (citations: Citation[], citationId: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [family, setFamily] = useState("");
  const [issuer, setIssuer] = useState("");
  const [org, setOrg] = useState("");
  const [effectiveFrom, setEffectiveFrom] = useState("");
  const [asOf, setAsOf] = useState(new Date().toISOString().slice(0, 10));
  const [policyQuestion, setPolicyQuestion] = useState("");
  const [policyAnswer, setPolicyAnswer] = useState<PolicyAnswer | null>(null);
  const [tenderId, setTenderId] = useState("");
  const [bidId, setBidId] = useState("");
  const [requirements, setRequirements] = useState("");
  const [tenderResult, setTenderResult] = useState<TenderCheckResult | null>(null);
  const tenders = documents.filter((item) => item.document_type === "tender" && item.status === "indexed");
  const bids = documents.filter((item) => item.document_type === "bid" && item.status === "indexed");

  async function confirmPolicy() {
    if (!family.trim() || !issuer.trim() || !org.trim() || !effectiveFrom) return;
    setBusy(true); setMessage("");
    try {
      await setPolicyMetadata(selected.latest_version_id, {
        policy_family_id: family.trim(), issuer: issuer.trim(), org_scope: org.trim(),
        effective_from: effectiveFrom, effective_to: null, status: "active",
      });
      setMessage("制度版本信息已确认。后续问答将按日期和组织范围过滤。");
    } catch (error) { setMessage(error instanceof Error ? error.message : "保存失败"); }
    finally { setBusy(false); }
  }

  async function queryPolicy() {
    if (!org.trim() || !asOf || !policyQuestion.trim()) return;
    setBusy(true); setMessage(""); setPolicyAnswer(null);
    try { setPolicyAnswer(await askPolicy(selected.document_id, org.trim(), asOf, policyQuestion.trim())); }
    catch (error) { setMessage(error instanceof Error ? error.message : "查询失败"); }
    finally { setBusy(false); }
  }

  async function compareTender() {
    const queries = requirements.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
    if (!tenderId || !bidId || !queries.length || queries.length > 20) return;
    setBusy(true); setMessage(""); setTenderResult(null);
    try { setTenderResult(await checkTender(tenderId, bidId, queries)); }
    catch (error) { setMessage(error instanceof Error ? error.message : "对齐失败"); }
    finally { setBusy(false); }
  }

  async function loadRequirements() {
    if (!tenderId) return;
    setBusy(true); setMessage("");
    try {
      const result = await listTenderRequirements(tenderId);
      setRequirements(result.items.slice(0, 20).map((item) => item.requirement_query).join("\n"));
      setMessage(`已提取 ${result.items.length} 条候选要求，请核对并编辑后提交；每次最多核对 20 条。`);
    } catch (error) { setMessage(error instanceof Error ? error.message : "提取失败"); }
    finally { setBusy(false); }
  }

  if (selected.document_type === "policy") return <section className="tool-card">
    <div className="section-heading"><div><span className="eyebrow">制度分析</span><h2>确认版本并按日期问答</h2></div></div>
    <p>版本适用信息须由用户确认；同一制度系列存在重叠生效版本时，系统会要求人工处理。</p>
    <div className="form-row"><input aria-label="制度系列" placeholder="制度系列 ID" value={family} onChange={(event) => setFamily(event.target.value)} /><input aria-label="发布部门" placeholder="发布部门" value={issuer} onChange={(event) => setIssuer(event.target.value)} /><input aria-label="适用组织" placeholder="适用组织" value={org} onChange={(event) => setOrg(event.target.value)} /><input aria-label="生效日期" type="date" value={effectiveFrom} onChange={(event) => setEffectiveFrom(event.target.value)} /><button disabled={busy || selected.status !== "indexed"} onClick={() => void confirmPolicy()}>确认版本</button></div>
    <div className="form-row"><input aria-label="查询日期" type="date" value={asOf} onChange={(event) => setAsOf(event.target.value)} /><input aria-label="制度问题" placeholder="例如：采购审批金额门槛是多少？" value={policyQuestion} onChange={(event) => setPolicyQuestion(event.target.value)} /><button disabled={busy || selected.status !== "indexed"} onClick={() => void queryPolicy()}>查询</button></div>
    {policyAnswer && <div><p className="answer">{policyAnswer.answer ?? "指定日期和组织范围内证据不足。"}</p>{policyAnswer.citations.map((cite) => <button key={cite.citation_id} onClick={() => onShowEvidence(policyAnswer.citations, cite.citation_id)}>查看原文 {cite.citation_id}</button>)}</div>}
    {message && <p className="inline-message">{message}</p>}
  </section>;

  if (selected.document_type === "tender" || selected.document_type === "bid") return <section className="tool-card">
    <div className="section-heading"><div><span className="eyebrow">招投标核对</span><h2>逐条对齐招标要求与投标证据</h2></div></div>
    <p>填写需要核对的要求，每行一条。结果只表示检索到的候选证据，合规判断仍需人工确认。</p>
    <div className="form-row"><select aria-label="招标文件" value={tenderId} onChange={(event) => setTenderId(event.target.value)}><option value="">选择招标文件</option>{tenders.map((item) => <option key={item.document_id} value={item.latest_version_id}>{item.display_name}</option>)}</select><select aria-label="投标文件" value={bidId} onChange={(event) => setBidId(event.target.value)}><option value="">选择投标文件</option>{bids.map((item) => <option key={item.document_id} value={item.latest_version_id}>{item.display_name}</option>)}</select></div>
    <button disabled={busy || !tenderId} onClick={() => void loadRequirements()}>提取要求候选</button>
    <textarea aria-label="逐条招标要求" placeholder="每行一条要求，最多 20 条" value={requirements} onChange={(event) => setRequirements(event.target.value)} />
    <button disabled={busy || !tenderId || !bidId} onClick={() => void compareTender()}>对齐证据</button>
    {tenderResult?.items.map((item) => <div className="screen-result-item" key={item.requirement_query}><strong>{item.requirement_query}</strong><span>{item.status === "needs_review" ? "两侧均有候选证据，待审核" : item.status === "bid_evidence_missing" ? "未检索到投标证据" : "未核实招标原文"}</span>{[...item.tender_citations, ...item.bid_citations].map((cite) => <button key={cite.citation_id} onClick={() => onShowEvidence([...item.tender_citations, ...item.bid_citations], cite.citation_id)}>查看 {cite.version_id} 原文</button>)}</div>)}
    {message && <p className="inline-message">{message}</p>}
  </section>;
  return null;
}
