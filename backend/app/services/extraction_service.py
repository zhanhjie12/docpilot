"""从合同文本中提取可解释的结构化事实。

该模块只做确定性的正则提取，方便筛选、审阅和测试复用；模型推理留在
Agent 层，避免把不可预测的生成结果直接写入业务字段。
"""

from __future__ import annotations

import re
from typing import Any


_NUMBER = r"([0-9][0-9,]*(?:\.[0-9]+)?)"
_DURATION_UNIT = r"(天|日|个月|月|年|days?|months?|years?)"


def _number(value: str) -> float:
    """去掉千分位后转换为数值，统一金额字段的类型。"""

    return float(value.replace(",", ""))


def _days(value: str, unit: str) -> int:
    """把月、年等单位换算为天数，不尝试推断具体日历日期。"""

    days = int(_number(value))
    normalized_unit = unit.lower()
    if normalized_unit in {"个月", "月", "months", "month"}:
        return days * 30
    if normalized_unit in {"年", "years", "year"}:
        return days * 365
    return days


def extract_facts(text: str) -> dict[str, Any]:
    """提取金额、合同期限、付款期限和风险等级。

    金额和期限的字段分别匹配；付款期限不能覆盖合同期限，避免“付款期限
    30 天”被错误识别为“合同期限 30 天”。未识别到的必填事实会进入
    ``missing_fields``，付款期限属于可选补充信息，不会触发缺失提示。
    """

    # 先统一中文标点，让同一条规则同时覆盖中文合同和英文导出文本。
    normalized = text.replace("，", ",").replace("：", ":")
    amount = None
    amount_match = re.search(
        rf"(?:合同金额|总价|金额|amount|price)[^0-9]{{0,25}}{_NUMBER}\s*(万元|万|元|人民币|rmb)?",
        normalized,
        flags=re.IGNORECASE,
    )
    if amount_match:
        # 金额先转成基础单位；“万元”等中文单位在这里集中换算。
        amount = _number(amount_match.group(1))
        if amount_match.group(2) in {"万", "万元"}:
            amount *= 10_000
    # 合同期限和付款期限是两个业务事实。term 必须是完整单词，避免
    # ``Payment Terms: 30 days`` 被识别成合同存续期。
    duration_days = None
    duration_match = re.search(
        rf"(?:合同期限|协议期限|有效期限|有效期|服务期限|服务期|履行期限|履行期|"
        rf"(?:contract|agreement|service)\s+(?:duration|term)|\bduration\b|"
        rf"(?<!payment\s)\bterm\b)[^0-9]{{0,25}}{_NUMBER}\s*{_DURATION_UNIT}",
        normalized,
        flags=re.IGNORECASE,
    )
    if duration_match:
        # 使用统一的换算函数，返回整数天数供筛选条件直接比较。
        duration_days = _days(duration_match.group(1), duration_match.group(2))

    payment_days = None
    payment_match = re.search(
        rf"(?:付款期限|支付期限|付款周期|账期|payment\s+terms?|payment\s+period)"
        rf"[^0-9]{{0,25}}{_NUMBER}\s*{_DURATION_UNIT}",
        normalized,
        flags=re.IGNORECASE,
    )
    if payment_match is None:
        # 支持“验收后 30 日内支付”等叙述句，但限制数字与付款动词的距离，
        # 避免借用同一段落中的其他期限。
        payment_match = re.search(
            rf"{_NUMBER}\s*{_DURATION_UNIT}[^0-9。；;\n]{{0,12}}(?:内)?"
            rf"(?:付款|支付|付清|pay(?:ment)?\b)",
            normalized,
            flags=re.IGNORECASE,
        )
    if payment_match:
        payment_days = _days(payment_match.group(1), payment_match.group(2))
    risk = None
    # 风险等级只接受明确关键词；没有证据时保持 None，由上层提示人工确认。
    if re.search(r"高风险|重大违约|无限责任|high\s*risk|uncapped\s+liability", normalized, re.I):
        risk = "high"
    elif re.search(r"中风险|一般违约|medium\s*risk", normalized, re.I):
        risk = "medium"
    elif re.search(r"低风险|low\s*risk", normalized, re.I):
        risk = "low"
    # payment_days 是可选补充字段，因此不加入必填缺失列表。
    missing = [name for name, value in (("amount", amount), ("duration_days", duration_days), ("risk", risk)) if value is None]
    return {
        "amount": amount,
        "duration_days": duration_days,
        "payment_days": payment_days,
        "risk": risk,
        "missing_fields": missing,
    }
