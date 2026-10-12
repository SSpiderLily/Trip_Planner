"""S06 实际相邻查询集合、参考时刻、同源方案与缓存生命周期回归。"""
import unittest
from collections import Counter

from app.models.planning_v4 import LegEndpoint, LodgingRef
from app.services.current_route_service import (
    CurrentRouteError, CurrentRouteService, _pending_leg,
)
from app.services.mcp_tool_executor import MCPToolExecutor
from app.services.planning_v4_pipeline import PlanningV4Pipeline
from app.services.route_fact_cache import RouteFactCache
from test_current_route_service import (
    FakeMapTool, TOOL_BY_MODE, make_facts, make_plan, make_planning_input,
)
from test_planning_v4_pipeline import (
    FakeDailyPlanAdapter, FakeDispatch, FakeInterpreter, FakeRepair,
    endpoint_fixture, request_fixture,
)


def request_pairs(calls):
    return Counter(
        (next(mode for mode, name in TOOL_BY_MODE.items() if name == tool),
         args.get("origin", args.get("origin_coordinates")),
         args.get("destination", args.get("destination_coordinates")))
        for tool, args in calls
    )


class GeometryMapTool(FakeMapTool):
    def __call__(self, tool_name, arguments):
        raw = super().__call__(tool_name, arguments)
        mode = next(mode for mode, name in TOOL_BY_MODE.items() if name == tool_name)
        # 两条方案用不同折线，最短方案与第一条不同，防止混用首条路径。
        slow_steps = [{"polyline": "121.47,31.23;121.49,31.24"}]
        fast_steps = [{"polyline": "121.47,31.23;121.48,31.23"}]
        if mode == "transit":
            raw["route"]["transits"] = [
                {"duration": "1300.5", "segments": [{"walking": {"steps": slow_steps}}]},
                {"duration": "1000.5", "segments": [{"walking": {"steps": fast_steps}}]},
            ]
        else:
            duration = "600.25" if mode == "walking" else "800.75"
            raw["data" if mode == "bicycling" else "route"]["paths"] = [
                {"duration": "1500.5", "steps": slow_steps},
                {"duration": duration, "steps": fast_steps},
            ]
        return raw


class SimplifiedRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.input = make_planning_input()
        self.draft = make_plan(self.input)
        self.facts = make_facts()
        self.tool = FakeMapTool(delay=0)
        self.executor = MCPToolExecutor(self.tool)
        self.service = CurrentRouteService(tool_executor=self.executor)
        self.context = self.service.create_route_context(self.input, self.draft.plan_id)

    async def asyncTearDown(self):
        await self.context.aclose()
        self.executor.close()

    async def query(self, *, draft=None, facts=None, **kwargs):
        return await self.service.run_async(
            self.input, draft or self.draft, facts or self.facts,
            route_context=self.context,
            verified_departure_fact_id="fact-terminal", **kwargs,
        )

    async def test_request_set_is_exactly_current_directed_adjacent_pairs_times_three_modes(self):
        result = await self.query()
        expected_pairs = (
            ("121.47,31.23", "121.475,31.23"),
            ("121.47,31.23", "121.48,31.23"),
            ("121.48,31.23", "121.485,31.23"),
            ("121.47,31.23", "121.49,31.23"),
        )
        self.assertEqual(request_pairs(self.tool.calls), Counter(
            (mode, origin, destination)
            for origin, destination in expected_pairs for mode in TOOL_BY_MODE
        ))
        self.assertEqual(len(result.draft.legs), 4)
        self.assertFalse(any(leg.to_endpoint.kind == "departure" for leg in result.draft.legs))
        self.assertNotIn("timing", result.model_dump())

    async def test_reordered_day_requests_only_new_neighbors_and_preserves_direction(self):
        await self.query()
        self.tool.calls.clear()
        middle = self.draft.days[1]
        reordered = middle.model_copy(update={"activities": tuple(reversed(middle.activities))})
        draft = self.draft.model_copy(update={
            "revision": self.draft.revision + 1, "base_revision": self.draft.revision,
            "days": tuple(reordered if day.day_id == middle.day_id else day for day in self.draft.days),
        })
        await self.query(draft=draft, only_day_id=middle.day_id)
        self.assertEqual(request_pairs(self.tool.calls), Counter(
            (mode, origin, destination)
            for origin, destination in (
                ("121.47,31.23", "121.485,31.23"),
                ("121.485,31.23", "121.48,31.23"),
            ) for mode in TOOL_BY_MODE
        ))

    async def test_repeat_call_hits_shared_cache_without_extra_queries(self):
        first = await self.query()
        second = await self.query(draft=first.draft, facts=first.fact_store)
        self.assertEqual(len(self.tool.calls), 12)
        self.assertEqual(first.draft.legs, second.draft.legs)
        self.assertEqual(first.fact_store.routes, second.fact_store.routes)

    async def test_cache_expiry_reloads_only_expired_modes(self):
        now = [0.0]
        self.context.route_cache = RouteFactCache(
            self.draft.plan_id, self.context._load, clock=lambda: now[0],
        )
        await self.query()
        now[0] = 900.0
        await self.query()
        self.assertEqual(len(self.tool.calls), 16)
        self.assertTrue(all(tool == TOOL_BY_MODE["transit"] for tool, _ in self.tool.calls[12:]))
        now[0] = 3600.0
        await self.query()
        self.assertEqual(len(self.tool.calls), 28)

    async def test_failed_mode_is_not_cached_and_does_not_requery_successful_modes(self):
        self.tool.fail_modes.add("transit")
        first = await self.query()
        second = await self.query()
        self.assertEqual(len(self.tool.calls), 16)
        self.assertTrue(all(tool == TOOL_BY_MODE["transit"] for tool, _ in self.tool.calls[12:]))
        self.assertTrue(all(leg.selected_mode == "bicycling" for leg in second.draft.legs))
        self.assertTrue(all(next(mode for mode in leg.mode_results if mode.mode == "transit").status == "error"
                            for leg in first.draft.legs))

    async def test_missing_lodging_does_not_invent_origin_and_still_queries_activity_neighbors(self):
        middle = self.draft.days[1]
        unknown = middle.model_copy(update={"lodging": LodgingRef(selection_source="unknown")})
        draft = self.draft.model_copy(update={"days": tuple(
            unknown if day.day_id == middle.day_id else day for day in self.draft.days
        )})
        result = await self.query(draft=draft, only_day_id=middle.day_id)
        self.assertEqual(request_pairs(self.tool.calls), Counter(
            (mode, "121.48,31.23", "121.485,31.23") for mode in TOOL_BY_MODE
        ))
        self.assertTrue(any(outcome.status == "unknown" and outcome.reason == "lodging_endpoint_missing"
                            for outcome in result.leg_results))
        self.assertFalse(any(leg.from_endpoint.kind == "lodging" and leg.day_id == middle.day_id
                             for leg in result.draft.legs))

    async def test_same_place_repeated_activity_skips_self_route(self):
        middle = self.draft.days[1]
        repeated = middle.activities[0].model_copy(update={"activity_id": "same-place-second-activity"})
        day = middle.model_copy(update={"activities": (middle.activities[0], repeated, middle.activities[1])})
        draft = self.draft.model_copy(update={"days": tuple(
            day if d.day_id == middle.day_id else d for d in self.draft.days
        )})
        result = await self.query(draft=draft, only_day_id=middle.day_id)
        self.assertEqual(len(self.tool.calls), 6)
        self.assertFalse(any(origin == destination for _, origin, destination in request_pairs(self.tool.calls)))
        self.assertTrue(any(leg.selection_source == "same_place" for leg in result.draft.legs))

    async def test_non_adjacent_live_query_is_rejected_before_any_tool_call(self):
        middle = self.draft.days[1]
        with self.assertRaisesRegex(CurrentRouteError, "requested_live_edge_not_unique"):
            await self.query(only_edge=(middle.day_id, f"lodging:{middle.day_id}", middle.activities[1].activity_id))
        self.assertFalse(self.tool.calls)

    async def test_preexisting_return_leg_cannot_enter_current_query_set(self):
        last = self.draft.days[-1]
        return_leg = _pending_leg(
            "discarded-return-leg", self.draft.revision, last.day_id,
            LegEndpoint(kind="activity", reference_id=last.activities[-1].activity_id, poi_id="poi-last"),
            LegEndpoint(kind="departure", reference_id="fact-terminal", poi_id="poi-terminal"),
        )
        result = await self.query(draft=self.draft.model_copy(update={"legs": (return_leg,)}))
        self.assertEqual(len(self.tool.calls), 12)
        self.assertFalse(any(leg.leg_id == return_leg.leg_id for leg in result.draft.legs))

    async def test_empty_departure_day_does_not_query_lodging_to_terminal(self):
        last = self.draft.days[-1].model_copy(update={"activities": ()})
        draft = self.draft.model_copy(update={"days": tuple(
            last if day.day_id == last.day_id else day for day in self.draft.days
        )})
        result = await self.query(draft=draft, only_day_id=last.day_id)
        self.assertFalse(self.tool.calls)
        self.assertFalse(any(leg.day_id == last.day_id for leg in result.draft.legs))

    async def test_public_transport_uses_explicit_daily_reference_without_propagating_durations(self):
        self.tool.durations["transit"] = 36000
        result = await self.query()
        calls = [args for tool, args in self.tool.calls if tool == TOOL_BY_MODE["transit"]]
        self.assertEqual({call["time"] for call in calls}, {"09:00"})
        self.assertEqual({call["date"] for call in calls}, {"2026-10-10", "2026-10-11", "2026-10-12"})
        self.assertTrue(all(leg.selected_mode == "bicycling" for leg in result.draft.legs))
        self.assertTrue(all(leg.time_basis == "reference" for leg in result.draft.legs))

    async def test_fastest_duration_and_geometry_bind_the_same_fact_and_option(self):
        executor = MCPToolExecutor(GeometryMapTool(delay=0))
        try:
            result = await CurrentRouteService(tool_executor=executor).run_async(self.input, self.draft, self.facts)
            for leg in result.draft.legs:
                self.assertEqual(leg.selected_mode, "walking")
                binding = next(mode for mode in leg.mode_results if mode.mode == leg.selected_mode)
                fact = next(fact for fact in result.fact_store.routes if fact.fact_id == binding.route_fact_id)
                option = next(option for option in fact.options if option.option_id == leg.selected_option_id)
                self.assertEqual(option.duration_s, 600.25)
                self.assertNotEqual(option.option_id, fact.options[0].option_id)
                self.assertNotEqual(option.segments, fact.options[0].segments)
                self.assertTrue(option.segments[0].polyline)
                outcome = next(item for item in result.leg_results if item.leg_id == leg.leg_id)
                selected = next(mode for mode in outcome.modes if mode.mode == "walking")
                self.assertEqual(selected.route_fact_id, fact.fact_id)
                self.assertEqual(selected.duration_s, option.duration_s)
                self.assertEqual(selected.geometry_status, option.geometry_status)
        finally:
            executor.close()

    async def test_production_pipeline_retains_cache_without_starting_matrix(self):
        request = request_fixture()
        facts, endpoint = endpoint_fixture(request)
        tracker = {}
        pipeline = PlanningV4Pipeline(
            requirement_interpreter_factory=lambda _: FakeInterpreter(tracker),
            dispatch_service_factory=lambda _: FakeDispatch(tracker, facts, endpoint),
            daily_plan_adapter_factory=lambda _: FakeDailyPlanAdapter(tracker),
            current_route_service_factory=lambda: self.service,
            day_repair_service_factory=lambda **callbacks: FakeRepair(tracker, **callbacks),
        )
        result = await pipeline.run(request, task_id="pipeline-task", plan_id="plan-s06", progress=lambda _: None)
        try:
            self.assertIsNone(result.backfill)
            self.assertEqual(result.snapshot.preview.calculation.alternative_backfill, "not_required")
            self.assertIsNotNone(result.route_context)
            self.assertFalse(result.route_context.cancel_event.is_set())
            self.assertFalse(self.tool.calls)
            self.assertNotIn("backfill_revisions", tracker)
        finally:
            await result.route_context.aclose()


if __name__ == "__main__":
    unittest.main()
