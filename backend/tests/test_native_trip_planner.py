"""主智能体每日提交、引用校验与按日修订；真实网络全部模拟。"""
from copy import deepcopy
from dataclasses import replace
import json
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from app.agents.native_trip_planner import NativeTripPlanner
from app.services.native_chain_trace import NativeChainTrace
from app.services.native_tool_loop import NativeToolLoopError
from app.services.native_tool_loop import NativeToolLoopResult
from app.services.planning_v4_validation import is_required_place_gap
from test_native_attraction_collection import FakeModel, call, final, request
import test_native_multiagent_collection as spatial


def fixed_requirements(summary):
    interpreter = Mock()
    interpreter.run.return_value = SimpleNamespace(summary=summary, protocol=NativeToolLoopResult("", (), (), 0))
    return interpreter


def collection():
    chain, _, _ = spatial.NativeMultiagentTests().chain()
    state = chain.run(request())
    slots = tuple(slot.slot_id for day in state.planning_input.days for slot in day.slots
                  if slot.period in {"breakfast", "lunch", "dinner"} and slot.eligibility != "skipped")
    candidates = tuple(candidate.model_copy(update={"slot_ids": slots}) if candidate.category == "meal" else
        candidate.model_copy(update={"lodging_day_ids": tuple(day.day_id for day in state.planning_input.days)})
        if candidate.category == "lodging" else candidate for candidate in state.candidates)
    by_id = {candidate.candidate_id: candidate for candidate in candidates}
    children = tuple(replace(child, task_result=child.task_result.model_copy(update={
        "candidates": tuple(by_id[candidate.candidate_id] for candidate in child.task_result.candidates)}))
        for child in state.children)
    return replace(state, candidates=candidates, children=children)


def payload(messages):
    return json.loads(next(message["content"] for message in reversed(messages) if message["role"] == "user"))


def plan(messages):
    data = payload(messages)
    output = {"days": [], "missing_notes": []}
    candidates = data["candidates"]
    hotel = next(candidate for candidate in candidates if candidate["category"] == "lodging")
    for day in data["planning_input"]["days"]:
        activities, unfilled = [], []
        options = next(options for options in data["day_slot_options"] if options["day_id"] == day["day_id"])
        periods = ["breakfast", "morning", "lunch", "afternoon", "dinner", "evening"]
        slot_ids = sorted([*options["available_meal_slot_ids"], *options["available_sightseeing_slot_ids"]],
                          key=lambda slot_id: periods.index(slot_id.split("-")[-1]))
        for slot_id in slot_ids:
            candidate = next((candidate for candidate in candidates if slot_id in candidate["applicable_slot_ids"]), None)
            if candidate is None:
                unfilled.append(slot_id)
                continue
            activities.append({"proposal_ref": "new-"+slot_id, "candidate_ref": candidate["candidate_id"],
                "slot_id": slot_id, "kind": "meal" if candidate["category"] == "meal" else "sightseeing",
                "requirement_ids": candidate["requirement_ids"], "recommendation_reason": "已知候选，沿用地点；重复用于测试"})
        output["days"].append({"day_id": day["day_id"], "day_date": day["day_date"], "theme": "漫步",
            "activities": activities, "unfilled_slot_ids": unfilled,
            "lodging": {"candidate_ref": hotel["candidate_id"], "area_hint_id": hotel["area_hint_id"]}})
    return output


def initial(messages):
    return call("submit_daily_plan", plan(messages), "plan-submit")


def omit_one(messages):
    output = plan(messages)
    output["days"][1]["activities"].pop(0)
    return call("submit_daily_plan", output, "plan-submit")


