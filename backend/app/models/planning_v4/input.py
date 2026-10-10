"""v4 原始需求、需求整理和后端规划约束。"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field, PositiveInt, model_validator

from .base import ContractModel


class TripBudget(ContractModel):
    amount: Decimal | None = Field(default=None, ge=0)
    currency: Literal["CNY"] = "CNY"
    scope: Literal["whole_trip"] = "whole_trip"
    party_basis: Literal["unspecified", "specified"] = "unspecified"
    party_size: PositiveInt | None = None

    @model_validator(mode="after")
    def validate_party_basis(self):
        if self.party_basis == "unspecified" and self.party_size is not None:
            raise ValueError("人数未指定时 party_size 必须为空")
        if self.party_basis == "specified" and self.party_size is None:
            raise ValueError("指定人数时必须提供 party_size")
        return self


class TripRequest(ContractModel):
    schema_version: Literal[4] = 4
    request_id: str = Field(min_length=1, max_length=128)
    city: str = Field(min_length=1, max_length=100)
    arrival_at: AwareDatetime
    departure_at: AwareDatetime
    timezone: str = "Asia/Shanghai"
    preferences: tuple[str, ...] = ()
    transport_preferences: tuple[Literal["transit", "bicycling", "walking"], ...] = ()
    arrival_place_query: str | None = None
    departure_place_query: str | None = None
    lodging_query: str | None = None
    budget: TripBudget = Field(default_factory=TripBudget)
    remarks: str = ""

    @model_validator(mode="after")
    def validate_time_range(self):
        try:
            zone = ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone 必须是有效的 IANA 时区") from exc
        arrival_local = self.arrival_at.astimezone(zone)
        departure_local = self.departure_at.astimezone(zone)
        if departure_local <= arrival_local:
            raise ValueError("离开时间必须晚于到达时间")
        if not self.city.strip():
            raise ValueError("city 不能为空")
        return self


class RequirementSource(ContractModel):
    field: Literal[
        "city",
        "arrival_at",
        "departure_at",
        "preferences",
        "transport_preferences",
        "arrival_place_query",
        "departure_place_query",
        "lodging_query",
        "budget",
        "remarks",
    ]
    quote: str = Field(min_length=1)


class HardRequirement(ContractModel):
    requirement_id: str = Field(min_length=1)
    kind: Literal["must_visit", "must_avoid", "must_include"]
    text: str = Field(min_length=1)
    source: RequirementSource


class RequirementSummary(ContractModel):
    requirements_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    core_goals: tuple[str, ...] = ()
    hard_requirements: tuple[HardRequirement, ...] = ()
    preferences: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()


class ReferenceWindow(ContractModel):
    period: Literal["breakfast", "morning", "lunch", "afternoon", "dinner", "evening"]
    start_minute: int = Field(ge=0, lt=24 * 60)
    end_minute: int = Field(gt=0, le=24 * 60)

    @model_validator(mode="after")
    def validate_order(self):
        if self.end_minute <= self.start_minute:
            raise ValueError("参考时段结束时间必须晚于开始时间")
        return self


class ArrivalBlockRule(ContractModel):
    block: Literal["midnight", "morning", "afternoon", "evening"]
    start_minute: int = Field(ge=0, lt=24 * 60)
    end_minute: int = Field(gt=0, le=24 * 60)
    first_visit_period: Literal["morning", "afternoon", "evening"]

    @model_validator(mode="after")
    def validate_order(self):
        if self.end_minute <= self.start_minute:
            raise ValueError("到达时段结束时间必须晚于开始时间")
        return self


class RuleSet(ContractModel):
    rule_version: str = Field(min_length=1)
    windows: tuple[ReferenceWindow, ...]
    meal_default_s: PositiveInt = 3600
    sightseeing_default_s: PositiveInt = 10800
    sightseeing_duration_adjustable: bool = True
    max_repair_rounds_per_day: PositiveInt = 3
    full_day_start_minute: int = Field(default=8 * 60, ge=0, lt=24 * 60)
    return_buffer_default_s: PositiveInt = 3600
    departure_day_minimum_usable_s: PositiveInt = 10800
    arrival_block_rules: tuple["ArrivalBlockRule", ...] = (
        ArrivalBlockRule(block="midnight", start_minute=0, end_minute=6 * 60, first_visit_period="afternoon"),
        ArrivalBlockRule(block="morning", start_minute=6 * 60, end_minute=12 * 60, first_visit_period="afternoon"),
        ArrivalBlockRule(block="afternoon", start_minute=12 * 60, end_minute=18 * 60, first_visit_period="evening"),
        ArrivalBlockRule(block="evening", start_minute=18 * 60, end_minute=24 * 60, first_visit_period="morning"),
    )
    arrival_meals_use_actual_availability: bool = True
    short_arrival_window_policy: Literal["skip_without_missing"] = "skip_without_missing"
    departure_day_activities_below_minimum: Literal["skip_without_missing"] = "skip_without_missing"
    return_buffer_priority: Literal["explicit_user_value_then_default"] = "explicit_user_value_then_default"

    @model_validator(mode="after")
    def validate_complete_windows(self):
        periods = {window.period for window in self.windows}
        expected = {"breakfast", "morning", "lunch", "afternoon", "dinner", "evening"}
        if periods != expected or len(self.windows) != len(expected):
            raise ValueError("RuleSet 必须完整定义六个参考窗口且每个窗口唯一")
        blocks = sorted(self.arrival_block_rules, key=lambda item: item.start_minute)
        if len(blocks) != 4 or [item.block for item in blocks] != ["midnight", "morning", "afternoon", "evening"]:
            raise ValueError("RuleSet 必须定义四个到达时段")
        if blocks[0].start_minute != 0 or blocks[-1].end_minute != 24 * 60:
            raise ValueError("到达规则必须覆盖完整一天")
        if any(left.end_minute != right.start_minute for left, right in zip(blocks, blocks[1:])):
            raise ValueError("到达时段必须连续且不重叠")
        return self


class DayConstraint(ContractModel):
    day_id: str = Field(min_length=1)
    day_date: date
    available_start: AwareDatetime | None = None
    available_end: AwareDatetime | None = None
    slots: tuple["SlotConstraint", ...]

    @model_validator(mode="after")
    def validate_bounds(self):
        if self.available_start and self.available_end and self.available_end <= self.available_start:
            raise ValueError("日期可用时段结束时间必须晚于开始时间")
        periods = [slot.period for slot in self.slots]
        expected = {"breakfast", "morning", "lunch", "afternoon", "dinner", "evening"}
        if len(periods) != len(expected) or set(periods) != expected:
            raise ValueError("每个日期必须为六个参考时段各定义一个 SlotConstraint")
        return self


class SlotConstraint(ContractModel):
    slot_id: str = Field(min_length=1)
    day_id: str = Field(min_length=1)
    period: Literal["breakfast", "morning", "lunch", "afternoon", "dinner", "evening"]
    eligibility: Literal["required", "skipped", "conditional"]
    condition_reason: str | None = None

    @model_validator(mode="after")
    def validate_condition(self):
        if self.eligibility == "conditional" and not self.condition_reason:
            raise ValueError("conditional 时段必须说明待核实条件")
        if self.eligibility != "conditional" and self.condition_reason:
            raise ValueError("只有 conditional 时段可以设置 condition_reason")
        return self


class PlanningInput(ContractModel):
    schema_version: Literal[4] = 4
    request: TripRequest
    requirements: RequirementSummary
    rules: RuleSet
    days: tuple[DayConstraint, ...]

    @model_validator(mode="after")
    def validate_local_references(self):
        if self.requirements.request_id != self.request.request_id:
            raise ValueError("需求整理必须引用原始 request_id")
        day_ids = [day.day_id for day in self.days]
        if len(day_ids) != len(set(day_ids)):
            raise ValueError("day_id 必须唯一")
        day_dates = [day.day_date for day in self.days]
        if len(day_dates) != len(set(day_dates)):
            raise ValueError("day_date 必须唯一")
        if day_dates != sorted(day_dates):
            raise ValueError("PlanningInput 日期必须按时间顺序排列")
        arrival_date = self.request.arrival_at.astimezone(ZoneInfo(self.request.timezone)).date()
        departure_date = self.request.departure_at.astimezone(ZoneInfo(self.request.timezone)).date()
        expected_dates = tuple(
            arrival_date + timedelta(days=offset)
            for offset in range((departure_date - arrival_date).days + 1)
        )
        if tuple(day_dates) != expected_dates:
            raise ValueError("PlanningInput 日期必须完整覆盖到达日至离开日，且日期连续")
        slot_ids = [slot.slot_id for day in self.days for slot in day.slots]
        if len(slot_ids) != len(set(slot_ids)):
            raise ValueError("slot_id 必须唯一")
        if any(slot.day_id != day.day_id for day in self.days for slot in day.slots):
            raise ValueError("slot 必须引用所属日期的 day_id")
        for requirement in self.requirements.hard_requirements:
            source_value = getattr(self.request, requirement.source.field)
            if isinstance(source_value, tuple):
                valid = requirement.source.quote in source_value
            elif isinstance(source_value, TripBudget):
                valid = requirement.source.quote in source_value.model_dump_json()
            elif source_value is None:
                valid = False
            else:
                valid = requirement.source.quote in str(source_value)
            if not valid:
                raise ValueError(f"硬性需求来源与原始输入不匹配: {requirement.requirement_id}")
        return self


DayConstraint.model_rebuild()
