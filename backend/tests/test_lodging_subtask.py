from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest

from app.agents.execution import PlanningError
from app.agents.lodging_subtask import (
    LodgingExecutionContext,
    LodgingSubtaskRunner,
)
from app.models.planning_v4 import (
    AreaHint,
    CandidateRef,
    FactSource,
    FactStore,
    HardRequirement,
    MapCoordinate,
    PlaceFact,
    ReferenceCost,
    RequirementSummary,
    RuleSet,
)
from app.models.planning_v4.input import RequirementSource
from app.models.planning_v4.tasks import Subtask, TaskDependency, TaskScope
from app.services.amap_contract_adapter import AmapContractAdapter
from app.services.planning_input_builder import PlanningInputBuildRequest, build_planning_input
from scripts.run_requirement_interpreter import shanghai_request
from scripts.run_lodging_subtask import build_context as build_live_context


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


def build_context(*, lodging_query=None, area_count=1, candidate_refs=()):
    request = shanghai_request().model_copy(update={"lodging_query": lodging_query})
    requirements = RequirementSummary(
        requirements_id="f04b-requirements",
        request_id=request.request_id,
        core_goals=("上海三日旅行",),
        preferences=request.preferences,
    )
    planning = build_planning_input(PlanningInputBuildRequest(
        request=request,
        requirements=requirements,
        rules=RuleSet(
            rule_version="f04b-f02-fixture-v1", windows=WINDOWS,
            full_day_start_minute=8 * 60, return_buffer_default_s=3600,
            departure_day_minimum_usable_s=10800,
        ),
    ))
    places, sources, hints, dependencies = [], [], [], []
    for index in range(area_count):
        area_id = f"stay-area-{index + 1}"
        poi_id = f"stay-anchor-{index + 1}"
        fact_id = f"stay-anchor-fact-{index + 1}"
        source_id = f"stay-anchor-source-{index + 1}"
        sources.append(FactSource(
            tool_call_id=source_id, provider="amap", tool_name="maps_text_search",
            api_version="v3", queried_at=datetime(2026, 10, 11, tzinfo=timezone.utc),
            effective_params={"keywords": f"区域锚点{index + 1}", "city": "上海"},
        ))
        places.append(PlaceFact(
            fact_id=fact_id, poi_id=poi_id, name=f"真实区域锚点{index + 1}",
            location=MapCoordinate(lng=121.47 + index * 0.01, lat=31.23),
            address="上海市", city="上海市", type_name="风景名胜;公园广场", typecode="110000",
            source_refs=(source_id,),
            field_sources={"poi_id": source_id, "name": source_id, "location": source_id, "city": source_id},
        ))
        hints.append(AreaHint(
            area_hint_id=area_id, label=f"来源化区域{index + 1}",
            anchor_poi_ids=(poi_id,), basis_fact_ids=(fact_id,), basis="provider_fact",
        ))
        dependencies.extend((
            TaskDependency(kind="area_hint", reference_id=area_id),
            TaskDependency(kind="fact", reference_id=fact_id),
        ))
    specified = isinstance(lodging_query, str) and bool(lodging_query.strip())
    scoped_hints = () if specified else tuple(hint.area_hint_id for hint in hints)
    task = Subtask(
        task_id="f04b-task", run_id="f04b-run", context_revision=1, base_revision=None,
        role="lodging", objective="核实指定住处或在来源化住宿区域推荐一家酒店",
        scope=TaskScope(city=request.city, area_hint_ids=scoped_hints),
        dependencies=() if specified else tuple(dependencies),
        known_fact_ids=() if specified else tuple(place.fact_id for place in places),
        candidate_target=1, required_fields=("poi_id", "name", "location"),
        preferences=request.preferences,
    )
    context = LodgingExecutionContext(
        request_id=request.request_id, run_id=task.run_id,
        context_revision=task.context_revision, base_revision=task.base_revision,
        planning_input=planning,
        fact_store=FactStore(places=tuple(places), sources=tuple(sources)),
        candidate_refs=tuple(candidate_refs), area_hints=tuple(hints),
    )
    return task, context


