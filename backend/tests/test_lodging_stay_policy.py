"""S04：真实派发/住宿/生成接线使用替身模型和地图，禁止网络。"""
import asyncio
from datetime import date
import json
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.agents.attraction_subtask import AttractionExecutionContext, AttractionSubtaskRunner
from app.agents.daily_plan_generator import DailyPlanGenerator
from app.agents.dining_subtask import DiningSubtaskRunner
from app.agents.execution import PlanningError
from app.agents.lodging_subtask import LodgingSubtaskRunner
from app.agents.master_decider import MasterDecider, MasterDecisionContext
from app.agents.requirement_interpreter import RequirementInterpreter
from app.models.planning_v4.facts import FactStore
from app.models.planning_v4.input import LodgingChange, RequirementSource, RequirementSummary
from app.services.amap_contract_adapter import AmapContractAdapter
from app.services.hotel_selection_service import HotelSelectionService
from app.services.planning_dispatch_service import PlanningDispatchContext, PlanningDispatchService
from app.services.planning_lodging_policy import lodging_stays, validate_lodging_changes, candidate_for_stay
from test_attraction_subtask import planning_input, subtask, place as attraction_row, map_result, SLOT_ID
from test_daily_plan_generator import make_input, make_facts, make_candidates, make_areas, make_model_output, FakeLLM
from test_dining_subtask import dining_context, active_meal_slots, restaurant, action_search as search_meal
from test_lodging_subtask import build_context, action_search, action_finish, vendor_result, place as hotel_row
from test_hotel_selection import request, planning, row


def with_change(pi, *, city=None, query=None, quote="2026-10-12换住处"):
    change = LodgingChange(start_date=pi.days[-1].day_date, city=city, lodging_query=query,
                           source=RequirementSource(field="remarks", quote=quote))
    return pi.model_copy(update={
        "request": pi.request.model_copy(update={"remarks": quote}),
        "requirements": pi.requirements.model_copy(update={"lodging_changes": (change,)}),
    })


def master_context(pi, facts=FactStore(), candidates=(), areas=()):
    return MasterDecisionContext(planning_input=pi, request_id=pi.request.request_id,
                                run_id="stay-run", context_revision=1, fact_store=facts,
                                candidates=candidates, area_hints=areas)


