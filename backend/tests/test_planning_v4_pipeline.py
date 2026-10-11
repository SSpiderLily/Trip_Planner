"""planning_v4 生产接线的纯替身端到端回归。"""
from __future__ import annotations

import asyncio
from datetime import date, datetime
import json
from threading import Event
from types import SimpleNamespace
import unittest

from app.models.planning_v4 import (
    AreaHint,
    CandidateRef,
    DraftActivity,
    DraftDay,
    FactSource,
    FactStore,
    LodgingRef,
    MapCoordinate,
    PlaceFact,
    DayRevision,
    ProposalActivity,
    ProposalDay,
    RepairState,
    RequirementSummary,
    TripRequest,
    WeatherFact,
    WeatherForecast,
)
from app.agents.daily_plan_generator import DailyPlanGenerator
from app.models.planning_v4.plan import PlanDraft, ValidationIssue
from app.services.day_repair_service import (
    DayRepairCollectionRequest,
    DayRepairCollectionTarget,
    DayRepairResult,
)
from app.services.endpoint_verification_service import (
    EndpointVerification,
    EndpointVerificationExecution,
)
from app.services.planning_dispatch_service import DispatchError, PlanningDispatchResult
from app.services.day_repair_service import DayRepairService
from app.services.daily_plan_adapter import DailyPlanGeneratorAdapter
from app.services.planning_input_builder import PlanningInputBuildRequest, build_planning_input
from app.services.planning_v4_pipeline import PlanningV4Pipeline, PlanningV4PipelineError
from app.services.planning_v4_rules import default_planning_v4_rules
from app.services.preview_task_service import BackfillSuperseded


def request_fixture(*, departure_query="上海虹桥站"):
    return TripRequest(
        request_id="pipeline-request",
        city="上海",
        arrival_at=datetime.fromisoformat("2026-10-10T09:00:00+08:00"),
        departure_at=datetime.fromisoformat("2026-10-12T19:00:00+08:00"),
        timezone="Asia/Shanghai",
        departure_place_query=departure_query,
        explicit_return_buffer_s=0,
    )


def endpoint_fixture(request, run_id="pipeline-task"):
    source = FactSource(
        tool_call_id="departure-source",
        tool_name="maps_text_search",
        api_version="v3",
        queried_at=datetime.fromisoformat("2026-10-10T01:00:00+00:00"),
        effective_params={"keywords": request.departure_place_query, "city": "上海"},
    )
    fact = PlaceFact(
        fact_id="departure-fact",
        poi_id="departure-poi",
        name="上海虹桥站",
        city="上海市",
        location=MapCoordinate(lng=121.32, lat=31.20),
        source_refs=(source.tool_call_id,),
        field_sources={
            "poi_id": source.tool_call_id,
            "name": source.tool_call_id,
            "location": source.tool_call_id,
            "city": source.tool_call_id,
        },
    )
    facts = FactStore(sources=(source,), places=(fact,))
    execution = EndpointVerificationExecution(
        request_id=request.request_id,
        run_id=run_id,
        context_revision=1,
        base_revision=None,
        city="上海",
        arrival=EndpointVerification(
            kind="arrival", user_query=None, status="missing", reason="query_missing",
        ),
        departure=EndpointVerification(
            kind="departure",
            user_query=request.departure_place_query,
            status="verified",
            reason="verified",
            fact_id=fact.fact_id,
            candidates=(fact,),
        ),
        fact_store=facts,
    )
    return facts, execution


def place_fact(fact_id, poi_id, name, source_id, lng, lat):
    source = FactSource(
        tool_call_id=source_id,
        tool_name="maps_text_search",
        api_version="v3",
        queried_at=datetime.fromisoformat("2026-10-10T01:00:00+00:00"),
        effective_params={"keywords": name, "city": "上海"},
    )
    fact = PlaceFact(
        fact_id=fact_id,
        poi_id=poi_id,
        name=name,
        city="上海市",
        location=MapCoordinate(lng=lng, lat=lat),
        source_refs=(source_id,),
        field_sources={
            "poi_id": source_id,
            "name": source_id,
            "location": source_id,
            "city": source_id,
        },
    )
    return source, fact


class FakeRouteContext:
    def __init__(self, planning_input, plan_id):
        self.planning_input = planning_input
        self.plan_id = plan_id
        self.cancel_event = Event()
        self.registered = []
        self.closed = False

    def register_facts(self, facts):
        self.registered.append(facts)

    def assert_binding(self, planning_input, draft):
        assert planning_input == self.planning_input
        assert draft.plan_id == self.plan_id

    async def aclose(self):
        self.cancel_event.set()
        self.closed = True


class FakeInterpreter:
    def __init__(self, tracker):
        self.tracker = tracker

    def interpret(self, request, *, deadline_monotonic, cancel_event):
        self.tracker["interpreter_request"] = request
        self.tracker["interpreter_deadline"] = deadline_monotonic
        return RequirementSummary(
            requirements_id="requirements-pipeline",
            request_id=request.request_id,
        )


class FakeDispatch:
    def __init__(self, tracker, facts, endpoint):
        self.tracker = tracker
        self.facts = facts
        self.endpoint = endpoint

    async def run_async(self, context):
        self.tracker.setdefault("dispatch_contexts", []).append(context)
        targeted = context.dispatch_scope is not None
        return PlanningDispatchResult(
            request_id=context.request_id,
            run_id=context.run_id,
            context_revision=context.context_revision,
            base_revision=context.base_revision,
            action="delegate" if targeted else "plan",
            stop_reason="no_progress" if targeted else "plan",
            reason="offline dispatch result",
            fact_store=self.facts,
            endpoint_results=() if targeted else (self.endpoint,),
        )


class FakeDailyPlanAdapter:
    def __init__(self, tracker, clock=None):
        self.tracker = tracker
        self.clock = clock

    async def generate(
        self, planning_input, candidates, facts, area_hints, task_results, *,
        source_run_id, plan_id, cancel_event,
    ):
        self.tracker["proposal_identity"] = (source_run_id, plan_id)
        self.tracker["proposal_request"] = planning_input.request
        draft = PlanDraft(
            plan_id=plan_id,
            request_id=planning_input.request.request_id,
            source_run_id=source_run_id,
            revision=1,
            rule_version=planning_input.rules.rule_version,
            days=tuple(DraftDay(
                day_id=day.day_id,
                day_date=day.day_date,
                unfilled_slot_ids=tuple(
                    slot.slot_id for slot in day.slots if slot.eligibility != "skipped"
                ),
            ) for day in planning_input.days),
        )
        self.tracker["generated_draft"] = draft
        if self.clock is not None:
            self.clock.value = self.clock.deadline
        return SimpleNamespace(draft=draft)


