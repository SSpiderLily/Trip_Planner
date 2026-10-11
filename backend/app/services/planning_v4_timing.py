"""根据既有草稿和路线事实计算活动时间；本模块不查询外部服务。"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from math import ceil
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field

from app.models.planning_v4 import DaySummary, FactStore, PlanDraft, PlanningInput, RouteLeg
from app.models.planning_v4.base import ContractModel
from app.models.planning_v4.facts import PlaceFact, RouteFact
from app.models.planning_v4.plan import DraftActivity, LegEndpoint


class ActivityTiming(ContractModel):
    """单个活动的计算时间；未知交通不会伪装成零耗时。"""

    day_id: str
    activity_id: str
    incoming_leg_id: str | None = None
    depart_at: AwareDatetime | None = None
    arrive_at: AwareDatetime | None = None
    start_at: AwareDatetime | None = None
    end_at: AwareDatetime | None = None
    earliest_possible_start_at: AwareDatetime | None = None
    earliest_possible_end_at: AwareDatetime | None = None
    travel_s: float | None = Field(default=None, ge=0)
    wait_s: int | None = Field(default=None, ge=0)
    reference_shift_s: int | None = Field(default=None, ge=0)
    duration_s: int = Field(ge=0)
    duration_source: Literal["model", "default", "user"]
    status: Literal["estimated", "unverified"]
    unknown_reason: str | None = None
    note: str | None = None


class UnknownTransition(ContractModel):
    """尚未算出耗时的相邻地点转移。"""

    from_reference_id: str
    to_reference_id: str
    leg_id: str | None = None
    reason: str


class ReturnTiming(ContractModel):
    """离开日返程时间边界；路线耗时与用户缓冲分开记录。"""

    status: Literal["not_applicable", "verified", "conflict", "unverified"]
    departure_poi_id: str | None = None
    departure_at: AwareDatetime | None = None
    deadline_at: AwareDatetime | None = None
    latest_activity_end_at: AwareDatetime | None = None
    estimated_arrival_at: AwareDatetime | None = None
    earliest_possible_arrival_at: AwareDatetime | None = None
    route_leg_id: str | None = None
    travel_s: float | None = Field(default=None, ge=0)
    buffer_s: int = Field(ge=0)
    usable_s: int | None = None
    optimistic_usable_s: int | None = None
    overrun_s: int | None = Field(default=None, ge=0)
    overrun_is_lower_bound: bool = False
    unknown_reasons: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


class DayTimingResult(ContractModel):
    day_id: str
    day_start_at: AwareDatetime | None = None
    activities: tuple[ActivityTiming, ...] = ()
    summary: DaySummary
    scheduling_decision: Literal[
        "scheduled", "conditional", "not_scheduled_insufficient_time"
    ]
    wait_s: int | None = Field(default=None, ge=0)
    elapsed_s: int | None = Field(default=None, ge=0)
    unknown_transitions: tuple[UnknownTransition, ...] = ()
    return_timing: ReturnTiming


class PlanTimingResult(ContractModel):
    plan_id: str
    based_on_revision: int = Field(ge=1)
    days: tuple[DayTimingResult, ...]


class _LegDuration(ContractModel):
    leg_id: str | None = None
    duration_s: float | None = Field(default=None, ge=0)
    reason: str | None = None


def calculate_plan_timing(
    planning_input: PlanningInput,
    draft: PlanDraft,
    facts: FactStore,
    *,
    verified_departure_fact_id: str | None = None,
) -> PlanTimingResult:
    """按草稿顺序计算活动时刻、每日摘要和离开日返程边界。

    ``verified_departure_fact_id`` 必须来自 F04d 对离开地点的核实结果。
    没有该绑定时，本函数不会把 FactStore 中任意 POI 当成返程终点。
    """
    _validate_plan_binding(planning_input, draft)
    _validate_fact_sources(facts)

    zone = ZoneInfo(planning_input.request.timezone)
    request = planning_input.request
    arrival_date = request.arrival_at.astimezone(zone).date()
    departure_at = request.departure_at.astimezone(zone)
    departure_date = departure_at.date()
    boundary = planning_input.boundary_facts
    buffer_s = (
        boundary.effective_return_buffer_s
        if boundary is not None
        else planning_input.rules.return_buffer_default_s
    )
    deadline_at = _subtract_elapsed(departure_at, buffer_s, zone)

    days_by_id = {day.day_id: day for day in draft.days}
    constraints = {day.day_id: day for day in planning_input.days}
    places_by_id = {place.fact_id: place for place in facts.places}
    route_facts_by_id = {route.fact_id: route for route in facts.routes}
    _validate_draft_endpoints(draft)

    results: list[DayTimingResult] = []
    for constraint in planning_input.days:
        day = days_by_id[constraint.day_id]
        day_start = _day_start(
            day_date=day.day_date,
            arrival_date=arrival_date,
            boundary_ready_at=boundary.day_ready_at if boundary else None,
            available_start=constraint.available_start,
            full_day_start_minute=planning_input.rules.full_day_start_minute,
            zone=zone,
        )
        optimistic_day_start = _optimistic_day_start(
            day_date=day.day_date,
            arrival_date=arrival_date,
            planning_input=planning_input,
            available_start=constraint.available_start,
            zone=zone,
        )
        _validate_activity_slots(day.activities, constraint, planning_input)

        edge_results: list[tuple[DraftActivity, _LegDuration, str, str]] = []
        transitions: list[UnknownTransition] = []
        unknown_leg_ids: set[str] = set()
        known_travel_s: list[float] = []
        for activity, from_kind, from_ref in _activity_edges(day.activities, day.day_id):
            if activity.kind == "free_time":
                edge_results.append((activity, _LegDuration(), from_kind or "", from_ref or ""))
                continue
            leg, missing_transition = _find_transition(
                draft=draft,
                day=day,
                activity=activity,
                from_kind=from_kind,
                from_ref=from_ref,
            )
            if leg is None:
                duration = _LegDuration(reason="missing_route_leg")
                transition = missing_transition
            else:
                duration = _resolve_leg_duration(
                    leg=leg,
                    draft=draft,
                    day=day,
                    facts=facts,
                    places_by_id=places_by_id,
                    route_facts_by_id=route_facts_by_id,
                    planning_input=planning_input,
                    verified_departure_fact_id=verified_departure_fact_id,
                )
                transition = (
                    UnknownTransition(
                        from_reference_id=from_ref,
                        to_reference_id=activity.activity_id,
                        leg_id=leg.leg_id,
                        reason=duration.reason,
                    )
                    if duration.reason is not None
                    else None
                )
            if transition is not None:
                transitions.append(transition)
            if duration.reason is not None:
                if duration.leg_id is not None:
                    unknown_leg_ids.add(duration.leg_id)
            elif duration.duration_s is not None:
                known_travel_s.append(duration.duration_s)
            edge_results.append((activity, duration, from_kind or "", from_ref or ""))

        is_departure_day = day.day_date == departure_date
        return_timing = (
            _calculate_return_timing(
                planning_input=planning_input,
                draft=draft,
                day=day,
                facts=facts,
                places_by_id=places_by_id,
                route_facts_by_id=route_facts_by_id,
                day_start=day_start,
                optimistic_day_start=optimistic_day_start,
                deadline_at=deadline_at,
                departure_at=departure_at,
                buffer_s=buffer_s,
                verified_departure_fact_id=verified_departure_fact_id,
            )
            if is_departure_day
            else ReturnTiming(status="not_applicable", buffer_s=buffer_s)
        )
        if return_timing.route_leg_id and return_timing.travel_s is None:
            unknown_leg_ids.add(return_timing.route_leg_id)
        elif return_timing.travel_s is not None:
            known_travel_s.append(return_timing.travel_s)

        short_day = is_departure_day and _departure_day_is_too_short(
            planning_input=planning_input,
            day=day,
            return_timing=return_timing,
        )
        earliest_by_activity = _earliest_activity_bounds(
            day=day,
            edge_results=edge_results,
            constraint=constraint,
            planning_input=planning_input,
            optimistic_day_start=optimistic_day_start,
            zone=zone,
        )

        timings: list[ActivityTiming] = []
        current_time = day_start
        activity_waits: list[int] = []
        for activity, incoming, _, _ in edge_results:
            earliest_start, earliest_end = earliest_by_activity[activity.activity_id]
            duration_s = _validated_duration(activity)
            is_free_time = activity.kind == "free_time"
            if short_day:
                timings.append(ActivityTiming(
                    day_id=day.day_id,
                    activity_id=activity.activity_id,
                    duration_s=duration_s,
                    duration_source=activity.duration_source,
                    earliest_possible_start_at=earliest_start,
                    earliest_possible_end_at=earliest_end,
                    status="unverified",
                    unknown_reason="departure_day_under_minimum_usable_time",
                    note=("该活动保留在草稿中，但离开日乐观可用时间已不足3小时。"),
                ))
                current_time = None
                continue

            depart_at = current_time
            arrive_at = (
                current_time
                if is_free_time
                else _add_elapsed(current_time, incoming.duration_s, zone)
                if current_time is not None and incoming.duration_s is not None
                else None
            )
            slot = next(item for item in constraint.slots if item.slot_id == activity.slot_id)
            window = next(item for item in planning_input.rules.windows if item.period == slot.period)
            reference_start = _at_minute(day.day_date, window.start_minute, zone)
            start_at = _later_instant(arrive_at, reference_start) if arrive_at is not None else None
            end_at = _add_elapsed(start_at, duration_s, zone) if start_at is not None else None
            wait_s = max(0, int(ceil(_seconds_between(arrive_at, reference_start)))) if arrive_at else None
            reference_shift_s = max(0, int(ceil(_seconds_between(reference_start, start_at)))) if start_at else None

            if incoming.reason is not None:
                reason = incoming.reason
            elif current_time is None:
                reason = "prior_time_unknown"
            elif arrive_at is None:
                reason = "arrival_time_unknown"
            elif end_at is None:
                reason = "activity_end_unknown"
            else:
                reason = None
            status: Literal["estimated", "unverified"] = (
                "estimated"
                if depart_at is not None and arrive_at is not None and start_at is not None and end_at is not None
                else "unverified"
            )
            timings.append(ActivityTiming(
                day_id=day.day_id,
                activity_id=activity.activity_id,
                incoming_leg_id=incoming.leg_id,
                depart_at=depart_at,
                arrive_at=arrive_at,
                start_at=start_at,
                end_at=end_at,
                earliest_possible_start_at=earliest_start,
                earliest_possible_end_at=earliest_end,
                travel_s=incoming.duration_s,
                wait_s=wait_s,
                reference_shift_s=reference_shift_s,
                duration_s=duration_s,
                duration_source=activity.duration_source,
                status=status,
                unknown_reason=reason,
                note=(
                    "自由时间没有 POI；后续路线沿用最近已定位地点。"
                    if is_free_time
                    else None
                ),
            ))
            if wait_s is not None:
                activity_waits.append(wait_s)
            current_time = end_at

        return_timing = (
            _finalize_return_timing(
                return_timing=return_timing,
                final_activity_timing=timings[-1] if timings else None,
                day_start=day_start,
                optimistic_day_start=optimistic_day_start,
                deadline_at=deadline_at,
                zone=zone,
                short_day=short_day,
            )
            if is_departure_day
            else return_timing
        )

        all_times_known = all(item.status == "estimated" for item in timings)
        if short_day:
            decision: Literal["scheduled", "conditional", "not_scheduled_insufficient_time"] = (
                "not_scheduled_insufficient_time"
            )
        elif day_start is None or not all_times_known or transitions:
            decision = "conditional"
        elif is_departure_day and return_timing.status != "verified":
            decision = "conditional"
        else:
            decision = "scheduled"

        if return_timing.status == "conflict":
            feasibility: Literal["verified", "conflict", "unverified"] = "conflict"
        elif decision != "scheduled":
            feasibility = "unverified"
        else:
            feasibility = "verified"
        elapsed_s = (
            max(0, int(ceil(_seconds_between(day_start, current_time))))
            if day_start is not None and current_time is not None and not short_day
            else (0 if not timings and day_start is not None else None)
        )
        wait_s = sum(activity_waits) if all_times_known and not short_day else None
        summary = DaySummary(
            day_id=day.day_id,
            known_stay_s=sum(_validated_duration(activity) for activity in day.activities),
            known_travel_s=int(ceil(sum(known_travel_s))),
            unknown_leg_ids=tuple(sorted(unknown_leg_ids)),
            feasibility=feasibility,
        )
        results.append(DayTimingResult(
            day_id=day.day_id,
            day_start_at=day_start,
            activities=tuple(timings),
            summary=summary,
            scheduling_decision=decision,
            wait_s=wait_s,
            elapsed_s=elapsed_s,
            unknown_transitions=tuple(transitions),
            return_timing=return_timing,
        ))

    return PlanTimingResult(plan_id=draft.plan_id, based_on_revision=draft.revision, days=tuple(results))


def _validate_plan_binding(planning_input: PlanningInput, draft: PlanDraft) -> None:
    if draft.request_id != planning_input.request.request_id:
        raise ValueError("草稿与 PlanningInput 的 request_id 不一致")
    if draft.rule_version != planning_input.rules.rule_version:
        raise ValueError("草稿与 PlanningInput 的 rule_version 不一致")
    constraints = {day.day_id: day for day in planning_input.days}
    draft_days = {day.day_id: day for day in draft.days}
    if set(draft_days) != set(constraints):
        raise ValueError("草稿日期集合必须与 PlanningInput 一致")
    if any(draft_days[day_id].day_date != constraint.day_date for day_id, constraint in constraints.items()):
        raise ValueError("草稿日期必须与 PlanningInput 一致")


def _validate_fact_sources(facts: FactStore) -> None:
    sources = {source.tool_call_id: source for source in facts.sources}
    for place in facts.places:
        if any(source_id not in sources for source_id in place.source_refs):
            raise ValueError(f"地点事实 {place.fact_id} 引用了 FactStore 中不存在的来源")
        if any(source_id not in sources for source_id in place.field_sources.values()):
            raise ValueError(f"地点事实 {place.fact_id} 的字段来源不在 FactStore 中")
    for route in facts.routes:
        source = sources.get(route.source.tool_call_id)
        if source is None or source != route.source:
            raise ValueError(f"路线事实 {route.fact_id} 的来源没有在 FactStore 中闭合")
    for weather in facts.weather:
        source = sources.get(weather.source.tool_call_id)
        if source is None or source != weather.source:
            raise ValueError(f"天气事实 {weather.fact_id} 的来源没有在 FactStore 中闭合")


def _validate_draft_endpoints(draft: PlanDraft) -> None:
    days = {day.day_id: day for day in draft.days}
    for leg in draft.legs:
        day = days.get(leg.day_id)
        if day is None:
            raise ValueError(f"RouteLeg {leg.leg_id} 引用了草稿外的日期")
        for side, endpoint in (("from", leg.from_endpoint), ("to", leg.to_endpoint)):
            if endpoint.kind == "activity":
                activity = next((item for item in day.activities if item.activity_id == endpoint.reference_id), None)
                if activity is None:
                    raise ValueError(f"RouteLeg {leg.leg_id} 的 {side} 活动端点不属于该日期草稿")
                if activity.kind == "free_time":
                    raise ValueError(f"RouteLeg {leg.leg_id} 不能把 free_time 活动作为路线端点")
                if activity.poi_id != endpoint.poi_id:
                    raise ValueError(f"RouteLeg {leg.leg_id} 的活动 POI 与草稿不一致")
            elif endpoint.kind == "lodging":
                if endpoint.reference_id != f"lodging:{day.day_id}":
                    raise ValueError(f"RouteLeg {leg.leg_id} 的住宿端点标识与日期不一致")
                lodging = day.lodging
                if lodging is not None and lodging.poi_id is not None and lodging.poi_id != endpoint.poi_id:
                    raise ValueError(f"RouteLeg {leg.leg_id} 的住宿 POI 与草稿不一致")
                if side == "to":
                    raise ValueError(f"RouteLeg {leg.leg_id} 不能以住宿作为活动或返程终点")
            elif endpoint.kind == "arrival" and side != "from":
                raise ValueError(f"RouteLeg {leg.leg_id} 的 arrival 端点只能作为起点")
            elif endpoint.kind == "departure" and side != "to":
                raise ValueError(f"RouteLeg {leg.leg_id} 的 departure 端点只能作为终点")


def _validate_activity_slots(activities, constraint, planning_input: PlanningInput) -> None:
    slots = {slot.slot_id: slot for slot in constraint.slots}
    windows = {window.period: window for window in planning_input.rules.windows}
    for activity in activities:
        slot = slots.get(activity.slot_id)
        if slot is None:
            raise ValueError(f"活动 {activity.activity_id} 引用了不存在的时段")
        if slot.period not in windows:
            raise ValueError(f"活动 {activity.activity_id} 的参考窗口不存在")
        _validated_duration(activity)


def _activity_edges(activities: tuple[DraftActivity, ...], day_id: str):
    previous_kind, previous_ref = "lodging", f"lodging:{day_id}"
    for activity in activities:
        if activity.kind == "free_time":
            yield activity, None, None
        else:
            yield activity, previous_kind, previous_ref
            previous_kind, previous_ref = "activity", activity.activity_id


def _find_transition(*, draft: PlanDraft, day, activity: DraftActivity, from_kind: str, from_ref: str):
    incoming = [
        leg for leg in draft.legs
        if leg.day_id == day.day_id
        and leg.to_endpoint.kind == "activity"
        and leg.to_endpoint.reference_id == activity.activity_id
    ]
    if len(incoming) > 1:
        raise ValueError(f"活动 {activity.activity_id} 的入向 RouteLeg 不唯一")
    if incoming:
        leg = incoming[0]
        if leg.from_endpoint.kind != from_kind or leg.from_endpoint.reference_id != from_ref:
            raise ValueError(f"RouteLeg {leg.leg_id} 的起点与草稿中的上一已定位地点不一致")
        return leg, None
    return None, UnknownTransition(
        from_reference_id=from_ref,
        to_reference_id=activity.activity_id,
        reason="missing_route_leg",
    )


def _resolve_leg_duration(
    *,
    leg: RouteLeg,
    draft: PlanDraft,
    day,
    facts: FactStore,
    places_by_id: dict[str, PlaceFact],
    route_facts_by_id: dict[str, RouteFact],
    planning_input: PlanningInput,
    verified_departure_fact_id: str | None,
) -> _LegDuration:
    if leg.selection_source == "same_place":
        if leg.from_endpoint.poi_id != leg.to_endpoint.poi_id:
            return _LegDuration(leg_id=leg.leg_id, reason="same_place_poi_mismatch")
        origin, origin_reason = _place_for_endpoint(
            leg.from_endpoint, planning_input, day, places_by_id,
            verified_departure_fact_id=verified_departure_fact_id,
        )
        destination, destination_reason = _place_for_endpoint(
            leg.to_endpoint, planning_input, day, places_by_id,
            verified_departure_fact_id=verified_departure_fact_id,
        )
        if origin is None or destination is None:
            return _LegDuration(leg_id=leg.leg_id, reason=origin_reason or destination_reason or "same_place_identity_unverified")
        if origin.location is None or destination.location is None or origin.location != destination.location:
            return _LegDuration(leg_id=leg.leg_id, reason="same_place_identity_unverified")
        return _LegDuration(leg_id=leg.leg_id, duration_s=0)

    if leg.selected_mode is None or leg.selected_option_id is None:
        return _LegDuration(leg_id=leg.leg_id, reason="route_not_selected")
    mode_result = next((item for item in leg.mode_results if item.mode == leg.selected_mode), None)
    if (
        mode_result is None
        or mode_result.status != "available"
        or leg.selected_option_id not in mode_result.option_ids
    ):
        raise ValueError(f"RouteLeg {leg.leg_id} 的选中方式或方案无效")
    route_fact = route_facts_by_id.get(mode_result.route_fact_id or "")
    if route_fact is None:
        raise ValueError(f"RouteLeg {leg.leg_id} 引用了不存在的 RouteFact")
    if (
        route_fact.status != "available"
        or route_fact.mode != leg.selected_mode
        or route_fact.origin_poi_id != leg.from_endpoint.poi_id
        or route_fact.destination_poi_id != leg.to_endpoint.poi_id
    ):
        raise ValueError(f"RouteFact {route_fact.fact_id} 与 RouteLeg {leg.leg_id} 的方向、端点或方式不一致")
    option = next((item for item in route_fact.options if item.option_id == leg.selected_option_id), None)
    if option is None:
        raise ValueError(f"RouteLeg {leg.leg_id} 的选中方案不属于其 RouteFact")

    origin, origin_reason = _place_for_endpoint(
        leg.from_endpoint, planning_input, day, places_by_id,
        verified_departure_fact_id=verified_departure_fact_id,
    )
    destination, destination_reason = _place_for_endpoint(
        leg.to_endpoint, planning_input, day, places_by_id,
        verified_departure_fact_id=verified_departure_fact_id,
    )
    if origin is None or destination is None:
        return _LegDuration(leg_id=leg.leg_id, reason=origin_reason or destination_reason or "route_endpoint_unverified")
    if origin.location is None or destination.location is None:
        return _LegDuration(leg_id=leg.leg_id, reason=origin_reason or destination_reason or "route_endpoint_location_unknown")
    if route_fact.origin != origin.location or route_fact.destination != destination.location:
        raise ValueError(f"RouteFact {route_fact.fact_id} 的路线坐标与草稿端点地点事实不一致")
    if option.duration_s is None:
        return _LegDuration(leg_id=leg.leg_id, reason="route_duration_unknown")
    if option.duration_s < 0:
        raise ValueError(f"RouteLeg {leg.leg_id} 的路线耗时不能为负数")
    return _LegDuration(leg_id=leg.leg_id, duration_s=float(option.duration_s))


def _place_for_endpoint(
    endpoint: LegEndpoint,
    planning_input: PlanningInput,
    day,
    places_by_id: dict[str, PlaceFact],
    *,
    verified_departure_fact_id: str | None,
) -> tuple[PlaceFact | None, str | None]:
    expected_fact_id: str | None
    if endpoint.kind == "activity":
        activity = next((item for item in day.activities if item.activity_id == endpoint.reference_id), None)
        if activity is None or activity.kind == "free_time" or activity.poi_id != endpoint.poi_id:
            raise ValueError("路线活动端点没有绑定到该日期的草稿活动")
        expected_fact_id = activity.place_fact_id
        missing_reason = "activity_place_fact_missing"
    elif endpoint.kind == "lodging":
        lodging = day.lodging
        if endpoint.reference_id != f"lodging:{day.day_id}":
            raise ValueError("路线住宿端点没有绑定到该日期")
        if lodging is None:
            return None, "lodging_unverified"
        if lodging.poi_id is not None and lodging.poi_id != endpoint.poi_id:
            raise ValueError("路线住宿端点 POI 与草稿住宿不一致")
        if lodging.poi_id is None or lodging.place_fact_id is None:
            return None, "lodging_unverified"
        expected_fact_id = lodging.place_fact_id
        missing_reason = "lodging_place_fact_missing"
    elif endpoint.kind == "departure":
        query = planning_input.request.departure_place_query
        if not query or not query.strip():
            return None, "departure_endpoint_not_provided"
        if verified_departure_fact_id is None:
            return None, "departure_fact_not_bound"
        expected_fact_id = verified_departure_fact_id
        missing_reason = "departure_fact_missing"
    elif endpoint.kind == "arrival":
        query = planning_input.request.arrival_place_query
        if not query or not query.strip():
            return None, "arrival_endpoint_not_provided"
        matches = [
            place for place in places_by_id.values()
            if place.poi_id == endpoint.poi_id
        ]
        if not matches:
            return None, "arrival_fact_missing"
        if any(place.location != matches[0].location for place in matches[1:]):
            return None, "arrival_fact_ambiguous"
        if matches[0].location is None:
            return matches[0], "arrival_location_unknown"
        return matches[0], None
    else:  # pragma: no cover - LegEndpoint Literal keeps this defensive branch unreachable.
        raise ValueError(f"不支持的路线端点类型: {endpoint.kind}")

    place = places_by_id.get(expected_fact_id or "")
    if place is None:
        return None, missing_reason
    if place.poi_id != endpoint.poi_id:
        raise ValueError("路线端点 POI 与绑定地点事实不一致")
    if endpoint.kind == "departure" and place.location is None:
        return place, "departure_location_unknown"
    return place, None


def _validate_return_leg_binding(
    *, day, draft: PlanDraft, last_located: DraftActivity | None, places_by_id: dict[str, PlaceFact],
) -> tuple[RouteLeg | None, str, str, str]:
    if last_located is not None:
        expected_kind, expected_ref, expected_poi = "activity", last_located.activity_id, last_located.poi_id or ""
    elif day.lodging is not None and day.lodging.poi_id is not None:
        expected_kind, expected_ref, expected_poi = "lodging", f"lodging:{day.day_id}", day.lodging.poi_id
    else:
        expected_kind, expected_ref, expected_poi = "lodging", f"lodging:{day.day_id}", ""

    return_legs = [
        leg for leg in draft.legs
        if leg.day_id == day.day_id and leg.to_endpoint.kind == "departure"
    ]
    if len(return_legs) > 1:
        raise ValueError("离开日返程 RouteLeg 不唯一")
    if not return_legs:
        return None, expected_kind, expected_ref, expected_poi
    leg = return_legs[0]
    if leg.from_endpoint.kind != expected_kind or leg.from_endpoint.reference_id != expected_ref:
        raise ValueError(f"RouteLeg {leg.leg_id} 的返程起点没有绑定到最后一个已定位地点")
    if expected_poi and leg.from_endpoint.poi_id != expected_poi:
        raise ValueError(f"RouteLeg {leg.leg_id} 的返程起点 POI 与草稿活动或住宿不一致")
    # A departure endpoint is verified separately against the exact F04d fact ID.
    return leg, expected_kind, expected_ref, expected_poi


def _calculate_return_timing(
    *,
    planning_input: PlanningInput,
    draft: PlanDraft,
    day,
    facts: FactStore,
    places_by_id: dict[str, PlaceFact],
    route_facts_by_id: dict[str, RouteFact],
    day_start: datetime | None,
    optimistic_day_start: datetime,
    deadline_at: datetime,
    departure_at: datetime,
    buffer_s: int,
    verified_departure_fact_id: str | None,
) -> ReturnTiming:
    reasons: list[str] = []
    notes: list[str] = []
    request = planning_input.request
    if not request.departure_place_query or not request.departure_place_query.strip():
        reasons.append("departure_endpoint_not_provided")
    if verified_departure_fact_id is None:
        reasons.append("departure_fact_not_bound")

    last_located = next((activity for activity in reversed(day.activities) if activity.kind != "free_time"), None)
    leg, from_kind, from_ref, from_poi = _validate_return_leg_binding(
        day=day, draft=draft, last_located=last_located, places_by_id=places_by_id,
    )
    trailing_free_time = bool(day.activities and day.activities[-1].kind == "free_time")
    if trailing_free_time:
        notes.append("末尾自由时间计入停留；返程沿用最近已定位地点，未计入自行离开所在区域的移动。")
        reasons.append("free_time_movement_not_included")
    if not from_poi:
        reasons.append("return_origin_unverified")
    if leg is None:
        reasons.append("return_route_missing")
        return ReturnTiming(
            status="unverified",
            departure_at=departure_at,
            deadline_at=deadline_at,
            buffer_s=buffer_s,
            optimistic_usable_s=int(_seconds_between(optimistic_day_start, deadline_at)),
            unknown_reasons=tuple(dict.fromkeys(reasons)),
            notes=tuple(notes),
        )

    route_duration = _resolve_leg_duration(
        leg=leg,
        draft=draft,
        day=day,
        facts=facts,
        places_by_id=places_by_id,
        route_facts_by_id=route_facts_by_id,
        planning_input=planning_input,
        verified_departure_fact_id=verified_departure_fact_id,
    )
    departure_fact = places_by_id.get(verified_departure_fact_id or "")
    terminal_verified = (
        bool(request.departure_place_query and request.departure_place_query.strip())
        and departure_fact is not None
        and departure_fact.location is not None
        and departure_fact.poi_id == leg.to_endpoint.poi_id
        and leg.from_endpoint.poi_id == from_poi
        and route_duration.reason is None
        and route_duration.duration_s is not None
    )
    if not terminal_verified and "return_endpoint_unverified" not in reasons:
        reasons.append("return_endpoint_unverified")
    if route_duration.reason is not None:
        reasons.append(route_duration.reason)

    travel_s = route_duration.duration_s if terminal_verified else None
    base_optimistic_s = int(_seconds_between(optimistic_day_start, deadline_at))
    optimistic_usable_s = base_optimistic_s - int(ceil(travel_s)) if travel_s is not None else base_optimistic_s
    usable_s = (
        int(_seconds_between(day_start, deadline_at) - travel_s)
        if day_start is not None and travel_s is not None
        else None
    )
    latest_activity_end = (
        _subtract_elapsed(deadline_at, int(ceil(travel_s)), ZoneInfo(request.timezone))
        if travel_s is not None
        else None
    )
    return ReturnTiming(
        status="unverified",
        departure_poi_id=leg.to_endpoint.poi_id if terminal_verified else None,
        departure_at=departure_at,
        deadline_at=deadline_at,
        latest_activity_end_at=latest_activity_end,
        route_leg_id=leg.leg_id,
        travel_s=travel_s,
        buffer_s=buffer_s,
        usable_s=usable_s,
        optimistic_usable_s=optimistic_usable_s,
        unknown_reasons=tuple(dict.fromkeys(reasons)),
        notes=tuple(notes),
    )


def _finalize_return_timing(
    *,
    return_timing: ReturnTiming,
    final_activity_timing: ActivityTiming | None,
    day_start: datetime | None,
    optimistic_day_start: datetime,
    deadline_at: datetime,
    zone: ZoneInfo,
    short_day: bool,
) -> ReturnTiming:
    reasons = list(return_timing.unknown_reasons)
    travel_s = return_timing.travel_s
    exact_end = final_activity_timing.end_at if final_activity_timing is not None else day_start
    earliest_end = (
        final_activity_timing.earliest_possible_end_at
        if final_activity_timing is not None
        else optimistic_day_start
    )
    if exact_end is None:
        reasons.append("terminal_activity_end_unknown")

    if return_timing.route_leg_id is None or travel_s is None or return_timing.departure_poi_id is None:
        # 活动结束本身晚于返程截止时，即使返程路线未知，也足以确认冲突。
        if exact_end is not None and _seconds_between(deadline_at, exact_end) > 0:
            end_overrun = int(ceil(_seconds_between(deadline_at, exact_end)))
            reasons.append("activity_end_exceeds_return_deadline")
            return return_timing.model_copy(update={
                "status": "conflict",
                "overrun_s": end_overrun,
                "overrun_is_lower_bound": True,
                "unknown_reasons": tuple(dict.fromkeys(reasons)),
            })
        if earliest_end is not None and _seconds_between(deadline_at, earliest_end) > 0:
            minimum_overrun = int(ceil(_seconds_between(deadline_at, earliest_end)))
            reasons.append("minimum_activity_end_exceeds_return_deadline")
            return return_timing.model_copy(update={
                "status": "conflict",
                "overrun_s": minimum_overrun,
                "overrun_is_lower_bound": True,
                "unknown_reasons": tuple(dict.fromkeys(reasons)),
            })
        return return_timing.model_copy(update={
            "status": "unverified",
            "unknown_reasons": tuple(dict.fromkeys(reasons)),
        })

    earliest_arrival = _add_elapsed(earliest_end, travel_s, zone) if earliest_end is not None else None
    estimated_arrival = _add_elapsed(exact_end, travel_s, zone) if exact_end is not None else None
    if estimated_arrival is not None and _seconds_between(deadline_at, estimated_arrival) > 0:
        actual_overrun = int(ceil(_seconds_between(deadline_at, estimated_arrival)))
        return return_timing.model_copy(update={
            "status": "conflict",
            "estimated_arrival_at": estimated_arrival,
            "earliest_possible_arrival_at": earliest_arrival,
            "overrun_s": actual_overrun,
            "overrun_is_lower_bound": False,
            "unknown_reasons": tuple(dict.fromkeys(reasons)),
        })
    if earliest_arrival is not None and _seconds_between(deadline_at, earliest_arrival) > 0:
        minimum_overrun = int(ceil(_seconds_between(deadline_at, earliest_arrival)))
        reasons.append("minimum_return_arrival_exceeds_deadline")
        return return_timing.model_copy(update={
            "status": "conflict",
            "estimated_arrival_at": estimated_arrival,
            "earliest_possible_arrival_at": earliest_arrival,
            "overrun_s": minimum_overrun,
            "overrun_is_lower_bound": True,
            "unknown_reasons": tuple(dict.fromkeys(reasons)),
        })
    if estimated_arrival is None:
        reasons.append("terminal_activity_end_unknown")
        return return_timing.model_copy(update={
            "status": "unverified",
            "earliest_possible_arrival_at": earliest_arrival,
            "unknown_reasons": tuple(dict.fromkeys(reasons)),
        })

    actual_overrun = _seconds_between(deadline_at, estimated_arrival)
    if actual_overrun > 0:
        return return_timing.model_copy(update={
            "status": "conflict",
            "estimated_arrival_at": estimated_arrival,
            "earliest_possible_arrival_at": earliest_arrival,
            "overrun_s": int(ceil(actual_overrun)),
            "overrun_is_lower_bound": False,
            "unknown_reasons": tuple(dict.fromkeys(reasons)),
        })
    if short_day:
        reasons.append("day_not_scheduled_under_minimum_usable_time")
    if reasons:
        status: Literal["verified", "conflict", "unverified"] = "unverified"
    else:
        status = "verified"
    return return_timing.model_copy(update={
        "status": status,
        "estimated_arrival_at": estimated_arrival,
        "earliest_possible_arrival_at": earliest_arrival,
        "unknown_reasons": tuple(dict.fromkeys(reasons)),
    })


def _departure_day_is_too_short(*, planning_input: PlanningInput, day, return_timing: ReturnTiming) -> bool:
    if day.day_date != planning_input.request.departure_at.astimezone(
        ZoneInfo(planning_input.request.timezone)
    ).date():
        return False
    # 已知就绪时刻和返程路线都是更紧的可用时间上界。
    usable_upper_bound = (
        return_timing.usable_s
        if return_timing.usable_s is not None
        else return_timing.optimistic_usable_s
    )
    if usable_upper_bound is None:
        return False
    return usable_upper_bound < planning_input.rules.departure_day_minimum_usable_s


def _earliest_activity_bounds(
    *,
    day,
    edge_results,
    constraint,
    planning_input: PlanningInput,
    optimistic_day_start: datetime,
    zone: ZoneInfo,
) -> dict[str, tuple[datetime, datetime]]:
    """计算保守的最早时间下界，仅用于证明必然冲突；不用于排程。

    未知路段仅使用“交通耗时非负”得到下界，不会写入 ActivityTiming.travel_s，
    也不会把该路段宣称为零耗时。
    """
    current_lower_bound: datetime | None = optimistic_day_start
    slot_by_id = {slot.slot_id: slot for slot in constraint.slots}
    window_by_period = {window.period: window for window in planning_input.rules.windows}
    bounds: dict[str, tuple[datetime, datetime]] = {}
    for activity, incoming, _, _ in edge_results:
        slot = slot_by_id[activity.slot_id]
        window_start = _at_minute(day.day_date, window_by_period[slot.period].start_minute, zone)
        if activity.kind == "free_time":
            earliest_arrival = current_lower_bound
        elif current_lower_bound is None:
            earliest_arrival = None
        elif incoming.duration_s is not None:
            earliest_arrival = _add_elapsed(current_lower_bound, incoming.duration_s, zone)
        else:
            # 未知路线有非负耗时；沿用到达下界，但不输出为实际到达时间。
            earliest_arrival = current_lower_bound
        earliest_start = (
            _later_instant(earliest_arrival, window_start)
            if earliest_arrival is not None
            else window_start
        )
        earliest_end = _add_elapsed(earliest_start, _validated_duration(activity), zone)
        bounds[activity.activity_id] = (earliest_start, earliest_end)
        current_lower_bound = earliest_end
    return bounds


def _validated_duration(activity: DraftActivity) -> int:
    duration = getattr(activity, "duration_s", None)
    if duration is None:
        raise ValueError(f"活动 {activity.activity_id} 必须提供停留时长")
    if duration < 0:
        raise ValueError(f"活动 {activity.activity_id} 的停留时长不能为负数")
    return duration


def _day_start(
    *,
    day_date: date,
    arrival_date: date,
    boundary_ready_at: datetime | None,
    available_start: datetime | None,
    full_day_start_minute: int,
    zone: ZoneInfo,
) -> datetime | None:
    if day_date == arrival_date:
        return boundary_ready_at.astimezone(zone) if boundary_ready_at is not None else None
    if available_start is not None:
        return available_start.astimezone(zone)
    return _at_minute(day_date, full_day_start_minute, zone)


def _optimistic_day_start(
    *,
    day_date: date,
    arrival_date: date,
    planning_input: PlanningInput,
    available_start: datetime | None,
    zone: ZoneInfo,
) -> datetime:
    if day_date == arrival_date:
        boundary = planning_input.boundary_facts
        if boundary is not None and boundary.day_ready_at is not None:
            return boundary.day_ready_at.astimezone(zone)
        if boundary is not None:
            return boundary.arrival_start_optimistic_at.astimezone(zone)
        return planning_input.request.arrival_at.astimezone(zone)
    if available_start is not None:
        return available_start.astimezone(zone)
    return _at_minute(day_date, planning_input.rules.full_day_start_minute, zone)


def _at_minute(day: date, minute: int, zone: ZoneInfo) -> datetime:
    if minute == 24 * 60:
        return datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return datetime.combine(day, time(minute // 60, minute % 60), tzinfo=zone)


def _subtract_elapsed(value: datetime, seconds: int, zone: ZoneInfo) -> datetime:
    return (value.astimezone(timezone.utc) - timedelta(seconds=seconds)).astimezone(zone)


def _add_elapsed(value: datetime, seconds: float, zone: ZoneInfo) -> datetime:
    return (value.astimezone(timezone.utc) + timedelta(seconds=seconds)).astimezone(zone)


def _seconds_between(start: datetime, end: datetime) -> float:
    return (end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds()


def _later_instant(left: datetime, right: datetime) -> datetime:
    return left if left.astimezone(timezone.utc) >= right.astimezone(timezone.utc) else right