def with_must_avoid(context, excluded_name="避雷酒店"):
    quote = f"不要住{excluded_name}"
    request = context.planning_input.request.model_copy(update={
        "remarks": f"{context.planning_input.request.remarks}；{quote}",
    })
    requirement = HardRequirement(
        requirement_id="f04b-must-avoid", kind="must_avoid", text=excluded_name,
        source=RequirementSource(field="remarks", quote=quote),
    )
    requirements = context.planning_input.requirements.model_copy(update={
        "hard_requirements": (*context.planning_input.requirements.hard_requirements, requirement),
    })
    planning_input = context.planning_input.model_copy(update={
        "request": request, "requirements": requirements,
    })
    return replace(context, planning_input=planning_input)


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


def place(poi_id, name, lng, lat, *, typecode="100100", type_name="住宿服务;酒店服务;宾馆", city="上海市", address="上海市黄浦区"):
    return {
        "id": poi_id, "name": name, "location": f"{lng},{lat}",
        "address": address, "cityname": city, "type": type_name, "typecode": typecode,
    }


def action_verify():
    return json.dumps({"action": "verify_specified"}, ensure_ascii=False)


def action_search(area_id="stay-area-1", anchor_id="stay-anchor-1"):
    return json.dumps({"action": "search_area", "area_hint_id": area_id,
                       "anchor_poi_id": anchor_id}, ensure_ascii=False)


def action_finish(poi_id=None, area_id=None, reason="基于地图事实的住宿参考"):
    selections = [] if poi_id is None else [{
        "poi_id": poi_id, "area_hint_id": area_id,
        "recommendation_reason": reason,
    }]
    return json.dumps({"action": "finish", "selections": selections}, ensure_ascii=False)


class FakeLLM:
    def __init__(self, replies, *, blocker=None, started=None):
        self.replies = list(replies)
        self.messages = []
        self.blocker = blocker
        self.started = started

    def invoke_with_usage(self, messages, **kwargs):
        self.messages.append(messages)
        if self.started is not None:
            self.started.set()
        if self.blocker is not None:
            self.blocker.wait(timeout=2)
        return self.replies.pop(0), {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}


