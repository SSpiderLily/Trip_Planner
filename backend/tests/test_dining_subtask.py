from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from app.agents.attraction_subtask import (
    AttractionExecutionContext,
    AttractionSubtaskRunner,
)
from app.agents.dining_subtask import (
    DiningExecutionContext,
    DiningMealAreaBinding,
    DiningRestaurantRequirement,
    DiningSubtaskRunner,
    _is_specific_restaurant_name,
)
from app.models.planning_v4 import (
    CandidateRef,
    FactSource,
    FactStore,
    HardRequirement,
    MapCoordinate,
    PlaceFact,
    PlaceSearchData,
    RequirementSummary,
    RuleSet,
    ToolResult,
)
from app.models.planning_v4.input import RequirementSource
from app.models.planning_v4.plan import AreaHint
from app.models.planning_v4.tasks import Subtask, TaskDependency, TaskScope
from app.services.amap_contract_adapter import AmapContractAdapter
from app.services.mcp_tool_executor import MCPToolExecutor
from app.services.planning_input_builder import PlanningInputBuildRequest, build_planning_input
from app.services.planning_dispatch_service import PlanningDispatchService
from scripts.run_requirement_interpreter import shanghai_request


WINDOWS = tuple(
    {"period": period, "start_minute": start, "end_minute": end}
    for period, start, end in (
        ("breakfast", 8 * 60, 9 * 60),
        ("morning", 9 * 60, 12 * 60),
        ("lunch", 12 * 60, 14 * 60),
        ("afternoon", 14 * 60, 17 * 60),
        ("dinner", 17 * 60, 19 * 60),
        ("evening", 19 * 60, 22 * 60),
    )
)
MUST_VISIT = "必须去外滩"


def build_planning(request=None, requirements=()):
    request = request or shanghai_request()
    hard_requirements = tuple(requirements) or (
        HardRequirement(
            requirement_id="M1", kind="must_visit", text=MUST_VISIT,
            source=RequirementSource(field="remarks", quote=MUST_VISIT),
        ),
    )
    summary = RequirementSummary(
        requirements_id="dining-test-requirements",
        request_id=request.request_id,
        core_goals=("上海代表性景点", "城市漫步与美食", "尽量少折返"),
        hard_requirements=hard_requirements,
        preferences=request.preferences,
    )
    rules = RuleSet(
        rule_version="dining-test-rules-v1",
        windows=WINDOWS,
        full_day_start_minute=8 * 60,
        return_buffer_default_s=3600,
        departure_day_minimum_usable_s=10800,
    )
    return build_planning_input(PlanningInputBuildRequest(
        request=request, requirements=summary, rules=rules,
    ))


def source(source_id: str, tool_name="maps_text_search"):
    return FactSource(
        tool_call_id=source_id,
        provider="amap",
        tool_name=tool_name,
        api_version="v3",
        queried_at=datetime(2026, 10, 11, tzinfo=timezone.utc),
        effective_params={},
    )


def fact(
    fact_id: str,
    poi_id: str,
    name: str,
    location: MapCoordinate | None,
    source_ref: str,
    *,
    city="上海市",
    type_name="餐饮服务;中餐厅;本帮菜",
    typecode="050100",
    cost=None,
):
    values = dict(
        fact_id=fact_id,
        poi_id=poi_id,
        name=name,
        location=location,
        address="上海市黄浦区",
        city=city,
        type_name=type_name,
        typecode=typecode,
        source_refs=(source_ref,),
        field_sources={"poi_id": source_ref, "name": source_ref, "location": source_ref,
                       "city": source_ref, "type_name": source_ref, "typecode": source_ref},
    )
    if cost is not None:
        values["cost"] = cost
    return PlaceFact(**values)


