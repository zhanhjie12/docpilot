"""将字段和风险的引用逐一核对到已索引原文。"""

from __future__ import annotations

from app.infrastructure.database import SQLiteDatabase, json_loads
from app.playbooks.evaluator import evaluate_playbook

from .extraction_agent import field_value_from_quote
from .state import EvidenceRef, NodeResult, TaskState


def _in_scope(state: TaskState, citation: EvidenceRef) -> bool:
    """按文档当前版本与显式版本的联合范围放行原文引用。"""

    selected_document = citation.document_id in state.document_ids
    selected_version = citation.version_id in state.version_ids
    if state.task_type == "compare":
        return bool(citation.quote.strip()) and selected_version
    return bool(citation.quote.strip()) and (selected_document or selected_version)


def _chunk_exists(db: SQLiteDatabase, citation: EvidenceRef, user_id: str | None) -> bool:
    """核对引用 ID、版本、页码、章节、原文和全文字符区间。"""

    if not citation.citation_id.startswith("cite_"):
        return False
    chunk_id = citation.citation_id.removeprefix("cite_")
    row = db.one(
        "SELECT c.*,d.owner_id FROM chunks c "
        "JOIN documents d ON d.document_id=c.document_id WHERE c.chunk_id=?",
        (chunk_id,),
    )
    if row is None or row["owner_id"] != user_id:
        return False
    return all(
        (
            citation.document_id == row["document_id"],
            citation.version_id == row["version_id"],
            citation.page == row["page"],
            citation.section == row["section_path"],
            citation.quote == row["text"],
            citation.char_start == row["char_start"],
            citation.char_end == row["char_end"],
            row["char_end"] - row["char_start"] == len(row["text"]),
        )
    )


def _compare_source_exists(db: SQLiteDatabase, citation: EvidenceRef, user_id: str | None) -> bool:
    """版本对比用全文切片；页码仍要落在该版本的解析页范围内。"""

    expected = f"cite_compare_{citation.version_id}_{citation.char_start}_{citation.char_end}"
    if citation.citation_id != expected or citation.char_start is None or citation.char_end is None:
        return False
    row = db.one(
        "SELECT v.document_id,v.extracted_text,v.parsed_content,d.owner_id "
        "FROM document_versions v JOIN documents d ON d.document_id=v.document_id "
        "WHERE v.version_id=?",
        (citation.version_id,),
    )
    if row is None or row["document_id"] != citation.document_id:
        return False
    if row["owner_id"] != user_id:
        return False
    text = row["extracted_text"] or ""
    start, end = citation.char_start, citation.char_end
    if end <= start or text[start:end] != citation.quote:
        return False
    pages = json_loads(row["parsed_content"], {}).get("pages", [])
    cursor = 0
    for page in pages:
        page_text = page.get("text", "")
        if (
            page.get("page") == citation.page
            and cursor <= start < end <= cursor + len(page_text)
            and page_text[start - cursor : end - cursor] == citation.quote
        ):
            return citation.section == page.get("section")
        cursor += len(page_text) + 2
    return False


def _valid_citations(state: TaskState, db: SQLiteDatabase | None) -> dict[str, EvidenceRef]:
    """仅接受内容一致、位于任务范围内且来源可回放的引用。"""

    valid: dict[str, EvidenceRef] = {}
    duplicate_ids: set[str] = set()
    for citation in state.evidence:
        if not _in_scope(state, citation):
            continue
        if citation.citation_id in valid and valid[citation.citation_id] != citation:
            duplicate_ids.add(citation.citation_id)
            continue
        if db is not None:
            if state.task_type == "compare" and citation.citation_id.startswith("cite_compare_"):
                verified = _compare_source_exists(db, citation, state.user_id)
            else:
                verified = _chunk_exists(db, citation, state.user_id)
                if verified and citation.version_id not in state.version_ids:
                    # 仅凭 document_id 不能把任意历史版本带进当前审阅。
                    version = db.one(
                        "SELECT d.latest_version_id FROM documents d WHERE d.document_id=?",
                        (citation.document_id,),
                    )
                    verified = version is not None and citation.version_id == version["latest_version_id"]
            if not verified:
                continue
        valid[citation.citation_id] = citation
    for citation_id in duplicate_ids:
        valid.pop(citation_id, None)
    return valid


def _citation_matches(reference: EvidenceRef, valid: dict[str, EvidenceRef]) -> bool:
    """引用对象本身必须与已验证的原文一致，不能只复用相同 ID。"""

    return valid.get(reference.citation_id) == reference


