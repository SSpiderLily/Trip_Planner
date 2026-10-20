"""从规划草稿和地点事实纯计算已知人均消费参考。"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from app.models.planning_v4 import (
    CostEntryKind,
    CostSummaryStatus,
    DayReferenceCostSummary,
    DraftActivity,
    DraftDay,
    FactStore,
    PlanDraft,
    PlanningInput,
    ReferenceCostEntry,
    ReferenceCostFactEvidence,
    ReferenceCostSummary,
)
from app.models.planning_v4.facts import FactSource, PlaceFact


class ReferenceCostAggregationError(ValueError):
    """消费计算输入中的日期、事实或来源引用不一致。"""


def aggregate_reference_costs(
    planning_input: PlanningInput,
    draft: PlanDraft,
    facts: FactStore,
) -> ReferenceCostSummary:
    """汇总已知人均景点和餐饮消费，并单独保留每天的酒店参考。

    景点在同一天按 POI 去重；跨日分别计入。每项餐次按活动分别计入。
    缺少金额和同日景点事实间的金额冲突均不计入已知小计。
    """
    _validate_plan_binding(planning_input, draft)
    places_by_id = {place.fact_id: place for place in facts.places}
    sources_by_id = {source.tool_call_id: source for source in facts.sources}
    _validate_fact_source_closure(facts, sources_by_id)

    day_summaries: list[DayReferenceCostSummary] = []
    for day in draft.days:
        # Keep the original activity order. Sightseeing groups occupy their first-use position.
        entries = _order_day_entries(day, places_by_id, sources_by_id)

        lodging_entry = None
        if day.lodging is not None and day.lodging.place_fact_id is not None:
            lodging_place = _bound_place(
                day.lodging.poi_id,
                day.lodging.place_fact_id,
                places_by_id,
                f"日期 {day.day_id} 的住宿",
            )
            lodging_entry = _entry_from_places(
                kind="lodging",
                day_id=day.day_id,
                day_date=day.day_date,
                activity_ids=(),
                slot_ids=(),
                poi_id=lodging_place.poi_id,
                places=(lodging_place,),
                sources_by_id=sources_by_id,
            )

        known_subtotal = sum(
            (entry.amount for entry in entries if entry.status == "known" and entry.amount is not None),
            Decimal("0"),
        )
        day_summaries.append(DayReferenceCostSummary(
            day_id=day.day_id,
            day_date=day.day_date,
            status=_status(entries),
            known_sightseeing_meal_per_person_subtotal=known_subtotal,
            entries=tuple(entries),
            lodging=lodging_entry,
        ))

    total = sum(
        (day.known_sightseeing_meal_per_person_subtotal for day in day_summaries),
        Decimal("0"),
    )
    all_entries = tuple(entry for day in day_summaries for entry in day.entries)
    return ReferenceCostSummary(
        request_id=draft.request_id,
        plan_id=draft.plan_id,
        based_on_revision=draft.revision,
        status=_status(all_entries),
        known_sightseeing_meal_per_person_subtotal=total,
        days=tuple(day_summaries),
    )


def _order_day_entries(
    day: DraftDay,
    places_by_id: dict[str, PlaceFact],
    sources_by_id: dict[str, FactSource],
) -> list[ReferenceCostEntry]:
    """生成同一日期的景点去重组与独立餐次，并保持首次活动顺序。"""
    sightseeing: OrderedDict[str, list[tuple[DraftActivity, PlaceFact]]] = OrderedDict()
    result_by_key: dict[tuple[str, str], ReferenceCostEntry] = {}
    ordered_keys: list[tuple[str, str]] = []

    for activity in day.activities:
        if activity.kind == "free_time":
            continue
        place = _bound_place(
            activity.poi_id,
            activity.place_fact_id,
            places_by_id,
            f"活动 {activity.activity_id}",
        )
        if activity.kind == "sightseeing":
            poi_id = activity.poi_id
            if poi_id not in sightseeing:
                sightseeing[poi_id] = []
                ordered_keys.append(("sightseeing", poi_id))
            sightseeing[poi_id].append((activity, place))
        else:
            entry = _entry_from_places(
                kind="meal",
                day_id=day.day_id,
                day_date=day.day_date,
                activity_ids=(activity.activity_id,),
                slot_ids=(activity.slot_id,),
                poi_id=activity.poi_id,
                places=(place,),
                sources_by_id=sources_by_id,
            )
            result_by_key[("meal", activity.activity_id)] = entry
            ordered_keys.append(("meal", activity.activity_id))

    for poi_id, uses in sightseeing.items():
        result_by_key[("sightseeing", poi_id)] = _entry_from_places(
            kind="sightseeing",
            day_id=day.day_id,
            day_date=day.day_date,
            activity_ids=tuple(activity.activity_id for activity, _ in uses),
            slot_ids=tuple(activity.slot_id for activity, _ in uses),
            poi_id=poi_id,
            places=tuple(place for _, place in uses),
            sources_by_id=sources_by_id,
        )
    return [result_by_key[key] for key in ordered_keys]


def _entry_from_places(
    *,
    kind: CostEntryKind,
    day_id: str,
    day_date: date,
    activity_ids: tuple[str, ...],
    slot_ids: tuple[str, ...],
    poi_id: str,
    places: tuple[PlaceFact, ...],
    sources_by_id: dict[str, FactSource],
) -> ReferenceCostEntry:
    # A same-POI sightseeing group may use the same fact in multiple activities.
    unique_places = tuple({place.fact_id: place for place in places}.values())
    known_values = {place.cost.value for place in unique_places if place.cost.value is not None}
    if len(known_values) > 1:
        status, amount, reason = "conflict", None, "conflicting_values"
    elif known_values:
        status, amount, reason = "known", next(iter(known_values)), None
    else:
        status, amount, reason = "unknown", None, "cost_not_returned"

    evidence = tuple(
        ReferenceCostFactEvidence(
            place_fact_id=place.fact_id,
            poi_id=place.poi_id,
            place_name=place.name,
            cost=place.cost,
            sources=tuple(sources_by_id[source_id] for source_id in place.source_refs),
            cost_source=(
                sources_by_id[place.field_sources["cost"]]
                if "cost" in place.field_sources else None
            ),
        )
        for place in unique_places
    )
    return ReferenceCostEntry(
        kind=kind,
        unit="per_person_per_night_reference" if kind == "lodging" else "per_person_reference",
        day_id=day_id,
        day_date=day_date,
        activity_ids=activity_ids,
        slot_ids=slot_ids,
        poi_id=poi_id,
        place_name=unique_places[0].name,
        amount=amount,
        status=status,
        unknown_reason=reason,
        evidence=evidence,
    )


def _bound_place(
    poi_id: str | None,
    fact_id: str | None,
    places_by_id: dict[str, PlaceFact],
    reference: str,
) -> PlaceFact:
    if poi_id is None or fact_id is None:
        raise ReferenceCostAggregationError(f"{reference} 缺少 POI 或地点事实引用")
    place = places_by_id.get(fact_id)
    if place is None:
        raise ReferenceCostAggregationError(f"{reference} 引用了 FactStore 中不存在的地点事实 {fact_id}")
    if place.poi_id != poi_id:
        raise ReferenceCostAggregationError(
            f"{reference} 的 poi_id {poi_id} 与地点事实 {fact_id} 的 poi_id {place.poi_id} 不一致"
        )
    return place


def _validate_fact_source_closure(
    facts: FactStore,
    sources_by_id: dict[str, FactSource],
) -> None:
    for place in facts.places:
        missing_refs = [source_id for source_id in place.source_refs if source_id not in sources_by_id]
        if missing_refs:
            raise ReferenceCostAggregationError(
                f"地点事实 {place.fact_id} 的 source_refs 未闭合: {', '.join(missing_refs)}"
            )
        cost_source_id = place.field_sources.get("cost")
        if cost_source_id is not None and cost_source_id not in sources_by_id:
            raise ReferenceCostAggregationError(
                f"地点事实 {place.fact_id} 的 field_sources.cost 未闭合: {cost_source_id}"
            )


def _validate_plan_binding(planning_input: PlanningInput, draft: PlanDraft) -> None:
    if draft.request_id != planning_input.request.request_id:
        raise ReferenceCostAggregationError("草稿与 PlanningInput 的 request_id 不一致")
    if draft.rule_version != planning_input.rules.rule_version:
        raise ReferenceCostAggregationError("草稿与 PlanningInput 的 rule_version 不一致")
    constraints = {day.day_id: day for day in planning_input.days}
    draft_days = {day.day_id: day for day in draft.days}
    if set(draft_days) != set(constraints):
        raise ReferenceCostAggregationError("草稿日期集合必须与 PlanningInput 一致")
    if any(draft_days[day_id].day_date != constraint.day_date for day_id, constraint in constraints.items()):
        raise ReferenceCostAggregationError("草稿日期必须与 PlanningInput 一致")


def _status(entries: Sequence[ReferenceCostEntry]) -> CostSummaryStatus:
    if not entries:
        return "no_activities"
    known_count = sum(entry.status == "known" for entry in entries)
    if known_count == len(entries):
        return "all_known"
    if known_count:
        return "known_partial"
    return "all_unknown"
