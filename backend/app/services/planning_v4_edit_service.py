"""Planning v4 预览的纯按日编辑核心；不负责持久化或外部查询。"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from uuid import NAMESPACE_URL, uuid5

from app.models.planning_v4 import (
    CandidateRef,
    DraftActivity,
    EditCommand,
    ItineraryPreview,
    PlanDraft,
    PreviewCalculation,
    RouteLeg,
)
from app.models.planning_v4.plan import (
    AddActivity,
    DeleteActivity,
    ReorderActivities,
    SelectLegMode,
)
from app.services.current_route_service import (
    CurrentRouteError,
    build_pending_route_legs_for_day,
)
from app.services.planning_v4_validation import build_day_summaries, validate_daily_plan
from app.services.planning_v4_validator import ContractValidationError, validate_preview


_MEAL_PERIODS = frozenset({"breakfast", "lunch", "dinner"})
_SIGHTSEEING_PERIODS = frozenset({"morning", "afternoon", "evening"})


class PlanningV4EditError(ValueError):
    """编辑输入不满足版本、范围或事实契约。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class EditExecution:
    preview: ItineraryPreview
    changed_activity_ids: tuple[str, ...]
    changed_leg_ids: tuple[str, ...]
    pending_leg_ids: tuple[str, ...]


class PlanningV4EditService:
    """将一个 EditCommand 原子应用到一个日期并返回待计算预览。"""

    def apply(
        self,
        preview: ItineraryPreview,
        command: EditCommand,
        *,
        verified_departure_fact_id: str | None = None,
        inherited_pending_day_ids: tuple[str, ...] = (),
    ) -> EditExecution:
        if not isinstance(preview, ItineraryPreview) or not isinstance(command, EditCommand):
            raise PlanningV4EditError("input_type_invalid")
        try:
            validate_preview(preview)
        except ContractValidationError as exc:
            raise PlanningV4EditError("preview_invalid") from exc

        draft = preview.draft
        if command.plan_id != draft.plan_id:
            raise PlanningV4EditError("plan_mismatch")
        if command.base_revision != draft.revision:
            raise PlanningV4EditError("stale_edit_revision")
        old_day = next((day for day in draft.days if day.day_id == command.day_id), None)
        constraint = next((day for day in preview.planning_input.days if day.day_id == command.day_id), None)
        if old_day is None or constraint is None:
            raise PlanningV4EditError("edit_day_missing")
        if any(operation.day_id != command.day_id for operation in command.operations):
            raise PlanningV4EditError("edit_operation_day_mismatch")

        candidates = {candidate.candidate_id: candidate for candidate in preview.candidates}
        places = {place.fact_id: place for place in preview.facts.places}
        slot_by_id = {slot.slot_id: slot for slot in constraint.slots}
        working = list(old_day.activities)
        original_target_legs = [leg for leg in draft.legs if leg.day_id == command.day_id]
        client_ids: dict[str, str] = {}
        selected_options: dict[tuple[str, str, str, str, str], tuple[str, str]] = {}
        changed_ids: list[str] = []
        changed_leg_ids: list[str] = []
        topology_changed = False
        preference_changed = False

        for operation in command.operations:
            if isinstance(operation, AddActivity):
                activity, resolved_id = self._new_activity(
                    preview, command, operation, candidates, places, slot_by_id,
                    existing_activity_ids={item.activity_id for day in draft.days for item in day.activities},
                    current_activity_ids={item.activity_id for item in working},
                    used_client_refs=set(client_ids),
                )
                client_ids[operation.client_ref] = resolved_id
                # 真实地点活动替换同一时段的 free_time 占位，避免重复占时。
                working = [
                    current for current in working
                    if not (current.kind == "free_time" and current.slot_id == operation.slot_id)
                ]
                slot_order = {
                    item.slot_id: index for index, item in enumerate(constraint.slots)
                }
                same_slot_indexes = [
                    index for index, current in enumerate(working)
                    if current.slot_id == operation.slot_id
                ]
                if same_slot_indexes:
                    # 同一时段的多项活动按添加顺序排列。
                    insert_at = same_slot_indexes[-1] + 1
                else:
                    new_slot_order = slot_order[operation.slot_id]
                    insert_at = next((
                        index for index, current in enumerate(working)
                        if slot_order.get(current.slot_id, -1) > new_slot_order
                    ), len(working))
                working.insert(insert_at, activity)
                changed_ids.append(resolved_id)
                topology_changed = True
            elif isinstance(operation, DeleteActivity):
                activity_id = client_ids.get(operation.activity_id, operation.activity_id)
                found = next((item for item in working if item.activity_id == activity_id), None)
                if found is None:
                    raise PlanningV4EditError("activity_not_in_target_day")
                working = [item for item in working if item.activity_id != activity_id]
                changed_ids.append(activity_id)
                topology_changed = True
            elif isinstance(operation, ReorderActivities):
                requested = tuple(client_ids.get(value, value) for value in operation.activity_ids)
                current_ids = [item.activity_id for item in working]
                if (
                    len(requested) != len(set(requested))
                    or len(requested) != len(current_ids)
                    or set(requested) != set(current_ids)
                ):
                    raise PlanningV4EditError("reorder_must_be_current_day_permutation")
                by_id = {item.activity_id: item for item in working}
                reordered = [by_id[activity_id] for activity_id in requested]
                if [item.activity_id for item in reordered] != current_ids:
                    topology_changed = True
                    changed_ids.extend(requested)
                working = reordered
            elif isinstance(operation, SelectLegMode):
                leg = next((item for item in original_target_legs if item.leg_id == operation.leg_id), None)
                if leg is None or leg.plan_revision != draft.revision:
                    raise PlanningV4EditError("selected_leg_not_current_target_day")
                mode_result = next((item for item in leg.mode_results if item.mode == operation.mode), None)
                if (
                    mode_result is None
                    or mode_result.status != "available"
                    or operation.option_id not in mode_result.option_ids
                ):
                    raise PlanningV4EditError("selected_option_not_available")
                route_fact = next((item for item in preview.facts.routes if item.fact_id == mode_result.route_fact_id), None)
                if (
                    route_fact is None
                    or route_fact.mode != operation.mode
                    or route_fact.status != "available"
                    or operation.option_id not in {item.option_id for item in route_fact.options}
                ):
                    raise PlanningV4EditError("selected_option_fact_invalid")
                option = next(
                    item for item in route_fact.options if item.option_id == operation.option_id
                )
                if option.duration_s is None or not isfinite(option.duration_s) or option.duration_s < 0:
                    raise PlanningV4EditError("selected_option_duration_unknown")
                signature = _leg_signature(leg)
                selected_options[signature] = (operation.mode, operation.option_id)
                previous_preference = leg.preferred_mode or (
                    leg.selected_mode if leg.selection_source == "user" else None
                )
                if (
                    previous_preference != operation.mode
                    or leg.selected_option_id != operation.option_id
                    or leg.selection_source != "user"
                ):
                    preference_changed = True
                    changed_leg_ids.append(leg.leg_id)
            else:  # pragma: no cover - Literal discriminator closes this branch.
                raise PlanningV4EditError("unsupported_edit_operation")

        if not topology_changed and not preference_changed:
            raise PlanningV4EditError("edit_has_no_effect")

        revision = draft.revision + 1
        edited_day = old_day.model_copy(update={
            "activities": tuple(working),
            "unfilled_slot_ids": _unfilled_slots(
                command.day_id,
                working,
                constraint.slots,
                candidates,
                places,
            ),
        }, deep=True)
        days = tuple(edited_day if day.day_id == command.day_id else day for day in draft.days)
        retagged_legs = []
        for leg in draft.legs:
            updates: dict[str, object] = {"plan_revision": revision}
            if leg.day_id == command.day_id:
                selected_option = selected_options.get(_leg_signature(leg))
                if selected_option is not None:
                    updates.update({
                        "selected_mode": selected_option[0],
                        "selected_option_id": selected_option[1],
                        "preferred_mode": selected_option[0],
                        "selection_source": "user",
                    })
            retagged_legs.append(leg.model_copy(update=updates, deep=True))

        pending_base = PlanDraft.model_validate(draft.model_dump(mode="python") | {
            "revision": revision,
            "base_revision": draft.revision,
            "days": days,
            "legs": tuple(retagged_legs),
        })
        try:
            target_legs = build_pending_route_legs_for_day(
                preview.planning_input,
                pending_base,
                preview.facts,
                command.day_id,
                verified_departure_fact_id=verified_departure_fact_id,
            )
        except CurrentRouteError as exc:
            raise PlanningV4EditError("route_scaffold_invalid") from exc

        refreshed_signatures = {_leg_signature(leg) for leg in target_legs}
        if set(selected_options) - refreshed_signatures:
            raise PlanningV4EditError("selected_leg_removed_by_same_command")
        previous_by_signature = {
            _leg_signature(leg): leg for leg in pending_base.legs
            if leg.day_id == command.day_id
        }
        # 日期、公共交通参考时刻及不可变地点事实均未改变。同向相邻边
        # 继续使用原方案与路径，仅绑定新版本；新邻边才保留待查询骨架。
        target_legs = tuple(
            previous_by_signature[_leg_signature(leg)]
            if _leg_signature(leg) in previous_by_signature
            and previous_by_signature[_leg_signature(leg)].from_endpoint == leg.from_endpoint
            and previous_by_signature[_leg_signature(leg)].to_endpoint == leg.to_endpoint
            else leg
            for leg in target_legs
        )
        old_leg_ids = {leg.leg_id for leg in original_target_legs}
        new_leg_ids = {leg.leg_id for leg in target_legs}
        changed_leg_ids.extend(leg.leg_id for leg in original_target_legs if leg.leg_id not in new_leg_ids)
        changed_leg_ids.extend(leg.leg_id for leg in target_legs if leg.leg_id not in old_leg_ids)
        non_target_legs = [leg for leg in pending_base.legs if leg.day_id != command.day_id]
        final_draft = PlanDraft.model_validate(pending_base.model_dump(mode="python") | {
            "legs": tuple((*non_target_legs, *target_legs)),
        })
        pending_leg_ids = tuple(
            leg.leg_id for leg in target_legs
            if any(result.status == "pending" for result in leg.mode_results)
        )
        inherited_days = set(inherited_pending_day_ids) - {command.day_id}
        pending_leg_ids = tuple(dict.fromkeys((*pending_leg_ids, *(
            leg.leg_id for leg in final_draft.legs
            if leg.day_id in inherited_days
            and any(result.status == "pending" for result in leg.mode_results)
        ))))

        report = validate_daily_plan(
            preview.planning_input,
            final_draft,
            preview.facts,
            preview.candidates,
            repair_states=preview.validation.repair_states,
        )
        calculation = PreviewCalculation(
            based_on_revision=revision,
            calculation_id=f"edit:{draft.plan_id}:{command.command_id}",
            current_routes=("pending" if pending_leg_ids else "failed" if any(
                result.status == "error" for leg in final_draft.legs for result in leg.mode_results
            ) else "completed"),
            alternative_backfill="not_required",
            pending_leg_ids=pending_leg_ids,
        )
        updated_preview = ItineraryPreview(
            preview_id=preview.preview_id,
            planning_input=preview.planning_input,
            draft=final_draft,
            facts=preview.facts,
            candidates=preview.candidates,
            validation=report,
            day_summaries=build_day_summaries(
                preview.planning_input, final_draft, preview.facts,
            ),
            calculation=calculation,
        )
        try:
            validate_preview(updated_preview)
        except ContractValidationError as exc:
            raise PlanningV4EditError("edited_preview_invalid") from exc
        return EditExecution(
            preview=updated_preview,
            changed_activity_ids=tuple(dict.fromkeys(changed_ids)),
            changed_leg_ids=tuple(dict.fromkeys(changed_leg_ids)),
            pending_leg_ids=pending_leg_ids,
        )

    @staticmethod
    def _new_activity(
        preview: ItineraryPreview,
        command: EditCommand,
        operation: AddActivity,
        candidates: dict[str, CandidateRef],
        places: dict[str, object],
        slot_by_id: dict[str, object],
        *,
        existing_activity_ids: set[str],
        current_activity_ids: set[str],
        used_client_refs: set[str],
    ) -> tuple[DraftActivity, str]:
        if operation.client_ref in used_client_refs:
            raise PlanningV4EditError("duplicate_client_ref")
        if operation.client_ref in existing_activity_ids or operation.client_ref in current_activity_ids:
            raise PlanningV4EditError("client_ref_conflicts_with_activity_id")
        candidate = candidates.get(operation.candidate_ref)
        slot = slot_by_id.get(operation.slot_id)
        if candidate is None:
            raise PlanningV4EditError("added_candidate_missing")
        if slot is None or slot.eligibility == "skipped":
            raise PlanningV4EditError("added_slot_invalid")
        if slot.period in _MEAL_PERIODS:
            expected_category = "meal"
        elif slot.period in _SIGHTSEEING_PERIODS:
            expected_category = "sightseeing"
        else:  # pragma: no cover - SlotConstraint closes the period enum.
            raise PlanningV4EditError("added_slot_period_invalid")
        if candidate.category != expected_category:
            raise PlanningV4EditError("candidate_category_mismatch")
        place = places.get(candidate.place_fact_id)
        if (
            place is None
            or place.poi_id != candidate.poi_id
            or place.location is None
            or not place.source_refs
        ):
            raise PlanningV4EditError("candidate_place_fact_invalid")

        activity_id = f"activity:{uuid5(NAMESPACE_URL, '|'.join((preview.draft.plan_id, command.command_id, operation.client_ref)))}"
        if activity_id in existing_activity_ids or activity_id in current_activity_ids:
            raise PlanningV4EditError("generated_activity_id_conflict")
        activity = DraftActivity(
            activity_id=activity_id,
            candidate_ref=candidate.candidate_id,
            slot_id=operation.slot_id,
            kind=expected_category,
            poi_id=candidate.poi_id,
            place_fact_id=candidate.place_fact_id,
            activity_source="user",
            requirement_ids=candidate.requirement_ids,
            recommendation_reason=candidate.recommendation_reason,
        )
        return activity, activity_id


