"""预览适配独立模拟测试：不导入其他测试文件；真实服务、正式工厂保持不变。"""
import asyncio
from dataclasses import replace, dataclass
from datetime import datetime
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from app.agents.native_multiagent_collection import NativeMultiagentCollectionResult
from app.agents.native_trip_planner import NativeTripPlanner
from app.models.planning_v4 import (TripRequest, RequirementSummary, FactSource, FactStore, PlaceFact, CandidateRef,
    DraftActivity, DraftDay, PlanDraft, LodgingRef, RepairState, EditCommand, AreaHint)
from app.models.planning_v4_preview import PreviewSnapshot
from app.services.amap_contract_adapter import AmapContractAdapter
from app.services.current_route_service import CurrentRouteService
from app.services.day_repair_service import DayRepairResult
from app.services.native_endpoint_service import NativeEndpointService
from app.services.native_preview_adapter import NativePreviewAdapter, NativePreviewPipeline, NativePreviewError
from app.services.native_tool_loop import NativeToolLoopError, NativeToolLoopResult
from app.services.llm_service import NativeLLMCompletion
from app.services.observation_service import ObservationService
from app.services.planning_input_builder import PlanningInputBuildRequest, build_planning_input
from app.services.planning_v4_rules import default_planning_v4_rules
from app.services.planning_v4_validation import validate_daily_plan
from app.services.planning_v4_preview_edit_service import PlanningV4PreviewEditService
from app.services.preview_repository import PreviewRepository
from app.services.preview_task_service import PreviewTaskService
from app.services.task_repository import TaskRepository
from app.services.task_service import TaskService


@dataclass(frozen=True)
class NativeTripPlanFixture:
    """旧测试构造辅助；生产端只接收公开PlanningOutcome。"""
    planning_input: object
    initial_draft: object
    repair: object
    collection: object
    master_history: object
    @property
    def draft(self): return self.repair.draft
    @property
    def fact_store(self): return self.repair.facts
    @property
    def report(self): return self.repair.report


def public_outcome(value):
    from app.models.planning_outcome import PlanningOutcome
    from app.models.planning_execution import PlanningSnapshot, DraftResult
    from app.models.planning_evidence import EndpointEvidence, WeatherEvidence
    from app.models.planning_runtime import PlanningRunIdentity
    identity=PlanningRunIdentity(task_id=value.collection.run_id,plan_id=value.initial_draft.plan_id)
    def evidence(item, cls):
        return None if item is None else cls.model_validate({key:getattr(item,key) for key in cls.model_fields if key!='schema_version'})
    snapshot=PlanningSnapshot.model_construct(identity=identity,planning_input=value.planning_input,
        facts=value.fact_store,candidates=value.repair.candidates,area_hints=value.repair.area_hints,
        task_results=value.repair.task_results,
        endpoint_evidence=evidence(value.collection.endpoints,EndpointEvidence),
        weather_evidence=evidence(value.collection.weather,WeatherEvidence))
    initial=DraftResult.model_construct(identity=identity,planning_input=value.planning_input,draft=value.initial_draft,
        report=validate_daily_plan(value.collection.planning_input,value.initial_draft,value.collection.fact_store,value.collection.candidates),
        facts=value.collection.fact_store,candidates=value.collection.candidates,area_hints=())
    return PlanningOutcome.model_construct(status=value.repair.status,error_code=None,initial_result=initial,
        snapshot=snapshot,repair=value.repair)


def request():
    return TripRequest(request_id="preview-fixture", city="上海", lodging_query="测试住处",
        arrival_at=datetime.fromisoformat("2026-10-20T14:00:00+08:00"),
        departure_at=datetime.fromisoformat("2026-10-21T20:00:00+08:00"))


