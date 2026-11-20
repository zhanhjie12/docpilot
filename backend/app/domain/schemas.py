"""Validated API contracts shared by HTTP routes and application services."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


def _reject_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    # IDs are opaque: validate them without silently rewriting their contents.
    return value


def _reject_duplicate_ids(values: list[str]) -> list[str]:
    if len(values) != len(set(values)):
        raise ValueError("IDs must be unique")
    return values


NonBlankString = Annotated[str, Field(min_length=1), AfterValidator(_reject_blank)]
DocumentIds = Annotated[list[NonBlankString], AfterValidator(_reject_duplicate_ids)]


class RequestModel(BaseModel):
    """Reject unknown request fields instead of silently discarding typos."""

    model_config = ConfigDict(extra="forbid")


class RegisterRequest(RequestModel):
    """创建用户所需的最小资料。"""

    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=128)
    display_name: NonBlankString = Field(default="DocPilot 用户", max_length=80)

    @model_validator(mode="after")
    def normalize_email(self) -> Self:
        """统一邮箱大小写和首尾空格，保证唯一性判断稳定。"""

        normalized = self.email.strip().lower()
        if "@" not in normalized or normalized.startswith("@") or normalized.endswith("@"):
            raise ValueError("email must be a valid address")
        if any(char.isspace() for char in normalized):
            raise ValueError("email must be a valid address")
        self.email = normalized
        return self


class LoginRequest(RequestModel):
    """登录请求；错误响应不会区分邮箱不存在还是密码错误。"""

    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def normalize_email(self) -> Self:
        self.email = self.email.strip().lower()
        return self


class UserResponse(BaseModel):
    """可安全返回给前端的用户资料，不包含密码哈希。"""

    user_id: NonBlankString
    email: NonBlankString
    display_name: NonBlankString
    role: NonBlankString
    created_at: NonBlankString


class AuthResponse(BaseModel):
    """注册或登录成功后的 Bearer 令牌和用户信息。"""

    access_token: NonBlankString
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(gt=0, strict=True)
    user: UserResponse


class DocumentScope(RequestModel):
    """Explicit document/version scope; never imply access to every document."""

    document_ids: DocumentIds | None = None
    version_ids: DocumentIds | None = None

    @model_validator(mode="after")
    def require_scope(self) -> Self:
        if not self.document_ids and not self.version_ids:
            raise ValueError("document_ids or version_ids must contain at least one ID")
        return self


class ChatSessionCreateRequest(DocumentScope):
    """Create a chat session limited to the supplied documents or versions."""


class ChatAskRequest(DocumentScope):
    question: NonBlankString
    top_k: int = Field(default=5, gt=0, strict=True)


class PolicyMetadataRequest(RequestModel):
    """Human-confirmed applicability of one uploaded policy version."""

    policy_family_id: NonBlankString
    issuer: NonBlankString
    org_scope: NonBlankString
    effective_from: date
    effective_to: date | None = None
    status: Literal["draft", "active", "retired"]

    @model_validator(mode="after")
    def valid_period(self) -> Self:
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must follow effective_from")
        return self


class PolicyAskRequest(RequestModel):
    """Ask within explicit documents, organization and effective date."""

    document_ids: DocumentIds = Field(min_length=1)
    org_scope: NonBlankString
    as_of_date: date
    question: NonBlankString
    top_k: int = Field(default=5, gt=0, le=20, strict=True)


class TenderCheckRequest(RequestModel):
    """Check explicitly named tender requirements against one bid version."""

    tender_version_id: NonBlankString
    bid_version_id: NonBlankString
    requirement_queries: list[NonBlankString] = Field(min_length=1, max_length=20)
    top_k: int = Field(default=3, gt=0, le=5, strict=True)

    @model_validator(mode="after")
    def distinct_versions(self) -> Self:
        if self.tender_version_id == self.bid_version_id:
            raise ValueError("tender and bid versions must differ")
        if len(self.requirement_queries) != len(set(self.requirement_queries)):
            raise ValueError("requirement queries must be unique")
        return self


class ReviewCreateRequest(DocumentScope):
    """Create a review for an explicit document/version scope."""


class ReviewDecisionRequest(RequestModel):
    decision: Literal["approved", "rejected"]
    note: str | None = None
    expected_version: int = Field(ge=1, strict=True)


class ReviewUnresolvedRequest(RequestModel):
    """Close an evidence-insufficient review with an explicit human note."""

    note: NonBlankString = Field(max_length=1000)
    expected_version: int = Field(ge=1, strict=True)


class CompareVersionsRequest(RequestModel):
    base_version_id: NonBlankString
    target_version_id: NonBlankString

    @model_validator(mode="after")
    def require_distinct_versions(self) -> Self:
        if self.base_version_id == self.target_version_id:
            raise ValueError("base_version_id and target_version_id must differ")
        return self


class Citation(BaseModel):
    """Evidence location; unavailable location fields remain explicit nulls."""

    citation_id: NonBlankString
    document_id: NonBlankString
    version_id: NonBlankString
    page: int | None = Field(ge=1, strict=True)
    section: str | None
    quote: str
    char_start: int | None = Field(ge=0, strict=True)
    char_end: int | None = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def validate_offsets(self) -> Self:
        if (self.char_start is None) != (self.char_end is None):
            raise ValueError("char_start and char_end must both be null or both be present")
        if (
            self.char_start is not None
            and self.char_end is not None
            and self.char_end <= self.char_start
        ):
            raise ValueError("char_end must be greater than char_start")
        return self


class ParsedBlockSchema(BaseModel):
    """Serializable evidence block returned by a format parser."""

    block_id: NonBlankString
    block_type: Literal["heading", "paragraph", "table"]
    text: NonBlankString
    page: int = Field(ge=1, strict=True)
    order: int = Field(ge=0, strict=True)
    char_start: int = Field(ge=0, strict=True)
    char_end: int = Field(gt=0, strict=True)
    bbox: tuple[float, float, float, float] | None = None
    heading_level: int | None = Field(default=None, ge=1, strict=True)
    table_rows: list[list[str]] = Field(default_factory=list)
    source_ref: str | None = None

    @model_validator(mode="after")
    def validate_block(self) -> Self:
        if self.char_end <= self.char_start:
            raise ValueError("char_end must be greater than char_start")
        if self.block_type == "heading" and self.heading_level is None:
            raise ValueError("heading blocks require heading_level")
        if self.block_type == "table" and not self.table_rows:
            raise ValueError("table blocks require table_rows")
        return self


class ParsedPageSchema(BaseModel):
    """One logical page and its ordered source blocks."""

    page: int = Field(ge=1, strict=True)
    text: str
    section: str | None = None
    blocks: list[ParsedBlockSchema] = Field(default_factory=list)
    title_candidates: list[str] = Field(default_factory=list)
    width: float | None = Field(default=None, gt=0)
    height: float | None = Field(default=None, gt=0)


class ParsedDocumentSchema(BaseModel):
    """Persisted structured parsing result for one document version."""

    pages: list[ParsedPageSchema]
    page_count: int = Field(ge=1, strict=True)
    parser_name: NonBlankString
    parser_version: NonBlankString
    quality_warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_page_count(self) -> Self:
        if self.page_count != len(self.pages):
            raise ValueError("page_count must equal the number of pages")
        if [page.page for page in self.pages] != list(range(1, self.page_count + 1)):
            raise ValueError("pages must be consecutive and 1-based")
        return self
