"""F08 当前相邻路线查询的离线集成测试。"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date, datetime
import threading
import time
import unittest

from app.models.planning_v4.facts import (
    FactSource,
    FactStore,
    MapCoordinate,
    PlaceFact,
    RouteFact,
    RouteOption,
)
from app.models.planning_v4.input import RequirementSummary, RuleSet, TripRequest
from app.models.planning_v4.plan import DraftActivity, DraftDay, LodgingRef, PlanDraft
from app.services.current_route_service import (
    CurrentRouteError,
    CurrentRouteService,
    PlanRouteContext,
    query_current_routes,
)
from app.services.endpoint_verification_service import (
    EndpointVerification,
    EndpointVerificationExecution,
)
from app.services.mcp_tool_executor import MCPToolExecutor
from app.services.planning_input_builder import PlanningInputBuildRequest, build_planning_input
from app.services.route_backfill_service import RouteBackfillService


WINDOWS = (
    {"period": "breakfast", "start_minute": 8 * 60, "end_minute": 9 * 60},
    {"period": "morning", "start_minute": 9 * 60, "end_minute": 12 * 60},
    {"period": "lunch", "start_minute": 12 * 60, "end_minute": 14 * 60},
    {"period": "afternoon", "start_minute": 14 * 60, "end_minute": 17 * 60},
    {"period": "dinner", "start_minute": 17 * 60, "end_minute": 19 * 60},
    {"period": "evening", "start_minute": 19 * 60, "end_minute": 22 * 60},
)
TOOL_BY_MODE = {
    "transit": "maps_direction_transit_integrated_by_coordinates",
    "bicycling": "maps_bicycling_by_coordinates",
    "walking": "maps_direction_walking_by_coordinates",
}


def make_planning_input(*, ready_at: str | None = None):
    request = TripRequest(
        request_id="current-route-request",
        city="上海",
        arrival_at=datetime.fromisoformat("2026-10-10T08:00:00+08:00"),
        departure_at=datetime.fromisoformat("2026-10-12T19:00:00+08:00"),
        timezone="Asia/Shanghai",
        departure_place_query="上海虹桥站",
        lodging_query="测试酒店",
    )
    return build_planning_input(PlanningInputBuildRequest(
        request=request,
        requirements=RequirementSummary(
            requirements_id="current-route-requirements",
            request_id=request.request_id,
            core_goals=("城市漫步",),
        ),
        rules=RuleSet(
            rule_version="current-route-rules-v1",
            windows=WINDOWS,
            meal_default_s=1800,
            sightseeing_default_s=600,
            full_day_start_minute=8 * 60,
            return_buffer_default_s=3600,
            departure_day_minimum_usable_s=3 * 3600,
        ),
        day_ready_at=datetime.fromisoformat(ready_at) if ready_at else None,
    ))


def make_plan(planning_input):
    activity_specs = {
        date(2026, 10, 10): (("afternoon", "arrival-poi", "poi-arrival", "fact-arrival", "sightseeing"),),
        date(2026, 10, 11): (
            ("breakfast", "middle-one", "poi-middle-one", "fact-middle-one", "sightseeing"),
            ("morning", "middle-two", "poi-middle-two", "fact-middle-two", "sightseeing"),
        ),
        date(2026, 10, 12): (("breakfast", "last-poi", "poi-last", "fact-last", "sightseeing"),),
    }
    draft_days = []
    for constraint in planning_input.days:
        activities = []
        for period, activity_id, poi_id, fact_id, kind in activity_specs[constraint.day_date]:
            slot = next(item for item in constraint.slots if item.period == period)
            activities.append(DraftActivity(
                activity_id=activity_id,
                candidate_ref=f"candidate-{activity_id}",
                slot_id=slot.slot_id,
                kind=kind,
                poi_id=poi_id,
                place_fact_id=fact_id,
                duration_s=600,
                duration_source="user",
            ))
        draft_days.append(DraftDay(
            day_id=constraint.day_id,
            day_date=constraint.day_date,
            activities=tuple(activities),
            lodging=LodgingRef(
                candidate_ref="candidate-hotel",
                poi_id="poi-hotel",
                place_fact_id="fact-hotel",
                selection_source="user",
            ),
        ))
    return PlanDraft(
        plan_id="current-route-plan",
        request_id=planning_input.request.request_id,
        source_run_id="current-route-run",
        revision=4,
        base_revision=3,
        rule_version=planning_input.rules.rule_version,
        days=tuple(draft_days),
    )


def make_facts():
    source = FactSource(
        tool_call_id="offline-place-source",
        tool_name="offline_fixture",
        api_version="v4",
        queried_at=datetime.fromisoformat("2026-10-09T12:00:00+08:00"),
        effective_params={},
    )
    places = []
    place_specs = (
        ("fact-hotel", "poi-hotel", "测试酒店", 121.470),
        ("fact-arrival", "poi-arrival", "到达景点", 121.475),
        ("fact-middle-one", "poi-middle-one", "中段景点一", 121.480),
        ("fact-middle-two", "poi-middle-two", "中段景点二", 121.485),
        ("fact-last", "poi-last", "末段景点", 121.490),
        ("fact-terminal", "poi-terminal", "上海虹桥站", 121.495),
    )
    for fact_id, poi_id, name, lng in place_specs:
        places.append(PlaceFact(
            fact_id=fact_id,
            poi_id=poi_id,
            name=name,
            city="上海",
            location=MapCoordinate(lng=lng, lat=31.230),
            source_refs=(source.tool_call_id,),
        ))
    return FactStore(sources=(source,), places=tuple(places))


class FakeMapTool:
    """返回 vendor 适配器能够读取的脱敏离线响应。"""

    def __init__(self, *, fail_modes=(), empty_modes=(), delay=0.015):
        self.fail_modes = set(fail_modes)
        self.empty_modes = set(empty_modes)
        self.durations = {"transit": 600, "bicycling": 900, "walking": 1200}
        self.wrong_params_modes: set[str] = set()
        self.delay = delay
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, tool_name: str, arguments: dict):
        mode = next(key for key, value in TOOL_BY_MODE.items() if value == tool_name)
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.calls.append((tool_name, deepcopy(arguments)))
        try:
            time.sleep(self.delay)
            origin = arguments.get("origin", arguments.get("origin_coordinates"))
            destination = arguments.get("destination", arguments.get("destination_coordinates"))
            effective = {"origin": origin, "destination": destination}
            api_version = "v4" if mode == "bicycling" else "v3"
            if mode == "transit":
                effective.update({
                    "city": arguments["city"],
                    "cityd": arguments["cityd"],
                    "extensions": "base",
                    "date": arguments["date"],
                    "time": arguments["time"],
                    "strategy": arguments["strategy"],
                })
            if mode in self.wrong_params_modes:
                effective["destination"] = "121.000000,31.000000"
            meta = {
                "api_version": api_version,
                "queried_at": "2026-10-11T00:00:00Z",
                "effective_params": effective,
            }
            if mode in self.fail_modes:
                return {
                    "result_status": "error",
                    "result_error": {"category": "provider_error"},
                    "result_source": meta,
                }
            if mode in self.empty_modes:
                payload = {"route": {"transits": []}} if mode == "transit" else (
                    {"data": {"paths": []}} if mode == "bicycling" else {"route": {"paths": []}}
                )
                return {**payload, "result_status": "empty", "result_source": meta}
            duration = self.durations[mode]
            route_row = {"duration": str(duration), "segments": []} if mode == "transit" else {
                "duration": str(duration), "steps": [],
            }
            payload = {"route": {"transits": [route_row]}} if mode == "transit" else (
                {"data": {"paths": [route_row]}} if mode == "bicycling" else {"route": {"paths": [route_row]}}
            )
            return {**payload, "result_status": "ok", "result_source": meta}
        finally:
            with self.lock:
                self.active -= 1


def make_endpoint_execution(planning_input, draft, facts, *, context_revision=7, base_revision=None):
    source = facts.sources[0]
    terminal = next(place for place in facts.places if place.fact_id == "fact-terminal").model_copy(update={
        "field_sources": {
            "poi_id": source.tool_call_id,
            "name": source.tool_call_id,
            "location": source.tool_call_id,
            "city": source.tool_call_id,
        },
    })
    execution_facts = FactStore(sources=(source,), places=(terminal,))
    departure = EndpointVerification(
        kind="departure",
        user_query=planning_input.request.departure_place_query,
        status="verified",
        reason="exact_name_city_and_coordinate",
        fact_id=terminal.fact_id,
        candidates=(terminal,),
    )
    return EndpointVerificationExecution(
        request_id=planning_input.request.request_id,
        run_id=draft.source_run_id,
        context_revision=context_revision,
        base_revision=base_revision,
        city=planning_input.request.city,
        arrival=EndpointVerification(
            kind="arrival", user_query=None, status="missing", reason="query_not_provided",
        ),
        departure=departure,
        fact_store=execution_facts,
    )


class CurrentRouteServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.input = make_planning_input()
        self.draft = make_plan(self.input)
        self.facts = make_facts()
        self.input_before = self.input.model_dump(mode="json")
        self.draft_before = self.draft.model_dump(mode="json")
        self.facts_before = self.facts.model_dump(mode="json")
        self.fake_tool = FakeMapTool()
        self.executor = MCPToolExecutor(self.fake_tool)

    async def asyncTearDown(self):
        self.executor.close()

    async def test_current_route_order_time_progression_reference_and_return(self):
        result = await query_current_routes(
            self.input, self.draft, self.facts,
            verified_departure_fact_id="fact-terminal",
            tool_executor=self.executor,
            tool_timeout_seconds=1,
        )

        route_pairs = [
            (leg.from_endpoint.poi_id, leg.to_endpoint.poi_id)
            for leg in result.draft.legs
        ]
        self.assertEqual(route_pairs, [
            ("poi-hotel", "poi-arrival"),
            ("poi-hotel", "poi-middle-one"),
            ("poi-middle-one", "poi-middle-two"),
            ("poi-hotel", "poi-last"),
            ("poi-last", "poi-terminal"),
        ])
        self.assertTrue(all(leg.plan_revision == self.draft.revision for leg in result.draft.legs))
        self.assertEqual(len(result.fact_store.routes), 15)

        self.assertLessEqual(self.fake_tool.peak, 4)
        self.assertEqual(self.fake_tool.peak, 4)

        transit_calls = [arguments for name, arguments in self.fake_tool.calls
                         if name == TOOL_BY_MODE["transit"]]
        date_two_calls = [call for call in transit_calls if call["origin"] == "121.47,31.23"
                          and call["date"] == "2026-10-11"]
        self.assertEqual(len(date_two_calls), 1)
        next_leg_call = next(call for call in transit_calls
                             if call["origin"] == "121.48,31.23" and call["date"] == "2026-10-11")
        self.assertEqual(date_two_calls[0]["time"], "08:00")
        self.assertEqual(next_leg_call["time"], "08:20")
        self.assertEqual(date_two_calls[0]["strategy"], "0")
        return_call = next(call for call in transit_calls
                           if call["origin"] == "121.49,31.23" and call["destination"] == "121.495,31.23")
        self.assertEqual(return_call["time"], "08:20")

        legs_by_end = {leg.to_endpoint.reference_id: leg for leg in result.draft.legs}
        computed = legs_by_end["middle-one"]
        self.assertEqual(computed.selected_mode, "transit")
        self.assertEqual(computed.time_basis, "computed")
        selected_result = next(item for item in computed.mode_results if item.mode == computed.selected_mode)
        selected_fact = next(fact for fact in result.fact_store.routes
                             if fact.fact_id == selected_result.route_fact_id)
        selected_option = next(option for option in selected_fact.options
                               if option.option_id == computed.selected_option_id)
        reported_mode = next(item for item in result.leg_results
                             if item.leg_id == computed.leg_id)
        reported_transit = next(item for item in reported_mode.modes if item.mode == "transit")
        self.assertEqual(reported_transit.route_fact_id, selected_fact.fact_id)
        self.assertEqual(reported_transit.duration_s, selected_option.duration_s)
        self.assertEqual(reported_transit.geometry_status, selected_option.geometry_status)
        reference = legs_by_end["arrival-poi"]
        self.assertEqual(reference.selected_mode, "transit")
        self.assertEqual(reference.time_basis, "reference")
        return_leg = legs_by_end["fact-terminal"]
        self.assertEqual(return_leg.time_basis, "computed")
        departure_timing = next(day for day in result.timing.days if day.day_id == "day-2026-10-12")
        self.assertEqual(departure_timing.return_timing.status, "verified")
        self.assertEqual(departure_timing.return_timing.travel_s, 600)
        self.assertEqual(result.based_on_revision, self.draft.revision)

        self.assertEqual(self.input.model_dump(mode="json"), self.input_before)
        self.assertEqual(self.draft.model_dump(mode="json"), self.draft_before)
        self.assertEqual(self.facts.model_dump(mode="json"), self.facts_before)

    async def test_only_day_requeries_target_and_preserves_other_day_routes(self):
        full = await query_current_routes(
            self.input, self.draft, self.facts,
            verified_departure_fact_id="fact-terminal",
            tool_executor=self.executor,
            tool_timeout_seconds=1,
        )
        target_day_id = "day-2026-10-11"
        preserved = {
            leg.leg_id: leg for leg in full.draft.legs if leg.day_id != target_day_id
        }
        self.fake_tool.calls.clear()

        scoped = await query_current_routes(
            self.input, full.draft, full.fact_store,
            verified_departure_fact_id="fact-terminal",
            tool_executor=self.executor,
            tool_timeout_seconds=1,
            only_day_id=target_day_id,
        )

        self.assertTrue(self.fake_tool.calls)
        self.assertTrue(all(
            outcome.day_id == target_day_id for outcome in scoped.leg_results
        ))
        scoped_other_legs = {
            leg.leg_id: leg for leg in scoped.draft.legs if leg.day_id != target_day_id
        }
        self.assertEqual(scoped_other_legs, preserved)
        self.assertLess(len(self.fake_tool.calls), 15)

    async def test_only_day_keeps_shared_route_context_and_rebases_other_legs(self):
        route_service = CurrentRouteService(
            tool_executor=self.executor, tool_timeout_seconds=1,
        )
        context = route_service.create_route_context(self.input, self.draft.plan_id)
        try:
            full = await route_service.run_async(
                self.input, self.draft, self.facts,
                verified_departure_fact_id="fact-terminal",
                route_context=context,
            )
            target_day_id = "day-2026-10-11"
            new_revision = full.draft.revision + 1
            revised = full.draft.model_copy(update={
                "revision": new_revision,
                "base_revision": full.draft.revision,
                "legs": tuple(
                    leg.model_copy(update={"plan_revision": new_revision})
                    for leg in full.draft.legs
                ),
            })
            preserved = {
                leg.leg_id: leg.model_copy(update={"plan_revision": new_revision})
                for leg in revised.legs if leg.day_id != target_day_id
            }

            scoped = await route_service.run_async(
                self.input, revised, full.fact_store,
                verified_departure_fact_id="fact-terminal",
                route_context=context,
                only_day_id=target_day_id,
            )

            scoped_other_legs = {
                leg.leg_id: leg for leg in scoped.draft.legs if leg.day_id != target_day_id
            }
            self.assertEqual(scoped_other_legs, preserved)
            self.assertTrue(scoped.leg_results)
            self.assertTrue(all(item.day_id == target_day_id for item in scoped.leg_results))
            self.assertEqual(scoped.based_on_revision, new_revision)
            self.assertFalse(context.route_cache._closed)
        finally:
            await context.aclose()

    async def test_failed_mode_is_retained_while_other_modes_remain_usable(self):
        self.fake_tool.fail_modes.add("walking")
        result = await query_current_routes(
            self.input, self.draft, self.facts,
            verified_departure_fact_id="fact-terminal",
            tool_executor=self.executor,
            tool_timeout_seconds=1,
        )
        leg = next(item for item in result.draft.legs if item.to_endpoint.reference_id == "middle-one")
        statuses = {item.mode: item.status for item in leg.mode_results}
        self.assertEqual(statuses, {"transit": "available", "bicycling": "available", "walking": "error"})
        self.assertEqual(leg.selected_mode, "transit")
        self.assertEqual(len(result.fact_store.routes), 15)
        self.assertEqual(result.status, "partial")

    async def test_free_time_keeps_previous_poi_as_route_endpoint(self):
        day = self.draft.days[1]
        free_slot = next(slot for slot in self.input.days[1].slots if slot.period == "lunch")
        free_time = DraftActivity(
            activity_id="free-break",
            candidate_ref=None,
            slot_id=free_slot.slot_id,
            kind="free_time",
            poi_id=None,
            place_fact_id=None,
            duration_s=1200,
            duration_source="user",
        )
        changed_day = day.model_copy(update={"activities": (day.activities[0], free_time, day.activities[1])})
        changed_draft = self.draft.model_copy(update={
            "days": (self.draft.days[0], changed_day, self.draft.days[2]),
        })
        result = await query_current_routes(
            self.input, changed_draft, self.facts,
            verified_departure_fact_id="fact-terminal",
            tool_executor=self.executor,
            tool_timeout_seconds=1,
        )
        middle_legs = [leg for leg in result.draft.legs if leg.day_id == day.day_id]
        self.assertEqual(len(middle_legs), 2)
        self.assertEqual(middle_legs[1].from_endpoint.reference_id, "middle-one")
        next_transit = next(arguments for name, arguments in self.fake_tool.calls
                            if name == TOOL_BY_MODE["transit"]
                            and arguments["origin"] == "121.48,31.23")
        self.assertEqual(next_transit["time"], "12:20")

    async def test_same_place_and_missing_coordinates_do_not_call_map_tool(self):
        same_place = PlaceFact(
            fact_id="fact-copy-hotel",
            poi_id="poi-hotel",
            name="测试酒店",
            city="上海",
            location=MapCoordinate(lng=121.470, lat=31.230),
            source_refs=("offline-place-source",),
        )
        same_activity = self.draft.days[1].activities[0].model_copy(update={
            "poi_id": "poi-hotel", "place_fact_id": "fact-copy-hotel",
        })
        changed_day = self.draft.days[1].model_copy(update={"activities": (same_activity,)})
        changed_draft = self.draft.model_copy(update={
            "days": (self.draft.days[0], changed_day, self.draft.days[2]),
        })
        facts = self.facts.model_copy(update={"places": (*self.facts.places, same_place)})
        result = await query_current_routes(
            self.input, changed_draft, facts,
            verified_departure_fact_id="fact-terminal",
            tool_executor=self.executor,
            tool_timeout_seconds=1,
        )
        same_leg = next(leg for leg in result.draft.legs if leg.day_id == changed_day.day_id)
        self.assertEqual(same_leg.selection_source, "same_place")
        self.assertEqual(
            next(item.status for item in result.leg_results if item.leg_id == same_leg.leg_id),
            "same_place",
        )
        self.assertFalse(any(
            arguments.get("origin", arguments.get("origin_coordinates"))
            == arguments.get("destination", arguments.get("destination_coordinates"))
            for _, arguments in self.fake_tool.calls
        ))

        no_coord_fact = next(place for place in self.facts.places if place.fact_id == "fact-middle-two")
        no_coord_fact = no_coord_fact.model_copy(update={"location": None})
        no_coord_store = self.facts.model_copy(update={
            "places": tuple(no_coord_fact if p.fact_id == no_coord_fact.fact_id else p for p in self.facts.places),
        })
        self.fake_tool.calls.clear()
        result = await query_current_routes(
            self.input, self.draft, no_coord_store,
            verified_departure_fact_id="fact-terminal",
            tool_executor=self.executor,
            tool_timeout_seconds=1,
        )
        missing_leg = next(leg for leg in result.draft.legs if leg.to_endpoint.reference_id == "middle-two")
        self.assertEqual(missing_leg.mode_results[0].status, "pending")
        self.assertFalse(any(arguments.get("destination") == "121.485,31.23"
                             for _, arguments in self.fake_tool.calls))

    async def test_verified_execution_binds_run_revision_context_and_original_query(self):
        execution = make_endpoint_execution(
            self.input, self.draft, self.facts,
            context_revision=7, base_revision=self.draft.revision,
        )
        facts_without_terminal = self.facts.model_copy(update={
            "places": tuple(place for place in self.facts.places if place.fact_id != "fact-terminal"),
        })
        result = await query_current_routes(
            self.input, self.draft, facts_without_terminal,
            endpoint_execution=execution,
            current_context_revision=7,
            tool_executor=self.executor,
            tool_timeout_seconds=1,
        )
        self.assertEqual(result.draft.legs[-1].to_endpoint.poi_id, "poi-terminal")
        self.assertIn("fact-terminal", {place.fact_id for place in result.fact_store.places})

    async def test_stale_endpoint_execution_is_rejected_before_any_tool_call(self):
        execution = make_endpoint_execution(
            self.input, self.draft, self.facts,
            context_revision=7, base_revision=self.draft.revision,
        )
        facts_without_terminal = self.facts.model_copy(update={
            "places": tuple(place for place in self.facts.places if place.fact_id != "fact-terminal"),
        })
        with self.assertRaises(CurrentRouteError):
            await query_current_routes(
                self.input, self.draft, facts_without_terminal,
                endpoint_execution=execution,
                current_context_revision=8,
                tool_executor=self.executor,
                tool_timeout_seconds=1,
            )
        self.assertEqual(self.fake_tool.calls, [])

        wrong_run = execution.model_copy(update={"run_id": "an-older-run"})
        with self.assertRaises(CurrentRouteError):
            await query_current_routes(
                self.input, self.draft, facts_without_terminal,
                endpoint_execution=wrong_run,
                current_context_revision=7,
                tool_executor=self.executor,
                tool_timeout_seconds=1,
            )
        wrong_base = execution.model_copy(update={"base_revision": self.draft.revision - 1})
        with self.assertRaises(CurrentRouteError):
            await query_current_routes(
                self.input, self.draft, facts_without_terminal,
                endpoint_execution=wrong_base,
                current_context_revision=7,
                tool_executor=self.executor,
                tool_timeout_seconds=1,
            )
        self.assertEqual(self.fake_tool.calls, [])

        changed_request = self.input.request.model_copy(update={"departure_place_query": "上海南站"})
        changed_input = self.input.model_copy(update={"request": changed_request})
        with self.assertRaises(CurrentRouteError):
            await query_current_routes(
                changed_input, self.draft, facts_without_terminal,
                endpoint_execution=execution,
                current_context_revision=7,
                tool_executor=self.executor,
                tool_timeout_seconds=1,
            )
        self.assertEqual(self.fake_tool.calls, [])

    async def test_empty_mode_is_retained_without_blocking_valid_selection(self):
        self.fake_tool.empty_modes.add("transit")
        result = await query_current_routes(
            self.input, self.draft, self.facts,
            verified_departure_fact_id="fact-terminal",
            tool_executor=self.executor,
            tool_timeout_seconds=1,
        )
        leg = next(item for item in result.draft.legs if item.to_endpoint.reference_id == "middle-one")
        statuses = {item.mode: item.status for item in leg.mode_results}
        self.assertEqual(statuses, {"transit": "empty", "bicycling": "available", "walking": "available"})
        self.assertEqual(leg.selected_mode, "bicycling")
        self.assertEqual(len(result.fact_store.routes), 15)

    async def test_ties_use_stable_mode_order_and_user_selection_wins_when_valid(self):
        self.fake_tool.durations = {"transit": 600, "bicycling": 600, "walking": 600}
        tie_result = await query_current_routes(
            self.input, self.draft, self.facts,
            verified_departure_fact_id="fact-terminal",
            tool_executor=self.executor,
            tool_timeout_seconds=1,
        )
        tie_leg = next(item for item in tie_result.draft.legs if item.to_endpoint.reference_id == "middle-one")
        self.assertEqual(tie_leg.selected_mode, "transit")

        user_leg = tie_leg.model_copy(update={"selected_mode": "walking"})
        user_leg = user_leg.model_copy(update={
            "selected_option_id": next(item.option_ids[0] for item in user_leg.mode_results if item.mode == "walking"),
            "selection_source": "user",
        })
        user_draft = tie_result.draft.model_copy(update={
            "legs": tuple(user_leg if item.leg_id == user_leg.leg_id else item
                          for item in tie_result.draft.legs),
        })
        user_result = await query_current_routes(
            self.input, user_draft, tie_result.fact_store,
            verified_departure_fact_id="fact-terminal",
            tool_executor=self.executor,
            tool_timeout_seconds=1,
        )
        user_selected = next(item for item in user_result.draft.legs if item.leg_id == user_leg.leg_id)
        self.assertEqual(user_selected.selected_mode, "walking")
        self.assertEqual(user_selected.selection_source, "user")

    async def test_source_parameter_mismatch_is_rejected_without_dropping_existing_facts(self):
        self.fake_tool.wrong_params_modes.add("walking")
        source = self.facts.sources[0].model_copy(update={
            "tool_call_id": "existing-route-source",
            "tool_name": TOOL_BY_MODE["walking"],
            "api_version": "v3",
            "effective_params": {"origin": "121.47,31.23", "destination": "121.475,31.23"},
        })
        existing_route = RouteFact(
            fact_id="existing-useful-route",
            origin_poi_id="poi-hotel",
            destination_poi_id="poi-arrival",
            origin=MapCoordinate(lng=121.470, lat=31.230),
            destination=MapCoordinate(lng=121.475, lat=31.230),
            mode="walking",
            status="available",
            options=(RouteOption(
                option_id="existing-option",
                duration_s=1200,
                geometry_status="missing",
            ),),
            source=source,
        )
        facts_with_existing = self.facts.model_copy(update={
            "sources": (*self.facts.sources, source),
            "routes": (existing_route,),
        })
        result = await query_current_routes(
            self.input, self.draft, facts_with_existing,
            verified_departure_fact_id="fact-terminal",
            tool_executor=self.executor,
            tool_timeout_seconds=1,
        )
        target = next(item for item in result.draft.legs if item.to_endpoint.reference_id == "arrival-poi")
        walking = next(item for item in target.mode_results if item.mode == "walking")
        self.assertEqual(walking.status, "error")
        self.assertEqual(walking.error_category, "invalid_response")
        self.assertIn("existing-useful-route", {fact.fact_id for fact in result.fact_store.routes})

    async def test_cancel_stops_queued_calls_and_propagates(self):
        self.fake_tool.delay = 0.05
        task = asyncio.create_task(query_current_routes(
            self.input, self.draft, self.facts,
            verified_departure_fact_id="fact-terminal",
            tool_executor=self.executor,
            tool_timeout_seconds=2,
        ))
        await asyncio.sleep(0.005)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.08)
        self.assertLess(len(self.fake_tool.calls), 15)
        self.assertLessEqual(self.fake_tool.peak, 4)

    async def test_only_one_requested_live_edge_is_queried(self):
        result = await query_current_routes(
            self.input, self.draft, self.facts,
            verified_departure_fact_id="fact-terminal",
            tool_executor=self.executor,
            tool_timeout_seconds=1,
            only_edge=("day-2026-10-11", "middle-one", "middle-two"),
        )
        self.assertEqual(len(self.fake_tool.calls), 3)
        selected = [item for item in result.leg_results if item.status == "queried"]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].from_reference_id, "middle-one")
        self.assertEqual(selected[0].to_reference_id, "middle-two")
        self.assertTrue(any(item.reason == "outside_selected_live_edge" for item in result.leg_results))

    async def test_endpoint_fact_without_user_query_is_rejected(self):
        no_departure_query = make_planning_input().model_copy(update={
            "request": make_planning_input().request.model_copy(update={"departure_place_query": None}),
        })
        with self.assertRaises(CurrentRouteError):
            await query_current_routes(
                no_departure_query, self.draft, self.facts,
                verified_departure_fact_id="fact-terminal",
                tool_executor=self.executor,
                tool_timeout_seconds=1,
            )

    def test_backfill_builds_distinct_directed_pairs_without_self_routes(self):
        service = RouteBackfillService()
        middle_day = self.draft.days[1]
        repeated = middle_day.activities[0].model_copy(update={
            "activity_id": "middle-one-repeat",
            "slot_id": self.input.days[1].slots[1].slot_id,
        })
        revised_day = middle_day.model_copy(update={
            "activities": (*middle_day.activities, repeated),
        })
        revised = self.draft.model_copy(update={
            "days": (self.draft.days[0], revised_day, self.draft.days[2]),
        })

        jobs = service._build_jobs(revised, self.facts)
        middle_jobs = [job for job in jobs if job.day_id == revised_day.day_id]
        pairs = {(job.origin.poi_id, job.destination.poi_id) for job in middle_jobs}
        self.assertEqual(len(middle_jobs), 6)
        self.assertEqual(pairs, {
            ("poi-hotel", "poi-middle-one"), ("poi-middle-one", "poi-hotel"),
            ("poi-hotel", "poi-middle-two"), ("poi-middle-two", "poi-hotel"),
            ("poi-middle-one", "poi-middle-two"), ("poi-middle-two", "poi-middle-one"),
        })
        self.assertTrue(all(origin != destination for origin, destination in pairs))

    async def test_backfill_reuses_route_context_and_loads_new_revision_pois(self):
        route_service = CurrentRouteService(
            tool_executor=self.executor, tool_timeout_seconds=1,
        )
        context = route_service.create_route_context(self.input, self.draft.plan_id)
        try:
            current = await route_service.run_async(
                self.input, self.draft, self.facts,
                verified_departure_fact_id="fact-terminal",
                current_context_revision=7,
                only_edge=("day-2026-10-11", "middle-one", "middle-two"),
                route_context=context,
            )
            self.assertEqual(len(self.fake_tool.calls), 3)
            self.assertEqual(current.based_on_revision, self.draft.revision)

            source = self.facts.sources[0]
            new_place = PlaceFact(
                fact_id="fact-middle-three",
                poi_id="poi-middle-three",
                name="中段景点三",
                city="上海",
                location=MapCoordinate(lng=121.500, lat=31.230),
                source_refs=(source.tool_call_id,),
            )
            revised_facts = self.facts.model_copy(update={
                "places": (*self.facts.places, new_place),
            })
            new_activity = DraftActivity(
                activity_id="middle-three",
                candidate_ref="candidate-middle-three",
                slot_id=self.input.days[1].slots[3].slot_id,
                kind="sightseeing",
                poi_id=new_place.poi_id,
                place_fact_id=new_place.fact_id,
                duration_s=600,
                duration_source="model",
            )
            revised_day = self.draft.days[1].model_copy(update={
                "activities": (*self.draft.days[1].activities, new_activity),
            })
            revised_draft = self.draft.model_copy(update={
                "revision": self.draft.revision + 1,
                "base_revision": self.draft.revision,
                "days": (revised_day,),
            })
            backfill = RouteBackfillService()
            result = await backfill.run_async(
                self.input, revised_draft, revised_facts, context,
            )

            # Twelve directed pairs in the edited day, three modes each. The
            # previously queried middle-one -> middle-two facts are reused.
            self.assertEqual(result.total_queries, 36)
            self.assertEqual(result.status, "complete")
            self.assertEqual(len(self.fake_tool.calls), 36)
            self.assertTrue(any(
                arguments.get("origin") == "121.5,31.23"
                or arguments.get("origin_coordinates") == "121.5,31.23"
                or arguments.get("destination") == "121.5,31.23"
                or arguments.get("destination_coordinates") == "121.5,31.23"
                for _, arguments in self.fake_tool.calls
            ))
            transit_calls = [
                arguments for tool, arguments in self.fake_tool.calls
                if tool == TOOL_BY_MODE["transit"]
                and arguments.get("origin") == "121.48,31.23"
                and arguments.get("destination") == "121.485,31.23"
            ]
            self.assertEqual(len(transit_calls), 1)
            self.assertEqual(transit_calls[0]["date"], "2026-10-11")
            self.assertEqual(transit_calls[0]["time"], "09:00")
            self.assertTrue(all(
                mode.time_basis == ("reference" if mode.mode == "transit" else "unspecified")
                for pair in result.pair_results for mode in pair.modes
            ))
        finally:
            await context.aclose()

    async def test_stale_backfill_late_result_is_cached_but_not_returned_to_preview(self):
        self.fake_tool.delay = 0.05
        route_service = CurrentRouteService(
            tool_executor=self.executor, tool_timeout_seconds=1,
        )
        context = route_service.create_route_context(self.input, self.draft.plan_id)
        latest_revision = [self.draft.revision]
        try:
            backfill = RouteBackfillService()
            task = asyncio.create_task(backfill.run_async(
                self.input,
                self.draft.model_copy(update={"days": (self.draft.days[1],)}),
                self.facts,
                context,
                current_revision=lambda: latest_revision[0],
            ))
            for _ in range(100):
                if self.fake_tool.calls:
                    break
                await asyncio.sleep(0.002)
            self.assertTrue(self.fake_tool.calls)
            latest_revision[0] += 1
            result = await task

            self.assertEqual(result.status, "cancelled")
            self.assertEqual(result.completed_queries, 0)
            self.assertEqual(result.route_facts, ())
            self.assertEqual(self.draft.revision, 4)
            self.assertEqual(self.draft.days[1].activities[0].activity_id, "middle-one")
            self.assertEqual(len(self.fake_tool.calls), 1)
            self.assertEqual(context.route_cache.stats.external_loads, 1)
        finally:
            await context.aclose()

    async def test_backfill_failure_reports_partial_without_changing_preview(self):
        self.fake_tool.fail_modes.update({"transit", "bicycling", "walking"})
        route_service = CurrentRouteService(
            tool_executor=self.executor, tool_timeout_seconds=1,
        )
        context = route_service.create_route_context(self.input, self.draft.plan_id)
        draft_before = self.draft.model_dump(mode="json")
        facts_before = self.facts.model_dump(mode="json")
        try:
            result = await RouteBackfillService().run_async(
                self.input,
                self.draft.model_copy(update={"days": (self.draft.days[1],)}),
                self.facts,
                context,
            )
            self.assertEqual(result.status, "partial")
            self.assertEqual(result.completed_queries, result.total_queries)
            self.assertTrue(all(
                mode.status == "error"
                for pair in result.pair_results for mode in pair.modes
            ))
            self.assertEqual(self.draft.model_dump(mode="json"), draft_before)
            self.assertEqual(self.facts.model_dump(mode="json"), facts_before)
        finally:
            await context.aclose()

    async def test_route_context_keeps_first_fact_source_on_same_key_conflict(self):
        route_service = CurrentRouteService(
            tool_executor=self.executor, tool_timeout_seconds=1,
        )
        context = route_service.create_route_context(self.input, self.draft.plan_id)
        try:
            context.register_facts(self.facts)
            original = next(place for place in self.facts.places if place.fact_id == "fact-middle-one")
            conflicting = original.model_copy(update={
                "fact_id": "fact-middle-one-alternate-source",
                "source_refs": ("alternate-source",),
            })
            context.register_facts(FactStore(places=(conflicting,)))
            resolved = context._resolve(
                original.poi_id,
                (float(original.location.lng), float(original.location.lat)),
            )
            self.assertEqual(context.alternative_source_count, 1)
            self.assertEqual(resolved.fact_id, original.fact_id)
            self.assertEqual(resolved.source_refs, original.source_refs)
        finally:
            await context.aclose()

    async def test_route_context_coordinate_change_uses_new_cache_key(self):
        route_service = CurrentRouteService(
            tool_executor=self.executor, tool_timeout_seconds=1,
        )
        context = route_service.create_route_context(self.input, self.draft.plan_id)
        first_day = self.draft.days[0]
        first_draft = self.draft.model_copy(update={"days": (first_day,)})
        original = next(place for place in self.facts.places if place.fact_id == "fact-arrival")
        moved = original.model_copy(update={
            "fact_id": "fact-arrival-reverified",
            "location": MapCoordinate(lng=121.476, lat=31.230),
        })
        moved_facts = self.facts.model_copy(update={
            "places": (*self.facts.places, moved),
        })
        revised_activity = first_day.activities[0].model_copy(update={
            "place_fact_id": moved.fact_id,
        })
        revised_day = first_day.model_copy(update={"activities": (revised_activity,)})
        revised_draft = first_draft.model_copy(update={
            "revision": first_draft.revision + 1,
            "base_revision": first_draft.revision,
            "days": (revised_day,),
        })
        try:
            first_result = await RouteBackfillService().run_async(
                self.input, first_draft, self.facts, context,
            )
            second_result = await RouteBackfillService().run_async(
                self.input, revised_draft, moved_facts, context,
            )
            self.assertEqual(first_result.total_queries, 6)
            self.assertEqual(second_result.total_queries, 6)
            self.assertEqual(len(self.fake_tool.calls), 12)
            self.assertEqual(context.alternative_source_count, 0)
            resolved = context._resolve("poi-arrival", (121.476, 31.23))
            self.assertEqual(resolved.fact_id, moved.fact_id)
            self.assertTrue(any(
                "121.476,31.23" in {
                    arguments.get("origin"), arguments.get("destination"),
                    arguments.get("origin_coordinates"), arguments.get("destination_coordinates"),
                }
                for _, arguments in self.fake_tool.calls
            ))
        finally:
            await context.aclose()


if __name__ == "__main__":
    unittest.main()
