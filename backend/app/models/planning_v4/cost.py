"""已知人均消费参考的类型化汇总契约。"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from .base import ContractModel
from .facts import FactSource, ReferenceCost


CostEntryKind = Literal["sightseeing", "meal", "lodging"]
CostEntryStatus = Literal["known", "unknown", "conflict"]
CostSummaryStatus = Literal["no_activities", "all_unknown", "known_partial", "all_known"]


class ReferenceCostFactEvidence(ContractModel):
    """单条地点事实及其来源证据，不改写查询原始消费文本。"""

    place_fact_id: str = Field(min_length=1)
    poi_id: str = Field(min_length=1)
    place_name: str = Field(min_length=1)
    cost: ReferenceCost
    sources: tuple[FactSource, ...] = ()
    cost_source: FactSource | None = None


class ReferenceCostEntry(ContractModel):
    """一个景点去重组、一个餐次或一个日期的住宿消费参考。"""

    kind: CostEntryKind
    day_id: str = Field(min_length=1)
    day_date: date
    activity_ids: tuple[str, ...] = ()
    slot_ids: tuple[str, ...] = ()
    poi_id: str = Field(min_length=1)
    place_name: str = Field(min_length=1)
    amount: Decimal | None = Field(default=None, ge=0)
    status: CostEntryStatus
    unknown_reason: Literal["cost_not_returned", "conflicting_values"] | None = None
    evidence: tuple[ReferenceCostFactEvidence, ...] = Field(min_length=1)
    unit: Literal["per_person_reference", "per_person_per_night_reference"] = "per_person_reference"

    @model_validator(mode="after")
    def validate_status(self):
        if self.status == "known":
            if self.amount is None or self.unknown_reason is not None:
                raise ValueError("known 消费明细必须有金额且不能带未知原因")
        elif self.status == "unknown":
            if self.amount is not None or self.unknown_reason != "cost_not_returned":
                raise ValueError("unknown 消费明细必须保留未知金额及原因")
        elif self.amount is not None or self.unknown_reason != "conflicting_values":
            raise ValueError("conflict 消费明细不能选择金额，且必须说明价格冲突")
        if any(evidence.poi_id != self.poi_id for evidence in self.evidence):
            raise ValueError("消费证据必须属于明细的同一 POI")
        return self


class DayReferenceCostSummary(ContractModel):
    day_id: str = Field(min_length=1)
    day_date: date
    status: CostSummaryStatus
    known_sightseeing_meal_per_person_subtotal: Decimal = Field(ge=0)
    entries: tuple[ReferenceCostEntry, ...] = ()
    lodging: ReferenceCostEntry | None = None
    unit: Literal["per_person_reference"] = "per_person_reference"

    @model_validator(mode="after")
    def validate_subtotal_and_scope(self):
        expected = sum(
            (entry.amount for entry in self.entries if entry.status == "known" and entry.amount is not None),
            Decimal("0"),
        )
        if expected != self.known_sightseeing_meal_per_person_subtotal:
            raise ValueError("日级已知景点餐饮小计必须等于已知明细之和")
        if any(entry.day_id != self.day_id or entry.day_date != self.day_date for entry in self.entries):
            raise ValueError("日级消费明细必须引用所属日期")
        if any(entry.kind == "lodging" for entry in self.entries):
            raise ValueError("住宿不能进入景点餐饮小计明细")
        if self.lodging is not None and (
            self.lodging.kind != "lodging"
            or self.lodging.day_id != self.day_id
            or self.lodging.day_date != self.day_date
        ):
            raise ValueError("住宿明细必须单独引用所属日期")
        if _summarize_status(self.entries) != self.status:
            raise ValueError("日级消费状态必须与明细覆盖情况一致")
        return self


class ReferenceCostSummary(ContractModel):
    """仅汇总已知的人均景点与餐饮参考；不代表全程旅费。"""

    request_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    based_on_revision: int = Field(ge=1)
    status: CostSummaryStatus
    known_sightseeing_meal_per_person_subtotal: Decimal = Field(ge=0)
    days: tuple[DayReferenceCostSummary, ...] = ()
    unit: Literal["per_person_reference"] = "per_person_reference"
    scope: Literal["sightseeing_and_meals_only"] = "sightseeing_and_meals_only"

    @model_validator(mode="after")
    def validate_trip_summary(self):
        expected = sum(
            (day.known_sightseeing_meal_per_person_subtotal for day in self.days),
            Decimal("0"),
        )
        if expected != self.known_sightseeing_meal_per_person_subtotal:
            raise ValueError("全程已知景点餐饮小计必须等于日级小计之和")
        if _summarize_status(tuple(entry for day in self.days for entry in day.entries)) != self.status:
            raise ValueError("全程消费状态必须与日级明细覆盖情况一致")
        return self


def _summarize_status(entries: tuple[ReferenceCostEntry, ...]) -> CostSummaryStatus:
    if not entries:
        return "no_activities"
    known_count = sum(entry.status == "known" for entry in entries)
    if known_count == len(entries):
        return "all_known"
    if known_count:
        return "known_partial"
    return "all_unknown"