class LodgingSubtaskTests(unittest.TestCase):
    def test_shanghai_fixture_has_no_user_selected_lodging(self):
        task, context = build_context()
        self.assertEqual(context.planning_input.request.arrival_at.date().isoformat(), "2026-10-10")
        self.assertEqual(context.planning_input.request.departure_at.date().isoformat(), "2026-10-12")
        self.assertIsNone(context.planning_input.request.lodging_query)
        self.assertEqual(task.role, "lodging")
        self.assertNotIn("用户已订酒店", task.objective)

    def test_live_cli_builder_uses_shanghai_fixture_and_source_anchor_file(self):
        _, context = build_context()
        payload = {
            "fact_store": context.fact_store.model_dump(mode="json"),
            "area_hints": [item.model_dump(mode="json") for item in context.area_hints],
        }
        with tempfile.TemporaryDirectory() as directory:
            anchor_file = Path(directory) / "anchors.json"
            anchor_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            task, live_context = build_live_context(anchor_file)

        self.assertEqual(task.scope.area_hint_ids, ("stay-area-1",))
        self.assertEqual(live_context.planning_input.request.arrival_at.date().isoformat(), "2026-10-10")
        self.assertEqual(live_context.planning_input.request.departure_at.date().isoformat(), "2026-10-12")
        self.assertIsNone(live_context.planning_input.request.lodging_query)

    def test_specified_residence_uses_exact_original_query_without_hotel_filter(self):
        task, context = build_context(lodging_query="朋友家小区 3 号楼")
        calls = []
        llm = FakeLLM([action_verify(), action_finish("home-poi")])

        def tool(name, args):
            calls.append((name, dict(args)))
            return vendor_result(name, args, [place(
                "home-poi", "朋友家小区 3 号楼", 121.48, 31.24,
                typecode="120300", type_name="商务住宅;住宅小区",
            )])

        result = LodgingSubtaskRunner(
            llm=llm, adapter=AmapContractAdapter(tool_runner=tool),
        ).run(task, context)

        self.assertEqual(result.task_result.status, "complete")
        self.assertEqual(result.task_result.candidates[0].category, "lodging")
        self.assertIsNone(result.task_result.candidates[0].area_hint_id)
        self.assertEqual(result.fact_store.places[-1].cost, ReferenceCost())
        self.assertEqual(calls[0][0], "maps_text_search")
        self.assertEqual(calls[0][1]["keywords"], "朋友家小区 3 号楼")
        self.assertNotIn("types", calls[0][1])
        self.assertIn('"original_lodging_query": "朋友家小区 3 号楼"', llm.messages[0][1]["content"])

    def test_specified_query_does_not_accept_area_or_similar_name(self):
        task, context = build_context(lodging_query="静安寺附近")
        tool_calls = []
        llm = FakeLLM([action_verify(), action_finish()])

        def tool(name, args):
            tool_calls.append(name)
            return vendor_result(name, args, [place(
                "nearby-hotel", "静安中心酒店", 121.45, 31.23,
            )])

        result = LodgingSubtaskRunner(
            llm=llm, adapter=AmapContractAdapter(tool_runner=tool),
        ).run(task, context)

        self.assertEqual(tool_calls, ["maps_text_search"])
        self.assertEqual(result.task_result.status, "partial")
        self.assertEqual(result.task_result.stop_reason, "no_match")
        self.assertFalse(result.task_result.candidates)

    def test_exact_named_business_district_is_not_a_specified_residence(self):
        task, context = build_context(lodging_query="陆家嘴")
        llm = FakeLLM([action_verify(), action_finish()])

        result = LodgingSubtaskRunner(
            llm=llm,
            adapter=AmapContractAdapter(tool_runner=lambda name, args: vendor_result(
                name, args, [place(
                    "lujiazui-area", "陆家嘴", 121.51, 31.24,
                    typecode="190110", type_name="地名地址信息;普通地名;商圈名",
                )],
            )),
        ).run(task, context)

        self.assertEqual(result.task_result.status, "partial")
        self.assertEqual(result.task_result.stop_reason, "no_match")
        self.assertEqual(result.task_result.coverage[0].status, "unknown")
        self.assertIn("区域或商圈", result.task_result.coverage[0].reason)
        self.assertFalse(result.task_result.candidates)

    def test_ambiguous_same_name_is_unknown_even_if_model_selects_one(self):
        task, context = build_context(lodging_query="海景公寓")
        llm = FakeLLM([action_verify(), action_finish("home-a")])

        def tool(name, args):
            return vendor_result(name, args, [
                place("home-a", "海景公寓", 121.48, 31.24, typecode="120300", type_name="商务住宅"),
                place("home-b", "海景公寓", 121.50, 31.25, typecode="120300", type_name="商务住宅"),
            ])

        result = LodgingSubtaskRunner(
            llm=llm, adapter=AmapContractAdapter(tool_runner=tool),
        ).run(task, context)

        self.assertEqual(result.task_result.status, "partial")
        self.assertEqual(result.task_result.stop_reason, "ambiguous")
        self.assertEqual(result.task_result.coverage[0].status, "unknown")
        self.assertFalse(result.task_result.candidates)

    def test_recommendation_uses_source_anchor_and_links_candidate_area(self):
        task, context = build_context(area_count=2)
        seen = []
        llm = FakeLLM([action_search("stay-area-2", "stay-anchor-2"),
                       action_finish("hotel-2", "stay-area-2")])

        def tool(name, args):
            seen.append((name, dict(args)))
            return vendor_result(name, args, [place("hotel-2", "真实酒店", 121.481, 31.231)])

        result = LodgingSubtaskRunner(
            llm=llm, adapter=AmapContractAdapter(tool_runner=tool),
        ).run(task, context)

        self.assertEqual(result.task_result.status, "complete")
        self.assertEqual(result.task_result.candidates[0].area_hint_id, "stay-area-2")
        self.assertEqual(seen[0][0], "maps_around_search")
        self.assertEqual(seen[0][1]["location"], "121.48,31.23")
        self.assertEqual(seen[0][1]["radius"], "5000")
        self.assertEqual(seen[0][1]["types"], "100000")
        self.assertIsNone(result.fact_store.places[-1].cost.value)

    def test_recommendation_expands_only_when_first_radius_has_no_valid_hotel(self):
        task, context = build_context()
        calls = []
        llm = FakeLLM([action_search(), action_finish("hotel-10k", "stay-area-1")])

        def tool(name, args):
            calls.append(dict(args))
            if args["radius"] == "5000":
                return vendor_result(name, args, [place(
                    "shop", "便利店", 121.471, 31.231, typecode="060000", type_name="购物服务",
                )])
            return vendor_result(name, args, [place("hotel-10k", "远处酒店", 121.52, 31.23)])

        result = LodgingSubtaskRunner(
            llm=llm, adapter=AmapContractAdapter(tool_runner=tool),
        ).run(task, context)

        self.assertEqual([item["radius"] for item in calls], ["5000", "10000"])
        self.assertEqual(result.task_result.status, "complete")
        self.assertEqual(result.task_result.candidates[0].poi_id, "hotel-10k")

    def test_unlocated_hotel_at_first_radius_does_not_block_expansion(self):
        task, context = build_context()
        calls = []
        llm = FakeLLM([action_search(), action_finish("hotel-10k", "stay-area-1")])

        def tool(name, args):
            calls.append(dict(args))
            if args["radius"] == "5000":
                unlocated = place("unlocated-hotel", "未定位酒店", 121.48, 31.23)
                unlocated.pop("location")
                return vendor_result(name, args, [unlocated])
            return vendor_result(name, args, [place("hotel-10k", "十公里内酒店", 121.55, 31.23)])

        result = LodgingSubtaskRunner(
            llm=llm, adapter=AmapContractAdapter(tool_runner=tool),
        ).run(task, context)

        self.assertEqual([item["radius"] for item in calls], ["5000", "10000"])
        self.assertEqual(result.task_result.status, "complete")
        self.assertEqual(result.task_result.candidates[0].poi_id, "hotel-10k")

    def test_must_avoid_hotel_is_filtered_from_new_candidate_results(self):
        task, context = build_context()
        context = with_must_avoid(context)
        calls = []
        llm = FakeLLM([action_search()])

        def tool(name, args):
            calls.append(dict(args))
            return vendor_result(name, args, [place(
                "forbidden-hotel", "避雷酒店", 121.471, 31.231,
            )])

        runner = LodgingSubtaskRunner(
            llm=llm, adapter=AmapContractAdapter(tool_runner=tool),
        )
        result = runner.run(task, context)

        self.assertEqual([item["radius"] for item in calls], ["5000", "10000"])
        self.assertEqual(result.task_result.status, "partial")
        self.assertFalse(result.task_result.candidates)
        self.assertFalse(any(
            item.poi_id == "forbidden-hotel"
            for item in runner._available_places(task, context, list(result.fact_store.places))
        ))

    def test_must_avoid_hotel_at_five_km_does_not_block_ten_km_candidate(self):
        task, context = build_context()
        context = with_must_avoid(context)
        calls = []
        llm = FakeLLM([action_search(), action_finish("hotel-10k", "stay-area-1")])

        def tool(name, args):
            calls.append(dict(args))
            if args["radius"] == "5000":
                return vendor_result(name, args, [place(
                    "forbidden-hotel", "避雷酒店", 121.471, 31.231,
                )])
            return vendor_result(name, args, [place("hotel-10k", "普通酒店", 121.55, 31.23)])

        result = LodgingSubtaskRunner(
            llm=llm, adapter=AmapContractAdapter(tool_runner=tool),
        ).run(task, context)

        self.assertEqual([item["radius"] for item in calls], ["5000", "10000"])
        self.assertEqual(result.task_result.status, "complete")
        self.assertEqual([item.poi_id for item in result.task_result.candidates], ["hotel-10k"])

    def test_missing_cost_does_not_expand_search_radius(self):
        task, context = build_context()
        calls = []
        llm = FakeLLM([action_search(), action_finish("hotel-5k", "stay-area-1")])

        def tool(name, args):
            calls.append(dict(args))
            return vendor_result(name, args, [place("hotel-5k", "住宿酒店", 121.471, 31.231)])

        result = LodgingSubtaskRunner(
            llm=llm, adapter=AmapContractAdapter(tool_runner=tool),
        ).run(task, context)

        self.assertEqual([item["radius"] for item in calls], ["5000"])
        self.assertEqual(result.task_result.status, "complete")

    def test_no_result_returns_unknown_without_fabrication(self):
        task, context = build_context()
        llm = FakeLLM([action_search()])

        result = LodgingSubtaskRunner(
            llm=llm,
            adapter=AmapContractAdapter(tool_runner=lambda name, args: vendor_result(name, args)),
        ).run(task, context)

        self.assertEqual(result.task_result.status, "partial")
        self.assertEqual(result.task_result.stop_reason, "no_match")
        self.assertFalse(result.task_result.candidates)
        self.assertEqual(result.task_result.coverage[0].status, "unknown")

    def test_existing_matching_candidate_can_be_reused_without_search(self):
        task, base_context = build_context(lodging_query="家园公寓")
        source = FactSource(
            tool_call_id="existing-source", provider="amap", tool_name="maps_text_search",
            api_version="v3", queried_at=datetime(2026, 10, 11, tzinfo=timezone.utc), effective_params={},
        )
        place_fact = PlaceFact(
            fact_id="existing-home-fact", poi_id="existing-home", name="家园公寓",
            location=MapCoordinate(lng=121.48, lat=31.24), city="上海市",
            type_name="商务住宅", typecode="120300", source_refs=(source.tool_call_id,),
            field_sources={"name": source.tool_call_id, "location": source.tool_call_id},
        )
        context = replace(
            base_context,
            fact_store=FactStore(places=(place_fact,), sources=(source,)),
            candidate_refs=(CandidateRef(
                candidate_id="existing-home-candidate", category="lodging",
                poi_id=place_fact.poi_id, place_fact_id=place_fact.fact_id,
            ),),
        )
        llm = FakeLLM([action_finish()])
        result = LodgingSubtaskRunner(
            llm=llm, adapter=AmapContractAdapter(tool_runner=lambda *_: self.fail("must reuse")),
        ).run(task, context)

        self.assertEqual(result.task_result.status, "complete")
        self.assertEqual(result.task_result.candidates[0].poi_id, "existing-home")
        self.assertEqual(result.tool_calls, ())

    def test_existing_recommendation_cannot_claim_a_distant_area_hint(self):
        task, context = build_context(area_count=2)
        source = FactSource(
            tool_call_id="hotel-source", provider="amap", tool_name="maps_around_search",
            api_version="v3", queried_at=datetime(2026, 10, 11, tzinfo=timezone.utc), effective_params={},
        )
        hotel = PlaceFact(
            fact_id="hotel-fact", poi_id="hotel", name="附近酒店",
            location=MapCoordinate(lng=121.481, lat=31.23), city="上海市",
            type_name="住宿服务;酒店", typecode="100100", source_refs=(source.tool_call_id,),
            field_sources={"poi_id": source.tool_call_id, "name": source.tool_call_id,
                           "location": source.tool_call_id},
        )
        moved_anchor = context.fact_store.places[1].model_copy(update={
            "location": MapCoordinate(lng=121.65, lat=31.23),
        })
        context = replace(
            context,
            fact_store=FactStore(
                places=(context.fact_store.places[0], moved_anchor, hotel),
                sources=(*context.fact_store.sources, source),
            ),
            candidate_refs=(CandidateRef(
                candidate_id="wrong-area-candidate", category="lodging", poi_id=hotel.poi_id,
                place_fact_id=hotel.fact_id, area_hint_id="stay-area-2",
            ),),
        )

        with self.assertRaises(PlanningError) as raised:
            LodgingSubtaskRunner(llm=FakeLLM([])).run(task, context)

        self.assertEqual(raised.exception.code, "CANDIDATE_REF_INVALID")

    def test_excluded_existing_candidate_is_rejected(self):
        task, context = build_context(lodging_query="家园公寓")
        source = FactSource(
            tool_call_id="excluded-source", provider="amap", tool_name="maps_text_search",
            api_version="v3", queried_at=datetime(2026, 10, 11, tzinfo=timezone.utc), effective_params={},
        )
        place_fact = PlaceFact(
            fact_id="excluded-fact", poi_id="excluded-home", name="家园公寓",
            location=MapCoordinate(lng=121.48, lat=31.24), city="上海市",
            type_name="住宅", typecode="120300", source_refs=(source.tool_call_id,),
            field_sources={"name": source.tool_call_id, "location": source.tool_call_id},
        )
        context = replace(
            context,
            fact_store=FactStore(places=(place_fact,), sources=(source,)),
            candidate_refs=(CandidateRef(
                candidate_id="excluded-candidate", category="lodging",
                poi_id=place_fact.poi_id, place_fact_id=place_fact.fact_id,
            ),),
        )
        task = task.model_copy(update={"excluded_poi_ids": (place_fact.poi_id,)})
        with self.assertRaises(PlanningError) as raised:
            LodgingSubtaskRunner(llm=FakeLLM([action_finish()])).run(task, context)
        self.assertEqual(raised.exception.code, "CANDIDATE_REF_INVALID")

    def test_area_without_source_backed_real_anchor_is_rejected(self):
        task, context = build_context()
        bad_hint = context.area_hints[0].model_copy(update={"basis_fact_ids": ()})
        context = replace(context, area_hints=(bad_hint,))
        task = task.model_copy(update={
            "dependencies": tuple(item for item in task.dependencies if item.kind != "area_hint"),
        })
        with self.assertRaises(PlanningError) as raised:
            LodgingSubtaskRunner(llm=FakeLLM([action_search()])).run(task, context)
        self.assertEqual(raised.exception.code, "LODGING_ANCHOR_UNVERIFIED")

    def test_expired_deadline_returns_partial_unknown_without_model_or_tool_call(self):
        task, context = build_context()
        task = task.model_copy(update={"deadline_at": datetime(2020, 1, 1, tzinfo=timezone.utc)})
        llm = FakeLLM([action_search()])
        runner = LodgingSubtaskRunner(
            llm=llm, adapter=AmapContractAdapter(tool_runner=lambda *_: self.fail("deadline must stop")),
        )
        result = runner.run(task, context)
        self.assertEqual(result.task_result.stop_reason, "deadline")
        self.assertEqual(result.task_result.coverage[0].status, "unknown")
        self.assertFalse(llm.messages)
        self.assertEqual(result.tool_calls, ())

    def test_async_cancellation_uses_shared_executor_lifecycle(self):
        task, context = build_context()
        started, release = threading.Event(), threading.Event()
        llm = FakeLLM([action_search()], blocker=release, started=started)
        runner = LodgingSubtaskRunner(
            llm=llm,
            adapter=AmapContractAdapter(tool_runner=lambda *_: self.fail("cancelled before tool")),
        )

        async def exercise():
            pending = asyncio.create_task(runner.run_async(task, context))
            reached_model = await asyncio.to_thread(started.wait, 1)
            self.assertTrue(reached_model)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            release.set()

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
