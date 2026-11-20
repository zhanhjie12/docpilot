import { useState } from "react";
import { uploadDocument } from "../../../shared/services/api";
import type { DocumentType } from "../../../types";

// taskId 为 null 表示服务端发现重复文件，已有文档无需重新处理。
export interface UploadedDocument { documentId: string; taskId: string | null; }

export function DocumentUploader({ onUploaded, disabled = false }: { onUploaded: (result: UploadedDocument) => void | Promise<void>; disabled?: boolean }) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [documentType, setDocumentType] = useState<DocumentType>("contract");
  async function submit(file?: File) {
    if (!file) return;
    setBusy(true); setMessage("");
    try {
      // 上传组件只负责选择文件和回传结果，轮询由页面容器统一管理。
      const result = await uploadDocument(file, undefined, documentType);
      setMessage(result.task_id ? "上传成功，正在解析并建立索引。" : "该文件已存在，已载入现有文档。");
      await onUploaded({ documentId: result.document_id, taskId: result.task_id });
    }
    catch (error) { setMessage(error instanceof Error ? error.message : "上传失败"); }
    finally { setBusy(false); }
  }
  return <div className="upload-card"><label>文档类型 <select value={documentType} disabled={busy || disabled} onChange={(event) => setDocumentType(event.target.value as DocumentType)}><option value="contract">合同</option><option value="tender">招标文件</option><option value="bid">投标文件</option><option value="policy">制度文件</option><option value="other">其他</option></select></label><label className={`drop-zone ${disabled ? "disabled" : ""}`}><input type="file" accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document" disabled={busy || disabled} onChange={(event) => submit(event.target.files?.[0])} /><span className="upload-icon">+</span><strong>{busy ? "正在上传……" : disabled ? "请等待当前任务完成" : "选择 PDF 或 DOCX 文件"}</strong><small>文件将在后台解析、建立索引，并保存在本地存储中。</small></label>{message && <p className="inline-message">{message}</p>}</div>;
}