class LodgingStayPolicyTests(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def adapter(self, rows):
        tool = Mock(return_value={"status": "1", "pois": rows})
        return AmapContractAdapter(tool_runner=tool), tool

    def test_text_residence_verified_once_for_all_days_without_hotel_filter_or_info_detail(self):
        req = request().model_copy(update={"lodging_poi_id": None, "lodging_query": "朋友家3号楼"})
        residence = {**row(), "name": req.lodging_query, "typecode": "120000", "type": "商务住宅;住宅小区"}
        adapter, tool = self.adapter([residence])
        pi = planning(req)
        facts, candidates = HotelSelectionService(adapter).verify_stays(pi)
        self.assertEqual(tool.call_count, 1)
        self.assertEqual(tool.call_args.args[0], "maps_text_search")
        self.assertEqual(tool.call_args.args[1]["keywords"], req.lodging_query)
        self.assertNotIn("types", tool.call_args.args[1])
        self.assertEqual(candidates[0].lodging_day_ids, tuple(day.day_id for day in pi.days))
        self.assertIsNone(candidates[0].area_hint_id)
        self.assertIsNone(facts.places[0].cost.value)
        self.assertIsNone(facts.places[0].opening_hours_raw)
        generated = DailyPlanGenerator(llm=FakeLLM([make_model_output(pi, lodging=False, weather=False)])).generate(
            pi, candidates, facts, (), (), source_run_id="stay-run")
        self.assertEqual({day.lodging.poi_id for day in generated.draft.days}, {"hotel-test"})
        self.assertTrue(all(day.lodging.selection_source == "user" and day.lodging.area_hint_id is None
                            for day in generated.draft.days))

    def test_text_ambiguity_wrong_city_region_and_similar_name_never_trigger_recommendation_or_detail(self):
        req = request().model_copy(update={"lodging_poi_id": None})
        for rows in ([row(), {**row(), "id": "other"}],
                     [{**row(), "cityname": "北京市"}],
                     [{**row(), "typecode": "190000", "type": "地名地址信息;商圈名"}],
                     [{**row(), "name": req.lodging_query + "二店"}]):
            with self.subTest(rows=len(rows)):
                adapter, tool = self.adapter(rows)
                facts, candidates = HotelSelectionService(adapter).verify_stays(planning(req))
                self.assertEqual(candidates, ())
                self.assertEqual(tool.call_count, 1)
                ctx = master_context(planning(req), facts)
                roles = MasterDecider._decision_schema(ctx)["$defs"]["TaskSpec"]["properties"]["role"]["enum"]
                self.assertNotIn("lodging", roles)

    def test_text_detail_only_fills_missing_coordinates(self):
        req = request().model_copy(update={"lodging_poi_id": None})
        tool = Mock(side_effect=[{"status": "1", "pois": [{**row(), "location": ""}]},
                                 {"status": "1", "pois": [row()]}])
        facts, candidates = HotelSelectionService(AmapContractAdapter(tool_runner=tool)).verify_stays(planning(req))
        self.assertEqual([call.args[0] for call in tool.call_args_list], ["maps_text_search", "maps_search_detail"])
        self.assertEqual(len(candidates), 1)
        self.assertIsNone(facts.places[0].cost.value)
        self.assertIsNone(facts.places[0].opening_hours_raw)

    def test_master_and_dispatch_reject_specified_lodging_task_even_with_coordinates_unknown(self):
        pi = make_input(lodging_query="用户住处")
        payload = {"action": "delegate", "reason": "补酒店", "tasks": [{
            "local_ref": "stay", "role": "lodging", "objective": "推荐酒店",
            "day_ids": [pi.days[0].day_id]}]}
        with self.assertRaises(PlanningError) as raised:
            MasterDecider(llm=FakeLLM([json.dumps(payload)])).run(master_context(pi))
        self.assertEqual(raised.exception.code, "MASTER_LODGING_SPECIFIED")
        from test_planning_dispatch_service import planning_context, subtask as task, resolved, decision, ScriptedDecider
        context = planning_context().model_copy(update={"planning_input": pi})
        hotel_task = task("stay", role="lodging").model_copy(update={"scope": task("stay", role="lodging").scope.model_copy(
            update={"day_ids": tuple(day.day_id for day in pi.days)})})
        runner = Mock()
        result = asyncio.run(PlanningDispatchService(
            decider=ScriptedDecider([decision(context, "delegate", tasks=(resolved(hotel_task),))]),
            decision_context_factory=lambda **values: SimpleNamespace(**values),
            runner_factories={"lodging": runner},
        ).run_async(context))
        self.assertEqual(result.stop_reason, "technical_error")
        runner.assert_not_called()

    def test_master_rejects_unused_accommodation_area_with_specified_residence(self):
        pi = make_input(lodging_query="用户住处")
        facts = make_facts()
        output = {"action": "delegate", "reason": "天气", "weather_dates": [pi.days[-1].day_date.isoformat()],
                  "areas": [{"local_ref": "extra-stay", "label": "住宿区", "basis_fact_ids": ["fact-bund"]}]}
        with self.assertRaises(PlanningError) as raised:
            MasterDecider(llm=FakeLLM([json.dumps(output)])).run(master_context(pi, facts))
        self.assertEqual(raised.exception.code, "MASTER_LODGING_AREA_FORBIDDEN")

    def test_recommended_multi_day_dispatch_queries_one_hotel_then_generator_reuses_it(self):
        _, initial = build_context()
        pi = initial.planning_input
        hints = tuple(hint.model_copy(update={"center": initial.fact_store.places[i].location,
                                               "basis": "verified_place_facts"}) for i, hint in enumerate(initial.area_hints))
        ctx = PlanningDispatchContext(planning_input=pi, run_id="stay-run", context_revision=1,
                                      fact_store=initial.fact_store, area_hints=hints)
        output = {"action": "delegate", "reason": "推荐住宿", "tasks": [{
            "local_ref": "stay", "role": "lodging", "objective": "住宿参考",
            "day_ids": [pi.days[0].day_id], "area_refs": [hints[0].area_hint_id]}]}
        master_llm = FakeLLM([json.dumps(output), json.dumps({"action": "plan", "reason": "地点已收集"})])
        tool = Mock(side_effect=lambda name, args: vendor_result(name, args, [hotel_row("one-hotel", "真实酒店", 121.47, 31.23)]))
        runner = LodgingSubtaskRunner(llm=FakeLLM([action_search(), action_finish("one-hotel", hints[0].area_hint_id)]),
                                      adapter=AmapContractAdapter(tool_runner=tool))
        result = asyncio.run(PlanningDispatchService(decider=MasterDecider(llm=master_llm),
                             runner_factories={"lodging": lambda: runner}).run_async(ctx))
        self.assertEqual(result.stop_reason, "plan")
        self.assertEqual(tool.call_count, 1)
        self.assertEqual(len(result.existing_tasks), 1)
        self.assertEqual(result.existing_tasks[0].scope.day_ids, tuple(day.day_id for day in pi.days))
        self.assertEqual(len(result.candidate_refs), 1)
        self.assertEqual(result.candidate_refs[0].lodging_day_ids, result.existing_tasks[0].scope.day_ids)
        self.assertNotIn("lodging", json.loads(master_llm.calls[1][0][1]["content"])["decision_json_schema"]["$defs"]["TaskSpec"]["properties"]["role"]["enum"])
        generated = DailyPlanGenerator(llm=FakeLLM([make_model_output(pi, lodging=False, weather=False)])).generate(
            pi, result.candidate_refs, result.fact_store, result.area_hints, result.task_results, source_run_id="stay-run")
        self.assertEqual({day.lodging.poi_id for day in generated.draft.days}, {"one-hotel"})
        self.assertTrue(all(day.lodging.selection_source == "recommended" for day in generated.draft.days))

    def test_same_city_explicit_change_splits_hotel_and_excludes_old_stay_candidate(self):
        pi = with_change(make_input())
        first, second = lodging_stays(pi)
        facts = make_facts()
        original = make_candidates()[-1].model_copy(update={"lodging_day_ids": first.day_ids})
        another = facts.places[-1].model_copy(update={"fact_id": "fact-new-hotel", "poi_id": "new-hotel", "name": "另一真实酒店"})
        facts = facts.model_copy(update={"places": (*facts.places, another)})
        candidate = original.model_copy(update={"candidate_id": "new-candidate", "place_fact_id": another.fact_id,
                                                "poi_id": another.poi_id, "lodging_day_ids": second.day_ids})
        self.assertIsNone(candidate_for_stay(second, (original,), facts, make_areas()))
        generated = DailyPlanGenerator(llm=FakeLLM([make_model_output(pi, lodging=False, weather=False)])).generate(
            pi, (original, candidate), facts, make_areas(), (), source_run_id="stay-run")
        self.assertEqual([day.lodging.poi_id for day in generated.draft.days], [original.poi_id, original.poi_id, candidate.poi_id])

    def test_cross_city_changed_residence_is_verified_and_reused_only_on_its_dates(self):
        req = request()
        pi = with_change(planning(req), city="苏州", query="苏州指定酒店", quote="2026-10-13跨城到苏州，住苏州指定酒店")
        tool = Mock(side_effect=[{"status": "1", "pois": [row()]},
                                {"status": "1", "pois": [{**row(), "id": "suzhou-hotel", "name": "苏州指定酒店", "cityname": "苏州市"}]}])
        facts, candidates = HotelSelectionService(AmapContractAdapter(tool_runner=tool)).verify_stays(pi)
        self.assertEqual(tool.call_count, 2)
        self.assertEqual(tool.call_args_list[1].args[1]["city"], "苏州")
        generated = DailyPlanGenerator(llm=FakeLLM([make_model_output(pi, lodging=False, weather=False)])).generate(
            pi, candidates, facts, (), (), source_run_id="stay-run")
        self.assertEqual([day.lodging.poi_id for day in generated.draft.days], ["hotel-test", "hotel-test", "suzhou-hotel"])
        self.assertTrue(all(day.lodging.selection_source == "user" for day in generated.draft.days))

    def test_cross_city_recommendation_uses_changed_city_source_anchor(self):
        _, initial = build_context()
        pi = with_change(initial.planning_input, city="苏州", quote="2026-10-12跨城到苏州住宿")
        stay = lodging_stays(pi)[-1]
        anchor = initial.fact_store.places[0].model_copy(update={"city": "苏州市"})
        facts = initial.fact_store.model_copy(update={"places": (anchor,)})
        hint = initial.area_hints[0].model_copy(update={"center": anchor.location, "basis": "verified_place_facts"})
        output = {"action": "delegate", "reason": "新城市住宿", "tasks": [{
            "local_ref": "suzhou-stay", "role": "lodging", "objective": "推荐酒店",
            "day_ids": list(stay.day_ids), "area_refs": [hint.area_hint_id]}]}
        master = MasterDecider(llm=FakeLLM([json.dumps(output), json.dumps({"action": "plan", "reason": "返回已收集事实"})]))
        tool = Mock(side_effect=lambda name, args: vendor_result(name, args, [
            hotel_row("suzhou-hotel", "苏州真实酒店", 121.47, 31.23, city="苏州市")]))
        runner = LodgingSubtaskRunner(llm=FakeLLM([action_search(), action_finish("suzhou-hotel", hint.area_hint_id)]),
                                      adapter=AmapContractAdapter(tool_runner=tool))
        result = asyncio.run(PlanningDispatchService(decider=master, runner_factories={"lodging": lambda: runner}).run_async(
            PlanningDispatchContext(planning_input=pi, run_id="stay-run", context_revision=1, fact_store=facts, area_hints=(hint,))))
        self.assertEqual(result.stop_reason, "plan")
        self.assertEqual(tool.call_count, 1)
        self.assertEqual(result.existing_tasks[0].scope.city, "苏州")
        self.assertEqual(result.candidate_refs[0].lodging_day_ids, stay.day_ids)

    def test_changes_require_original_sources_valid_dates_and_literal_names(self):
        pi = make_input()
        valid = with_change(pi, city="苏州", quote="2026-10-12到苏州住宿")
        change = valid.requirements.lodging_changes[0]
        for changes in ((change,), (change, change), (change.model_copy(update={"start_date": date(2026, 10, 20)}),),
                        (change.model_copy(update={"city": "北京"}),)):
            request_for_case = valid.request if changes != (change,) else pi.request
            with self.subTest(changes=len(changes)), self.assertRaises(ValueError):
                validate_lodging_changes(request_for_case, changes)
        validate_lodging_changes(valid.request, (change,))
        reply = {"core_goals": [], "hard_requirements": [], "lodging_changes": [change.model_dump(mode="json")],
                 "preferences": [], "unknowns": [], "assumptions": []}
        llm = FakeLLM([json.dumps(reply)])
        summary = RequirementInterpreter(llm=llm).interpret(valid.request)
        self.assertEqual(summary.lodging_changes, (change,))
        self.assertEqual(len(llm.calls), 1)

    def test_optional_information_does_not_trigger_detail_or_extra_acquisition_in_three_roles(self):
        # 即使模型继续发起search，核心候选够用时也不再执行补费用/营业查询。
        pi = planning_input(with_requirement=False)
        task = subtask(requirement_ids=())
        ctx = AttractionExecutionContext(request_id=pi.request.request_id, run_id=task.run_id,
              context_revision=task.context_revision, base_revision=task.base_revision, planning_input=pi)
        tool = Mock(return_value=map_result([attraction_row()]))
        llm = FakeLLM([json.dumps({"action": "search", "keywords": "外滩"}),
                       json.dumps({"action": "search", "keywords": "外滩费用营业"})])
        result = AttractionSubtaskRunner(llm=llm, adapter=AmapContractAdapter(tool_runner=tool)).run(task, ctx)
        self.assertEqual(tool.call_count, 1)
        self.assertEqual(len(result.task_result.candidates), 1)
        slot = active_meal_slots(1)[0]
        task, ctx = dining_context({slot: ("area", "anchor", 121.49, 31.24)})
        tool = Mock(side_effect=lambda name, args: vendor_result(name, args, [restaurant("meal", "真实餐馆", 121.49, 31.24)]))
        result = DiningSubtaskRunner(llm=FakeLLM([search_meal(slot), search_meal(slot, "费用营业")]),
                                    adapter=AmapContractAdapter(tool_runner=tool)).run(task, ctx)
        self.assertEqual(tool.call_count, 1)
        self.assertEqual(len(result.task_result.candidates), 1)
        task, ctx = build_context(area_count=2)
        tool = Mock(side_effect=lambda name, args: vendor_result(name, args, [hotel_row("hotel", "真实酒店", 121.47, 31.23)]))
        result = LodgingSubtaskRunner(llm=FakeLLM([action_search(), action_search("stay-area-2", "stay-anchor-2")]),
                                     adapter=AmapContractAdapter(tool_runner=tool)).run(task, ctx)
        self.assertEqual(tool.call_count, 1)
        self.assertEqual(len(result.task_result.candidates), 1)
        self.assertIsNone(result.fact_store.places[-1].cost.value)
        self.assertIsNone(result.fact_store.places[-1].opening_hours_raw)

    def test_pipeline_text_residence_is_preverified_without_lodging_model(self):
        from app.services.planning_v4_pipeline import PlanningV4Pipeline, PlanningV4PipelineError
        req = request().model_copy(update={"lodging_poi_id": None})
        adapter, tool = self.adapter([row()])
        captured = []
        class Dispatch:
            async def run_async(self, context):
                captured.append(context)
                raise RuntimeError("stop after inspecting pipeline dispatch boundary")
        pipeline = PlanningV4Pipeline(
            requirement_interpreter_factory=lambda _: SimpleNamespace(interpret=lambda req, **kw: RequirementSummary(
                requirements_id="summary", request_id=req.request_id)),
            dispatch_service_factory=lambda _: Dispatch(),
            current_route_service_factory=lambda: SimpleNamespace(create_route_context=lambda *args: None),
        )
        with patch("app.services.hotel_selection_service.HotelSelectionService", return_value=HotelSelectionService(adapter)):
            with self.assertRaises(PlanningV4PipelineError):
                asyncio.run(pipeline.run(req, task_id="stay-run", plan_id="stay-plan", progress=lambda _: None))
        self.assertEqual(tool.call_count, 1)
        self.assertEqual(len(captured[0].candidate_refs), 1)
        ctx = master_context(captured[0].planning_input, captured[0].fact_store, captured[0].candidate_refs)
        self.assertNotIn("lodging", MasterDecider._decision_schema(ctx)["$defs"]["TaskSpec"]["properties"]["role"]["enum"])

    def test_generator_refuses_second_hotel_in_same_stay(self):
        pi, facts, candidates, areas = make_input(), make_facts(), make_candidates(), make_areas()
        hotel = facts.places[-1].model_copy(update={"fact_id": "second-fact", "poi_id": "second-hotel"})
        facts = facts.model_copy(update={"places": (*facts.places, hotel)})
        second = candidates[-1].model_copy(update={"candidate_id": "second-candidate", "place_fact_id": hotel.fact_id, "poi_id": hotel.poi_id})
        output = json.loads(make_model_output(pi, weather=False))
        output["days"][1]["lodging"]["candidate_ref"] = second.candidate_id
        llm = FakeLLM([json.dumps(output), json.dumps(output)])
        with self.assertRaises(PlanningError):
            DailyPlanGenerator(llm=llm).generate(pi, (*candidates, second), facts, areas, (), source_run_id="stay-run")
        self.assertLessEqual(len(llm.calls), 2)

    def test_initial_specified_residence_does_not_override_later_unspecified_change(self):
        _, initial = build_context(lodging_query="初始指定酒店")
        pi = with_change(initial.planning_input)
        stay = lodging_stays(pi)[-1]
        anchor = initial.fact_store.places[0]
        hint = initial.area_hints[0].model_copy(update={"center": anchor.location, "basis": "verified_place_facts"})
        output = {"action": "delegate", "reason": "用户要换住处", "tasks": [{
            "local_ref": "new-stay", "role": "lodging", "objective": "推荐新酒店",
            "day_ids": list(stay.day_ids), "area_refs": [hint.area_hint_id]}]}
        master = MasterDecider(llm=FakeLLM([json.dumps(output), json.dumps({"action": "plan", "reason": "完成收集"})]))
        tool = Mock(side_effect=lambda name, args: vendor_result(name, args, [hotel_row("new", "新真实酒店", 121.47, 31.23)]))
        runner = LodgingSubtaskRunner(llm=FakeLLM([action_search(), action_finish("new", hint.area_hint_id)]),
                                     adapter=AmapContractAdapter(tool_runner=tool))
        result = asyncio.run(PlanningDispatchService(decider=master, runner_factories={"lodging": lambda: runner}).run_async(
            PlanningDispatchContext(planning_input=pi, run_id="stay-run", context_revision=1,
                                    fact_store=initial.fact_store, area_hints=(hint,))))
        self.assertEqual(result.stop_reason, "plan")
        self.assertEqual(tool.call_args.args[0], "maps_around_search")
        self.assertEqual(result.candidate_refs[0].lodging_day_ids, stay.day_ids)
        self.assertFalse(any("初始指定酒店" in item for item in result.existing_tasks[0].constraints))

    def test_two_same_city_recommendations_after_explicit_change_exclude_previous_hotel(self):
        _, initial = build_context()
        pi = with_change(initial.planning_input)
        first, second = lodging_stays(pi)
        hint = initial.area_hints[0].model_copy(update={"center": initial.fact_store.places[0].location,
                                                     "basis": "verified_place_facts"})
        output = {"action": "delegate", "reason": "明确换住处", "tasks": [
            {"local_ref": "second", "role": "lodging", "objective": "换住处", "day_ids": list(second.day_ids), "area_refs": [hint.area_hint_id]},
            {"local_ref": "first", "role": "lodging", "objective": "推荐住宿", "day_ids": list(first.day_ids), "area_refs": [hint.area_hint_id]},
        ]}
        master = MasterDecider(llm=FakeLLM([json.dumps(output), json.dumps({"action": "plan", "reason": "收集完成"})]))
        tool = Mock(side_effect=[vendor_result("maps_around_search", {}, [hotel_row("A", "酒店A", 121.47, 31.23)]),
                                vendor_result("maps_around_search", {}, [hotel_row("A", "酒店A", 121.47, 31.23),
                                                                       hotel_row("B", "酒店B", 121.47, 31.23)])])
        runners = [LodgingSubtaskRunner(llm=FakeLLM([action_search(), action_finish(poi, hint.area_hint_id)]),
                                       adapter=AmapContractAdapter(tool_runner=tool)) for poi in ("A", "B")]
        def factory():
            return runners.pop(0)
        result = asyncio.run(PlanningDispatchService(decider=master, runner_factories={"lodging": factory}).run_async(
            PlanningDispatchContext(planning_input=pi, run_id="stay-run", context_revision=1,
                                    fact_store=initial.fact_store, area_hints=(hint,))))
        self.assertEqual(result.stop_reason, "plan")
        self.assertEqual(tool.call_count, 2)
        self.assertEqual({candidate.poi_id for candidate in result.candidate_refs}, {"A", "B"})
        generated = DailyPlanGenerator(llm=FakeLLM([make_model_output(pi, lodging=False, weather=False)])).generate(
            pi, result.candidate_refs, result.fact_store, result.area_hints, result.task_results, source_run_id="stay-run")
        self.assertEqual([day.lodging.poi_id for day in generated.draft.days], ["A", "A", "B"])

    def test_new_preview_rejects_out_of_stay_candidate_and_unrequested_hotel_switch(self):
        from test_planning_v4_contract import make_full_baseline_preview
        from app.services.planning_v4_validator import validate_preview, ContractValidationError
        preview = make_full_baseline_preview()[-1]
        candidate = preview.candidates[-1].model_copy(update={"lodging_day_ids": tuple(day.day_id for day in preview.draft.days[1:])})
        invalid = preview.model_copy(update={"candidates": (*preview.candidates[:-1], candidate)})
        with self.assertRaises(ContractValidationError) as raised:
            validate_preview(invalid)
        self.assertTrue(any(issue.code == "lodging_scope_mismatch" for issue in raised.exception.issues))
        hotel = preview.facts.places[-1].model_copy(update={"fact_id": "other-hotel-fact", "poi_id": "other-hotel"})
        candidate = preview.candidates[-1].model_copy(update={"candidate_id": "other-hotel-candidate", "place_fact_id": hotel.fact_id, "poi_id": hotel.poi_id})
        day = preview.draft.days[1]
        changed_day = day.model_copy(update={"lodging": day.lodging.model_copy(update={
            "candidate_ref": candidate.candidate_id, "poi_id": hotel.poi_id, "place_fact_id": hotel.fact_id})})
        invalid = preview.model_copy(update={"facts": preview.facts.model_copy(update={"places": (*preview.facts.places, hotel)}),
            "candidates": (*preview.candidates, candidate),
            "draft": preview.draft.model_copy(update={"days": (preview.draft.days[0], changed_day, preview.draft.days[2])})})
        with self.assertRaises(ContractValidationError) as raised:
            validate_preview(invalid)
        self.assertTrue(any(issue.code == "lodging_reuse_mismatch" for issue in raised.exception.issues))
