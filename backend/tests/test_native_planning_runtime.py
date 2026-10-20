"""正式接线第一点：固定契约、模拟模型/采集结果，不启动API、数据库或真实服务。"""
import asyncio
from dataclasses import replace
from datetime import datetime
import json
from threading import Event, get_ident
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from pydantic import ValidationError

from app.agents.native_attraction_agent import NativeCandidateExecution
from app.agents.native_multiagent_collection import NativeMultiagentCollection, NativeMultiagentCollectionResult
from app.agents.native_trip_planner import NativeTripPlanner
from app.models.planning_runtime import PlanningRunIdentity, PreviewProgress
from app.models.planning_v4 import TripRequest, RequirementSummary, TaskResult, CandidateRef
from app.models.planning_v4.facts import FactStore, FactSource, PlaceFact
from app.services.native_weather_service import NativeWeatherService
from app.services.amap_contract_adapter import AmapContractAdapter
from app.services.llm_service import NativeLLMCompletion
from app.services.native_chain_trace import NativeChainTrace
from app.services.native_planning_runtime import NativePlanningRuntime
from app.services.native_tool_loop import NativeToolLoopError, NativeToolLoopResult
from app.services.planning_input_builder import build_planning_input, PlanningInputBuildRequest
from app.services.planning_v4_rules import default_planning_v4_rules


def weather_fixture():
    return NativeWeatherService(adapter=AmapContractAdapter(lambda *_: {"result_status": "empty", "city": "上海", "forecasts": []}))


def request():
    return TripRequest(request_id="runtime-request", city="上海", lodging_query="测试住处",
        arrival_at=datetime.fromisoformat("2026-10-20T14:00:00+08:00"),
        departure_at=datetime.fromisoformat("2026-10-21T15:00:00+08:00"))


def fixture_state(req, *, run_id="native-fixture"):
    planning = build_planning_input(PlanningInputBuildRequest(request=req,
        requirements=RequirementSummary(requirements_id="fixture-requirements", request_id=req.request_id),
        rules=default_planning_v4_rules()))
    source = FactSource(tool_call_id="fixture-source", tool_name="maps_text_search", api_version="v3",
        queried_at=datetime.fromisoformat("2026-10-13T00:00:00+08:00"), effective_params={"city": "上海"})
    candidates, places = [], []
    for category, code in (("sightseeing", "110000"), ("meal", "050100"), ("lodging", "120300")):
        places.append(PlaceFact(fact_id="fact-"+category, poi_id="poi-"+category,
            name=req.lodging_query if category == "lodging" else "测试"+category,
            city="上海市", typecode=code, location={"lng": 121.49, "lat": 31.24},
            source_refs=(source.tool_call_id,)))
        candidates.append(CandidateRef(candidate_id="candidate-"+category, category=category,
            poi_id=places[-1].poi_id, place_fact_id=places[-1].fact_id,
            slot_ids=tuple(slot.slot_id for day in planning.days for slot in day.slots
                if slot.eligibility == "required" and ((category == "meal" and slot.period in ("breakfast", "lunch", "dinner"))
                or (category == "sightseeing" and slot.period in ("morning", "afternoon", "evening")))),
            lodging_day_ids=tuple(day.day_id for day in planning.days) if category == "lodging" else ()))
    facts = FactStore(sources=(source,), places=tuple(places))
    child = NativeCandidateExecution(task_result=TaskResult(task_id="fixture-child", run_id=run_id,
        context_revision=1, request_id=req.request_id, status="complete", stop_reason="target_met",
        candidates=tuple(candidates)), fact_store=facts)
    return NativeMultiagentCollectionResult(run_id, "", tuple(candidates), facts, (child,), (), 0, 1,
        planning, NativeToolLoopResult("", (), (), 0))


