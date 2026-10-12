"""纯规则转换：仅按到离时间、返程缓冲和固定窗口构造 PlanningInput。"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from pydantic import Field

from app.models.planning_v4.base import ContractModel
from app.models.planning_v4.input import (
    ArrivalBlockRule,
    DayConstraint,
    PlanningBoundaryFacts,
    PlanningInput,
    RequirementSummary,
    RuleSet,
    SlotConstraint,
    TripRequest,
)


SLOT_ORDER = ("breakfast", "morning", "lunch", "afternoon", "dinner", "evening")
SIGHTSEEING_PERIODS = frozenset(("morning", "afternoon", "evening"))
UTC = timezone.utc


class PlanningInputBuildRequest(ContractModel):
    """规划规则转换所需输入；不接收就绪时刻或候选地点的交通耗时。"""

    request: TripRequest
    requirements: RequirementSummary
    rules: RuleSet
    explicit_return_buffer_s: int | None = Field(default=None, ge=0)


def build_planning_input(source: PlanningInputBuildRequest) -> PlanningInput:
    """确定六时段的 required/skipped；资格阈值不生成活动停留时长。"""
    request = source.request
    zone = ZoneInfo(request.timezone)
    arrival = request.arrival_at.astimezone(zone)
    departure = request.departure_at.astimezone(zone)
    return_buffer_s = (
        source.explicit_return_buffer_s
        if source.explicit_return_buffer_s is not None
        else (
            request.explicit_return_buffer_s
            if request.explicit_return_buffer_s is not None
            else source.rules.return_buffer_default_s
        )
    )
    departure_cutoff = _subtract_elapsed(departure, return_buffer_s, zone)

    arrival_date = arrival.date()
    departure_date = departure.date()
    arrival_block = _arrival_block(source.rules, arrival)
    first_visit_period = arrival_block.first_visit_period
    first_visit_date = arrival_date + timedelta(days=1) if arrival_block.block == "evening" else arrival_date

    windows = {window.period: window for window in source.rules.windows}
    days: list[DayConstraint] = []
    current = arrival_date
    while current <= departure_date:
        day_start = (
            arrival if current == arrival_date
            else _at_minute(current, source.rules.full_day_start_minute, zone)
        )
        upper_end = departure_cutoff if current == departure_date else None
        departure_day_too_short = (
            current == departure_date
            and _seconds_between(day_start, departure_cutoff) < source.rules.departure_day_minimum_usable_s
        )

        day_id = f"day-{current.isoformat()}"
        slot_models: list[SlotConstraint] = []

        for period in SLOT_ORDER:
            window = windows[period]
            slot_start = _at_minute(current, window.start_minute, zone)
            slot_end = _at_minute(current, window.end_minute, zone)
            available_s = _overlap_seconds(slot_start, slot_end, day_start, upper_end)
            minimum_available_s = (
                source.rules.sightseeing_minimum_available_s if period in SIGHTSEEING_PERIODS
                else source.rules.meal_minimum_available_s
            )

            if departure_day_too_short:
                eligibility = "skipped"
            elif current == arrival_date and period in SIGHTSEEING_PERIODS and not _arrival_allows_period(
                period=period,
                current=current,
                first_visit_date=first_visit_date,
                first_visit_period=first_visit_period,
            ):
                eligibility = "skipped"
            elif available_s < minimum_available_s:
                eligibility = "skipped"
            else:
                eligibility = "required"

            slot_models.append(SlotConstraint(
                slot_id=f"slot-{current.isoformat()}-{period}",
                day_id=day_id,
                period=period,
                eligibility=eligibility,
            ))

        days.append(DayConstraint(
            day_id=day_id,
            day_date=current,
            available_start=day_start,
            # 缓冲覆盖整个离开日时没有正区间；截止点仍保留在 boundary_facts。
            available_end=(
                upper_end if upper_end is not None and _seconds_between(day_start, upper_end) > 0
                else None
            ),
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
                "explicit_user"
                if source.explicit_return_buffer_s is not None
                or request.explicit_return_buffer_s is not None
                else "default"
            ),
            arrival_start_optimistic_at=arrival,
            departure_activity_end_upper_bound_at=departure_cutoff,
        ),
    )


def _arrival_block(rules: RuleSet, arrival: datetime) -> ArrivalBlockRule:
    minute = arrival.hour * 60 + arrival.minute
    for rule in rules.arrival_block_rules:
        if rule.start_minute <= minute < rule.end_minute:
            return rule
    raise ValueError("RuleSet 到达时段未覆盖本地到达时刻")


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
