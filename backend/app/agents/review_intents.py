"""Versioned evidence queries for common contract review intents."""

from __future__ import annotations

from dataclasses import dataclass


INTENT_VERSION = "contract_review_intents_v1"


@dataclass(frozen=True, slots=True)
class ReviewIntent:
    intent_id: str
    query: str


REVIEW_INTENTS = (
    ReviewIntent("amount", "合同金额 总价 金额"),
    ReviewIntent("contract_term", "合同期限 有效期 服务期"),
    ReviewIntent("payment_terms", "付款期限 支付期限 账期 付款"),
    ReviewIntent("acceptance", "验收标准 交付确认"),
    ReviewIntent("liability_cap", "责任上限 赔偿责任 无限责任"),
    ReviewIntent("renewal", "自动续约 续期 提前通知"),
    ReviewIntent("confidentiality", "保密义务 机密"),
    ReviewIntent("dispute_resolution", "争议解决 管辖法院 仲裁"),
    ReviewIntent("termination", "解除 终止条件"),
    ReviewIntent("explicit_risk", "高风险 重大违约 无限责任"),
)