def _leg_signature(leg: RouteLeg) -> tuple[str, str, str, str, str]:
    return (
        leg.day_id,
        leg.from_endpoint.kind,
        leg.from_endpoint.reference_id,
        leg.to_endpoint.kind,
        leg.to_endpoint.reference_id,
    )


def _departure_fact_id(draft: PlanDraft) -> str | None:
    departure_ids = {
        endpoint.reference_id
        for leg in draft.legs
        for endpoint in (leg.from_endpoint, leg.to_endpoint)
        if endpoint.kind == "departure"
    }
    return next(iter(departure_ids)) if len(departure_ids) == 1 else None


def _unfilled_slots(day_id, activities, slots, candidates, places) -> tuple[str, ...]:
    covered: set[str] = set()
    for activity in activities:
        if activity.kind == "free_time":
            continue
        slot = next((item for item in slots if item.slot_id == activity.slot_id), None)
        candidate = candidates.get(activity.candidate_ref or "")
        place = places.get(activity.place_fact_id or "")
        if slot is None or slot.eligibility == "skipped" or candidate is None or place is None:
            continue
        expected = "meal" if slot.period in _MEAL_PERIODS else "sightseeing"
        if (
            activity.kind == expected
            and candidate.category == expected
            and candidate.poi_id == activity.poi_id
            and candidate.place_fact_id == activity.place_fact_id
            and place.poi_id == activity.poi_id
            and place.location is not None
        ):
            covered.add(slot.slot_id)
    return tuple(
        slot.slot_id for slot in slots
        if slot.eligibility != "skipped" and slot.slot_id not in covered
    )
