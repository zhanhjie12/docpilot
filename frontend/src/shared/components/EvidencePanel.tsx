import type { Citation } from "../../types";

export function EvidencePanel({ citations, selectedCitationId }: { citations: Citation[]; selectedCitationId: string | null }) {
  // 展示引用的文档、版本、页码和字符位置，便于人工回到原文核对。
  return <aside className="evidence-panel">
    <div className="eyebrow">证据引用</div><h3>原文片段</h3>
    {citations.length ? citations.map((citation, index) => <div className={`citation ${selectedCitationId === citation.citation_id ? "active" : ""}`} key={`${citation.citation_id}-${index}`}>
      <span>{citation.page ? `第 ${citation.page} 页` : "页码待确认"}</span>
      {citation.section && <small>章节：{citation.section}</small>}
      <small>文档：{citation.document_id}</small>
      <small>版本：{citation.version_id}</small>
      {citation.char_start !== null && citation.char_end !== null && <small>字符位置：{citation.char_start}–{citation.char_end}</small>}
      <p>{citation.quote || "引用未包含可展示的原文，请打开文档人工核对。"}</p>
    </div>) : <p className="muted">提问或查看风险引用后，可在这里核对原文位置和片段。</p>}
  </aside>;
}
