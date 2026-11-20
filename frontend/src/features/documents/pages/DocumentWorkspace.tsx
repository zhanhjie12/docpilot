import type { Document } from "../../../types";
import { DocumentUploader, type UploadedDocument } from "../components/DocumentUploader";

const documentStatus: Record<Document["status"], string> = { uploaded: "已上传", parsing: "解析中", parsed: "已解析", indexed: "已建立索引", failed: "处理失败" };

export function DocumentWorkspace({ documents, selectedId, onSelect, onUploaded, uploadDisabled }: { documents: Document[]; selectedId: string | null; onSelect: (id: string) => void; onUploaded: (result: UploadedDocument) => void | Promise<void>; uploadDisabled: boolean }) {
  // selectedId 由页面容器控制，列表只负责展示和回传用户选择。
  return <section className="workspace-grid">
    <div><DocumentUploader onUploaded={onUploaded} disabled={uploadDisabled} /><div className="section-heading"><div><span className="eyebrow">文档管理</span><h2>合同库</h2></div><span className="count-pill">{documents.length}</span></div>
      <div className="document-list">{documents.length === 0 ? <div className="empty-state">上传一份合同开始分析。</div> : documents.map((document) => <button className={`document-row ${selectedId === document.document_id ? "active" : ""}`} key={document.document_id} onClick={() => onSelect(document.document_id)}><span className="file-mark">{document.filename.toLowerCase().endsWith(".pdf") ? "PDF" : "DOCX"}</span><span className="document-copy"><strong>{document.display_name}</strong><small>{document.page_count ?? "-"} 页 · {documentStatus[document.status]}</small></span><span className={`status-dot ${document.status}`} /></button>)}</div>
    </div>
  </section>;
}