def dining_context(
    slot_anchors: dict[str, tuple[str, str, float, float]],
    *,
    candidate_target=1,
    requirements=(),
    named_restaurants=(),
    raw_request=None,
):
    planning = build_planning(raw_request, requirements)
    slots = {slot.slot_id: slot for day in planning.days for slot in day.slots}
    place_facts = []
    sources = []
    hints = []
    meal_areas = []
    dependencies = []
    for index, (slot_id, (area_id, anchor_id, lng, lat)) in enumerate(slot_anchors.items(), start=1):
        ref = f"anchor-source-{index}"
        fact_id = f"anchor-fact-{index}"
        sources.append(source(ref))
        place_facts.append(fact(
            fact_id, anchor_id, f"来源锚点{index}", MapCoordinate(lng=lng, lat=lat), ref,
            type_name="风景名胜;城市广场", typecode="110000",
        ))
        hints.append(AreaHint(
            area_hint_id=area_id,
            label=f"锚点区域{index}",
            anchor_poi_ids=(anchor_id,),
            basis_fact_ids=(fact_id,),
            basis="provider_fact",
        ))
        meal_areas.append(DiningMealAreaBinding(
            slot_id=slot_id, area_hint_id=area_id, anchor_poi_id=anchor_id,
        ))
        dependencies.extend((
            TaskDependency(kind="area_hint", reference_id=area_id),
            TaskDependency(kind="fact", reference_id=fact_id),
        ))
    task = Subtask(
        task_id="dining-task",
        run_id="dining-run",
        context_revision=1,
        base_revision=1,
        role="dining",
        objective="为指定餐次挑选有来源的餐馆候选",
        scope=TaskScope(
            city=planning.request.city,
            day_ids=tuple(dict.fromkeys(slots[slot_id].day_id for slot_id in slot_anchors)),
            slot_ids=tuple(slot_anchors),
            area_hint_ids=tuple(hint.area_hint_id for hint in hints),
        ),
        dependencies=tuple(dependencies),
        known_fact_ids=tuple(item.fact_id for item in place_facts),
        requirement_ids=tuple(item.requirement_id for item in requirements),
        candidate_target=candidate_target,
        required_fields=("poi_id", "name", "location"),
        preferences=planning.request.preferences,
    )
    context = DiningExecutionContext(
        request_id=planning.request.request_id,
        run_id=task.run_id,
        context_revision=task.context_revision,
        base_revision=task.base_revision,
        planning_input=planning,
        fact_store=FactStore(places=tuple(place_facts), sources=tuple(sources)),
        area_hints=tuple(hints),
        meal_areas=tuple(meal_areas),
        must_eat_restaurants=tuple(named_restaurants),
    )
    return task, context


def active_meal_slots(count=2, period="dinner"):
    planning = build_planning()
    selected = [slot for day in planning.days for slot in day.slots
                if slot.period == period and slot.eligibility != "skipped"]
    return tuple(item.slot_id for item in selected[:count])


def action_search(slot_id, keywords="上海餐馆"):
    return json.dumps({"action": "search", "slot_id": slot_id, "keywords": keywords}, ensure_ascii=False)


def action_finish(selections):
    return json.dumps({"action": "finish", "selections": selections}, ensure_ascii=False)


def selection(poi_id, slot_ids=(), requirement_ids=()):
    return {
        "poi_id": poi_id,
        "slot_ids": list(slot_ids),
        "requirement_ids": list(requirement_ids),
        "recommendation_reason": "供应商已核实的餐馆候选",
    }


class FakeLLM:
    def __init__(self, replies, fail_at=None):
        self.replies = list(replies)
        self.calls = 0
        self.fail_at = fail_at
        self.messages = []

    def invoke_with_usage(self, messages, **kwargs):
        self.calls += 1
        self.messages.append(messages)
        if self.fail_at == self.calls:
            raise RuntimeError("provider output must not be persisted")
        return self.replies.pop(0), {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}


def vendor_result(tool_name, arguments, pois=()):
    return {
        "result_status": "ok" if pois else "empty",
        "count": str(len(pois)),
        "pois": list(pois),
        "result_source": {
            "api_version": "v3",
            "queried_at": "2026-10-11T00:00:00+00:00",
            "effective_params": dict(arguments),
        },
    }


def restaurant(poi_id, name, lng, lat, *, city="上海市", typecode="050100"):
    return {
        "id": poi_id,
        "name": name,
        "location": f"{lng},{lat}",
        "address": "上海市黄浦区",
        "cityname": city,
        "type": "餐饮服务;中餐厅;本帮菜",
        "typecode": typecode,
        # Cost is intentionally omitted to check that missing price does not
        # cause the backend to search a wider radius.
    }