class FakeRouteService:
    def __init__(self, tracker):
        self.tracker = tracker

    def create_route_context(self, planning_input, plan_id):
        context = FakeRouteContext(planning_input, plan_id)
        self.tracker.setdefault("route_contexts", []).append(context)
        self.tracker["route_context"] = context
        return self.tracker["route_context"]

    async def run_async(
        self, planning_input, draft, facts, *, endpoint_execution=None,
        verified_departure_fact_id=None, current_context_revision=None,
        route_context=None, only_day_id=None,
    ):
        self.tracker["route_call"] = {
            "endpoint_execution": endpoint_execution,
            "verified_departure_fact_id": verified_departure_fact_id,
            "current_context_revision": current_context_revision,
            "only_day_id": only_day_id,
        }
        from app.services.planning_v4_timing import calculate_plan_timing

        return SimpleNamespace(
            draft=draft,
            fact_store=facts,
            timing=calculate_plan_timing(
                planning_input, draft, facts,
                verified_departure_fact_id=verified_departure_fact_id,
            ),
        )


class FakeBackfill:
    def __init__(self, tracker):
        self.tracker = tracker

    async def run_async(self, *args, current_revision, **kwargs):
        revision = current_revision()
        self.tracker.setdefault("backfill_revisions", []).append(revision)
        draft = args[1]
        return SimpleNamespace(status="complete" if revision == draft.revision else "cancelled")


class ManualClock:
    def __init__(self, deadline):
        self.value = 0.0
        self.deadline = deadline

    def __call__(self):
        return self.value


class FakeRepair:
    def __init__(self, tracker, *, collect, revise, recalculate):
        self.tracker = tracker
        self.collect = collect

    async def run_async(
        self, planning_input, draft, facts, candidates, area_hints, task_results,
        timing, report, *, global_deadline_monotonic,
    ):
        day = planning_input.days[0]
        slot = next(item for item in day.slots if item.period == "morning")
        draft_day = next(item for item in draft.days if item.day_id == day.day_id)
        issue = ValidationIssue(
            issue_id="missing-target-morning",
            code="MISSING_ARRANGEMENT",
            category="missing_arrangement",
            severity="warning",
            scope="slot",
            day_id=day.day_id,
            target_id=slot.slot_id,
            message="缺少景点安排",
            triggers_repair=True,
            based_on_revision=draft.revision,
        )
        result = await self.collect(DayRepairCollectionRequest(
            planning_input=planning_input,
            draft=draft,
            day=draft_day,
            issues=(issue,),
            assignments=(),
            round_id="repair-round-1",
            targets=(DayRepairCollectionTarget(
                day_id=day.day_id,
                role="attractions",
                issue_ids=(issue.issue_id,),
                slot_ids=(slot.slot_id,),
            ),),
            fact_store=facts,
            candidates=candidates,
            area_hints=area_hints,
            task_results=task_results,
        ))
        self.tracker["repair_collection"] = result
        final_report = report.model_copy(update={
            "repair_states": tuple(RepairState(
                day_id=item.day_id,
                completed_rounds=0,
                status="open",
            ) for item in planning_input.days),
        })
        return DayRepairResult(
            status="partial",
            draft=draft,
            facts=result.fact_store,
            candidates=result.candidates,
            area_hints=result.area_hints,
            task_results=result.task_results,
            timing=timing,
            report=final_report,
            repair_states=final_report.repair_states,
        )