def replacement(messages, *, fill=True):
    data = payload(messages)
    day = data["current_day"]
    activities = [{"proposal_ref": "preserve-"+activity["activity_id"],
        "existing_activity_id": activity["activity_id"], "candidate_ref": activity["candidate_ref"],
        "slot_id": activity["slot_id"], "kind": activity["kind"], "requirement_ids": activity["requirement_ids"],
        "recommendation_reason": activity["recommendation_reason"]} for activity in day["activities"]]
    if fill:
        for issue in data["issues"]:
            slot_id = issue["target_id"]
            candidate = next(candidate for candidate in data["candidates"] if slot_id in candidate["applicable_slot_ids"])
            activities.append({"proposal_ref": "fill-"+slot_id, "candidate_ref": candidate["candidate_id"],
                "slot_id": slot_id, "kind": "meal" if candidate["category"] == "meal" else "sightseeing",
                "recommendation_reason": "填补必需时段缺项"})
        order = {slot["slot_id"]: index for index,slot in enumerate(data["planning_input"]["days"][0]["slots"])}
        activities.sort(key=lambda activity: order[activity["slot_id"]])
    lodging = {key: value for key, value in day["lodging"].items() if key in {"candidate_ref", "area_hint_id"}}
    return {"replacement_day": {"day_id": day["day_id"], "day_date": day["day_date"],
        "theme": day["theme"], "activities": activities, "unfilled_slot_ids": [] if fill else day["unfilled_slot_ids"],
        "lodging": lodging, "weather_advice": day["weather_advice"]}}


def revision(messages):
    return call("submit_day_revision", replacement(messages), "revision-submit")