def verify_node(state: TaskState, db: SQLiteDatabase | None = None) -> NodeResult:
    """发现伪造或错配证据时暂停；成功结果供 Report 精确过滤。"""

    valid = _valid_citations(state, db)
    valid_scopes = {(item.document_id, item.version_id) for item in valid.values()}
    approved_fields: list[int] = []
    invalid_fields: list[str] = []
    for index, field in enumerate(state.extracted_fields):
        if field.status != "found":
            # 未知和缺失不是事实结论，但它们的文档归属仍须处于已核验范围。
            pair = (field.document_id, field.version_id)
            if field.status != "needs_review" and (
                pair in valid_scopes
                or (field.document_id is None and field.version_id is None and len(valid_scopes) == 1)
            ):
                approved_fields.append(index)
            else:
                invalid_fields.append(field.name)
            continue
        scoped = (
            field.document_id is not None
            and field.version_id is not None
            and all(
                citation.document_id == field.document_id
                and citation.version_id == field.version_id
                for citation in field.citations
            )
        )
        # 旧任务可省略归属，但只能在唯一文档版本的上下文中推断。
        if field.document_id is None and field.version_id is None:
            scoped = len(valid_scopes) == 1
        matching = bool(field.citations) and all(
            _citation_matches(citation, valid)
            and field_value_from_quote(citation.quote, field.name) == field.value
            for citation in field.citations
        )
        if scoped and matching:
            approved_fields.append(index)
        else:
            invalid_fields.append(field.name)

    approved_risks: list[str] = []
    invalid_risks: list[str] = []
    # A finding is accepted only when approved fields reproduce the same rule result.
    expected_risks = {
        finding.finding_id: finding
        for finding in evaluate_playbook(
            state.task_id,
            [state.extracted_fields[index] for index in approved_fields],
        )
    }
    for risk in state.risks:
        matching = risk.rule_hit and bool(risk.citations) and all(
            citation.document_id == risk.document_id
            and citation.version_id == risk.version_id
            and _citation_matches(citation, valid)
            for citation in risk.citations
        )
        expected = expected_risks.get(risk.finding_id)
        matching = matching and expected is not None and (
            risk.model_dump(exclude={"review_status"})
            == expected.model_dump(exclude={"review_status"})
        )
        if matching:
            approved_risks.append(risk.finding_id)
        else:
            invalid_risks.append(risk.rule_id)

    compare = state.node_results.get("compare")
    empty_compare = (
        state.task_type == "compare"
        and compare is not None
        and compare.status == "succeeded"
        and compare.output.get("changed") is False
        and not compare.output.get("changes")
    )
    if state.task_type == "compare" and not empty_compare:
        # 有变更时，每条变更的两侧引用都必须与已回放的来源对象完全一致。
        changes = compare.output.get("changes") if compare is not None else None
        if not isinstance(changes, list) or not changes:
            invalid_risks.append("compare_changes")
        else:
            for change in changes:
                if not isinstance(change, dict):
                    invalid_risks.append("compare_change")
                    continue
                for key, expected_version in (
                    ("base_citation", state.version_ids[0] if state.version_ids else None),
                    ("target_citation", state.version_ids[1] if len(state.version_ids) > 1 else None),
                ):
                    try:
                        reference = EvidenceRef.model_validate(change.get(key))
                    except (TypeError, ValueError):
                        invalid_risks.append("compare_change")
                        continue
                    if reference.version_id != expected_version or not _citation_matches(reference, valid):
                        invalid_risks.append("compare_change")
        covered_versions = {item.version_id for item in valid.values()}
        if not set(state.version_ids).issubset(covered_versions):
            invalid_risks.append("compare_evidence")
    if state.task_type == "qa" and not state.request.strip():
        invalid_risks.append("qa_question")

    output = {
        "approved_field_indexes": approved_fields,
        "approved_risk_ids": approved_risks,
        "verified_citation_ids": sorted(valid),
        "verified_evidence_count": len(valid),
        "invalid_fields": invalid_fields,
        "invalid_risks": invalid_risks,
    }
    if invalid_fields or invalid_risks or (not valid and not empty_compare):
        state.status = "waiting_human"
        return NodeResult(
            node="verify",
            status="waiting_human",
            output=output,
            error="证据与结论不一致或无法回到原文",
        )
    return NodeResult(node="verify", status="succeeded", output=output)


class VerificationAgent:
    """证据校验的工作流适配器。"""

    def __init__(self, db: SQLiteDatabase | None = None) -> None:
        self.db = db

    def run(self, state: TaskState) -> NodeResult:
        return verify_node(state, self.db)
