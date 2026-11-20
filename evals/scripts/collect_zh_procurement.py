"""下载公开采购 PDF，并用 DocPilot 解析器生成不含正文的技术清单。"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.services.document_parser import parse_document  # noqa: E402


SOURCES = PROJECT_ROOT / "evals" / "zh_procurement_sources_v1.json"
RAW_DIR = PROJECT_ROOT / "evals" / "raw" / "zh_procurement"
MANIFEST = PROJECT_ROOT / "evals" / "zh_procurement_manifest_v1.json"
MAX_BYTES = 30 * 1024 * 1024
ALLOWED_HOSTS = {"www.ccgp-neimenggu.gov.cn", "obs.gcy.zfcg.gxzf.gov.cn"}


def _download(url: str) -> bytes:
    """只访问清单中的官方域名，并限制响应体大小。"""

    if urlparse(url).hostname not in ALLOWED_HOSTS:
        raise ValueError("source host is not allowlisted")
    request = Request(url, headers={"User-Agent": "DocPilot-Research/1.0"})
    with urlopen(request, timeout=35) as response:
        # 重定向后的地址也必须仍属于允许的官方域名。
        if urlparse(response.geturl()).hostname not in ALLOWED_HOSTS:
            raise ValueError("redirected host is not allowlisted")
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("PDF exceeds 30 MiB collection limit")
    if not data.startswith(b"%PDF-"):
        raise ValueError("response is not a PDF")
    return data


def _inspect(data: bytes, document_id: str, project_id: str | None) -> dict[str, object]:
    """调用项目当前 PDF 解析器，统计可提取文本的页数。"""

    parsed = parse_document(data, "application/pdf", f"{document_id}.pdf")
    page_char_counts = [len(page.text.strip()) for page in parsed.pages]
    text_pages = sum(length >= 40 for length in page_char_counts)
    chinese_chars = sum("\u3400" <= char <= "\u9fff" for char in parsed.text)
    # PDF 提取可能在编号中插入空格或换行，比较前统一去除空白。
    compact_text = "".join(parsed.text.split())
    project_id_verified = bool(project_id and project_id in compact_text)
    return {
        "page_count": parsed.page_count,
        "text_pages": text_pages,
        "extracted_chars": len(parsed.text),
        "chinese_chars": chinese_chars,
        "parser_name": parsed.parser_name,
        "parser_version": parsed.parser_version,
        "quality_warnings": parsed.quality_warnings,
        "project_id": project_id if project_id_verified else None,
        "project_id_verified_in_text": project_id_verified,
        # 这是技术准入判断，不表示脱敏、许可核对或人工标注完成。
        "text_extractable": chinese_chars >= 200 and text_pages >= max(1, parsed.page_count // 2),
    }


def _collect(item: dict[str, object], acquired_date: str) -> dict[str, object]:
    """保存一份原件；即使下载失败，也保留候选及失败原因。"""

    document_id = str(item["id"])
    url = str(item["source_url"])
    path = RAW_DIR / f"{document_id}.pdf"
    record: dict[str, object] = {
        "id": document_id,
        "document_type": "tender" if document_id.startswith("cn_tender_") else "contract",
        "category": item.get("category"),
        "project_id_hint": item.get("project_id_hint") or item.get("project_id"),
        "paired_contract_id": item.get("contract_id"),
        "source_url": url,
        "acquired_date": acquired_date,
        "usage_restrictions": "公开附件的进一步使用范围待核实；原件仅保留于本地非公开目录。公开再分发及模型训练须先核实授权。",
        "rights_review_status": "pending_manual_review",
    }
    try:
        # 已存在的原件只读复核，避免重复网络请求或覆盖历史文件。
        data = path.read_bytes() if path.exists() else _download(url)
        inspection = _inspect(data, document_id, record["project_id_hint"])
        if not path.exists():
            path.write_bytes(data)
        record.update({
            "status": "downloaded",
            "raw_path": str(path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            **inspection,
        })
    except Exception as exc:  # noqa: BLE001 - 单份失败不阻止清单记录其他原件。
        record.update({"status": "failed", "error_type": type(exc).__name__})
    return record


def main() -> None:
    """逐份收集并输出来源、文件哈希和解析质量。"""

    source_set = json.loads(SOURCES.read_text(encoding="utf-8"))
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for item in [*source_set["candidates"], *source_set["paired_tenders"]]:
        row = _collect(item, source_set["acquired_date"])
        rows.append(row)
        print(f"{row['id']}: {row['status']}; text_extractable={row.get('text_extractable')}", flush=True)
    result = {
        "source_set": source_set["source_set"],
        "acquired_date": source_set["acquired_date"],
        "contract_count": sum(row["document_type"] == "contract" and row.get("text_extractable") is True for row in rows),
        "tender_count": sum(row["document_type"] == "tender" and row.get("text_extractable") is True for row in rows),
        "files": rows,
    }
    MANIFEST.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Manifest: {MANIFEST}", flush=True)


if __name__ == "__main__":
    main()
