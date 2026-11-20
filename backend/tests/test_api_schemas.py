"""Contract regressions for document scope, review decisions and evidence."""

import pytest
from pydantic import ValidationError

from app.domain.schemas import (
    ChatAskRequest,
    ChatSessionCreateRequest,
    Citation,
    CompareVersionsRequest,
    DocumentScope,
    ReviewCreateRequest,
    ReviewDecisionRequest,
)
from app.screening.models import NumericCondition, ScreeningConditions, ScreeningRequest


@pytest.mark.parametrize("scope", [{}, {"document_ids": []}, {"version_ids": None}])
@pytest.mark.parametrize("request_model", [ChatSessionCreateRequest, ReviewCreateRequest])
def test_requests_cannot_implicitly_search_all_documents(request_model, scope):
    with pytest.raises(ValidationError):
        request_model.model_validate(scope)


def test_scope_accepts_versions_and_combined_scope_without_rewriting_ids():
    assert DocumentScope(version_ids=["ver_001"]).version_ids == ["ver_001"]
    scope = DocumentScope(document_ids=["opaque doc id"], version_ids=["ver_001"])
    assert scope.document_ids == ["opaque doc id"]


@pytest.mark.parametrize("ids", [["  "], ["doc_001", "doc_001"], [1]])
@pytest.mark.parametrize("field", ["document_ids", "version_ids"])
def test_scope_rejects_unusable_ids(field, ids):
    with pytest.raises(ValidationError):
        DocumentScope.model_validate({field: ids})


@pytest.mark.parametrize(
    "payload",
    [
        {"question": "付款条件是什么？"},
        {"question": " \t", "document_ids": ["doc_001"]},
        {"question": "付款条件是什么？", "document_ids": ["doc_001"], "top_k": 0},
        {"question": "付款条件是什么？", "document_ids": ["doc_001"], "top_k": True},
        {"question": "付款条件是什么？", "document_ids": ["doc_001"], "top_k": 1.5},
    ],
)
def test_chat_requires_question_scope_and_valid_retrieval_count(payload):
    with pytest.raises(ValidationError):
        ChatAskRequest.model_validate(payload)


def test_chat_default_and_documented_request_example():
    request = ChatAskRequest(question="付款条件是什么？", document_ids=["doc_001"])
    assert request.top_k == 5


@pytest.mark.parametrize(
    "payload",
    [
        {"decision": "accepted", "expected_version": 2},
        {"decision": "approved", "expected_version": 0},
        {"decision": "approved", "expected_version": True},
        {"decision": "approved"},
    ],
)
def test_review_decision_requires_supported_action_and_concurrency_version(payload):
    with pytest.raises(ValidationError):
        ReviewDecisionRequest.model_validate(payload)


def test_review_accepts_rejection_without_note():
    decision = ReviewDecisionRequest(decision="rejected", expected_version=2)
    assert decision.note is None


def test_compare_requires_two_distinct_versions():
    with pytest.raises(ValidationError):
        CompareVersionsRequest(base_version_id="ver_001", target_version_id="ver_001")
    request = CompareVersionsRequest(base_version_id="ver_001", target_version_id="ver_002")
    assert request.target_version_id == "ver_002"


@pytest.mark.parametrize(
    "payload",
    [
        {"query": "金额超过100万元"},
        {"document_ids": [], "query": "金额超过100万元"},
        {"document_ids": [f"doc_{index}" for index in range(21)], "query": "金额超过100万元"},
        {"document_ids": ["doc_001", "doc_001"], "query": "金额超过100万元"},
        {"document_ids": [" "], "query": "金额超过100万元"},
        {"document_ids": ["doc_001"], "query": " \n"},
    ],
)
def test_screening_requires_a_bounded_scope_and_nonblank_query(payload):
    with pytest.raises(ValidationError):
        ScreeningRequest.model_validate(payload)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), True, "100"])
def test_screening_threshold_requires_finite_json_number(value):
    with pytest.raises(ValidationError):
        NumericCondition(field="amount", operator="gt", value=value)


def test_screening_conditions_cannot_silently_target_a_different_field():
    with pytest.raises(ValidationError):
        ScreeningConditions(amount={"field": "duration_days", "operator": "gt", "value": 365})


@pytest.mark.parametrize(
    "payload",
    [
        {"document_ids": ["doc_001"], "query": "金额超过100万元", "deep_anaylsis": True},
        {"document_ids": ["doc_001"], "query": "金额超过100万元", "conditions": {"ammount": 1}},
        {
            "document_ids": ["doc_001"],
            "query": "金额超过100万元",
            "conditions": {"amount": {"field": "amount", "operator": "gt", "value": 1, "unit": "元"}},
        },
    ],
)
def test_unknown_conditions_and_request_fields_are_not_ignored(payload):
    with pytest.raises(ValidationError):
        ScreeningRequest.model_validate(payload)


def test_screening_documented_request_still_validates():
    request = ScreeningRequest(
        document_ids=["doc_001", "doc_002"],
        query="金额超过100万元、期限超过一年且风险较高",
        conditions={
            "amount": {"field": "amount", "operator": "gt", "value": 1_000_000},
            "duration_days": {"field": "duration_days", "operator": "gt", "value": 365},
            "risk": "high",
        },
        deep_analysis=True,
    )
    assert request.conditions.amount.value == 1_000_000


def _citation_payload():
    return {
        "citation_id": "cite_001",
        "document_id": "doc_001",
        "version_id": "ver_001",
        "page": None,
        "section": None,
        "quote": "验收合格后30日内支付",
        "char_start": None,
        "char_end": None,
    }


def test_citation_nullable_fields_must_remain_in_response():
    citation = Citation.model_validate(_citation_payload())
    assert citation.model_dump() == _citation_payload()
    payload = _citation_payload()
    del payload["page"]
    with pytest.raises(ValidationError):
        Citation.model_validate(payload)


@pytest.mark.parametrize(
    "location",
    [
        {"page": 0},
        {"char_start": 0},
        {"char_end": 20},
        {"char_start": -1, "char_end": 20},
        {"char_start": 20, "char_end": 20},
        {"char_start": 20, "char_end": 10},
    ],
)
def test_citation_rejects_incomplete_or_invalid_locations(location):
    with pytest.raises(ValidationError):
        Citation.model_validate(_citation_payload() | location)


def test_citation_accepts_zero_based_character_range():
    citation = Citation.model_validate(_citation_payload() | {"char_start": 0, "char_end": 12})
    assert citation.char_start == 0
