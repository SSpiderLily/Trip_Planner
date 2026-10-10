"""纯规则转换：从请求、规则和边界事实构造 PlanningInput。"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Mapping
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field, model_validator

from app.models.planning_v4.base import ContractModel
from app.models.planning_v4.input import (
    ArrivalBlockRule,
    DayConstraint,
    PlanningBoundaryFacts,
    PlanningInput,
    ReferenceWindow,
    RequirementSummary,
    RuleSet,
    SlotConstraint,
    TripRequest,
)


SLOT_ORDER = ("breakfast", "morning", "lunch", "afternoon", "dinner", "evening")
SIGHTSEEING_PERIODS = frozenset(("morning", "afternoon", "evening"))
UTC = timezone.utc


class PlanningInputBuildRequest(ContractModel):
    """规划规则转换所需输入；不接收单个候选地点的返程耗时。"""

    request: TripRequest
    requirements: RequirementSummary
    rules: RuleSet
    explicit_return_buffer_s: int | None = Field(default=None, ge=0)
    day_ready_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_boundaries(self):
        zone = ZoneInfo(self.request.timezone)
        arrival = self.request.arrival_at.astimezone(zone)
        departure = self.request.departure_at.astimezone(zone)
        if self.day_ready_at is not None:
            ready = self.day_ready_at.astimezone(zone)
            if ready.astimezone(UTC) < arrival.astimezone(UTC):
                raise ValueError("day_ready_at 不能早于到达时间")
            if ready.astimezone(UTC) > departure.astimezone(UTC):
                raise ValueError("day_ready_at 不能晚于离开时间")
        return self


def build_planning_input(source: PlanningInputBuildRequest) -> PlanningInput:
    """构造每日六时段约束；required表示应覆盖，不表示时间或交通已验证可行。"""
    request = source.request
    zone = ZoneInfo(request.timezone)
    arrival = request.arrival_at.astimezone(zone)
    departure = request.departure_at.astimezone(zone)
    ready = source.day_ready_at.astimezone(zone) if source.day_ready_at else None
    return_buffer_s = (
        source.explicit_return_buffer_s
        if source.explicit_return_buffer_s is not None
        else source.rules.return_buffer_default_s
    )
    latest_return_arrival = _subtract_elapsed(departure, return_buffer_s, zone)

    arrival_date = arrival.date()
    departure_date = departure.date()
    arrival_block = _arrival_block(source.rules, arrival)
    first_visit_period = arrival_block.first_visit_period
    first_visit_date = arrival_date + timedelta(days=1) if arrival_block.block == "evening" else arrival_date

    windows = {window.period: window for window in source.rules.windows}
    days: list[DayConstraint] = []
    current = arrival_date
    while current <= departure_date:
        day_start = _day_start(
            current=current,
            arrival=arrival,
            arrival_date=arrival_date,
            ready=ready,
            zone=zone,
            full_day_start_minute=source.rules.full_day_start_minute,
        )
        upper_end = latest_return_arrival if current == departure_date else None
        definitely_too_short = (
            current == departure_date
            and _seconds_between(day_start, latest_return_arrival) < source.rules.departure_day_minimum_usable_s
        )

        day_id = f"day-{current.isoformat()}"
        slot_models: list[SlotConstraint] = []
        arrival_readiness_pending = ready is None and current == first_visit_date
        first_arrival_slot_pending = arrival_readiness_pending

        for period in SLOT_ORDER:
            window = windows[period]
            slot_start = _at_minute(current, window.start_minute, zone)
            slot_end = _at_minute(current, window.end_minute, zone)
            available_s = _overlap_seconds(slot_start, slot_end, day_start, upper_end)
            reason: str | None = None

            if definitely_too_short:
                eligibility = "skipped"
            elif current == arrival_date and period in SIGHTSEEING_PERIODS and not _arrival_allows_period(
                period=period,
                current=current,
                first_visit_date=first_visit_date,
                first_visit_period=first_visit_period,
            ):
                eligibility = "skipped"
            elif available_s <= 0:
                eligibility = "skipped"
            elif period not in SIGHTSEEING_PERIODS and available_s < source.rules.meal_default_s:
                eligibility = "skipped"
            else:
                eligibility = "required"

                if arrival_readiness_pending and first_arrival_slot_pending:
                    eligibility = "conditional"
                    reason = "到达后实际可用时刻未核实；当前仅按到达时间上界保留该时段。"
                    first_arrival_slot_pending = False

                if (
                    eligibility == "required"
                    and current == arrival_date
                    and period not in SIGHTSEEING_PERIODS
                    and _is_first_arrival_meal(
                        period=period,
                        current=current,
                        arrival=arrival,
                        windows=windows,
                        day_start=day_start,
                        upper_end=upper_end,
                        meal_default_s=source.rules.meal_default_s,
                    )
                ):
                    eligibility = "conditional"
                    reason = "到达后的首个可用餐次还需核实前往餐馆的实际时间。"

                if (
                    current == departure_date
                    and upper_end is not None
                    and _window_crosses_end(slot_start, slot_end, upper_end)
                ):
                    eligibility = "conditional"
                    reason = "返程地点或真实交通耗时未核实；保留该时段，后续按具体路线检查。"

            slot_models.append(SlotConstraint(
                slot_id=f"slot-{current.isoformat()}-{period}",
                day_id=day_id,
                period=period,
                eligibility=eligibility,
                condition_reason=reason,
            ))

        day_available_start = _day_constraint_start(
            current=current,
            arrival_date=arrival_date,
            ready=ready,
            zone=zone,
            full_day_start_minute=source.rules.full_day_start_minute,
        )
        days.append(DayConstraint(
            day_id=day_id,
            day_date=current,
            available_start=day_available_start,
            # 实际可用终点依赖具体末活动到返程地点的路线，F02不把它当作零。
            available_end=None,
            slots=tuple(slot_models),
        ))
        current += timedelta(days=1)

    return PlanningInput(
        request=request,
        requirements=source.requirements,
        rules=source.rules,
        days=tuple(days),
        boundary_facts=PlanningBoundaryFacts(
            effective_return_buffer_s=return_buffer_s,
            return_buffer_source=(
                "explicit_user" if source.explicit_return_buffer_s is not None else "default"
            ),
            day_ready_at=ready,
            arrival_start_optimistic_at=arrival,
            departure_activity_end_upper_bound_at=latest_return_arrival,
        ),
    )


def _arrival_block(rules: RuleSet, arrival: datetime) -> ArrivalBlockRule:
    minute = arrival.hour * 60 + arrival.minute
    for rule in rules.arrival_block_rules:
        if rule.start_minute <= minute < rule.end_minute:
            return rule
    raise ValueError("RuleSet 到达时段未覆盖本地到达时刻")


def _day_start(
    *,
    current: date,
    arrival: datetime,
    arrival_date: date,
    ready: datetime | None,
    zone: ZoneInfo,
    full_day_start_minute: int,
) -> datetime:
    scheduled_start = _at_minute(current, full_day_start_minute, zone)
    if ready is not None:
        if current < ready.date():
            return _at_minute(current + timedelta(days=1), 0, zone)
        if current == ready.date():
            return ready if current == arrival_date else _later_instant(ready, scheduled_start)
    if current == arrival_date:
        return arrival
    return scheduled_start


def _day_constraint_start(
    *,
    current: date,
    arrival_date: date,
    ready: datetime | None,
    zone: ZoneInfo,
    full_day_start_minute: int,
) -> datetime | None:
    if ready is not None and current < ready.date():
        return None
    if current == arrival_date and ready is None:
        return None
    if ready is not None and current == ready.date():
        if current == arrival_date:
            return ready
        return _later_instant(ready, _at_minute(current, full_day_start_minute, zone))
    return _at_minute(current, full_day_start_minute, zone)


def _arrival_allows_period(
    *,
    period: str,
    current: date,
    first_visit_date: date,
    first_visit_period: str,
) -> bool:
    if current < first_visit_date:
        return False
    if current > first_visit_date:
        return True
    order = {name: index for index, name in enumerate(SLOT_ORDER)}
    return order[period] >= order[first_visit_period]


def _is_first_arrival_meal(
    *,
    period: str,
    current: date,
    arrival: datetime,
    windows: Mapping[str, ReferenceWindow],
    day_start: datetime,
    upper_end: datetime | None,
    meal_default_s: int,
) -> bool:
    arrival_meals = []
    for candidate in ("breakfast", "lunch", "dinner"):
        window = windows[candidate]
        start = _at_minute(current, window.start_minute, arrival.tzinfo)
        end = _at_minute(current, window.end_minute, arrival.tzinfo)
        overlap = _overlap_seconds(start, end, day_start, upper_end)
        if overlap >= meal_default_s:
            arrival_meals.append(candidate)
    return bool(arrival_meals and period == arrival_meals[0])


def _at_minute(day: date, minute: int, zone: ZoneInfo) -> datetime:
    if minute == 24 * 60:
        return datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return datetime.combine(day, time(minute // 60, minute % 60), tzinfo=zone)


def _subtract_elapsed(value: datetime, seconds: int, zone: ZoneInfo) -> datetime:
    return (value.astimezone(UTC) - timedelta(seconds=seconds)).astimezone(zone)


def _seconds_between(start: datetime, end: datetime) -> float:
    return (end.astimezone(UTC) - start.astimezone(UTC)).total_seconds()


def _overlap_seconds(
    slot_start: datetime,
    slot_end: datetime,
    available_start: datetime,
    available_end: datetime | None,
) -> float:
    start_ts = max(slot_start.timestamp(), available_start.timestamp())
    end_ts = slot_end.timestamp()
    if available_end is not None:
        end_ts = min(end_ts, available_end.timestamp())
    return max(0.0, end_ts - start_ts)


def _window_crosses_end(slot_start: datetime, slot_end: datetime, available_end: datetime) -> bool:
    end_ts = available_end.timestamp()
    return slot_start.timestamp() < end_ts <= slot_end.timestamp()


def _later_instant(left: datetime, right: datetime) -> datetime:
    return left if left.timestamp() >= right.timestamp() else right