def native_result(req=None, *, task_id="task", plan_id="plan", partial=False, empty=False, endpoints=False):
    req = req or request()
    planning = build_planning_input(PlanningInputBuildRequest(request=req, requirements=RequirementSummary(
        requirements_id="preview-summary", request_id=req.request_id), rules=default_planning_v4_rules()))
    source = FactSource(tool_call_id="fixture-source", tool_name="maps_text_search", api_version="v3",
        queried_at=datetime.fromisoformat("2026-10-13T00:00:00+08:00"), effective_params={"city": "上海"})
    places, candidates = [], []
    for index, (category, code, price) in enumerate((("sightseeing", "110000", "20"), ("meal", "050100", None), ("lodging", "120300", None))):
        places.append(PlaceFact(fact_id="fact-"+category, poi_id="poi-"+category, name=req.lodging_query if category == "lodging" else "测试"+category,
            city="上海市", typecode=code, location={"lng": 121.48+index*.01, "lat": 31.24},
            source_refs=(source.tool_call_id,), cost={"value": price}))
        candidates.append(CandidateRef(candidate_id="candidate-"+category, category=category,
            poi_id=places[-1].poi_id, place_fact_id=places[-1].fact_id,
            slot_ids=tuple(s.slot_id for d in planning.days for s in d.slots if s.eligibility == "required"
                and ((category == "meal" and s.period in ("breakfast", "lunch", "dinner"))
                     or (category == "sightseeing" and s.period in ("morning", "afternoon", "evening")))),
            lodging_day_ids=tuple(d.day_id for d in planning.days) if category == "lodging" else ()))
    facts = FactStore(sources=(source,), places=tuple(places))
    days = []
    for day in planning.days:
        activities = []
        for slot in day.slots:
            if slot.eligibility != "required": continue
            category = "meal" if slot.period in ("breakfast", "lunch", "dinner") else "sightseeing"
            candidate = next(c for c in candidates if c.category == category)
            activities.append(DraftActivity(activity_id=f"activity:{slot.slot_id}", slot_id=slot.slot_id, kind=category,
                candidate_ref=candidate.candidate_id, poi_id=candidate.poi_id, place_fact_id=candidate.place_fact_id))
        days.append(DraftDay(day_id=day.day_id, day_date=day.day_date, theme="初版", activities=tuple(activities),
            lodging=LodgingRef(candidate_ref="candidate-lodging", poi_id="poi-lodging", place_fact_id="fact-lodging", selection_source="user"),
            weather_advice="天气未知"))
    initial = PlanDraft(plan_id=plan_id, request_id=req.request_id, source_run_id=task_id, revision=1,
        rule_version=planning.rules.rule_version, days=tuple(days))
    days = [d.model_copy(update={"theme": "最终修订"}) for d in days]
    if partial: days[0] = days[0].model_copy(update={"activities": days[0].activities[1:]})
    if empty: days = [d.model_copy(update={"activities": (), "lodging": None}) for d in days]
    for index, day in enumerate(days):
        used = {a.slot_id for a in day.activities}
        days[index] = day.model_copy(update={"unfilled_slot_ids": tuple(s.slot_id for s in planning.days[index].slots
            if s.eligibility == "required" and s.slot_id not in used)})
    draft = initial.model_copy(update={"revision": 2, "base_revision": 1, "days": tuple(days)})
    states = tuple(RepairState(day_id=day.day_id, completed_rounds=3 if (partial and i == 0) or empty else 0,
        status="stopped" if (partial and i == 0) or empty else "complete",
        stop_reason="round_limit" if (partial and i == 0) or empty else "complete") for i, day in enumerate(days))
    endpoint_result = None
    if endpoints:
        endpoint_result = NativeEndpointService(adapter=AmapContractAdapter(lambda *_: {"result_status": "empty", "pois": []})).run(
            planning, run_id=task_id)
    collection = NativeMultiagentCollectionResult(task_id, "采集结果", tuple(candidates), facts, (), (), 0, 1, planning,
        endpoints=endpoint_result)
    # 最终池包含新增候选，故意不放在初版采集池，验证适配器采用修订结果。
    extra = PlaceFact(fact_id="fact-extra", poi_id="poi-extra", name="备用餐馆", city="上海市", typecode="050100",
        location={"lng": 121.53, "lat": 31.24}, source_refs=(source.tool_call_id,))
    final_facts = facts.model_copy(update={"places": (*facts.places, extra)})
    final_candidates = (*candidates, CandidateRef(candidate_id="candidate-extra", category="meal", poi_id=extra.poi_id,
        place_fact_id=extra.fact_id, slot_ids=candidates[1].slot_ids))
    report = validate_daily_plan(planning, draft, final_facts, final_candidates, repair_states=states)
    repair = DayRepairResult(status="partial" if partial or empty else "complete", draft=draft, facts=final_facts,
        candidates=final_candidates, report=report, repair_states=states, stop_reason="round_limit" if partial or empty else "complete")
    return NativeTripPlanFixture(planning, initial, repair, collection, ({"provider_reasoning": "private-model-history"},))


class FixturePlanner:
    def __init__(self, *, partial=False, endpoints=False):
        self.calls = []; self.partial = partial; self.endpoints = endpoints

    async def run_async(self, req, **kwargs):
        self.calls.append(kwargs)
        return public_outcome(native_result(req, task_id=kwargs["task_id"], plan_id=kwargs["plan_id"], partial=self.partial, endpoints=self.endpoints))


