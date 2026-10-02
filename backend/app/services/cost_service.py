"""仅汇总地图查询到的参考费用；模型填写的金额永不作为价格事实。"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation


def _amount(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def queried_reference_cost(place: dict | None, category: str) -> dict:
    """从已核实POI元数据提取明确标注的查询消费，拒绝模型生成的费用字段。"""
    if not place:
        return {"amount": None, "currency": "CNY", "unit": None, "source": None, "status": "missing"}
    reference = place.get("reference_cost")
    amount = _amount(reference)
    basis = place.get("cost_basis")
    if amount is None:
        return {"amount": None, "currency": "CNY", "unit": None, "source": None, "status": "missing"}
    # 住宿只有服务端明确标为房/晚的查询值才能按晚数计入。
    unit = "room_night" if category == "lodging" and basis == "room_night" else basis
    if category == "lodging" and unit != "room_night":
        return {"amount": None, "currency": "CNY", "unit": None, "source": None, "status": "missing"}
    return {"amount": float(amount), "currency": "CNY", "unit": unit or "reference", "source": "amap", "status": "available"}


def summarize_day_costs(activities: list[dict], *, lodging_base: dict | None = None,
                        lodging_nights: int = 0, include_lodging_unknown: bool = False) -> dict:
    buckets = {name: {"known_total": Decimal("0"), "unknown_count": 0} for name in ("lodging", "meals", "tickets")}
    for activity in activities:
        kind = activity.get("type")
        category = "tickets" if kind == "sightseeing" else "meals" if kind == "meal" else None
        if category is None:
            continue
        item = activity.get("reference_cost") or {}
        amount = _amount(item.get("amount")) if item.get("status") == "available" and item.get("source") == "amap" else None
        if amount is None:
            buckets[category]["unknown_count"] += 1
        else:
            buckets[category]["known_total"] += amount
    if lodging_nights > 0:
        item = (lodging_base or {}).get("reference_cost") or {}
        amount = _amount(item.get("amount")) if item.get("status") == "available" and item.get("source") == "amap" and item.get("unit") == "room_night" else None
        if amount is None:
            if include_lodging_unknown:
                buckets["lodging"]["unknown_count"] = 1
        else:
            buckets["lodging"]["known_total"] += amount * lodging_nights
    normalized = {}
    for name, value in buckets.items():
        normalized[name] = {"known_total": float(value["known_total"].quantize(Decimal("0.01"))),
                            "unknown_count": value["unknown_count"], "complete": value["unknown_count"] == 0}
    total = sum((value["known_total"] for value in buckets.values()), Decimal("0"))
    unknown = sum(value["unknown_count"] for value in buckets.values())
    normalized["known_total"] = float(total.quantize(Decimal("0.01")))
    normalized["unknown_count"] = unknown
    normalized["complete"] = unknown == 0
    normalized["currency"] = "CNY"
    normalized["basis"] = "查询到的住宿、餐饮和门票参考价格；交通费用不计入"
    return normalized


def summarize_trip(days: list[dict], budget_per_person: float | None = None) -> dict:
    categories = {name: {"known_total": Decimal("0"), "unknown_count": 0} for name in ("lodging", "meals", "tickets")}
    for day in days:
        summary = day.get("cost_summary") or {}
        for name in categories:
            source = summary.get(name) or {}
            categories[name]["known_total"] += _amount(source.get("known_total")) or Decimal("0")
            categories[name]["unknown_count"] += int(source.get("unknown_count") or 0)
    result = {}
    for name, value in categories.items():
        result[name] = {"known_total": float(value["known_total"].quantize(Decimal("0.01"))),
                        "unknown_count": value["unknown_count"], "complete": value["unknown_count"] == 0}
    known = sum((v["known_total"] for v in categories.values()), Decimal("0"))
    unknown = sum(v["unknown_count"] for v in categories.values())
    result.update(known_total=float(known.quantize(Decimal("0.01"))), unknown_count=unknown,
                  complete=unknown == 0, currency="CNY", budget_per_person=budget_per_person,
                  over_budget_known=budget_per_person is not None and known > Decimal(str(budget_per_person)),
                  budget_status="known_amount_over_budget" if budget_per_person is not None and known > Decimal(str(budget_per_person))
                  else "incomplete" if unknown else "within_budget" if budget_per_person is not None else "not_set")
    return result
