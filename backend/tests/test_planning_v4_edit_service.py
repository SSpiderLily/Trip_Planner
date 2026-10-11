"""Planning v4 纯日编辑服务的离线回归。"""
from datetime import datetime
import unittest

from app.models.planning_v4 import (
    EditCommand,
    FactSource,
    PreviewCalculation,
    RouteFact,
    RouteModeResult,
    RouteOption,
)
from app.models.planning_v4.plan import (
    AddActivity,
    DeleteActivity,
    ReorderActivities,
    SelectLegMode,
)
from app.services.current_route_service import _select_leg
from app.services.planning_v4_edit_service import PlanningV4EditError, PlanningV4EditService
from app.services.planning_v4_timing import calculate_plan_timing
from app.services.planning_v4_validation import build_day_summaries, validate_daily_plan
from app.services.planning_v4_validator import validate_preview
from app.services.current_route_service import build_pending_route_legs_for_day
from test_planning_v4_contract import make_full_baseline_preview


def _prepare_preview(preview, *, availability_fact_id=None):
    planning_input, facts, candidates = preview.planning_input, preview.facts, preview.candidates
    draft = preview.draft
    legs = []
    for day in draft.days:
        legs.extend(build_pending_route_legs_for_day(
            planning_input, draft, facts, day.day_id,
        ))
    if availability_fact_id is not None:
        route = next(item for item in facts.routes if item.fact_id == availability_fact_id)
        updated_legs = []
        for leg in legs:
            if (
                leg.day_id == "synthetic-day-2"
                and leg.from_endpoint.poi_id == route.origin_poi_id
                and leg.to_endpoint.poi_id == route.destination_poi_id
                and leg.from_endpoint.reference_id == "lodging:synthetic-day-2"
            ):
                option = route.options[0]
                leg = leg.model_copy(update={
                    "mode_results": (RouteModeResult(
                        mode=route.mode, status="available", route_fact_id=route.fact_id,
                        option_ids=(option.option_id,),
                    ),),
                    "selected_mode": route.mode,
                    "selected_option_id": option.option_id,
                    "selection_source": "backend_fastest_available",
                })
            updated_legs.append(leg)
        legs = updated_legs
    draft = draft.model_copy(update={"legs": tuple(legs)})
    return _with_recalculated_report(preview, draft=draft, facts=facts)


def _with_recalculated_report(preview, *, draft=None, facts=None):
    draft = draft or preview.draft
    facts = facts or preview.facts
    timing = calculate_plan_timing(
        preview.planning_input, draft, facts,
        verified_departure_fact_id=next((
            endpoint.reference_id
            for leg in draft.legs
            for endpoint in (leg.from_endpoint, leg.to_endpoint)
            if endpoint.kind == "departure"
        ), None),
    )
    report = validate_daily_plan(
        preview.planning_input, draft, facts, preview.candidates, timing,
        repair_states=preview.validation.repair_states,
    )
    pending_ids = tuple(
        leg.leg_id for leg in draft.legs
        if any(result.status == "pending" for result in leg.mode_results)
    )
    calculation = PreviewCalculation(
        based_on_revision=draft.revision,
        calculation_id="test-recalculation",
        current_routes="pending" if pending_ids else "completed",
        alternative_backfill="pending",
        pending_leg_ids=pending_ids,
    )
    prepared = preview.model_copy(update={
        "draft": draft,
        "facts": facts,
        "validation": report,
        "day_summaries": build_day_summaries(preview.planning_input, draft, timing),
        "calculation": calculation,
    })
    validate_preview(prepared)
    return prepared