class NativePreviewAdapterTests(unittest.TestCase):
    def convert(self, result=None, req=None):
        req = req or request()
        return NativePreviewAdapter().convert(public_outcome(result or native_result(req)), request=req, task_id="task", plan_id="plan")

    def test_final_revision_facts_candidates_order_and_costs_match_existing_contract(self):
        native = native_result(); result = self.convert(native); preview = result.snapshot.preview
        self.assertEqual(preview.draft.days, native.draft.days)
        self.assertEqual(preview.draft.revision, 2)
        self.assertEqual(preview.draft.base_revision, 1)
        self.assertEqual(preview.draft.source_run_id, "task")
        self.assertEqual(preview.facts, native.repair.facts)
        self.assertEqual(preview.candidates, native.repair.candidates)
        self.assertNotEqual(preview.facts, native.collection.fact_store)
        self.assertEqual(result.snapshot.cost.based_on_revision, 2)
        self.assertGreater(result.snapshot.cost.known_sightseeing_meal_per_person_subtotal, 0)
        self.assertTrue(all(s.cost_status == "available" for s in preview.day_summaries))
        self.assertEqual(PreviewSnapshot.model_validate_json(result.snapshot.model_dump_json()), result.snapshot)
        self.assertIsNone(result.route_context); self.assertIsNone(result.route_service); self.assertIsNone(result.backfill)

    def test_only_current_adjacent_routes_pending_and_no_arrival_or_return_leg(self):
        result = self.convert(); draft = result.snapshot.preview.draft
        self.assertTrue(result.initial_route_day_ids)
        for day in draft.days:
            legs = [leg for leg in draft.legs if leg.day_id == day.day_id]
            self.assertEqual(len(legs), len(day.activities))
            self.assertEqual(legs[0].from_endpoint.kind, "lodging")
            self.assertEqual([l.to_endpoint.reference_id for l in legs], [a.activity_id for a in day.activities])
            self.assertTrue(all(l.from_endpoint.kind not in {"arrival", "departure"} and l.to_endpoint.kind == "activity" for l in legs))
        self.assertEqual(result.snapshot.preview.calculation.current_routes, "pending")
        self.assertEqual(result.snapshot.preview.calculation.alternative_backfill, "not_required")
        self.assertFalse(result.snapshot.preview.facts.routes)

    def test_final_repair_area_hints_are_projected_with_final_fact_pool(self):
        native = native_result()
        place = next(p for p in native.fact_store.places if p.poi_id == "poi-extra")
        area = AreaHint(area_hint_id="final-area", label="最终补查区域", anchor_poi_ids=(place.poi_id,), center=place.location,
            basis_fact_ids=(place.fact_id,), basis="verified_candidate_anchor")
        native = replace(native, repair=native.repair.model_copy(update={"area_hints": (area,)}))
        preview = self.convert(native).snapshot.preview
        self.assertEqual(preview.draft.area_hints, (area,))
        self.assertEqual(native.initial_draft.area_hints, ())

    def test_partial_round_limit_stays_editable_with_same_repair_counts_and_missing_slots(self):
        native = native_result(partial=True); preview = self.convert(native).snapshot.preview
        self.assertEqual(preview.validation.repair_states, native.repair.repair_states)
        self.assertEqual(preview.validation.repair_states[0].completed_rounds, 3)
        self.assertEqual(preview.draft.days[0].unfilled_slot_ids, native.draft.days[0].unfilled_slot_ids)
        self.assertTrue(any(i.triggers_repair for i in preview.validation.issues))

    def test_empty_draft_has_no_transport_work_but_remains_incomplete(self):
        result = self.convert(native_result(empty=True))
        self.assertEqual(result.initial_route_day_ids, ())
        self.assertEqual(result.snapshot.preview.draft.legs, ())
        self.assertEqual(result.snapshot.preview.calculation.current_routes, "completed")
        self.assertTrue(any(d.completeness == "incomplete" for d in result.snapshot.preview.validation.days))

    def test_endpoint_unknown_status_and_reason_survive_preview_projection(self):
        req = request().model_copy(update={"arrival_place_query": "不存在的车站"})
        preview = self.convert(native_result(req, endpoints=True), req).snapshot.preview
        notices = [i for i in preview.validation.issues if i.code.startswith("ENDPOINT_")]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].code, "ENDPOINT_ARRIVAL_UNKNOWN")
        self.assertIn("未查到你填写的到达地点", notices[0].message)
        self.assertTrue(all(not i.triggers_repair and i.based_on_revision == 2 for i in notices))

    def test_snapshot_is_detached_and_does_not_persist_model_protocol_or_history(self):
        native = native_result(); result = self.convert(native)
        text = result.snapshot.model_dump_json()
        self.assertNotIn("private-model-history", text)
        self.assertNotIn("master_history", text)
        self.assertNotIn("provider_reasoning", text)
        result.snapshot.preview.facts.sources[0].effective_params["city"] = "fixture-mutated"
        self.assertEqual(native.fact_store.sources[0].effective_params["city"], "上海")
        self.assertEqual(native.draft.legs, ())

    def test_foreign_request_plan_run_or_initial_draft_is_rejected(self):
        native = native_result()
        for invalid in [replace(native, collection=replace(native.collection, run_id="foreign")),
            replace(native, initial_draft=native.initial_draft.model_copy(update={"plan_id": "foreign"})),
            replace(native, repair=native.repair.model_copy(update={"draft": native.draft.model_copy(update={"source_run_id": "foreign"})})),
            replace(native, planning_input=native.planning_input.model_copy(update={"request": request().model_copy(update={"request_id": "foreign"})}))]:
            with self.assertRaises(NativePreviewError) as caught: self.convert(invalid)
            self.assertEqual(caught.exception.code, "NATIVE_RESULT_IDENTITY_MISMATCH")

    def test_stale_report_is_rejected_before_recomputation(self):
        native = native_result()
        with self.assertRaises(NativePreviewError) as caught:
            self.convert(replace(native, repair=native.repair.model_copy(update={"report": native.report.model_copy(update={"based_on_revision": 1})})))
        self.assertEqual(caught.exception.code, "NATIVE_REPORT_VERSION_MISMATCH")

    def test_broken_source_closure_is_not_deliverable(self):
        native = native_result()
        with self.assertRaises(NativePreviewError) as caught:
            self.convert(replace(native, repair=native.repair.model_copy(update={"facts": native.fact_store.model_copy(update={"sources": ()})})))
        self.assertEqual(caught.exception.code, "NATIVE_PREVIEW_INVALID")

    def test_technical_failure_does_not_become_ready_preview(self):
        native = native_result()
        with self.assertRaises(NativePreviewError) as caught:
            self.convert(replace(native, repair=native.repair.model_copy(update={"status": "technical_error", "error_code": "private-provider-response"})))
        self.assertEqual(caught.exception.code, "NATIVE_PLANNING_TECHNICAL_ERROR")
        self.assertNotIn("private-provider-response", str(caught.exception))

    def test_verified_endpoint_facts_must_survive_final_repair_and_do_not_create_routes(self):
        req = request().model_copy(update={"arrival_place_query": "测试车站"})
        native = native_result(req)
        endpoint = NativeEndpointService(adapter=AmapContractAdapter(lambda *_: {"result_status": "ok", "pois": [
            {"id": "station", "name": "测试车站", "cityname": "上海", "location": "121.40,31.21"}]})).run(native.planning_input, run_id="task")
        native = replace(native, collection=replace(native.collection, endpoints=endpoint))
        with self.assertRaises(NativePreviewError) as caught: self.convert(native, req)
        self.assertEqual(caught.exception.code, "NATIVE_FACTS_MISSING")
        merged = FactStore(sources=(*native.fact_store.sources, *endpoint.fact_store.sources),
                          places=(*native.fact_store.places, *endpoint.fact_store.places))
        native = replace(native, repair=native.repair.model_copy(update={"facts": merged}))
        preview = self.convert(native, req).snapshot.preview
        self.assertIn("station", {p.poi_id for p in preview.facts.places})
        self.assertFalse(any(l.from_endpoint.kind == "arrival" or l.to_endpoint.kind == "arrival" for l in preview.draft.legs))
        self.assertFalse(any(i.code == "ENDPOINT_ARRIVAL_UNKNOWN" for i in preview.validation.issues))


class NativePreviewPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_native_planner_mock_tool_response_converts_to_preview(self):
        from planning_collection_fixtures import normalized_collection
        seed = normalized_collection(native_result().collection)
        interpreter = Mock(); interpreter.run.return_value = SimpleNamespace(summary=seed.planning_input.requirements,
            protocol=NativeToolLoopResult("", (), (), 0))
        collector = Mock(); collector.run.side_effect = lambda req, **kw: normalized_collection(replace(seed, protocol=NativeToolLoopResult("", (), (), 0)), kw["run_id"])
        class Master:
            def __init__(self): self.called = False
            def invoke_native(self, messages, *, tools, **kwargs):
                if self.called: return NativeLLMCompletion({"role": "assistant", "content": "已提交"}, None, "stop")
                self.called = True; data = json.loads(messages[-1]["content"]); output = []
                for day in data["planning_input"]["days"]:
                    activities = []
                    options = next(o for o in data["day_slot_options"] if o["day_id"] == day["day_id"])
                    periods = ["breakfast", "morning", "lunch", "afternoon", "dinner", "evening"]
                    slots = sorted((*options["available_meal_slot_ids"], *options["available_sightseeing_slot_ids"]),
                                   key=lambda slot: periods.index(slot.split("-")[-1]))
                    for slot in slots:
                        candidate = next(c for c in data["candidates"] if slot in c["applicable_slot_ids"])
                        activities.append({"proposal_ref": slot, "slot_id": slot,
                            "candidate_ref": candidate["candidate_id"], "kind": candidate["category"]})
                    output.append({"day_id": day["day_id"], "day_date": day["day_date"], "theme": "原生模拟",
                        "activities": activities, "lodging": {"candidate_ref": "candidate-lodging"}})
                return NativeLLMCompletion({"role": "assistant", "content": None, "tool_calls": [{"id": "native-plan",
                    "type": "function", "function": {"name": "submit_daily_plan", "arguments": json.dumps({"days": output})}}]}, None, "tool_calls")
        planner = NativeTripPlanner(master_llm=Master(), collector=collector, requirement_interpreter=interpreter)
        routes = CurrentRouteService(adapter=AmapContractAdapter(Mock()))
        result = await NativePreviewPipeline(planner_factory=lambda: planner, current_route_service_factory=lambda: routes).run(
            request(), task_id="task", plan_id="plan")
        try:
            self.assertEqual(result.snapshot.preview.draft.revision, 1)
            self.assertEqual(result.snapshot.preview.draft.source_run_id, "task")
            self.assertTrue(all(d.theme == "原生模拟" for d in result.snapshot.preview.draft.days))
        finally: await result.route_context.aclose()

    async def test_factory_bridge_progress_and_context_ownership_without_route_query(self):
        planner = FixturePlanner(); maps = Mock()
        routes = CurrentRouteService(adapter=AmapContractAdapter(maps)); updates = []
        async def progress(update): updates.append(update.stage)
        result = await NativePreviewPipeline(planner_factory=lambda: planner, current_route_service_factory=lambda: routes).run(
            request(), task_id="task", plan_id="plan", progress=progress)
        try:
            self.assertEqual(planner.calls[0]["task_id"], "task")
            self.assertEqual(planner.calls[0]["plan_id"], "plan")
            self.assertEqual(updates, ["cost", "cost"])
            self.assertFalse(result.route_context.cancel_event.is_set())
            self.assertIs(result.route_service, routes)
            result.route_context.assert_binding(result.snapshot.preview.planning_input, result.snapshot.preview.draft)
            maps.assert_not_called()
        finally: await result.route_context.aclose()

    async def test_progress_failure_closes_unretained_context(self):
        routes = CurrentRouteService(adapter=AmapContractAdapter(Mock()))
        contexts = []; original = routes.create_route_context
        def create(*args):
            context = original(*args); contexts.append(context); return context
        routes.create_route_context = create
        async def progress(update):
            if update.completed: raise RuntimeError("private-callback-error")
        with self.assertRaises(NativeToolLoopError) as caught:
            await NativePreviewPipeline(planner_factory=FixturePlanner, current_route_service_factory=lambda: routes).run(
                request(), task_id="task", plan_id="plan", progress=progress)
        self.assertEqual(caught.exception.code, "PROGRESS_CALLBACK_FAILED")
        self.assertTrue(contexts[0].cancel_event.is_set())

    async def test_cancel_during_native_planning_sets_shared_signal_and_never_converts(self):
        entered = asyncio.Event(); signals = []
        class Blocked:
            async def run_async(self, req, **kwargs):
                signals.append(kwargs["cancel_event"]); entered.set(); await asyncio.Event().wait()
        adapter = Mock()
        task = asyncio.create_task(NativePreviewPipeline(planner_factory=Blocked, adapter=adapter).run(
            request(), task_id="task", plan_id="plan"))
        await entered.wait(); task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(signals[0].is_set()); adapter.convert.assert_not_called()

    async def test_context_initialization_failure_keeps_valid_place_preview(self):
        routes = Mock(); routes.create_route_context.side_effect = RuntimeError("private-context-error")
        events = []
        result = await NativePreviewPipeline(planner_factory=FixturePlanner, current_route_service_factory=lambda: routes,
            emit=lambda name, data: events.append((name, data))).run(request(), task_id="task", plan_id="plan")
        self.assertIsNone(result.route_context)
        self.assertTrue(result.initial_route_day_ids)
        self.assertEqual(result.snapshot.preview.draft.plan_id, "plan")
        self.assertNotIn("private-context-error", str(events))

    async def test_unexpected_planner_error_is_safe_and_does_not_create_context(self):
        class Broken:
            async def run_async(self, *args, **kwargs): raise RuntimeError("private-provider-error")
        factory = Mock()
        with self.assertRaises(NativePreviewError) as caught:
            await NativePreviewPipeline(planner_factory=Broken, current_route_service_factory=factory).run(
                request(), task_id="task", plan_id="plan")
        self.assertEqual(caught.exception.code, "NATIVE_PREVIEW_PIPELINE_FAILED")
        self.assertNotIn("private-provider-error", str(caught.exception)); factory.assert_not_called()