class DiningSubtaskTests(unittest.TestCase):
    def test_different_meal_scopes_use_their_own_anchors(self):
        lunch, dinner = active_meal_slots(2, "dinner")
        task, context = dining_context({
            lunch: ("area-west", "anchor-west", 121.4900, 31.2410),
            dinner: ("area-east", "anchor-east", 121.5600, 31.2410),
        })
        calls = []

        def tools(tool_name, arguments):
            calls.append((tool_name, dict(arguments)))
            if arguments["location"].startswith("121.49"):
                rows = [restaurant("meal-west", "西侧餐馆", 121.491, 31.241)]
            else:
                rows = [restaurant("meal-east", "东侧餐馆", 121.561, 31.241)]
            return vendor_result(tool_name, arguments, rows)

        llm = FakeLLM([
            action_search(lunch),
            action_search(dinner),
            action_finish([selection("meal-west", (lunch,)), selection("meal-east", (dinner,))]),
        ])
        result = DiningSubtaskRunner(llm=llm, adapter=AmapContractAdapter(tool_runner=tools)).run(task, context)

        self.assertEqual(result.task_result.status, "complete")
        self.assertEqual({candidate.poi_id for candidate in result.task_result.candidates}, {"meal-west", "meal-east"})
        self.assertEqual([args["radius"] for _, args in calls], ["1000", "1000"])
        self.assertEqual([args["types"] for _, args in calls], ["050000", "050000"])
        self.assertTrue(all("city" not in args for _, args in calls))
        for candidate in result.task_result.candidates:
            self.assertEqual(len(candidate.slot_ids), 1)

    def test_one_restaurant_can_cover_two_nearby_meal_slots_and_cost_is_not_required(self):
        lunch, dinner = active_meal_slots(2, "dinner")
        task, context = dining_context({
            lunch: ("area-one", "anchor-one", 121.4900, 31.2410),
            dinner: ("area-two", "anchor-two", 121.4930, 31.2410),
        })
        calls = []

        def tools(tool_name, arguments):
            calls.append(dict(arguments))
            return vendor_result(tool_name, arguments, [restaurant("same-place", "同一家餐馆", 121.4910, 31.2410)])

        llm = FakeLLM([
            action_search(lunch),
            action_search(dinner),
            action_finish([selection("same-place", (lunch, dinner))]),
        ])
        result = DiningSubtaskRunner(llm=llm, adapter=AmapContractAdapter(tool_runner=tools)).run(task, context)

        self.assertEqual(result.task_result.status, "complete")
        self.assertEqual(len(result.task_result.candidates), 1)
        self.assertEqual(set(result.task_result.candidates[0].slot_ids), {lunch, dinner})
        self.assertEqual([args["radius"] for args in calls], ["1000", "1000"])
        restaurant_fact = next(place for place in result.fact_store.places if place.poi_id == "same-place")
        self.assertIsNone(restaurant_fact.cost.raw)

    def test_search_expands_only_after_no_eligible_candidate_and_continues_other_slot(self):
        lunch, dinner = active_meal_slots(2, "dinner")
        task, context = dining_context({
            lunch: ("area-one", "anchor-one", 121.4900, 31.2410),
            dinner: ("area-two", "anchor-two", 121.5600, 31.2410),
        })
        calls = []

        def tools(tool_name, arguments):
            calls.append(dict(arguments))
            if arguments["location"].startswith("121.49"):
                # No restaurants within either permitted search radius.
                return vendor_result(tool_name, arguments)
            return vendor_result(tool_name, arguments, [restaurant("meal-two", "第二餐馆", 121.561, 31.241)])

        llm = FakeLLM([
            action_search(lunch),
            action_search(dinner),
            action_finish([selection("meal-two", (dinner,))]),
        ])
        result = DiningSubtaskRunner(llm=llm, adapter=AmapContractAdapter(tool_runner=tools)).run(task, context)

        self.assertEqual([args["radius"] for args in calls], ["1000", "3000", "1000"])
        self.assertEqual(result.task_result.status, "partial")
        self.assertEqual(result.task_result.stop_reason, "scope_exhausted")
        self.assertEqual(
            {coverage.target_id: coverage.status for coverage in result.task_result.coverage if coverage.target_id.startswith("slot:")},
            {f"slot:{lunch}": "missing", f"slot:{dinner}": "covered"},
        )

    def test_missing_coordinate_uses_source_checked_poi_detail_without_radius_expansion(self):
        dinner = active_meal_slots(1, "dinner")[0]
        task, context = dining_context({
            dinner: ("area-dinner", "anchor-dinner", 121.4900, 31.2410),
        })
        calls = []

        def tools(tool_name, arguments):
            calls.append((tool_name, dict(arguments)))
            if tool_name == "maps_search_detail":
                self.assertEqual(arguments["id"], "detail-meal")
                row = restaurant("detail-meal", "详情补坐标餐馆", 121.491, 31.241)
                return vendor_result(tool_name, arguments, [row])
            row = restaurant("detail-meal", "详情补坐标餐馆", 121.491, 31.241)
            row.pop("location")
            return vendor_result(tool_name, arguments, [row])

        llm = FakeLLM([
            action_search(dinner),
            action_finish([selection("detail-meal", (dinner,))]),
        ])
        result = DiningSubtaskRunner(llm=llm, adapter=AmapContractAdapter(tool_runner=tools)).run(task, context)

        self.assertEqual(result.task_result.status, "complete")
        self.assertEqual([name for name, _ in calls], ["maps_around_search", "maps_search_detail"])
        self.assertEqual(calls[0][1]["radius"], "1000")
        self.assertTrue(any(place.poi_id == "detail-meal" and place.location is not None
                            for place in result.fact_store.places))

    def test_skipped_breakfast_slot_is_rejected_before_model_or_tool_call(self):
        skipped = "slot-2026-10-10-breakfast"
        task, context = dining_context({
            skipped: ("area-breakfast", "anchor-breakfast", 121.4900, 31.2410),
        })
        llm = FakeLLM([])
        runner = DiningSubtaskRunner(llm=llm)
        with self.assertRaisesRegex(Exception, "餐饮任务不能使用 skipped 时段"):
            runner._validate_preflight(task, context)
        self.assertEqual(llm.calls, 0)

    def test_partial_result_keeps_candidate_unassigned_and_preserves_stop_reason(self):
        dinner = active_meal_slots(1, "dinner")[0]
        task, context = dining_context({
            dinner: ("area-dinner", "anchor-dinner", 121.4900, 31.2410),
        })

        def tools(tool_name, arguments):
            return vendor_result(tool_name, arguments, [restaurant("meal-one", "餐馆一", 121.491, 31.241)])

        llm = FakeLLM([action_search(dinner)], fail_at=2)
        result = DiningSubtaskRunner(llm=llm, adapter=AmapContractAdapter(tool_runner=tools)).run(task, context)

        self.assertEqual(result.task_result.status, "partial")
        self.assertEqual(result.task_result.stop_reason, "model_error")
        self.assertEqual(len(result.task_result.candidates), 1)
        self.assertEqual(result.task_result.candidates[0].slot_ids, ())
        self.assertEqual(result.task_result.coverage[0].status, "missing")

    def test_reusing_candidate_with_updated_reason_returns_new_stable_identity(self):
        dinner = active_meal_slots(1, "dinner")[0]
        task, context = dining_context({
            dinner: ("area-dinner", "anchor-dinner", 121.4900, 31.2410),
        })
        restaurant_source = source("existing-meal-source", tool_name="maps_around_search")
        restaurant_fact = fact(
            "existing-meal-fact", "meal-one", "餐馆一",
            MapCoordinate(lng=121.491, lat=31.241), restaurant_source.tool_call_id,
        )
        old_candidate = CandidateRef(
            candidate_id="existing-meal-candidate", category="meal", poi_id=restaurant_fact.poi_id,
            place_fact_id=restaurant_fact.fact_id, slot_ids=(dinner,),
            recommendation_reason="旧推荐理由",
        )
        context = replace(
            context,
            fact_store=FactStore(
                sources=(*context.fact_store.sources, restaurant_source),
                places=(*context.fact_store.places, restaurant_fact),
            ),
            candidate_refs=(old_candidate,),
        )
        llm = FakeLLM([action_finish([{
            "poi_id": restaurant_fact.poi_id,
            "slot_ids": [dinner],
            "recommendation_reason": "更新后的餐次推荐理由",
        }])])

        result = DiningSubtaskRunner(
            llm=llm, adapter=AmapContractAdapter(tool_runner=lambda *_: self.fail("must reuse")),
        ).run(task, context)

        updated = next(item for item in result.task_result.candidates if item.poi_id == restaurant_fact.poi_id)
        self.assertNotEqual(updated.candidate_id, old_candidate.candidate_id)
        self.assertEqual(updated.recommendation_reason, "更新后的餐次推荐理由")
        self.assertEqual(context.candidate_refs, (old_candidate,), "不能原地改写旧候选版本")
        merged = PlanningDispatchService._merge_candidates((old_candidate,), (updated,))
        self.assertEqual({item.candidate_id for item in merged}, {old_candidate.candidate_id, updated.candidate_id})

    def test_candidate_target_is_a_distinct_candidate_count_contract(self):
        dinner = active_meal_slots(1, "dinner")[0]
        task, context = dining_context({
            dinner: ("area-dinner", "anchor-dinner", 121.4900, 31.2410),
        }, candidate_target=2)

        def tools(tool_name, arguments):
            return vendor_result(tool_name, arguments, [restaurant("meal-one", "餐馆一", 121.491, 31.241)])

        llm = FakeLLM([
            action_search(dinner),
            action_finish([selection("meal-one", (dinner,))]),
        ])
        result = DiningSubtaskRunner(llm=llm, adapter=AmapContractAdapter(tool_runner=tools)).run(task, context)
        status_by_target = {item.target_id: item.status for item in result.task_result.coverage}

        self.assertEqual(result.task_result.status, "partial")
        self.assertEqual(status_by_target[f"slot:{dinner}"], "covered")
        self.assertEqual(status_by_target["candidate_target"], "missing")

    def test_specific_named_restaurant_does_not_need_marker_words_and_ambiguity_clears_binding(self):
        raw_request = shanghai_request().model_copy(update={
            "remarks": shanghai_request().remarks + " 晚餐选老正兴菜馆。",
        })
        requirement = HardRequirement(
            requirement_id="R1", kind="must_include", text="晚餐选老正兴菜馆",
            source=RequirementSource(field="remarks", quote="晚餐选老正兴菜馆"),
        )
        self.assertTrue(_is_specific_restaurant_name("老正兴菜馆", requirement))
        named = DiningRestaurantRequirement(requirement_id="R1", restaurant_name="老正兴菜馆")
        task, context = dining_context(
            {}, requirements=(requirement,), named_restaurants=(named,), raw_request=raw_request,
        )
        call_log = []

        def tools(tool_name, arguments):
            call_log.append((tool_name, dict(arguments)))
            return vendor_result(tool_name, arguments, [
                restaurant("venue-one", "老正兴菜馆", 121.49, 31.24),
                restaurant("venue-two", "老正兴菜馆", 121.50, 31.24),
            ])

        llm = FakeLLM([
            json.dumps({"action": "verify_restaurant", "requirement_id": "R1"}),
            action_finish([selection("venue-one", requirement_ids=("R1",))]),
        ])
        execution = DiningSubtaskRunner(llm=llm, adapter=AmapContractAdapter(tool_runner=tools)).run(task, context)
        result = execution.task_result

        self.assertEqual(result.status, "partial")
        self.assertEqual(result.stop_reason, "ambiguous")
        self.assertEqual(result.candidates[0].requirement_ids, ())
        self.assertEqual(next(item.status for item in result.coverage if item.target_id == "R1"), "unknown")
        self.assertEqual([name for name, _ in call_log], ["maps_text_search"])

    def test_other_role_derived_fact_is_preserved_but_not_used_as_provider_input(self):
        root_source = source("raw-anchor")
        attraction_raw_source = source("attraction-provider")
        foreign_source = source("attraction-composite")
        dining_source = source("dining-query", tool_name="maps_around_search")
        anchor = fact(
            "anchor-provider", "anchor", "外滩锚点", MapCoordinate(lng=121.49, lat=31.24), "raw-anchor",
            type_name="风景名胜;城市广场", typecode="110000",
        )
        foreign_derived = fact(
            "derived:attraction:meal-poi", "meal-poi", "派生景点侧餐馆", None, "attraction-composite",
        )
        attraction_raw = fact(
            "attraction-provider-meal", "meal-poi", "供应商旧事实", None, "attraction-provider",
        )
        provider = fact(
            "provider-dining-meal", "meal-poi", "新餐馆事实", MapCoordinate(lng=121.491, lat=31.24),
            "dining-query",
        )
        store = {"sources": [root_source, attraction_raw_source, foreign_source],
                 "places": [anchor, attraction_raw, foreign_derived], "weather": [], "routes": []}
        tool_result = ToolResult(
            tool_call_id="dining-query", status="ok", data=PlaceSearchData(places=(provider,)),
            source=dining_source,
        )

        DiningSubtaskRunner._append_result(tool_result, store)
        dining_versions = [item for item in store["places"]
                           if item.fact_id.startswith("derived:dining:")]
        self.assertEqual(len(dining_versions), 1)
        merged = dining_versions[0]

        self.assertIn(foreign_derived, store["places"])
        self.assertEqual(merged.source_refs, ("attraction-provider", "dining-query"))
        self.assertNotIn("attraction-composite", merged.source_refs)
        self.assertEqual(merged.name, "新餐馆事实")

    def test_attractions_and_dining_can_share_the_four_worker_mcp_executor(self):
        dinner = active_meal_slots(1, "dinner")[0]
        dining_task, dining_ctx = dining_context({
            dinner: ("area-dinner", "anchor-dinner", 121.4900, 31.2410),
        })
        attraction_task = Subtask(
            task_id="attraction-task", run_id="attraction-run", context_revision=1,
            base_revision=1, role="attractions", objective="搜索景点",
            scope=TaskScope(city="上海", day_ids=("day-2026-10-10",),
                            slot_ids=("slot-2026-10-10-afternoon",)),
            candidate_target=1,
        )
        attraction_ctx = AttractionExecutionContext(
            request_id=dining_ctx.request_id, run_id=attraction_task.run_id,
            context_revision=1, base_revision=1, planning_input=dining_ctx.planning_input,
        )
        lock = threading.Lock()
        active = 0
        max_active = 0
        seen_roles = set()

        def tool_callable(tool_name, arguments):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
                seen_roles.add("dining" if tool_name == "maps_around_search" else "attractions")
            time.sleep(0.03)
            if tool_name == "maps_around_search":
                rows = [restaurant("shared-meal", "共享餐馆", 121.491, 31.241)]
            else:
                rows = [{
                    "id": "shared-sight", "name": "共享景点", "location": "121.491,31.241",
                    "address": "上海市", "cityname": "上海市", "type": "风景名胜;公园广场",
                    "typecode": "110000",
                }]
            try:
                return vendor_result(tool_name, arguments, rows)
            finally:
                with lock:
                    active -= 1

        executor = MCPToolExecutor(tool_callable, max_workers=4)
        dining_runner = DiningSubtaskRunner(
            llm=FakeLLM([action_search(dinner), action_finish([selection("shared-meal", (dinner,))])]),
            tool_executor=executor,
        )
        attraction_runner = AttractionSubtaskRunner(
            llm=FakeLLM([
                json.dumps({"action": "search", "keywords": "上海景点"}),
                json.dumps({"action": "finish", "selections": [{
                    "poi_id": "shared-sight", "slot_ids": ["slot-2026-10-10-afternoon"],
                }]}),
            ]),
            tool_executor=executor,
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            dining_future = pool.submit(dining_runner.run, dining_task, dining_ctx)
            attraction_future = pool.submit(attraction_runner.run, attraction_task, attraction_ctx)
            dining_result = dining_future.result(timeout=3)
            attraction_result = attraction_future.result(timeout=3)
        executor.close()

        self.assertEqual(dining_result.task_result.status, "complete")
        self.assertEqual(attraction_result.task_result.status, "complete")
        self.assertEqual(seen_roles, {"dining", "attractions"})
        self.assertLessEqual(max_active, 4)


if __name__ == "__main__":
    unittest.main()