class PlanningV4PipelineTests(unittest.TestCase):
    def test_initial_dispatch_semantic_error_keeps_valid_snapshot_and_warns_on_preview(self):
        request = request_fixture()
        facts, endpoint = endpoint_fixture(request, run_id="pipeline-partial-dispatch-task")
        tracker = {}
        initial_result = PlanningDispatchResult(
            request_id=request.request_id,
            run_id="pipeline-partial-dispatch-task",
            context_revision=1,
            base_revision=None,
            action=None,
            stop_reason="technical_error",
            reason="拒绝了无效餐饮派发，保留之前已核实的结果。",
            fact_store=facts,
            endpoint_results=(endpoint,),
            error=DispatchError(
                code="MASTER_DINING_BINDING_INVALID",
                step="master_decision",
                message="主智能体决策未能完成。",
            ),
        )

        class PartialDispatch:
            async def run_async(self, context):
                if context.dispatch_scope is None:
                    return initial_result
                return PlanningDispatchResult(
                    request_id=context.request_id,
                    run_id=context.run_id,
                    context_revision=context.context_revision,
                    base_revision=context.base_revision,
                    action="delegate",
                    stop_reason="no_progress",
                    reason="没有可执行的新定向查询。",
                    fact_store=context.fact_store,
                    candidate_refs=context.candidate_refs,
                    area_hints=context.area_hints,
                )

        progress_messages = []
        pipeline = PlanningV4Pipeline(
            requirement_interpreter_factory=lambda _timeout: FakeInterpreter(tracker),
            dispatch_service_factory=lambda _timeout: PartialDispatch(),
            daily_plan_adapter_factory=lambda _timeout: FakeDailyPlanAdapter(tracker),
            current_route_service_factory=lambda: FakeRouteService(tracker),
            route_backfill_service_factory=lambda: FakeBackfill(tracker),
            day_repair_service_factory=lambda **callbacks: FakeRepair(tracker, **callbacks),
        )

        result = asyncio.run(pipeline.run(
            request,
            task_id="pipeline-partial-dispatch-task",
            plan_id="plan-partial-dispatch",
            progress=lambda update: progress_messages.append(update.message),
        ))

        preview = result.snapshot.preview
        self.assertEqual(preview.facts.places, facts.places)
        self.assertIn("departure-fact", {fact.fact_id for fact in preview.facts.places})
        self.assertIn("dispatch-partial-collection", {issue.issue_id for issue in preview.validation.issues})
        warning = next(issue for issue in preview.validation.issues if issue.issue_id == "dispatch-partial-collection")
        self.assertEqual(warning.code, "DISPATCH_COLLECTION_PARTIAL")
        self.assertEqual(warning.category, "unknown_info")
        self.assertFalse(warning.triggers_repair)
        self.assertTrue(any("信息收集未能全部完成" in message for message in progress_messages))
        self.assertEqual(tracker["proposal_identity"], ("pipeline-partial-dispatch-task", "plan-partial-dispatch"))

    def test_initial_dispatch_semantic_error_does_not_recover_broken_fact_closure(self):
        request = request_fixture()
        facts, endpoint = endpoint_fixture(request, run_id="pipeline-broken-partial-task")
        tracker = {}
        broken_candidate = CandidateRef(
            candidate_id="candidate-with-missing-fact",
            category="sightseeing",
            poi_id="missing-poi",
            place_fact_id="missing-fact",
        )
        initial_result = PlanningDispatchResult(
            request_id=request.request_id,
            run_id="pipeline-broken-partial-task",
            context_revision=1,
            base_revision=None,
            action=None,
            stop_reason="technical_error",
            reason="保留快照损坏。",
            fact_store=facts,
            candidate_refs=(broken_candidate,),
            endpoint_results=(endpoint,),
            error=DispatchError(
                code="MASTER_DINING_BINDING_INVALID",
                step="master_decision",
                message="主智能体决策未能完成。",
            ),
        )

        class BrokenPartialDispatch:
            async def run_async(self, _context):
                return initial_result

        pipeline = PlanningV4Pipeline(
            requirement_interpreter_factory=lambda _timeout: FakeInterpreter(tracker),
            dispatch_service_factory=lambda _timeout: BrokenPartialDispatch(),
            daily_plan_adapter_factory=lambda _timeout: FakeDailyPlanAdapter(tracker),
            current_route_service_factory=lambda: FakeRouteService(tracker),
            route_backfill_service_factory=lambda: FakeBackfill(tracker),
        )

        with self.assertRaises(PlanningV4PipelineError) as raised:
            asyncio.run(pipeline.run(
                request,
                task_id="pipeline-broken-partial-task",
                plan_id="plan-broken-partial",
                progress=lambda _update: None,
            ))

        self.assertEqual(raised.exception.code, "CANDIDATE_SOURCE_INVALID")
        self.assertNotIn("proposal_identity", tracker)

    def test_actual_f06_weather_fallback_reaches_pipeline_preview_offline(self):
        request = request_fixture()
        endpoint_facts, endpoint = endpoint_fixture(request, run_id="pipeline-weather-fallback-task")
        weather_source = FactSource(
            tool_call_id="weather-source",
            tool_name="weather_query",
            api_version="v4",
            queried_at=datetime.fromisoformat("2026-10-10T01:00:00+00:00"),
            effective_params={"city": "上海", "date": "2026-10-10"},
        )
        weather = WeatherFact(
            fact_id="weather-day-one",
            city="上海",
            forecasts=(WeatherForecast(
                forecast_date=date(2026, 10, 10),
                day_weather="多云",
                day_temp_c=22,
                night_temp_c=17,
            ),),
            source=weather_source,
        )
        facts = FactStore(
            sources=(*endpoint_facts.sources, weather_source),
            places=endpoint_facts.places,
            weather=(weather,),
        )
        tracker = {}

        class OneResponseLLM:
            def __init__(self, response):
                self.response = response
                self.calls = 0

            def invoke_with_usage(self, _messages, **_kwargs):
                self.calls += 1
                return self.response, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

        class ActualF06Adapter:
            async def generate(
                self, planning_input, candidates, actual_facts, area_hints, task_results,
                *, source_run_id, plan_id, cancel_event,
            ):
                payload = {"days": [], "missing_notes": []}
                for day in planning_input.days:
                    payload["days"].append({
                        "day_id": day.day_id,
                        "day_date": day.day_date.isoformat(),
                        "theme": "上海城市漫步",
                        "activities": [],
                        "unfilled_slot_ids": [
                            slot.slot_id for slot in day.slots if slot.eligibility != "skipped"
                        ],
                        "lodging": None,
                        "weather_advice": None,
                    })
                llm = OneResponseLLM(json.dumps(payload, ensure_ascii=False))
                adapter = DailyPlanGeneratorAdapter(DailyPlanGenerator(llm=llm))
                execution = await adapter.generate(
                    planning_input,
                    candidates,
                    actual_facts,
                    area_hints,
                    task_results,
                    source_run_id=source_run_id,
                    plan_id=plan_id,
                    cancel_event=cancel_event,
                )
                tracker["execution"] = execution
                tracker["llm_calls"] = llm.calls
                return execution

        pipeline = PlanningV4Pipeline(
            requirement_interpreter_factory=lambda _timeout: FakeInterpreter(tracker),
            dispatch_service_factory=lambda _timeout: FakeDispatch(tracker, facts, endpoint),
            daily_plan_adapter_factory=lambda _timeout: ActualF06Adapter(),
            current_route_service_factory=lambda: FakeRouteService(tracker),
            route_backfill_service_factory=lambda: FakeBackfill(tracker),
            day_repair_service_factory=lambda **callbacks: FakeRepair(tracker, **callbacks),
        )

        result = asyncio.run(pipeline.run(
            request,
            task_id="pipeline-weather-fallback-task",
            plan_id="plan-weather-fallback",
            progress=lambda _update: None,
        ))

        day = result.snapshot.preview.draft.days[0]
        self.assertEqual(day.weather_fact_id, weather.fact_id)
        self.assertEqual(
            day.weather_advice,
            "天气已查询，出行建议暂未生成，请结合当天天气调整。",
        )
        self.assertEqual(tracker["llm_calls"], 1)
        self.assertEqual(tracker["execution"].format_repairs, 0)

    def test_f09_scope_keeps_area_anchors_on_their_actual_target_day(self):
        request = request_fixture(departure_query=None)
        planning_input = build_planning_input(PlanningInputBuildRequest(
            request=request,
            requirements=RequirementSummary(
                requirements_id="requirements-pipeline",
                request_id=request.request_id,
            ),
            rules=default_planning_v4_rules(),
        ))
        source = FactSource(
            tool_call_id="day-two-source",
            tool_name="maps_text_search",
            api_version="v3",
            queried_at=datetime.fromisoformat("2026-10-10T01:00:00+00:00"),
            effective_params={"city": "上海"},
        )
        fact = PlaceFact(
            fact_id="day-two-fact",
            poi_id="day-two-poi",
            name="第二日景点",
            city="上海市",
            location=MapCoordinate(lng=121.47, lat=31.23),
            source_refs=(source.tool_call_id,),
            field_sources={
                "poi_id": source.tool_call_id,
                "name": source.tool_call_id,
                "location": source.tool_call_id,
                "city": source.tool_call_id,
            },
        )
        second_day = planning_input.days[1]
        second_slot = next(item for item in second_day.slots if item.period == "morning")
        candidate = CandidateRef(
            candidate_id="day-two-candidate",
            category="sightseeing",
            poi_id=fact.poi_id,
            place_fact_id=fact.fact_id,
            slot_ids=(second_slot.slot_id,),
        )
        area = AreaHint(
            area_hint_id="day-two-area",
            label="第二日区域",
            anchor_poi_ids=(fact.poi_id,),
            center=fact.location,
            basis_fact_ids=(fact.fact_id,),
        )
        draft = PlanDraft(
            plan_id="plan-f09-scope",
            request_id=request.request_id,
            source_run_id="pipeline-task",
            revision=1,
            rule_version=planning_input.rules.rule_version,
            days=tuple(DraftDay(
                day_id=day.day_id,
                day_date=day.day_date,
                activities=((DraftActivity(
                    activity_id="day-two-activity",
                    candidate_ref=candidate.candidate_id,
                    slot_id=second_slot.slot_id,
                    kind="sightseeing",
                    poi_id=fact.poi_id,
                    place_fact_id=fact.fact_id,
                    duration_s=10800,
                    duration_source="default",
                ),) if day.day_id == second_day.day_id else ()),
            ) for day in planning_input.days),
            area_hints=(area,),
        )
        first_slot = next(item for item in planning_input.days[0].slots if item.period == "morning")
        collection = DayRepairCollectionRequest(
            planning_input=planning_input,
            draft=draft,
            day=draft.days[0],
            issues=(),
            assignments=(),
            round_id="scope-round",
            targets=(
                DayRepairCollectionTarget(
                    day_id=planning_input.days[0].day_id,
                    role="attractions",
                    issue_ids=("first-day-issue",),
                    slot_ids=(first_slot.slot_id,),
                ),
                DayRepairCollectionTarget(
                    day_id=second_day.day_id,
                    role="attractions",
                    issue_ids=("second-day-issue",),
                    slot_ids=(second_slot.slot_id,),
                ),
            ),
            fact_store=FactStore(sources=(source,), places=(fact,)),
            candidates=(candidate,),
            area_hints=(area,),
        )

        scope = PlanningV4Pipeline._dispatch_scope(collection)

        self.assertEqual(
            {item.day_id for item in scope.anchors},
            {second_day.day_id},
        )
        area_ids_by_day = {item.day_id: set(item.area_hint_ids) for item in scope.targets}
        self.assertEqual(area_ids_by_day[planning_input.days[0].day_id], set())
        self.assertEqual(area_ids_by_day[second_day.day_id], {area.area_hint_id})

    def test_pipeline_keeps_request_plan_and_endpoint_identity_across_targeted_repair(self):
        request = request_fixture()
        facts, endpoint = endpoint_fixture(request)
        tracker = {}
        current_revision = {"plan-preallocated": 1}

        pipeline = PlanningV4Pipeline(
            requirement_interpreter_factory=lambda _timeout: FakeInterpreter(tracker),
            dispatch_service_factory=lambda _timeout: FakeDispatch(tracker, facts, endpoint),
            daily_plan_adapter_factory=lambda _timeout: FakeDailyPlanAdapter(tracker),
            current_route_service_factory=lambda: FakeRouteService(tracker),
            route_backfill_service_factory=lambda: FakeBackfill(tracker),
            day_repair_service_factory=lambda **callbacks: FakeRepair(tracker, **callbacks),
            current_revision_lookup=lambda plan_id: current_revision.get(plan_id),
        )

        async def run():
            return await pipeline.run(
                request,
                task_id="pipeline-task",
                plan_id="plan-preallocated",
                progress=lambda _update: None,
            )

        result = asyncio.run(run())

        self.assertEqual(tracker["proposal_identity"], ("pipeline-task", "plan-preallocated"))
        self.assertEqual(tracker["proposal_request"], request)
        self.assertEqual(result.snapshot.preview.planning_input.request, request)
        self.assertEqual(result.snapshot.preview.planning_input.boundary_facts.effective_return_buffer_s, 0)
        self.assertEqual(tracker["route_call"]["endpoint_execution"], endpoint)
        self.assertEqual(tracker["route_call"]["verified_departure_fact_id"], "departure-fact")
        self.assertEqual(tracker["route_call"]["current_context_revision"], 1)
        self.assertIsNone(tracker["dispatch_contexts"][0].dispatch_scope)
        targeted = tracker["dispatch_contexts"][1]
        self.assertEqual((targeted.context_revision, targeted.base_revision), (2, 1))
        self.assertEqual(targeted.existing_tasks, ())
        self.assertEqual(targeted.task_results, ())
        self.assertEqual(targeted.dispatch_scope.targets[0].day_id, targeted.planning_input.days[0].day_id)
        self.assertEqual(tracker["repair_collection"].stop_reason, "no_progress")
        self.assertIsNotNone(result.route_context)
        self.assertIsNotNone(result.backfill)

        async def run_backfill():
            await result.backfill(lambda _progress: None)

        asyncio.run(run_backfill())
        current_revision["plan-preallocated"] = 2
        with self.assertRaises(BackfillSuperseded):
            asyncio.run(run_backfill())
        self.assertEqual(tracker["backfill_revisions"], [1, 2])

    def test_pipeline_repair_calls_adapter_with_day_revision_request_day_id(self):
        request = request_fixture()
        facts, endpoint = endpoint_fixture(request, run_id="pipeline-real-repair-task")
        tracker = {}

        class TrackingDailyPlanAdapter(FakeDailyPlanAdapter):
            async def revise_day(
                self, planning_input, draft, report, day_id, issues, _assignments,
                _candidates, _facts, _area_hints, _task_results, *,
                preserve_activity_ids, **_kwargs,
            ):
                self.tracker.setdefault("revision_day_ids", []).append(day_id)
                current_day = next(item for item in draft.days if item.day_id == day_id)
                return DayRevision(
                    day_id=day_id,
                    base_revision=draft.revision,
                    revision=draft.revision + 1,
                    based_on_report_revision=report.based_on_revision,
                    replacement_day=ProposalDay(
                        day_id=day_id,
                        day_date=current_day.day_date,
                        theme=current_day.theme,
                        activities=(),
                        unfilled_slot_ids=current_day.unfilled_slot_ids,
                        weather_advice=current_day.weather_advice,
                    ),
                    preserve_activity_ids=preserve_activity_ids,
                    source_issue_ids=tuple(issue.issue_id for issue in issues),
                )

        pipeline = PlanningV4Pipeline(
            requirement_interpreter_factory=lambda _timeout: FakeInterpreter(tracker),
            dispatch_service_factory=lambda _timeout: FakeDispatch(tracker, facts, endpoint),
            daily_plan_adapter_factory=lambda _timeout: TrackingDailyPlanAdapter(tracker),
            current_route_service_factory=lambda: FakeRouteService(tracker),
            route_backfill_service_factory=lambda: FakeBackfill(tracker),
            day_repair_service_factory=DayRepairService,
        )

        result = asyncio.run(pipeline.run(
            request,
            task_id="pipeline-real-repair-task",
            plan_id="plan-real-repair",
            progress=lambda _update: None,
        ))

        self.assertTrue(tracker.get("revision_day_ids"), "真实 DayRepairService 必须触发 F06 单日修订回调")
        self.assertTrue(set(tracker["revision_day_ids"]).issubset({day.day_id for day in result.snapshot.preview.draft.days}))

    def test_route_repair_timeout_keeps_revision_and_continues_to_later_day(self):
        request = request_fixture()
        facts, endpoint = endpoint_fixture(request, run_id="pipeline-route-repair-task")
        tracker = {}

        class RepairDispatch(FakeDispatch):
            async def run_async(self, context):
                if context.dispatch_scope is None:
                    return await super().run_async(context)
                self.tracker.setdefault("dispatch_contexts", []).append(context)
                sources, places, candidates = [], [], []
                existing_candidate_ids = {item.candidate_id for item in context.candidate_refs}
                for target in context.dispatch_scope.targets:
                    category = "meal" if target.role == "dining" else "sightseeing"
                    for slot_id in target.slot_ids:
                        candidate_id = f"repair-candidate-{slot_id}"
                        if candidate_id in existing_candidate_ids:
                            continue
                        source_id = f"repair-source-{slot_id}"
                        source = FactSource(
                            tool_call_id=source_id,
                            tool_name="maps_text_search",
                            api_version="v3",
                            queried_at=datetime.fromisoformat("2026-10-10T01:00:00+00:00"),
                            effective_params={"keywords": f"修订地点 {slot_id}", "city": "上海"},
                        )
                        place = PlaceFact(
                            fact_id=f"repair-fact-{slot_id}",
                            poi_id=f"repair-poi-{slot_id}",
                            name=f"修订地点 {slot_id}",
                            city="上海市",
                            type_name="中餐厅" if category == "meal" else "风景名胜区",
                            typecode="050000" if category == "meal" else "110000",
                            location=MapCoordinate(lng=121.48, lat=31.23),
                            source_refs=(source_id,),
                            field_sources={
                                "poi_id": source_id,
                                "name": source_id,
                                "location": source_id,
                                "city": source_id,
                                "type_name": source_id,
                                "typecode": source_id,
                            },
                        )
                        sources.append(source)
                        places.append(place)
                        candidates.append(CandidateRef(
                            candidate_id=candidate_id,
                            category=category,
                            poi_id=place.poi_id,
                            place_fact_id=place.fact_id,
                            slot_ids=(slot_id,),
                        ))
                return PlanningDispatchResult(
                    request_id=context.request_id,
                    run_id=context.run_id,
                    context_revision=context.context_revision,
                    base_revision=context.base_revision,
                    action="delegate",
                    stop_reason="no_progress",
                    reason="offline scoped fixture",
                    fact_store=FactStore(
                        sources=(*context.fact_store.sources, *sources),
                        places=(*context.fact_store.places, *places),
                        weather=context.fact_store.weather,
                        routes=context.fact_store.routes,
                    ),
                    candidate_refs=(*context.candidate_refs, *candidates),
                    area_hints=context.area_hints,
                )

        class RevisingDailyPlanAdapter(FakeDailyPlanAdapter):
            async def revise_day(
                self, planning_input, draft, report, day_id, issues, _assignments,
                candidates, _facts, _area_hints, _task_results, *,
                preserve_activity_ids, **_kwargs,
            ):
                self.tracker.setdefault("revision_day_ids", []).append(day_id)
                current_day = next(item for item in draft.days if item.day_id == day_id)
                proposals = [ProposalActivity(
                    proposal_ref=activity.activity_id,
                    existing_activity_id=activity.activity_id,
                    candidate_ref=activity.candidate_ref,
                    slot_id=activity.slot_id,
                    kind=activity.kind,
                    duration_s=activity.duration_s,
                    requirement_ids=activity.requirement_ids,
                    recommendation_reason=activity.recommendation_reason,
                ) for activity in current_day.activities if activity.kind != "free_time"]
                added_slots = set()
                if not self.tracker.get("activity_added"):
                    issue_slots = {issue.target_id for issue in issues if issue.target_id}
                    matching_candidates = [
                        item for item in candidates
                        if issue_slots.intersection(item.slot_ids)
                    ][:2]
                    for candidate in matching_candidates:
                        self.tracker["repair_day_id"] = day_id
                        added_slot = next(slot_id for slot_id in candidate.slot_ids if slot_id in issue_slots)
                        added_slots.add(added_slot)
                        proposals.append(ProposalActivity(
                            proposal_ref=f"proposal-{candidate.candidate_id}",
                            candidate_ref=candidate.candidate_id,
                            slot_id=added_slot,
                            kind="meal" if candidate.category == "meal" else "sightseeing",
                            duration_s=3600,
                        ))
                    if matching_candidates:
                        self.tracker["activity_added"] = True
                unfilled = tuple(
                    slot_id for slot_id in current_day.unfilled_slot_ids if slot_id not in added_slots
                )
                return DayRevision(
                    day_id=day_id,
                    base_revision=draft.revision,
                    revision=draft.revision + 1,
                    based_on_report_revision=report.based_on_revision,
                    replacement_day=ProposalDay(
                        day_id=day_id,
                        day_date=current_day.day_date,
                        theme=current_day.theme,
                        activities=tuple(proposals),
                        unfilled_slot_ids=unfilled,
                        weather_advice=current_day.weather_advice,
                    ),
                    preserve_activity_ids=preserve_activity_ids,
                    source_issue_ids=tuple(issue.issue_id for issue in issues),
                )

        class FailingRouteService(FakeRouteService):
            async def run_async(
                self, planning_input, draft, facts, *, route_context, only_day_id=None,
                **_kwargs,
            ):
                self.tracker.setdefault("route_day_ids", []).append(only_day_id)
                route_context.cancel_event.set()
                raise asyncio.TimeoutError

        pipeline = PlanningV4Pipeline(
            requirement_interpreter_factory=lambda _timeout: FakeInterpreter(tracker),
            dispatch_service_factory=lambda _timeout: RepairDispatch(tracker, facts, endpoint),
            daily_plan_adapter_factory=lambda _timeout: RevisingDailyPlanAdapter(tracker),
            current_route_service_factory=lambda: FailingRouteService(tracker),
            route_backfill_service_factory=lambda: FakeBackfill(tracker),
        )

        result = asyncio.run(pipeline.run(
            request,
            task_id="pipeline-route-repair-task",
            plan_id="plan-route-repair",
            progress=lambda _update: None,
        ))

        draft = result.snapshot.preview.draft
        day_constraints = result.snapshot.preview.planning_input.days
        first_day_id = tracker.get("repair_day_id", day_constraints[1].day_id)
        first_index = next(index for index, day in enumerate(day_constraints) if day.day_id == first_day_id)
        later_day_id = day_constraints[first_index + 1].day_id
        first_day = next(day for day in draft.days if day.day_id == first_day_id)
        self.assertEqual(len(first_day.activities), 2)
        self.assertEqual(tracker["route_day_ids"][0], None)
        self.assertEqual(tracker["route_day_ids"][1], first_day_id)
        self.assertEqual(result.snapshot.preview.calculation.current_routes, "failed")
        self.assertIn(later_day_id, tracker["revision_day_ids"])
        for day in tracker["generated_draft"].days:
            if day.day_id != first_day_id:
                self.assertEqual(
                    day.model_dump(mode="json"),
                    next(item for item in draft.days if item.day_id == day.day_id).model_dump(mode="json"),
                )
        self.assertGreaterEqual(len(tracker["route_contexts"]), 3)
        self.assertTrue(tracker["route_contexts"][0].closed)
        self.assertTrue(tracker["route_contexts"][1].closed)
        self.assertIs(result.route_context, tracker["route_contexts"][-1])
        target_legs = [leg for leg in draft.legs if leg.day_id == first_day_id]
        self.assertTrue(target_legs)
        self.assertTrue(all(
            mode.status == "error" and mode.error_category == "timeout"
            for leg in target_legs for mode in leg.mode_results
        ))

    def test_verified_departure_with_wrong_request_query_is_rejected(self):
        request = request_fixture(departure_query="其他站")
        _, endpoint = endpoint_fixture(request_fixture(), run_id="pipeline-task")
        result = PlanningDispatchResult(
            request_id=request.request_id,
            run_id="pipeline-task",
            context_revision=1,
            base_revision=None,
            action="plan",
            stop_reason="plan",
            reason="fake",
            endpoint_results=(endpoint,),
        )

        with self.assertRaises(PlanningV4PipelineError) as raised:
            PlanningV4Pipeline._verified_departure(result, "pipeline-task", request)
        self.assertEqual(raised.exception.code, "ENDPOINT_RESULT_IDENTITY_MISMATCH")

    def test_stage_timeout_does_not_cancel_pipeline_or_claim_global_deadline(self):
        clock = ManualClock(deadline=10)
        pipeline = PlanningV4Pipeline(clock=clock)
        stage_cancel_event = Event()
        global_cancel_event = Event()

        async def slow_stage():
            await asyncio.sleep(0.1)

        with self.assertRaises(PlanningV4PipelineError) as raised:
            asyncio.run(pipeline._await_stage(
                slow_stage(),
                timeout=0.001,
                stage="repair_revision",
                cancel_event=stage_cancel_event,
                global_deadline=10,
                global_cancel_event=global_cancel_event,
                timeout_code="DAY_REVISION_TIMEOUT",
            ))

        self.assertEqual(raised.exception.code, "DAY_REVISION_TIMEOUT")
        self.assertFalse(raised.exception.global_deadline)
        self.assertTrue(stage_cancel_event.is_set())
        self.assertFalse(global_cancel_event.is_set())

    def test_repair_model_failure_keeps_verified_partial_facts_and_returns_draft(self):
        request = request_fixture()
        initial_facts, endpoint = endpoint_fixture(request, run_id="pipeline-partial-task")
        extra_source, extra_fact = place_fact(
            "partial-fact", "partial-poi", "局部成功地点", "partial-source", 121.5, 31.2,
        )
        partial_facts = FactStore(
            sources=(*initial_facts.sources, extra_source),
            places=(*initial_facts.places, extra_fact),
        )
        tracker = {}

        class PartialFailureDispatch(FakeDispatch):
            async def run_async(self, context):
                if context.dispatch_scope is None:
                    return await super().run_async(context)
                self.tracker.setdefault("dispatch_contexts", []).append(context)
                return PlanningDispatchResult(
                    request_id=context.request_id,
                    run_id=context.run_id,
                    context_revision=context.context_revision,
                    base_revision=context.base_revision,
                    action=None,
                    stop_reason="technical_error",
                    reason="safe local model failure",
                    fact_store=partial_facts,
                    error=DispatchError(
                        code="MASTER_DECISION_FAILED",
                        step="master_decision",
                        message="主智能体决策未能完成。",
                    ),
                )

        pipeline = PlanningV4Pipeline(
            requirement_interpreter_factory=lambda _timeout: FakeInterpreter(tracker),
            dispatch_service_factory=lambda _timeout: PartialFailureDispatch(
                tracker, initial_facts, endpoint,
            ),
            daily_plan_adapter_factory=lambda _timeout: FakeDailyPlanAdapter(tracker),
            current_route_service_factory=lambda: FakeRouteService(tracker),
            route_backfill_service_factory=lambda: FakeBackfill(tracker),
        )

        result = asyncio.run(pipeline.run(
            request,
            task_id="pipeline-partial-task",
            plan_id="plan-partial-failure",
            progress=lambda _update: None,
        ))

        self.assertEqual(result.snapshot.preview.draft.plan_id, "plan-partial-failure")
        self.assertIn(extra_fact, result.snapshot.preview.facts.places)

    def test_route_timeout_replaces_cancelled_context_for_repair_and_backfill(self):
        request = request_fixture()
        facts, endpoint = endpoint_fixture(request, run_id="pipeline-route-timeout-task")
        tracker = {}

        class TimeoutRouteService(FakeRouteService):
            async def run_async(self, *_args, **_kwargs):
                raise asyncio.TimeoutError

        pipeline = PlanningV4Pipeline(
            requirement_interpreter_factory=lambda _timeout: FakeInterpreter(tracker),
            dispatch_service_factory=lambda _timeout: FakeDispatch(tracker, facts, endpoint),
            daily_plan_adapter_factory=lambda _timeout: FakeDailyPlanAdapter(tracker),
            current_route_service_factory=lambda: TimeoutRouteService(tracker),
            route_backfill_service_factory=lambda: FakeBackfill(tracker),
            day_repair_service_factory=lambda **callbacks: FakeRepair(tracker, **callbacks),
        )

        result = asyncio.run(pipeline.run(
            request,
            task_id="pipeline-route-timeout-task",
            plan_id="plan-route-timeout",
            progress=lambda _update: None,
        ))

        self.assertEqual(result.snapshot.preview.calculation.current_routes, "failed")
        self.assertEqual(result.snapshot.preview.calculation.alternative_backfill, "pending")
        self.assertIsNotNone(result.route_context)
        self.assertIsNotNone(result.backfill)
        self.assertGreaterEqual(len(tracker["route_contexts"]), 2)
        self.assertTrue(tracker["route_contexts"][0].closed)
        self.assertIs(result.route_context, tracker["route_contexts"][-1])
        self.assertFalse(result.route_context.cancel_event.is_set())

    def test_successful_targeted_route_results_clear_initial_failed_days(self):
        request = request_fixture(departure_query=None)
        planning_input = build_planning_input(PlanningInputBuildRequest(
            request=request,
            requirements=RequirementSummary(
                requirements_id="requirements-pipeline",
                request_id=request.request_id,
            ),
            rules=default_planning_v4_rules(),
        ))
        draft = PlanDraft(
            plan_id="plan-route-status",
            request_id=request.request_id,
            source_run_id="pipeline-route-status-task",
            revision=1,
            rule_version=planning_input.rules.rule_version,
            days=tuple(DraftDay(day_id=day.day_id, day_date=day.day_date)
                       for day in planning_input.days),
        )
        failed_days = {day.day_id for day in planning_input.days}

        PlanningV4Pipeline._record_route_day_result(
            failed_days, draft, planning_input.days[0].day_id, succeeded=True,
        )
        self.assertEqual(
            failed_days,
            {day.day_id for day in planning_input.days[1:]},
        )

        PlanningV4Pipeline._record_route_day_result(
            failed_days, draft, planning_input.days[1].day_id, succeeded=True,
        )
        PlanningV4Pipeline._record_route_day_result(
            failed_days, draft, planning_input.days[2].day_id, succeeded=True,
        )
        self.assertEqual(failed_days, set())

    def test_pending_route_fallback_marks_each_unfinished_mode_as_timeout_error(self):
        request = request_fixture(departure_query=None)
        planning_input = build_planning_input(PlanningInputBuildRequest(
            request=request,
            requirements=RequirementSummary(
                requirements_id="requirements-pipeline",
                request_id=request.request_id,
            ),
            rules=default_planning_v4_rules(),
        ))
        source_a, place_a = place_fact("route-fact-a", "route-poi-a", "甲地点", "route-source-a", 121.4, 31.2)
        source_b, place_b = place_fact("route-fact-b", "route-poi-b", "乙地点", "route-source-b", 121.5, 31.2)
        first_day = planning_input.days[0]
        slots = {item.period: item for item in first_day.slots}
        activities = (
            DraftActivity(
                activity_id="route-activity-a", candidate_ref="candidate-a",
                slot_id=slots["morning"].slot_id, kind="sightseeing",
                poi_id=place_a.poi_id, place_fact_id=place_a.fact_id,
                duration_s=3600, duration_source="default",
            ),
            DraftActivity(
                activity_id="route-activity-b", candidate_ref="candidate-b",
                slot_id=slots["afternoon"].slot_id, kind="sightseeing",
                poi_id=place_b.poi_id, place_fact_id=place_b.fact_id,
                duration_s=3600, duration_source="default",
            ),
        )
        draft = PlanDraft(
            plan_id="plan-route-pending",
            request_id=request.request_id,
            source_run_id="pipeline-task",
            revision=1,
            rule_version=planning_input.rules.rule_version,
            days=tuple(DraftDay(
                day_id=day.day_id,
                day_date=day.day_date,
                activities=activities if day.day_id == first_day.day_id else (),
            ) for day in planning_input.days),
        )
        failed, _ = PlanningV4Pipeline._pending_routes(
            planning_input,
            draft,
            FactStore(sources=(source_a, source_b), places=(place_a, place_b)),
            None,
            error_category="timeout",
        )

        self.assertEqual(len(failed.legs), 1)
        self.assertTrue(all(mode.status == "error" for mode in failed.legs[0].mode_results))
        self.assertTrue(all(mode.error_category == "timeout" for mode in failed.legs[0].mode_results))

    def test_verified_lodging_can_anchor_dining_scope_without_sightseeing(self):
        request = request_fixture(departure_query=None)
        planning_input = build_planning_input(PlanningInputBuildRequest(
            request=request,
            requirements=RequirementSummary(
                requirements_id="requirements-pipeline",
                request_id=request.request_id,
            ),
            rules=default_planning_v4_rules(),
        ))
        source, hotel = place_fact(
            "hotel-fact", "hotel-poi", "已核实酒店", "hotel-source", 121.47, 31.23,
        )
        area = AreaHint(
            area_hint_id="hotel-area",
            label="酒店附近",
            anchor_poi_ids=(hotel.poi_id,),
            center=hotel.location,
            basis_fact_ids=(hotel.fact_id,),
        )
        first_day = planning_input.days[0]
        draft = PlanDraft(
            plan_id="plan-hotel-anchor",
            request_id=request.request_id,
            source_run_id="pipeline-task",
            revision=1,
            rule_version=planning_input.rules.rule_version,
            days=tuple(DraftDay(
                day_id=day.day_id,
                day_date=day.day_date,
                lodging=(LodgingRef(
                    poi_id=hotel.poi_id,
                    place_fact_id=hotel.fact_id,
                    area_hint_id=area.area_hint_id,
                    selection_source="recommended",
                ) if day.day_id == first_day.day_id else None),
            ) for day in planning_input.days),
            area_hints=(area,),
        )
        lunch = next(slot for slot in first_day.slots if slot.period == "lunch")
        collection = DayRepairCollectionRequest(
            planning_input=planning_input,
            draft=draft,
            day=draft.days[0],
            issues=(),
            assignments=(),
            round_id="hotel-anchor-round",
            targets=(DayRepairCollectionTarget(
                day_id=first_day.day_id,
                role="dining",
                issue_ids=("missing-lunch",),
                slot_ids=(lunch.slot_id,),
            ),),
            fact_store=FactStore(sources=(source,), places=(hotel,)),
            candidates=(),
            area_hints=(area,),
        )

        scope = PlanningV4Pipeline._dispatch_scope(collection)

        self.assertEqual(
            {(item.day_id, item.place_fact_id) for item in scope.anchors},
            {(first_day.day_id, hotel.fact_id)},
        )
        self.assertEqual(scope.targets[0].area_hint_ids, (area.area_hint_id,))

    def test_route_context_closes_if_ready_progress_callback_fails(self):
        request = request_fixture()
        facts, endpoint = endpoint_fixture(request)
        tracker = {}
        pipeline = PlanningV4Pipeline(
            requirement_interpreter_factory=lambda _timeout: FakeInterpreter(tracker),
            dispatch_service_factory=lambda _timeout: FakeDispatch(tracker, facts, endpoint),
            daily_plan_adapter_factory=lambda _timeout: FakeDailyPlanAdapter(tracker),
            current_route_service_factory=lambda: FakeRouteService(tracker),
            route_backfill_service_factory=lambda: FakeBackfill(tracker),
            day_repair_service_factory=lambda **callbacks: FakeRepair(tracker, **callbacks),
        )

        async def progress(update):
            if update.stage == "ready":
                raise RuntimeError("progress sink unavailable")

        with self.assertRaises(PlanningV4PipelineError):
            asyncio.run(pipeline.run(
                request,
                task_id="pipeline-progress-failure-task",
                plan_id="plan-progress-failure",
                progress=progress,
            ))
        self.assertTrue(tracker["route_context"].closed)


    def test_pipeline_preserves_valid_draft_when_global_deadline_expires_after_f06(self):
        request = request_fixture()
        facts, endpoint = endpoint_fixture(request)
        tracker = {}
        clock = ManualClock(deadline=10.0)
        pipeline = PlanningV4Pipeline(
            total_timeout_seconds=10,
            requirement_interpreter_factory=lambda _timeout: FakeInterpreter(tracker),
            dispatch_service_factory=lambda _timeout: FakeDispatch(tracker, facts, endpoint),
            daily_plan_adapter_factory=lambda _timeout: FakeDailyPlanAdapter(tracker, clock),
            current_route_service_factory=lambda: FakeRouteService(tracker),
            route_backfill_service_factory=lambda: FakeBackfill(tracker),
            day_repair_service_factory=lambda **callbacks: FakeRepair(tracker, **callbacks),
            current_revision_lookup=lambda _plan_id: 1,
            clock=clock,
        )

        result = asyncio.run(pipeline.run(
            request,
            task_id="pipeline-task",
            plan_id="plan-deadline",
            progress=lambda _update: None,
        ))

        self.assertEqual(result.snapshot.preview.draft.plan_id, "plan-deadline")
        self.assertEqual(tracker["repair_collection"].stop_reason, "deadline")
        self.assertEqual(len(tracker["dispatch_contexts"]), 1)

    def test_initial_collection_result_can_return_after_local_window_before_global_deadline(self):
        request = request_fixture()
        facts, endpoint = endpoint_fixture(request, run_id="pipeline-initial-window-task")
        tracker = {}

        class SlowPartialDispatch(FakeDispatch):
            async def run_async(self, context):
                tracker["initial_dispatch_window"] = self.tracker.get("factory_timeout")
                await asyncio.sleep(0.02)
                return PlanningDispatchResult(
                    request_id=context.request_id,
                    run_id=context.run_id,
                    context_revision=context.context_revision,
                    base_revision=context.base_revision,
                    action="plan",
                    stop_reason="deadline",
                    reason="partial facts retained at the collection deadline",
                    fact_store=self.facts,
                    endpoint_results=(self.endpoint,),
                )

        def dispatch_factory(timeout):
            tracker["factory_timeout"] = timeout
            return SlowPartialDispatch(tracker, facts, endpoint)

        pipeline = PlanningV4Pipeline(
            total_timeout_seconds=1,
            initial_collection_timeout_seconds=0.001,
            requirement_interpreter_factory=lambda _timeout: FakeInterpreter(tracker),
            dispatch_service_factory=dispatch_factory,
            daily_plan_adapter_factory=lambda _timeout: FakeDailyPlanAdapter(tracker),
            current_route_service_factory=lambda: FakeRouteService(tracker),
            route_backfill_service_factory=lambda: FakeBackfill(tracker),
            day_repair_service_factory=lambda **callbacks: FakeRepair(tracker, **callbacks),
        )

        result = asyncio.run(pipeline.run(
            request,
            task_id="pipeline-initial-window-task",
            plan_id="plan-initial-window",
            progress=lambda _update: None,
        ))

        self.assertEqual(tracker["initial_dispatch_window"], 0.001)
        self.assertEqual(result.snapshot.preview.draft.plan_id, "plan-initial-window")


if __name__ == "__main__":
    unittest.main()