class RouteFixture:
    def __init__(self): self.calls = []

    def __call__(self, name, args):
        self.calls.append((name, dict(args)))
        transit = "transit" in name; bike = "bicycling" in name
        effective = {"origin": args.get("origin", args.get("origin_coordinates")),
                     "destination": args.get("destination", args.get("destination_coordinates"))}
        if transit:
            effective.update({key: args[key] for key in ("city", "cityd", "date", "time", "strategy")})
            effective["extensions"] = "base"
        row = {"duration": "300" if transit else "600", "segments": []} if transit else {"duration": "900", "steps": []}
        payload = {"route": {"transits": [row]}} if transit else {"data": {"paths": [row]}} if bike else {"route": {"paths": [row]}}
        return {**payload, "result_status": "ok", "result_source": {"api_version": "v4" if bike else "v3",
            "queried_at": "2026-10-13T00:00:00+08:00", "effective_params": effective}}


class NativePreviewDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.map = RouteFixture()
        self.started = asyncio.Event(); self.release = asyncio.Event(); self.failure = False
        owner = self
        class GatedRoutes(CurrentRouteService):
            async def run_async(service, *args, **kwargs):
                owner.started.set(); await owner.release.wait()
                if owner.failure: raise TimeoutError("private-provider-response")
                return await super().run_async(*args, **kwargs)
        self.routes = GatedRoutes(adapter=AmapContractAdapter(self.map), tool_timeout_seconds=1)
        self.pipeline = NativePreviewPipeline(planner_factory=lambda: FixturePlanner(endpoints=True), current_route_service_factory=lambda: self.routes)
        self.task_repo = TaskRepository(Path(self.temp.name)/"tasks.sqlite3"); self.task_repo.initialize()
        self.repository = PreviewRepository(Path(self.temp.name)/"previews.sqlite3"); self.repository.initialize()
        self.tasks = TaskService(self.task_repo, ObservationService(self.task_repo), lambda: None)
        self.tasks.preview_completion_resolver = self.repository.ready_task_completion
        self.tasks.preview_completion_batch_resolver = self.repository.ready_task_completions
        self.previews = PreviewTaskService(self.tasks, self.repository, self.pipeline)
        self.coordinator = PlanningV4PreviewEditService(self.previews, self.repository, route_service=self.routes, route_deadline_seconds=2)
        self.previews.edit_coordinator = self.coordinator

    async def asyncTearDown(self):
        self.release.set(); await self.tasks.close(); await self.previews.close(); self.temp.cleanup()

    async def ready(self):
        accepted = await self.previews.submit(request().model_copy(update={
            "arrival_place_query": "测试到达站", "departure_place_query": "测试离开站"}))
        for _ in range(200):
            status = await self.previews.status(accepted["task_id"], accepted["access_token"])
            if status["preview_ready"]: break
            if status["status"] in {"failed", "interrupted"}: self.fail(str(status))
            await asyncio.sleep(.01)
        else: self.fail("preview not ready")
        await asyncio.wait_for(self.started.wait(), 2)
        return accepted, await self.previews.preview(accepted["task_id"], accepted["access_token"])

    async def finish(self, accepted, calculation_id):
        self.release.set()
        for _ in range(200):
            job = self.repository.get_route_job_by_plan(accepted["plan_id"], calculation_id)
            if job.status in {"completed", "failed", "superseded"}: return job
            await asyncio.sleep(.01)
        self.fail("route job did not terminate")

    async def test_ready_is_durable_before_transport_and_savable_while_waiting(self):
        accepted, envelope = await self.ready()
        self.assertEqual(envelope.plan_revision, 2); self.assertEqual(envelope.store_version, 1)
        self.assertFalse(self.map.calls)
        self.assertEqual(envelope.preview.calculation.current_routes, "pending")
        saved = await self.previews.save_final(accepted["task_id"], accepted["access_token"], save_id="pending-save", source_store_version=envelope.store_version)
        self.assertTrue(saved.restorable); self.assertEqual(saved.status, "saved")
        self.assertEqual((await self.tasks.status(accepted["task_id"]))["status"], "succeeded")
        context = self.previews.route_contexts[accepted["plan_id"]]
        job = await self.finish(accepted, envelope.preview.calculation.calculation_id)
        self.assertEqual(job.status, "completed")
        updated = await self.previews.preview(accepted["task_id"], accepted["access_token"])
        self.assertTrue(self.map.calls); self.assertTrue(updated.preview.facts.routes)
        self.assertEqual(updated.preview.draft.days, envelope.preview.draft.days)
        self.assertIs(self.previews.route_contexts[accepted["plan_id"]], context)
        self.assertTrue(all(any(m.status == "available" for m in leg.mode_results) for leg in updated.preview.draft.legs))
        self.assertTrue(all({m.mode for m in leg.mode_results if m.status == "available"} == {"transit", "bicycling", "walking"}
                            for leg in updated.preview.draft.legs))
        self.assertEqual(updated.preview.calculation.current_routes, "completed")
        self.assertTrue(any(i.code == "ENDPOINT_ARRIVAL_UNKNOWN" for i in updated.preview.validation.issues))

    async def test_failed_transport_keeps_places_readable_editable_and_savable(self):
        self.failure = True
        accepted, envelope = await self.ready()
        with self.assertLogs("app.services.planning_v4_preview_edit_service", level="WARNING") as logs:
            job = await self.finish(accepted, envelope.preview.calculation.calculation_id)
        self.assertEqual(job.status, "failed")
        self.assertNotIn("private-provider-response", "\n".join(logs.output))
        updated = await self.previews.preview(accepted["task_id"], accepted["access_token"])
        self.assertEqual(updated.preview.draft.days, envelope.preview.draft.days)
        self.assertEqual(updated.preview.calculation.current_routes, "failed")
        saved = await self.previews.save_final(accepted["task_id"], accepted["access_token"], save_id="failed-route-save", source_store_version=updated.store_version)
        self.assertTrue(saved.restorable)

    async def test_transport_mode_edit_uses_existing_context_and_keeps_user_choice(self):
        accepted, original = await self.ready()
        await self.finish(accepted, original.preview.calculation.calculation_id)
        envelope = await self.previews.preview(accepted["task_id"], accepted["access_token"])
        leg = envelope.preview.draft.legs[0]; context = self.previews.route_contexts[accepted["plan_id"]]
        option_id = next(m for m in leg.mode_results if m.mode == "walking").option_ids[0]
        command = EditCommand(plan_id=envelope.plan_id, day_id=leg.day_id, base_revision=envelope.plan_revision, command_id="walking",
            operations=({"type": "select_leg_mode", "day_id": leg.day_id, "leg_id": leg.leg_id, "mode": "walking", "option_id": option_id},))
        edited = await self.coordinator.edit(accepted["task_id"], accepted["access_token"], command, expected_store_version=envelope.store_version)
        if edited.envelope.preview.calculation.current_routes == "pending":
            await self.finish(accepted, edited.envelope.preview.calculation.calculation_id)
        updated = await self.previews.preview(accepted["task_id"], accepted["access_token"])
        selected = next(l for l in updated.preview.draft.legs if l.leg_id == leg.leg_id)
        self.assertEqual(selected.selected_mode, "walking")
        self.assertEqual(selected.selection_source, "user")
        self.assertIs(self.previews.route_contexts[accepted["plan_id"]], context)

    async def test_edit_during_routes_and_restore_saved_snapshot_reject_late_original_result(self):
        accepted, original = await self.ready()
        saved = await self.previews.save_final(accepted["task_id"], accepted["access_token"], save_id="before-edit", source_store_version=original.store_version)
        day = original.preview.draft.days[0]; removed = day.activities[0]
        command = EditCommand(plan_id=original.plan_id, day_id=day.day_id, base_revision=original.plan_revision, command_id="remove",
            operations=({"type": "remove_activity", "day_id": day.day_id, "activity_id": removed.activity_id},))
        edited = await self.coordinator.edit(accepted["task_id"], accepted["access_token"], command, expected_store_version=original.store_version)
        self.assertEqual(edited.envelope.plan_revision, 3)
        self.assertTrue(any(i.code == "ENDPOINT_DEPARTURE_UNKNOWN" and i.based_on_revision == 3 for i in edited.envelope.preview.validation.issues))
        restored = await self.previews.restore_final(accepted["task_id"], accepted["access_token"], restore_id="restore",
            expected_store_version=edited.envelope.store_version, expected_plan_revision=3, expected_save_id=saved.final.save_id)
        self.assertEqual(restored.envelope.plan_revision, 4)
        self.assertEqual(restored.envelope.preview.draft.days, original.preview.draft.days)
        await self.finish(accepted, restored.envelope.preview.calculation.calculation_id)
        latest = await self.previews.preview(accepted["task_id"], accepted["access_token"])
        self.assertEqual(latest.plan_revision, restored.envelope.plan_revision)
        self.assertEqual(latest.preview.draft.days, original.preview.draft.days)
        self.assertEqual(self.repository.get_route_job_by_plan(original.plan_id, original.preview.calculation.calculation_id).status, "superseded")

    async def test_save_failure_keeps_previous_successful_final_and_stale_edit_is_rejected(self):
        from unittest.mock import patch
        from app.services.task_service import TaskError
        accepted, envelope = await self.ready()
        saved = await self.previews.save_final(accepted["task_id"], accepted["access_token"], save_id="good-save", source_store_version=envelope.store_version)
        with patch.object(self.repository, "save_final", side_effect=RuntimeError("private-storage-error")):
            with self.assertRaises(TaskError) as caught:
                await self.previews.save_final(accepted["task_id"], accepted["access_token"], save_id="bad-save", source_store_version=envelope.store_version)
        self.assertEqual(caught.exception.code, "PREVIEW_STORAGE_UNAVAILABLE")
        state = await self.previews.final_state(accepted["task_id"], accepted["access_token"])
        self.assertEqual(state.final.save_id, saved.final.save_id)
        day = envelope.preview.draft.days[0]
        command = EditCommand(plan_id=envelope.plan_id, day_id=day.day_id, base_revision=2, command_id="stale",
            operations=({"type": "remove_activity", "day_id": day.day_id, "activity_id": day.activities[0].activity_id},))
        with self.assertRaises(TaskError) as caught:
            await self.coordinator.edit(accepted["task_id"], accepted["access_token"], command, expected_store_version=envelope.store_version+1)
        self.assertEqual(caught.exception.code, "PREVIEW_VERSION_CONFLICT")
        self.assertEqual(await self.previews.preview(accepted["task_id"], accepted["access_token"]), envelope)

    async def test_storage_failure_never_reports_ready_and_closes_handed_context(self):
        from unittest.mock import patch
        contexts = []; original = self.routes.create_route_context
        def create(*args):
            context = original(*args); contexts.append(context); return context
        self.routes.create_route_context = create
        with patch.object(self.repository, "complete_job", side_effect=RuntimeError("private-storage-error")):
            accepted = await self.previews.submit(request().model_copy(update={
            "arrival_place_query": "测试到达站", "departure_place_query": "测试离开站"}))
            for _ in range(200):
                status = await self.previews.status(accepted["task_id"], accepted["access_token"])
                if status["status"] == "failed": break
                await asyncio.sleep(.01)
            else: self.fail("failed write did not terminate generation")
        self.assertFalse(status["preview_ready"])
        self.assertTrue(contexts[0].cancel_event.is_set())
        self.assertFalse(self.started.is_set()); self.assertFalse(self.map.calls)