def _with_day2_afternoon_missing():
    _, _, _, _, preview = make_full_baseline_preview()
    day_id = "synthetic-day-2"
    slot_id = "synthetic-2-afternoon"
    original_day = next(day for day in preview.draft.days if day.day_id == day_id)
    trimmed_day = original_day.model_copy(update={
        "activities": tuple(item for item in original_day.activities if item.slot_id != slot_id),
        "unfilled_slot_ids": tuple(dict.fromkeys((*original_day.unfilled_slot_ids, slot_id))),
    })
    draft = preview.draft.model_copy(update={
        "days": tuple(trimmed_day if day.day_id == day_id else day for day in preview.draft.days),
    })
    return _prepare_preview(preview.model_copy(update={"draft": draft})), preview.candidates


def _add_candidate_with_search_scope(preview, candidates):
    candidate = candidates[0].model_copy(update={
        "candidate_id": "manual-bund-morning-search",
        "slot_ids": ("synthetic-2-morning",),
    })
    return preview.model_copy(update={
        "candidates": (*preview.candidates, candidate),
    }), candidate


def _command(preview, *operations, command_id="edit-command", day_id="synthetic-day-2"):
    return EditCommand(
        plan_id=preview.draft.plan_id,
        day_id=day_id,
        base_revision=preview.draft.revision,
        command_id=command_id,
        operations=tuple(operations),
    )