class NativeTripPlannerTests(unittest.TestCase):
    def planner(self, model, state=None, emit=None):
        state = state or collection()
        collector = Mock()
        collector.run.return_value = state
        return NativeTripPlanner(master_llm=model, collector=collector, emit=emit,
            requirement_interpreter=fixed_requirements(state.planning_input.requirements)), collector

    def test_native_initial_plan_grounded_days_lodging_unknowns_without_business_repairs(self):
        trace = NativeChainTrace()
        model = FakeModel(initial, final("初版已提交"))
        planner, collector = self.planner(model, emit=trace.emit)
        result = planner.run(request())
        self.assertEqual(result.repair.status, "complete")
        self.assertEqual(len(result.draft.days), 2)
        self.assertEqual(result.draft.revision, 1)
        self.assertEqual(result.repair.attempts, ())
        self.assertTrue(all(state.completed_rounds == 0 for state in result.repair.repair_states))
        self.assertTrue(result.report.issues)  # 价格/天气/营业未知不触发业务补查。
        self.assertFalse(any(is_required_place_gap(result.planning_input, issue) for issue in result.report.issues))
        self.assertEqual(collector.run.call_count, 1)
        self.assertEqual(result.draft.legs, ())
        self.assertEqual(result.draft.days[0].lodging.poi_id, result.draft.days[1].lodging.poi_id)
        self.assertTrue(any(message.get("tool_calls", [{}])[0].get("function", {}).get("name") == "find_attractions"
                            for message in model.requests[0]["messages"] if message["role"] == "assistant"))
        self.assertEqual(model.requests[0]["tools"][0]["function"]["name"], "submit_daily_plan")

    def test_invalid_candidate_and_wrong_date_rejected_then_corrected_without_map_calls(self):
        for mutate in [lambda output: output["days"][0]["activities"][0].update(candidate_ref="invented"),
                       lambda output: output["days"][0].update(day_date="2026-10-25")]:
            def bad(messages):
                output = plan(messages); mutate(output)
                return call("submit_daily_plan", output, "bad-plan")
            model = FakeModel(bad, initial, final("已纠正"))
            planner, collector = self.planner(model)
            result = planner.run(request())
            self.assertEqual(result.repair.status, "complete")
            self.assertEqual(json.loads(model.requests[1]["messages"][-1]["content"])["data"]["status"], "rejected")
            self.assertEqual(collector.run.call_count, 1)
            self.assertEqual(result.repair.attempts, ())

    def test_missing_day2_slot_uses_existing_candidates_and_preserves_day1_ids_and_order(self):
        model = FakeModel(omit_one, final("初版缺项"), revision, final("修订提交"))
        planner, collector = self.planner(model)
        result = planner.run(request())
        self.assertEqual(result.repair.status, "complete")
        self.assertEqual(result.draft.revision, 2)
        self.assertEqual(result.draft.days[0], result.initial_draft.days[0])
        preserved = [activity.activity_id for activity in result.initial_draft.days[1].activities]
        self.assertEqual([activity.activity_id for activity in result.draft.days[1].activities
                          if activity.activity_id in preserved], preserved)
        self.assertEqual(result.repair.repair_states[1].completed_rounds, 1)
        self.assertEqual(collector.run.call_count, 1)
        self.assertEqual(payload(model.requests[2]["messages"])["target_date_only"]["day_id"], result.draft.days[1].day_id)

    def test_revision_cannot_modify_other_day_or_drop_preserved_activity(self):
        for change in [lambda output: output["replacement_day"].update(day_id="day-2026-10-20"),
                       lambda output: output["replacement_day"]["activities"].pop(-1)]:
            def bad(messages):
                output = replacement(messages); change(output)
                return call("submit_day_revision", output, "bad-revision")
            model = FakeModel(omit_one, final("缺项"), bad, revision, final("纠正"))
            planner, _ = self.planner(model)
            result = planner.run(request())
            self.assertEqual(result.repair.status, "complete")
            self.assertEqual(result.draft.days[0], result.initial_draft.days[0])
            self.assertEqual(json.loads(model.requests[3]["messages"][-1]["content"])["data"]["status"], "rejected")
            self.assertEqual(result.repair.repair_states[1].completed_rounds, 1)

    def test_no_progress_stops_after_three_business_rounds_with_editable_draft(self):
        def unchanged(messages):
            data = payload(messages)
            return call("submit_day_revision", replacement(messages, fill=False), "unchanged-"+str(data["repair_round"]["round_number"]))
        model = FakeModel(omit_one, final("缺项"), unchanged, final("无进展"), unchanged, final("无进展"), unchanged, final("无进展"))
        planner, collector = self.planner(model)
        result = planner.run(request())
        self.assertEqual(result.repair.status, "partial")
        self.assertEqual(result.repair.repair_states[1].completed_rounds, 3)
        self.assertEqual(result.repair.repair_states[1].stop_reason, "round_limit")
        self.assertTrue(result.draft.days[1].unfilled_slot_ids)
        self.assertEqual(collector.run.call_count, 1)

    def test_provider_failure_does_not_consume_business_round_and_can_retry(self):
        model = FakeModel(omit_one, final("缺项"), lambda _: (_ for _ in ()).throw(RuntimeError("private-secret")), revision, final("已修复"))
        planner, _ = self.planner(model)
        result = planner.run(request())
        self.assertEqual(result.repair.status, "complete")
        self.assertEqual(result.repair.repair_states[1].completed_rounds, 1)
        self.assertEqual([attempt.outcome for attempt in result.repair.attempts], ["technical_error", "applied"])
        self.assertNotIn("private-secret", repr(result.repair))

    def test_missing_candidate_dispatches_scoped_child_then_revises_without_requerying_same_map(self):
        from app.agents.native_multiagent_collection import NativeMultiagentCollection
        from app.services.amap_contract_adapter import AmapContractAdapter
        from app.services.planning_input_builder import PlanningInputBuildRequest, build_planning_input
        from app.services.planning_v4_rules import default_planning_v4_rules
        from app.models.planning_v4.input import RequirementSummary
        from test_native_attraction_collection import task_args, choice
        req = request()
        planning = build_planning_input(PlanningInputBuildRequest(request=req,
            requirements=RequirementSummary(requirements_id="fixture-req", request_id=req.request_id),
            rules=default_planning_v4_rules()))
        meal_scopes = [{"date": day.day_date.isoformat(), "period": slot.period, "anchor_poi_id": "p1"}
            for day in planning.days for slot in day.slots
            if slot.eligibility != "skipped" and slot.period in {"breakfast", "lunch", "dinner"}
            and slot.slot_id != "slot-2026-10-21-breakfast"]
        def all_submit(messages):
            data = payload(messages)
            return call("submit_restaurants", {"selections": [{"poi_id": "food",
                "slot_ids": [meal["scope_id"] for meal in data["meal_scopes"]],
                "recommendation_reason": "核实范围内的餐馆"}]}, "dining-submit")
        master = FakeModel(call("find_attractions", task_args(dates=["2026-10-20", "2026-10-21"]), "initial-sights"),
            spatial.batch(call("find_restaurants", spatial.dining_args(dates=["2026-10-20", "2026-10-21"], meals=meal_scopes), "initial-food"),
                call("recommend_lodging", spatial.lodging_args(dates=["2026-10-20", "2026-10-21"]), "initial-hotel")),
            final("采集完成"), initial, final("初版缺早餐"),
            call("find_restaurants", spatial.dining_args(dates=["2026-10-21"], meals=[{
                "date": "2026-10-21", "period": "breakfast", "anchor_poi_id": "p1"}]), "repair-food"),
            final("已补查"), revision, final("已修订"))
        dining = FakeModel(spatial.spatial_query, all_submit, final("提交"),
                           spatial.spatial_query, all_submit, final("补查提交"))
        maps = Mock(side_effect=spatial.map_data)
        trace = NativeChainTrace()
        collector = NativeMultiagentCollection(master_llm=master, weather_service=spatial.weather_fixture(),
            attraction_llm=FakeModel(call("maps_text_search", {"city": "上海", "keywords": "外滩"}), choice()),
            dining_llm=dining, lodging_llm=FakeModel(spatial.spatial_query, spatial.spatial_submit, final("提交")),
            adapter=AmapContractAdapter(maps), emit=trace.emit)
        from app.models.planning_v4.input import HardRequirement
        summary = planning.requirements.model_copy(update={"hard_requirements": (HardRequirement(
            requirement_id="fixture-must", kind="must_visit", text="外滩", source={"field":"remarks", "quote":"必须去外滩"}),)})
        result = NativeTripPlanner(master_llm=master, collector=collector, emit=trace.emit,
            requirement_interpreter=fixed_requirements(summary)).run(req)
        self.assertEqual(result.repair.status, "complete")
        self.assertEqual(result.repair.repair_states[1].completed_rounds, 1)
        self.assertEqual(result.draft.days[0], result.initial_draft.days[0])
        self.assertEqual(maps.call_count, 3)
        self.assertTrue(any(row["event"] == "repair.collect" for row in trace.events))
        tasks = [row["data"]["task"] for row in trace.events if row["event"] == "master.task"]
        self.assertEqual(tasks[-1]["scope"]["day_ids"], ["day-2026-10-21"])
        self.assertEqual(tasks[-1]["scope"]["slot_ids"], ["slot-2026-10-21-breakfast"])
        ids = {candidate.candidate_id for candidate in result.repair.candidates}
        self.assertTrue(all(candidate.candidate_id in ids for task in result.repair.task_results for candidate in task.candidates))

    def test_provider_map_failure_stops_technically_without_consuming_business_rounds(self):
        from app.agents.native_attraction_agent import NativeCandidateExecution
        from app.models.planning_v4 import TaskResult, FactStore
        from app.services.native_tool_loop import NativeToolLoopResult
        from app.services.amap_contract_adapter import AmapContractAdapter
        state = collection()
        candidates = tuple(candidate.model_copy(update={"slot_ids": tuple(slot for slot in candidate.slot_ids
            if slot != "slot-2026-10-21-breakfast")}) if candidate.category == "meal" else candidate
            for candidate in state.candidates)
        by_id = {candidate.candidate_id: candidate for candidate in candidates}
        children = tuple(replace(child, task_result=child.task_result.model_copy(update={
            "candidates": tuple(by_id[c.candidate_id] for c in child.task_result.candidates)})) for child in state.children)
        state = replace(state, candidates=candidates, children=children)
        calls = []
        def collect(*args, **kwargs):
            if kwargs.get("initial_state") is None: return state
            seed = kwargs["initial_state"]
            calls.append(kwargs["repair_request"])
            tool = AmapContractAdapter(lambda *a: {"result_status": "error", "result_error": {"category": "rate_limit"}}).search_text(keywords="早餐", city="上海")
            facts = FactStore(sources=(*seed.fact_store.sources, tool.source), places=seed.fact_store.places)
            child = NativeCandidateExecution(TaskResult(task_id="error-"+str(len(calls)), run_id=state.run_id,
                context_revision=3, request_id=request().request_id, status="partial", stop_reason="no_match"), facts,
                protocol=NativeToolLoopResult("", ({"role": "tool", "content": json.dumps({"ok": True,
                    "data": {"result_status": "error", "error": {"category": "rate_limit"}}})},), (), 1))
            protocol = NativeToolLoopResult("查询故障", tuple(kwargs["messages"]), (), 0)
            return replace(seed, children=(*seed.children, child), fact_store=facts, protocol=protocol)
        collector = Mock(); collector.run.side_effect = collect
        model = FakeModel(initial, final("初版缺早餐"))
        result = NativeTripPlanner(master_llm=model, collector=collector,
            requirement_interpreter=fixed_requirements(state.planning_input.requirements)).run(request())
        self.assertEqual(len(calls), 3)
        self.assertEqual(result.repair.repair_states[1].completed_rounds, 0)
        self.assertEqual(result.repair.repair_states[1].stop_reason, "technical_error")
        self.assertEqual(result.draft, result.initial_draft)
        self.assertTrue(all(attempt.error_code == "REPAIR_MAP_FAILED" for attempt in result.repair.attempts))
        self.assertEqual(len(model.requests), 2)

    def test_repair_collection_rejects_wrong_day_role_and_unrelated_meal_before_child(self):
        from app.agents.native_multiagent_collection import NativeMultiagentCollection
        from app.agents.daily_plan_generator import DailyPlanGenerator
        from app.agents.daily_plan_activity_repair import DailyPlanModelOutput
        from app.services.day_repair_service import DayRepairCollectionRequest, _collection_scope
        from app.services.planning_v4_validation import validate_daily_plan
        state = collection(); generator = DailyPlanGenerator()
        messages = generator._messages(state.planning_input, state.candidates, state.fact_store,
            state.area_hints, tuple(child.task_result for child in state.children))
        output = plan(messages); output["days"][1]["activities"].pop(0)
        proposal = generator._validate_proposal_output(DailyPlanModelOutput.model_validate(output),
            state.planning_input, state.candidates, state.fact_store, state.area_hints)
        draft = generator._build_draft(proposal, state.planning_input, state.candidates,
            state.fact_store, state.area_hints, state.run_id)
        report = validate_daily_plan(state.planning_input, draft, state.fact_store, state.candidates)
        issue = next(issue for issue in report.issues if is_required_place_gap(state.planning_input, issue))
        from app.services.day_repair_service import DayRepairCollectionTarget
        target = DayRepairCollectionTarget(day_id=draft.days[1].day_id, role="dining",
            issue_ids=(issue.issue_id,), slot_ids=(issue.target_id,))
        scoped = DayRepairCollectionRequest(planning_input=state.planning_input, draft=draft, day=draft.days[1],
            issues=(issue,), assignments=(), round_id="repair-fixture", fact_store=state.fact_store,
            candidates=state.candidates, area_hints=state.area_hints, task_results=tuple(c.task_result for c in state.children),
            targets=(target,))
        for name,args in [("recommend_lodging", spatial.lodging_args(dates=["2026-10-21"])),
                          ("find_restaurants", spatial.dining_args()),
                          ("find_restaurants", spatial.dining_args(dates=["2026-10-21"], meals=[{
                              "date":"2026-10-21", "period":"dinner", "anchor_poi_id":"p1"}]))]:
            master = FakeModel(call(name,args,"out-of-scope"),final("拒绝"))
            maps = Mock(side_effect=AssertionError("不应查询地图"))
            collector = NativeMultiagentCollection(master_llm=master, tool_executor=maps)
            result = collector.run(request(), initial_state=state, repair_request=scoped)
            self.assertEqual(len(result.children),len(state.children))
            self.assertEqual(json.loads(master.requests[1]["messages"][-1]["content"])["data"]["code"],"REPAIR_TASK_OUTSIDE_SCOPE")
            maps.assert_not_called()

    def test_cancellation_after_initial_submission_never_returns_plan(self):
        event = Event()
        def cancel(messages):
            event.set(); return final("取消")
        planner, _ = self.planner(FakeModel(initial, cancel))
        with self.assertRaises(NativeToolLoopError) as raised: planner.run(request(), cancel_event=event)
        self.assertEqual(raised.exception.code, "CANCELLED")

    def test_no_native_submit_is_not_a_successful_plan(self):
        planner, _ = self.planner(FakeModel(final("我已经安排了虚构行程")))
        with self.assertRaises(NativeToolLoopError) as raised: planner.run(request())
        self.assertEqual(raised.exception.code, "MASTER_DID_NOT_SUBMIT_PLAN")