if __name__ == "__main__": unittest.main()


class OptionalEndpointNoticeTests(unittest.TestCase):
    def test_optional_queries_and_real_query_outcomes(self):
        from app.services.native_preview_adapter import _endpoint_notices
        for kind in ("arrival", "departure"):
            for query, payload, expected in (
                (None, {"result_status": "empty", "pois": []}, None),
                ("", {"result_status": "empty", "pois": []}, None),
                ("   ", {"result_status": "empty", "pois": []}, None),
                ("测试车站", {"result_status": "empty", "pois": []}, "未查到"),
                ("测试车站", {"result_status": "ok", "pois": [
                    {"id": "one", "name": "测试车站", "cityname": "上海", "location": "121.4,31.2"},
                    {"id": "two", "name": "测试车站", "cityname": "上海", "location": "121.5,31.2"}]}, "无法唯一确定"),
                ("测试车站", {"result_status": "error", "result_error": {"category": "network"}}, "查询失败"),
                ("测试车站", {"result_status": "ok", "pois": [
                    {"id": "one", "name": "测试车站", "cityname": "上海", "location": "121.4,31.2"}]}, None),
            ):
                with self.subTest(kind=kind, query=query, expected=expected):
                    req = request().model_copy(update={"arrival_place_query": None, "departure_place_query": None,
                                                       f"{kind}_place_query": query})
                    native = native_result(req)
                    maps = Mock(return_value=payload)
                    endpoints = NativeEndpointService(adapter=AmapContractAdapter(maps)).run(native.planning_input, run_id="task")
                    report = _endpoint_notices(native.repair.report, SimpleNamespace(snapshot=SimpleNamespace(endpoint_evidence=endpoints)))
                    notices = [i for i in report.issues if i.code.startswith("ENDPOINT_")]
                    if expected:
                        self.assertEqual(len(notices), 1)
                        self.assertEqual(notices[0].code, f"ENDPOINT_{kind.upper()}_UNKNOWN")
                        self.assertIn(expected, notices[0].message)
                        self.assertNotIn("query_missing", notices[0].message)
                    else:
                        self.assertEqual(notices, [])
                    if not (query or "").strip(): maps.assert_not_called()