class PlanningV4EditServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = PlanningV4EditService()

    def test_add_same_verified_poi_to_second_slot_and_pending_only_target_day_routes(self):
        preview, candidates = _with_day2_afternoon_missing()
        preview, candidate = _add_candidate_with_search_scope(preview, candidates)
        target = "synthetic-day-2"
        command = _command(
            preview,
            AddActivity(
                type="add_activity", client_ref="new-bund-afternoon",
                candidate_ref=candidate.candidate_id,
                slot_id="synthetic-2-afternoon", day_id=target,
            ),
        )

        result = self.service.apply(preview, command)
        updated = result.preview
        day = next(item for item in updated.draft.days if item.day_id == target)
        repeated = [item for item in day.activities if item.poi_id == candidate.poi_id]

        self.assertEqual(len(repeated), 2)  # 同一POI在上午和下午保留为两个活动。
        self.assertEqual(len({item.activity_id for item in repeated}), 2)
        added = next(item for item in repeated if item.activity_source == "user")
        self.assertEqual(added.slot_id, "synthetic-2-afternoon")
        self.assertEqual(added.duration_s, preview.planning_input.rules.sightseeing_default_s)
        self.assertEqual(added.duration_source, "default")
        self.assertLess(
            next(index for index, item in enumerate(day.activities) if item.slot_id == "synthetic-2-afternoon"),
            next(index for index, item in enumerate(day.activities) if item.slot_id == "synthetic-2-dinner"),
        )
        self.assertEqual(candidate.slot_ids, ("synthetic-2-morning",))
        self.assertGreater(updated.draft.revision, preview.draft.revision)
        self.assertEqual(updated.calculation.current_routes, "pending")
        self.assertTrue(result.pending_leg_ids)
        self.assertTrue(all(
            leg.day_id == target
            for leg in updated.draft.legs
            if leg.leg_id in updated.calculation.pending_leg_ids
        ))
        self.assertTrue(all(
            leg.plan_revision == updated.draft.revision
            for leg in updated.draft.legs
        ))
        validate_preview(updated)

    def test_empty_day_remains_empty_and_reported_without_automatic_refill(self):
        _, _, _, _, preview = make_full_baseline_preview()
        preview = _prepare_preview(preview)
        day = next(item for item in preview.draft.days if item.day_id == "synthetic-day-1")
        command = _command(
            preview,
            *(DeleteActivity(
                type="remove_activity", day_id=day.day_id, activity_id=activity.activity_id,
            ) for activity in day.activities),
            command_id="delete-day-one",
            day_id=day.day_id,
        )

        updated = self.service.apply(preview, command).preview
        empty = next(item for item in updated.draft.days if item.day_id == day.day_id)
        report = next(item for item in updated.validation.days if item.day_id == day.day_id)

        self.assertEqual(empty.activities, ())
        self.assertTrue(empty.unfilled_slot_ids)
        self.assertEqual(report.completeness, "incomplete")
        self.assertFalse(updated.calculation.pending_leg_ids)
        self.assertNotEqual(updated.draft.days[1].activities, ())
        validate_preview(updated)

    def test_reorder_applies_exact_target_day_order_and_preserves_other_days(self):
        _, _, _, _, preview = make_full_baseline_preview()
        preview = _prepare_preview(preview)
        day = next(item for item in preview.draft.days if item.day_id == "synthetic-day-2")
        requested = tuple(activity.activity_id for activity in reversed(day.activities))
        other_days_before = {
            other.day_id: other.model_dump(mode="json")
            for other in preview.draft.days if other.day_id != day.day_id
        }
        command = _command(
            preview,
            ReorderActivities(
                type="reorder_activities", day_id=day.day_id,
                activity_ids=requested,
            ),
        )

        updated = self.service.apply(preview, command).preview
        reordered = next(item for item in updated.draft.days if item.day_id == day.day_id)

        self.assertEqual(
            tuple(activity.activity_id for activity in reordered.activities),
            requested,
        )
        self.assertEqual(
            {
                other.day_id: other.model_dump(mode="json")
                for other in updated.draft.days if other.day_id != day.day_id
            },
            other_days_before,
        )
        validate_preview(updated)

    def test_selected_mode_becomes_preference_until_target_day_recalculation(self):
        planning_input, facts, candidates, _, preview = make_full_baseline_preview()
        day = next(item for item in preview.draft.days if item.day_id == "synthetic-day-2")
        # 该固定夹具的可用路线是 hotel -> morning；去掉更早的早餐活动，使其成为当前拓扑边。
        trimmed_day = day.model_copy(update={
            "activities": tuple(
                item for item in day.activities if item.slot_id != "synthetic-2-breakfast"
            ),
            "unfilled_slot_ids": tuple((*day.unfilled_slot_ids, "synthetic-2-breakfast")),
        })
        draft = preview.draft.model_copy(update={
            "days": tuple(trimmed_day if item.day_id == day.day_id else item for item in preview.draft.days),
        })
        preview = _prepare_preview(preview.model_copy(update={"draft": draft}), availability_fact_id="synthetic-route-fact")
        leg = next(item for item in preview.draft.legs if item.day_id == "synthetic-day-2")
        command = _command(
            preview,
            SelectLegMode(
                type="select_leg_mode", day_id=leg.day_id, leg_id=leg.leg_id,
                mode="transit", option_id="synthetic-route-option",
            ),
        )

        execution = self.service.apply(preview, command)
        updated = execution.preview
        self.assertEqual(execution.changed_leg_ids, (leg.leg_id,))
        self.assertEqual(execution.changed_activity_ids, ())
        pending = next(item for item in updated.draft.legs if item.day_id == leg.day_id)

        self.assertIsNone(pending.selected_mode)
        self.assertIsNone(pending.selected_option_id)
        self.assertEqual(pending.preferred_mode, "transit")
        self.assertEqual(pending.selection_source, "user")
        self.assertIn(pending.leg_id, updated.calculation.pending_leg_ids)

        source = FactSource(
            tool_call_id="test-walking-route", tool_name="maps_direction_walking_integrated_by_coordinates",
            api_version="v3", queried_at=datetime.fromisoformat("2026-10-11T00:00:00+00:00"),
            effective_params={},
        )
        walking_fact = RouteFact(
            fact_id="test-walking-fact",
            origin_poi_id=pending.from_endpoint.poi_id,
            destination_poi_id=pending.to_endpoint.poi_id,
            origin=next(place.location for place in preview.facts.places if place.poi_id == pending.from_endpoint.poi_id),
            destination=next(place.location for place in preview.facts.places if place.poi_id == pending.to_endpoint.poi_id),
            mode="walking", status="available",
            options=(RouteOption(option_id="test-walk-option", duration_s=300, geometry_status="missing"),),
            source=source,
        )
        queried = pending.model_copy(update={
            "mode_results": (RouteModeResult(
                mode="walking", status="available", route_fact_id=walking_fact.fact_id,
                option_ids=("test-walk-option",),
            ),),
        })
        selected = _select_leg(pending, queried, {"walking": walking_fact})
        self.assertEqual(selected.selected_mode, "walking")
        self.assertEqual(selected.preferred_mode, "transit")
        self.assertEqual(selected.selection_source, "backend_fastest_available")

        transit_fact = next(route for route in preview.facts.routes if route.mode == "transit")
        queried_with_transit = pending.model_copy(update={
            "mode_results": (
                RouteModeResult(mode="transit", status="available", route_fact_id=transit_fact.fact_id,
                                option_ids=("synthetic-route-option",)),
                RouteModeResult(mode="walking", status="available", route_fact_id=walking_fact.fact_id,
                                option_ids=("test-walk-option",)),
            ),
        })
        selected_again = _select_leg(
            pending, queried_with_transit,
            {"transit": transit_fact, "walking": walking_fact},
        )
        self.assertEqual(selected_again.selected_mode, "transit")
        self.assertEqual(selected_again.preferred_mode, "transit")
        self.assertEqual(selected_again.selection_source, "user")

    def test_select_leg_mode_rejects_available_option_without_duration(self):
        _, _, _, _, preview = make_full_baseline_preview()
        day = next(item for item in preview.draft.days if item.day_id == "synthetic-day-2")
        trimmed_day = day.model_copy(update={
            "activities": tuple(
                item for item in day.activities if item.slot_id != "synthetic-2-breakfast"
            ),
            "unfilled_slot_ids": tuple((*day.unfilled_slot_ids, "synthetic-2-breakfast")),
        })
        draft = preview.draft.model_copy(update={
            "days": tuple(trimmed_day if item.day_id == day.day_id else item for item in preview.draft.days),
        })
        preview = _prepare_preview(
            preview.model_copy(update={"draft": draft}), availability_fact_id="synthetic-route-fact",
        )
        leg = next(item for item in preview.draft.legs if item.day_id == day.day_id)
        route = next(item for item in preview.facts.routes if item.fact_id == "synthetic-route-fact")
        unknown_duration_route = route.model_copy(update={
            "options": (route.options[0].model_copy(update={"duration_s": None}),),
        })
        facts_without_duration = preview.facts.model_copy(update={
            "routes": tuple(
                unknown_duration_route if item.fact_id == route.fact_id else item
                for item in preview.facts.routes
            ),
        })
        preview = _with_recalculated_report(preview, facts=facts_without_duration)
        command = _command(
            preview,
            SelectLegMode(
                type="select_leg_mode", day_id=day.day_id, leg_id=leg.leg_id,
                mode="transit", option_id="synthetic-route-option",
            ),
        )
        with self.assertRaisesRegex(PlanningV4EditError, "selected_option_duration_unknown"):
            self.service.apply(preview, command)

    def test_invalid_edit_is_atomic_and_cross_day_command_is_rejected(self):
        _, _, _, _, preview = make_full_baseline_preview()
        day = next(item for item in preview.draft.days if item.day_id == "synthetic-day-2")
        before = preview.model_dump(mode="json")
        invalid = _command(
            preview,
            ReorderActivities(
                type="reorder_activities", day_id=day.day_id,
                activity_ids=(day.activities[0].activity_id,),
            ),
        )
        with self.assertRaises(PlanningV4EditError):
            self.service.apply(preview, invalid)
        self.assertEqual(preview.model_dump(mode="json"), before)

        with self.assertRaises(ValueError):
            EditCommand(
                plan_id=preview.draft.plan_id, day_id=day.day_id,
                base_revision=preview.draft.revision, command_id="cross-day",
                operations=(DeleteActivity(
                    type="remove_activity", day_id="synthetic-day-1",
                    activity_id="synthetic-activity-1-lunch",
                ),),
            )


if __name__ == "__main__":
    unittest.main()