def tool_call(name, arguments, identifier="fixture-call"):
    return NativeLLMCompletion({"role": "assistant", "content": None, "tool_calls": [
        {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]},
        None, "tool_calls")


def final():
    return NativeLLMCompletion({"role": "assistant", "content": "已提交"}, None, "stop")


class PlanModel:
    """只模拟每日提案/单日修订响应，不运行其他智能体。"""
    def __init__(self, *, omit_first=False, no_submit=False):
        self.requests = []
        self.omit_first = omit_first
        self.no_submit = no_submit

    def invoke_native(self, messages, *, tools, **kwargs):
        self.requests.append(messages)
        if self.no_submit or messages[-1]["role"] == "tool":
            return final()
        data = json.loads(next(m["content"] for m in reversed(messages) if m["role"] == "user"))
        def day_output(day):
            activities = []
            if "slots" in day:
                slot_ids = [slot["slot_id"] for slot in day["slots"] if slot["eligibility"] == "required"]
            else:
                options = next(o for o in data["day_slot_options"] if o["day_id"] == day["day_id"])
                slot_ids = [*options["available_meal_slot_ids"], *options["available_sightseeing_slot_ids"]]
                periods = ["breakfast", "morning", "lunch", "afternoon", "dinner", "evening"]
                slot_ids.sort(key=lambda slot: periods.index(slot.split("-")[-1]))
            for slot_id in slot_ids:
                category = "meal" if slot_id.split("-")[-1] in ("breakfast", "lunch", "dinner") else "sightseeing"
                candidate = next((c for c in data["candidates"] if slot_id in c["applicable_slot_ids"]), None)
                if candidate is None: continue
                activities.append({"proposal_ref": slot_id, "candidate_ref": candidate["candidate_id"],
                    "slot_id": slot_id, "kind": category, "recommendation_reason": "固定测试候选；重复只用于验证"})
            return {"day_id": day["day_id"], "day_date": day["day_date"], "theme": "测试",
                "activities": activities, "lodging": {"candidate_ref": "candidate-lodging"}}
        if tools[0]["function"]["name"] == "submit_daily_plan":
            days = [day_output(day) for day in data["planning_input"]["days"]]
            if self.omit_first:
                days[1]["activities"] = [a for a in days[1]["activities"] if not a["slot_id"].endswith("breakfast")]
            return tool_call("submit_daily_plan", {"days": days}, "initial-submit")
        current = data["current_day"]
        day = next(d for d in data["planning_input"]["days"] if d["day_id"] == current["day_id"])
        output = day_output(day)
        by_slot = {a["slot_id"]: a for a in current["activities"]}
        for activity in output["activities"]:
            old = by_slot.get(activity["slot_id"])
            if old:
                activity.update(existing_activity_id=old["activity_id"], recommendation_reason=old["recommendation_reason"])
        return tool_call("submit_day_revision", {"replacement_day": output}, "revision-submit")


def planner(*, model=None, collector=None, emit=None):
    collector = collector or Mock()
    if collector.run.side_effect is None:
        collector.run.side_effect = lambda req, **kw: fixture_state(req, run_id=kw.get("run_id", "native-fixture"))
    return NativeTripPlanner(master_llm=model or PlanModel(), collector=collector, emit=emit,
        requirement_interpreter=RequirementFixture()), collector


class RequirementFixture:
    def run(self, req, **kwargs):
        return SimpleNamespace(summary=fixture_state(req).planning_input.requirements,
                               protocol=NativeToolLoopResult("", (), (), 0))


class RuntimeContractTests(unittest.TestCase):
    def test_shared_progress_contract_preserves_existing_import_and_json(self):
        from app.services.preview_task_service import PreviewProgress as ExistingProgress
        self.assertIs(ExistingProgress, PreviewProgress)
        self.assertEqual(PreviewProgress(stage="dispatch").model_dump(),
            {"stage": "dispatch", "completed": 0, "total": 0, "message": None})
        for payload in ({"stage": "unknown"}, {"stage": "ready", "completed": -1}, {"stage": "ready", "secret": "value"}):
            with self.assertRaises(ValidationError): PreviewProgress(**payload)

    def test_identity_strict_and_immutable(self):
        identity = PlanningRunIdentity(task_id="trip-1", plan_id="plan-1")
        with self.assertRaises(ValidationError): identity.plan_id = "plan-2"
        for value in ("", " ", " trip-1", "trip-1 ", 42, "x"*129):
            with self.assertRaises(ValidationError): PlanningRunIdentity(task_id=value, plan_id="plan-1")

    def test_external_identity_reaches_initial_and_repaired_draft(self):
        for missing in (False, True):
            with self.subTest(repair=missing):
                trace = NativeChainTrace()
                instance, collector = planner(model=PlanModel(omit_first=missing), emit=trace.emit)
                result = instance.run(request(), task_id="trip-formal", plan_id="plan-formal")
                self.assertEqual(result.repair.status, "complete")
                self.assertEqual(result.initial_draft.plan_id, "plan-formal")
                self.assertEqual(result.draft.plan_id, "plan-formal")
                self.assertEqual(result.draft.source_run_id, "trip-formal")
                self.assertEqual(result.collection.run_id, "trip-formal")
                self.assertTrue(all(c.task_result.run_id == "trip-formal" for c in result.collection.children))
                self.assertEqual(collector.run.call_args.kwargs["run_id"], "trip-formal")
                self.assertEqual(result.draft.revision, 2 if missing else 1)

    def test_invalid_or_partial_identity_stops_before_progress_or_models(self):
        for args in ({"task_id": "trip"}, {"plan_id": "plan"}, {"task_id": "", "plan_id": "plan"},
                     {"task_id": "trip", "plan_id": " "}, {"task_id": 4, "plan_id": "plan"}):
            with self.subTest(args=args):
                model = PlanModel(); instance, collector = planner(model=model)
                with self.assertRaises(NativeToolLoopError) as caught: instance.run(request(), **args)
                self.assertEqual(caught.exception.code, "PLANNING_IDENTITY_INVALID")
                collector.run.assert_not_called(); self.assertEqual(model.requests, [])

    def test_foreign_collection_identity_and_request_rejected_before_plan(self):
        for foreign in (fixture_state(request(), run_id="other-trip"), fixture_state(request().model_copy(update={"remarks": "other"}), run_id="trip")):
            collector = Mock(); collector.run.return_value = foreign
            model = PlanModel()
            instance = NativeTripPlanner(master_llm=model, collector=collector, requirement_interpreter=RequirementFixture())
            with self.assertRaises(NativeToolLoopError) as caught:
                instance.run(request(), task_id="trip", plan_id="plan")
            self.assertEqual(caught.exception.code, "COLLECTION_IDENTITY_MISMATCH")
            self.assertEqual(model.requests, [])

    def test_standalone_entry_keeps_generated_identity_without_progress(self):
        result = planner()[0].run(request())
        self.assertEqual(result.collection.run_id, "native-fixture")
        self.assertTrue(result.draft.plan_id.startswith("plan"))
        self.assertEqual(result.draft.source_run_id, "native-fixture")

    def test_no_submission_cannot_report_planning_finished(self):
        updates = []
        async def progress(update): updates.append(update)
        with self.assertRaises(NativeToolLoopError):
            planner(model=PlanModel(no_submit=True))[0].run(request(), progress=progress)
        self.assertFalse(any(u.stage in ("ready", "repair") for u in updates))

    def test_collector_accepts_formal_identity_and_rejects_foreign_seed(self):
        model = Mock()
        model.invoke_native.side_effect = [tool_call("find_attractions", {"city": "上海", "dates": ["2026-10-20"],
            "objective": "测试采集", "candidate_target": 1}), final()]
        child = Mock()
        child.invoke_native.side_effect = [tool_call("maps_text_search", {"city": "上海", "keywords": "外滩"}),
            tool_call("submit_attractions", {"selections": [{"poi_id": "p1", "recommendation_reason": "测试地点"}]} , "child-submit"), final()]
        maps = Mock(return_value={"result_status": "ok", "pois": [{"id": "p1", "name": "外滩",
            "location": "121.49,31.24", "cityname": "上海市", "typecode": "110000", "type": "风景名胜"}],
            "result_source": {"api_version": "v3", "queried_at": "2026-10-13T00:00:00+08:00"}})
        collector = NativeMultiagentCollection(weather_service=weather_fixture(), master_llm=model, attraction_llm=child, adapter=AmapContractAdapter(maps))
        result = collector.run(request(), run_id="trip-formal")
        self.assertEqual(result.run_id, "trip-formal")
        self.assertEqual(result.children[0].task_result.run_id, "trip-formal")
        before = model.invoke_native.call_count
        with self.assertRaises(NativeToolLoopError) as caught:
            collector.run(request(), run_id="other-trip", initial_state=result)
        self.assertEqual(caught.exception.code, "COLLECTION_IDENTITY_MISMATCH")
        self.assertEqual(model.invoke_native.call_count, before)

    def test_formal_identity_and_cancel_event_survive_repair_collection(self):
        calls = []
        def collect(req, **kwargs):
            calls.append(kwargs)
            state = fixture_state(req, run_id=kwargs["run_id"])
            if kwargs.get("initial_state") is not None:
                seed = kwargs["initial_state"]
                breakfast = next(c for c in state.candidates if c.category == "meal").model_copy(update={
                    "candidate_id": "candidate-breakfast", "slot_ids": ("slot-2026-10-21-breakfast",)})
                child = replace(state.children[0], task_result=state.children[0].task_result.model_copy(update={
                    "task_id": "fixture-repair-child", "candidates": (breakfast,)}))
                return replace(seed, candidates=(*seed.candidates, breakfast), children=(*seed.children, child),
                    protocol=NativeToolLoopResult("", tuple(kwargs["messages"]), (), 0))
            candidates = tuple(c.model_copy(update={"slot_ids": tuple(s for s in c.slot_ids
                if s != "slot-2026-10-21-breakfast")}) if c.category == "meal" else c for c in state.candidates)
            child = replace(state.children[0], task_result=state.children[0].task_result.model_copy(update={"candidates": candidates}))
            return replace(state, candidates=candidates, children=(child,))
        collector = Mock(); collector.run.side_effect = collect
        event = Event()
        instance = NativeTripPlanner(master_llm=PlanModel(omit_first=True), collector=collector,
                                     requirement_interpreter=RequirementFixture())
        result = instance.run(request(), task_id="trip", plan_id="plan", cancel_event=event)
        self.assertEqual(result.repair.status, "complete")
        self.assertEqual(result.draft.plan_id, "plan"); self.assertEqual(result.draft.source_run_id, "trip")
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(c["run_id"] == "trip" and c["cancel_event"] is event for c in calls))
        self.assertEqual(calls[1]["initial_state"].run_id, "trip")


class AsyncRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_progress_runs_on_caller_loop_including_worker_submission(self):
        loop, thread, updates = asyncio.get_running_loop(), get_ident(), []
        async def progress(update):
            self.assertIs(asyncio.get_running_loop(), loop); self.assertEqual(get_ident(), thread)
            updates.append(update)
        result = await planner()[0].run_async(request(), task_id="trip", plan_id="plan", progress=progress)
        self.assertEqual(result.repair.status, "complete")
        stages = list(dict.fromkeys(u.stage for u in updates))
        self.assertEqual(stages, ["planning_input", "dispatch", "proposal", "validation", "repair"])
        self.assertEqual(updates[-1].completed, 1)
        self.assertFalse(any(u.stage in ("ready", "cost", "current_routes") for u in updates))

    async def test_repair_progress_names_only_target_date(self):
        updates = []
        async def progress(update): updates.append(update)
        result = await planner(model=PlanModel(omit_first=True))[0].run_async(request(), progress=progress)
        self.assertEqual(result.draft.revision, 2)
        targeted = [u.message for u in updates if u.stage == "repair" and "2026-" in u.message]
        self.assertTrue(targeted); self.assertTrue(all("2026-10-21" in s for s in targeted))

    async def test_caller_event_cancelled_before_or_during_progress_stops_collection(self):
        for already in (True, False):
            event = Event()
            if already: event.set()
            async def progress(_): event.set()
            instance, collector = planner()
            with self.assertRaises(NativeToolLoopError) as caught:
                await instance.run_async(request(), cancel_event=event, progress=progress)
            self.assertEqual(caught.exception.code, "CANCELLED"); collector.run.assert_not_called()

    async def test_progress_failure_is_safe_and_stops_collection(self):
        event = Event()
        async def progress(_): raise RuntimeError("private callback detail")
        instance, collector = planner()
        with self.assertRaises(NativeToolLoopError) as caught:
            await instance.run_async(request(), cancel_event=event, progress=progress)
        self.assertEqual(caught.exception.code, "PROGRESS_CALLBACK_FAILED")
        self.assertNotIn("private", str(caught.exception)); self.assertTrue(event.is_set())
        collector.run.assert_not_called()

    async def test_coroutine_cancel_sets_shared_event_and_rejects_late_collection(self):
        entered, release, event = Event(), Event(), Event()
        def collect(req, **kwargs):
            self.assertIs(kwargs["cancel_event"], event)
            entered.set(); release.wait(2)
            return fixture_state(req, run_id=kwargs["run_id"])
        collector = Mock(); collector.run.side_effect = collect
        model = PlanModel(); instance = NativeTripPlanner(master_llm=model, collector=collector,
                                                         requirement_interpreter=RequirementFixture())
        task = asyncio.create_task(instance.run_async(request(), task_id="trip", plan_id="plan", cancel_event=event))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            self.assertTrue(event.is_set()); self.assertEqual(model.requests, [])
        finally: release.set()

    async def test_concurrent_runs_on_one_planner_keep_identity_and_progress_separate(self):
        instance, _ = planner()
        updates = {"one": [], "two": []}
        async def run(key):
            async def progress(update): updates[key].append(update)
            return await instance.run_async(request(), task_id="trip-"+key, plan_id="plan-"+key, progress=progress)
        one, two = await asyncio.gather(run("one"), run("two"))
        self.assertEqual((one.draft.plan_id, two.draft.plan_id), ("plan-one", "plan-two"))
        self.assertEqual((one.draft.source_run_id, two.draft.source_run_id), ("trip-one", "trip-two"))
        self.assertTrue(all(rows[-1].completed == 1 for rows in updates.values()))

    async def test_runtime_worker_progress_failure_never_accepts_late_result(self):
        event = Event()
        async def progress(_): raise RuntimeError("private")
        runtime = NativePlanningRuntime(progress=progress, cancel_event=event)
        with self.assertRaises(NativeToolLoopError) as caught:
            await runtime.blocking(runtime.report_from_thread, "validation")
        self.assertEqual(caught.exception.code, "PROGRESS_CALLBACK_FAILED")
        self.assertTrue(event.is_set())

    async def test_callback_failure_inside_native_submission_remains_progress_error(self):
        updates, trace = [], NativeChainTrace()
        async def progress(update):
            updates.append(update)
            if update.stage == "validation": raise RuntimeError("private callback detail")
        instance, _ = planner(emit=trace.emit)
        with self.assertRaises(NativeToolLoopError) as caught:
            await instance.run_async(request(), progress=progress)
        self.assertEqual(caught.exception.code, "PROGRESS_CALLBACK_FAILED")
        self.assertFalse(any(r["event"] in ("planning.initial", "planning.result") for r in trace.events))


if __name__ == "__main__": unittest.main()
