"""F05b 调度器由可控主决策与执行器验证，不调用真实模型或地图。"""
import asyncio
from datetime import date, datetime
import json
import threading
from types import SimpleNamespace
import unittest

from app.agents.dining_subtask import DiningMealAreaBinding
from app.agents.execution import PlanningError
from app.models.planning_v4.facts import (
    FactSource,
    FactStore,
    MapCoordinate,
    PlaceFact,
    ToolError,
    ToolResult,
    WeatherFact,
)
from app.models.planning_v4.input import (
    DayConstraint,
    HardRequirement,
    PlanningInput,
    ReferenceWindow,
    RequirementSource,
    RequirementSummary,
    RuleSet,
    SlotConstraint,
    TripRequest,
)
from app.models.planning_v4.plan import AreaHint
from app.models.planning_v4.dispatch_scope import DispatchScope, DispatchTarget
from app.models.planning_v4.tasks import CandidateRef, Subtask, TaskDependency, TaskResult, TaskScope
from app.services.endpoint_verification_service import EndpointVerification, EndpointVerificationExecution
from app.services.planning_dispatch_service import (
    PlanningDispatchContext,
    PlanningDispatchService,
)
from app.agents.master_decider import MasterDecider
from app.services.weather_query_service import WeatherDateCoverage, WeatherQueryExecution


PERIODS = ("breakfast", "morning", "lunch", "afternoon", "dinner", "evening")
WINDOWS = (
    ReferenceWindow(period="breakfast", start_minute=420, end_minute=600),
    ReferenceWindow(period="morning", start_minute=540, end_minute=720),
    ReferenceWindow(period="lunch", start_minute=660, end_minute=840),
    ReferenceWindow(period="afternoon", start_minute=840, end_minute=1020),
    ReferenceWindow(period="dinner", start_minute=1020, end_minute=1200),
    ReferenceWindow(period="evening", start_minute=1140, end_minute=1380),
)


def make_input(*, arrival_query=None, departure_query=None):
    request = TripRequest(
        request_id="request-1",
        city="上海",
        arrival_at=datetime.fromisoformat("2026-10-10T09:00:00+08:00"),
        departure_at=datetime.fromisoformat("2026-10-12T19:00:00+08:00"),
        arrival_place_query=arrival_query,
        departure_place_query=departure_query,
    )
    requirements = RequirementSummary(requirements_id="requirements-1", request_id=request.request_id)
    rules = RuleSet(rule_version="rules-1", windows=WINDOWS)
    days = []
    for offset in range(3):
        day_date = date(2026, 10, 10 + offset)
        day_id = f"day-{day_date.isoformat()}"
        slots = tuple(SlotConstraint(
            slot_id=f"{day_id}-{period}", day_id=day_id, period=period, eligibility="required",
        ) for period in PERIODS)
        days.append(DayConstraint(day_id=day_id, day_date=day_date, slots=slots))
    return PlanningInput(request=request, requirements=requirements, rules=rules, days=tuple(days))


def subtask(task_id, *, role="attractions", day=0, period="morning", objective="找上海景点", dependencies=(),
            known_fact_ids=(), requirement_ids=(), area_hint_ids=(), slots=None):
    planning_input = make_input()
    day_row = planning_input.days[day]
    slot_ids = tuple(slots) if slots is not None else (f"{day_row.day_id}-{period}",)
    return Subtask(
        task_id=task_id,
        run_id="run-1",
        context_revision=7,
        base_revision=3,
        role=role,
        objective=objective,
        scope=TaskScope(
            city="上海",
            day_ids=(day_row.day_id,),
            slot_ids=slot_ids,
            area_hint_ids=tuple(area_hint_ids),
        ),
        dependencies=tuple(dependencies),
        known_fact_ids=tuple(known_fact_ids),
        requirement_ids=tuple(requirement_ids),
        candidate_target=1,
    )


def resolved(task, *, local_ref=None, meal_areas=(), must_eat=()):
    return SimpleNamespace(
        local_ref=local_ref or f"local-{task.task_id}",
        subtask=task,
        meal_areas=tuple(meal_areas),
        must_eat_restaurants=tuple(must_eat),
    )


def decision(context, action, *, tasks=(), weather_dates=(), arrival=False, departure=False, missing=(), areas=()):
    return SimpleNamespace(
        action=action,
        request_id=context.request_id,
        run_id=context.run_id,
        context_revision=context.context_revision,
        base_revision=context.base_revision,
        rule_version=context.planning_input.rules.rule_version,
        reason=f"{action} decision",
        missing_information=tuple(missing),
        areas=tuple(areas),
        tasks=tuple(tasks),
        weather_dates=tuple(weather_dates),
        verify_arrival_place=arrival,
        verify_departure_place=departure,
    )


class ScriptedDecider:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.contexts = []

    def run(self, context):
        self.contexts.append(context)
        return SimpleNamespace(decision=self.decisions.pop(0))


class ImmediateRunner:
    def __init__(self, tracker=None, *, result_factory=None, fail=False, pause=None):
        self.tracker = tracker if tracker is not None else {}
        self.result_factory = result_factory
        self.fail = fail
        self.pause = pause

    async def run_async(self, task, context):
        tracker = self.tracker
        tracker["created"] = tracker.get("created", 0) + 1
        tracker["active"] = tracker.get("active", 0) + 1
        tracker["peak"] = max(tracker.get("peak", 0), tracker["active"])
        tracker.setdefault("calls", []).append(task.task_id)
        tracker.setdefault("contexts", {})[task.task_id] = context
        try:
            if self.pause is not None:
                await self.pause.wait()
            else:
                await asyncio.sleep(0.01)
            if self.fail:
                raise RuntimeError("sensitive exception details must not leak")
            if self.result_factory:
                return self.result_factory(task, context)
            return execution(task, context, FactStore(), (), status="complete")
        finally:
            tracker["active"] -= 1


def execution(task, context, facts, candidates, *, status="complete", revision=None, request_id=None):
    stop_reason = {"complete": "target_met", "partial": "scope_exhausted", "failed": "tool_error"}[status]
    result = TaskResult(
        task_id=task.task_id,
        run_id=task.run_id,
        context_revision=revision if revision is not None else task.context_revision,
        request_id=request_id if request_id is not None else context.request_id,
        base_revision=task.base_revision,
        status=status,
        stop_reason=stop_reason,
        candidates=tuple(candidates),
    )
    return SimpleNamespace(request_id=context.request_id, fact_store=facts, task_result=result)


def place_fact(fact_id="place-fact-1", poi_id="poi-1", *, source_id="source-1", city="上海市", lng=121.47, lat=31.23):
    source = FactSource(
        tool_call_id=source_id,
        tool_name="maps_text_search",
        api_version="v3",
        queried_at=datetime.fromisoformat("2026-10-10T01:00:00+00:00"),
        effective_params={"city": "上海"},
    )
    fact = PlaceFact(
        fact_id=fact_id,
        poi_id=poi_id,
        name=f"地点 {poi_id}",
        city=city,
        location=MapCoordinate(lng=lng, lat=lat),
        source_refs=(source_id,),
        field_sources={"poi_id": source_id, "name": source_id, "location": source_id, "city": source_id},
    )
    return source, fact


def candidate(candidate_id, fact, *, category="sightseeing", slots=(), requirements=(), area_hint_id=None):
    return CandidateRef(
        candidate_id=candidate_id,
        category=category,
        poi_id=fact.poi_id,
        place_fact_id=fact.fact_id,
        slot_ids=tuple(slots),
        requirement_ids=tuple(requirements),
        area_hint_id=area_hint_id,
    )


def planning_context(planning_input=None, *, facts=None, candidates=(), areas=(), tasks=(), results=()):
    return PlanningDispatchContext(
        planning_input=planning_input or make_input(),
        run_id="run-1",
        context_revision=7,
        base_revision=3,
        fact_store=facts or FactStore(),
        candidate_refs=tuple(candidates),
        area_hints=tuple(areas),
        existing_tasks=tuple(tasks),
        task_results=tuple(results),
    )


def unknown_weather_execution(context, days):
    source = FactSource(
        tool_call_id=f"weather-source-{days[0].isoformat()}",
        tool_name="maps_weather",
        api_version="v3",
        queried_at=datetime.fromisoformat("2026-10-10T01:00:00+00:00"),
        effective_params={"city": "上海", "dates": ",".join(day.isoformat() for day in days)},
    )
    diagnostic = ToolResult[WeatherFact](
        tool_call_id=source.tool_call_id,
        status="error",
        source=source,
        error=ToolError(category="network", retryable=True),
    )
    return WeatherQueryExecution(
        request_id=context.request_id,
        run_id=context.run_id,
        context_revision=context.context_revision,
        base_revision=context.base_revision,
        city="上海市",
        travel_dates=tuple(days),
        tool_status="error",
        tool_error_category="network",
        diagnostic_tool_result=diagnostic,
        coverage=tuple(WeatherDateCoverage(
            forecast_date=day, status="unknown", reason="query_error",
        ) for day in days),
        fact_store=FactStore(),
    )


def make_service(decider, *, runner_factory=None, **kwargs):
    return PlanningDispatchService(
        decider=decider,
        decision_context_factory=lambda **values: SimpleNamespace(**values),
        runner_factories={"attractions": runner_factory} if runner_factory else {},
        **kwargs,
    )


class PlanningDispatchServiceTests(unittest.TestCase):
    def test_targeted_dispatch_accepts_area_derived_from_new_target_day_attraction(self):
        base = make_input()
        day = base.days[0]
        morning = f"{day.day_id}-morning"
        lunch = f"{day.day_id}-lunch"
        source, place = place_fact()
        attraction = subtask("target-attraction", day=0, period="morning")
        attraction_candidate = candidate(
            "target-attraction-candidate", place, slots=(morning,),
        )
        attraction_result = execution(
            attraction,
            SimpleNamespace(request_id=base.request.request_id),
            FactStore(sources=(source,), places=(place,)),
            (attraction_candidate,),
            status="partial",
        ).task_result
        scope = DispatchScope(targets=(
            DispatchTarget(
                target_id="missing-attraction", issue_code="missing_sightseeing",
                role="attractions", day_id=day.day_id, slot_ids=(morning,),
            ),
            DispatchTarget(
                target_id="missing-lunch", issue_code="missing_meal",
                role="dining", day_id=day.day_id, slot_ids=(lunch,),
            ),
        ))
        context = planning_context(
            planning_input=base,
            facts=FactStore(sources=(source,), places=(place,)),
            candidates=(attraction_candidate,),
            tasks=(attraction,),
            results=(attraction_result,),
        ).model_copy(update={"dispatch_scope": scope})
        area = AreaHint(
            area_hint_id="new-area-from-target-day",
            label="目标日景点附近",
            anchor_poi_ids=(place.poi_id,),
            center=place.location,
            basis_fact_ids=(place.fact_id,),
            basis="verified_place_facts",
        )
        dining = subtask(
            "target-lunch", role="dining", day=0, period="lunch",
            known_fact_ids=(place.fact_id,), area_hint_ids=(area.area_hint_id,),
        )
        valid_decision = decision(
            context,
            "delegate",
            tasks=(resolved(dining, meal_areas=(SimpleNamespace(
                slot_id=lunch,
                area_hint_id=area.area_hint_id,
                anchor_poi_id=place.poi_id,
            ),)),),
            areas=(area,),
        )
        live_context = SimpleNamespace(
            fact_store=context.fact_store,
            candidates=context.candidate_refs,
            existing_tasks=context.existing_tasks,
            task_results=context.task_results,
        )

        PlanningDispatchService._validate_delegate_identity(
            context, valid_decision, live_context=live_context,
        )

        wrong_day_candidate = candidate(
            "other-day-candidate", place,
            slots=(f"{base.days[1].day_id}-morning",),
        )
        wrong_day_context = context.model_copy(update={
            "candidate_refs": (wrong_day_candidate,),
            "task_results": (attraction_result.model_copy(update={
                "candidates": (wrong_day_candidate,),
            }),),
        })
        wrong_day_live = SimpleNamespace(
            fact_store=wrong_day_context.fact_store,
            candidates=wrong_day_context.candidate_refs,
            existing_tasks=wrong_day_context.existing_tasks,
            task_results=wrong_day_context.task_results,
        )
        with self.assertRaises(Exception) as raised:
            PlanningDispatchService._validate_delegate_identity(
                wrong_day_context,
                valid_decision,
                live_context=wrong_day_live,
            )
        self.assertEqual(getattr(raised.exception, "code", None), "DISPATCH_SCOPE_AREA_INVALID")

    def test_targeted_dispatch_rejects_fake_decider_task_outside_slot_scope(self):
        context = planning_context()
        targeted_slot = context.planning_input.days[0].slots[1].slot_id
        context = context.model_copy(update={
            "dispatch_scope": DispatchScope(targets=(DispatchTarget(
                target_id="missing-morning",
                issue_code="missing_sightseeing",
                role="attractions",
                day_id=context.planning_input.days[0].day_id,
                slot_ids=(targeted_slot,),
            ),)),
        })
        outside_task = subtask("outside-scope", period="afternoon")
        tracker = {}
        decider = ScriptedDecider([decision(context, "delegate", tasks=(resolved(outside_task),))])

        result = asyncio.run(make_service(
            decider, runner_factory=lambda: ImmediateRunner(tracker),
        ).run_async(context))

        self.assertEqual(result.stop_reason, "technical_error")
        self.assertEqual(result.error.code, "DISPATCH_SCOPE_TASK_INVALID")
        self.assertEqual(tracker.get("calls", []), [])

    def test_ready_tasks_run_in_parallel_with_three_dispatch_slots_and_fixed_revision(self):
        context = planning_context()
        tasks = tuple(resolved(subtask(f"task-{index}", objective=f"查询区域 {index}")) for index in range(5))
        tracker = {}
        decider = ScriptedDecider([
            decision(context, "delegate", tasks=tasks),
            decision(context, "plan", missing=("天气未知",)),
        ])
        service = make_service(decider, runner_factory=lambda: ImmediateRunner(tracker))

        result = asyncio.run(service.run_async(context))

        self.assertEqual(result.stop_reason, "plan")
        self.assertEqual(len(result.task_results), 5)
        self.assertEqual(tracker["peak"], 3)
        self.assertEqual(tracker["created"], 5)
        self.assertEqual([item.context_revision for item in decider.contexts], [7, 7])
        self.assertEqual({item.context_revision for item in result.task_results}, {7})

    def test_fact_dependency_waits_until_fact_exists_and_unknown_dependency_never_starts(self):
        context = planning_context()
        pending = resolved(subtask(
            "wait-task",
            dependencies=(TaskDependency(kind="fact", reference_id="future-fact"),),
            known_fact_ids=("future-fact",),
        ))
        decider = ScriptedDecider([decision(context, "delegate", tasks=(pending,))])
        tracker = {}

        result = asyncio.run(make_service(
            decider, runner_factory=lambda: ImmediateRunner(tracker),
        ).run_async(context))

        self.assertEqual(result.stop_reason, "no_progress")
        self.assertEqual(tracker.get("calls", []), [])
        self.assertEqual(result.task_states[0].status, "waiting")

    def test_partial_prerequisite_with_candidate_facts_unlocks_dependent_task(self):
        context = planning_context()
        first = subtask("first", objective="先找景点")
        second = subtask(
            "second", period="afternoon", objective="依赖先行结果",
            dependencies=(TaskDependency(kind="task", reference_id="first"),),
        )
        source, fact = place_fact()
        selected = candidate("candidate-1", fact, slots=(first.scope.slot_ids[0],))
        tracker = {}

        def make_result(task, child_context):
            if task.task_id == "first":
                return execution(task, child_context, FactStore(sources=(source,), places=(fact,)), (selected,), status="partial")
            self.assertIn("first", child_context.ready_task_ids)
            self.assertIn(fact.fact_id, {item.fact_id for item in child_context.fact_store.places})
            return execution(task, child_context, FactStore(), (), status="complete")

        decider = ScriptedDecider([
            decision(context, "delegate", tasks=(resolved(first), resolved(second))),
            decision(context, "plan"),
        ])
        service = make_service(
            decider,
            runner_factory=lambda: ImmediateRunner(tracker, result_factory=make_result),
        )

        result = asyncio.run(service.run_async(context))

        self.assertEqual(tracker["calls"], ["first", "second"])
        self.assertEqual([item.status for item in result.task_results], ["partial", "complete"])

    def test_failed_prerequisite_blocks_dependent_but_keeps_local_failure(self):
        context = planning_context()
        first = subtask("first", objective="先行步骤")
        second = subtask(
            "second", period="afternoon", objective="依赖先行步骤",
            dependencies=(TaskDependency(kind="task", reference_id="first"),),
        )
        tracker = {}
        decider = ScriptedDecider([
            decision(context, "delegate", tasks=(resolved(first), resolved(second))),
            decision(context, "plan"),
        ])

        result = asyncio.run(make_service(
            decider,
            runner_factory=lambda: ImmediateRunner(tracker, fail=True),
        ).run_async(context))

        self.assertEqual(tracker["calls"], ["first"])
        self.assertEqual(result.stop_reason, "plan")
        self.assertEqual(result.task_results[0].status, "failed")
        self.assertEqual(next(item for item in result.task_states if item.task_id == "second").status, "blocked")
        self.assertNotIn("sensitive exception details", " ".join(result.missing_information))

    def test_dining_context_uses_resolved_meal_area_and_named_restaurant(self):
        context = planning_context()
        source, fact = place_fact()
        facts = FactStore(sources=(source,), places=(fact,))
        hint = AreaHint(
            area_hint_id="area-lunch",
            label="外滩",
            anchor_poi_ids=(fact.poi_id,),
            center=fact.location,
            basis_fact_ids=(fact.fact_id,),
            basis="verified_place_facts",
        )
        slot_id = context.planning_input.days[0].slots[2].slot_id
        task = subtask(
            "dining-task", role="dining", period="lunch", objective="查询指定餐馆",
            known_fact_ids=(fact.fact_id,), area_hint_ids=(hint.area_hint_id,),
        )
        resolved_task = resolved(
            task,
            meal_areas=(SimpleNamespace(slot_id=slot_id, area_hint_id=hint.area_hint_id, anchor_poi_id=fact.poi_id),),
            must_eat=(SimpleNamespace(requirement_id="req-food", restaurant_name="老饭店"),),
        )
        context = planning_context(facts=facts, areas=(hint,))
        observed = {}
        runner = ImmediateRunner(observed)
        decider = ScriptedDecider([
            decision(context, "delegate", tasks=(resolved_task,)),
            decision(context, "plan"),
        ])
        service = PlanningDispatchService(
            decider=decider,
            decision_context_factory=lambda **values: SimpleNamespace(**values),
            runner_factories={"dining": lambda: runner},
        )

        result = asyncio.run(service.run_async(context))

        dining_context = observed["contexts"]["dining-task"]
        self.assertEqual(dining_context.meal_areas[0].area_hint_id, "area-lunch")
        self.assertEqual(dining_context.meal_areas[0].anchor_poi_id, fact.poi_id)
        self.assertEqual(dining_context.must_eat_restaurants[0].restaurant_name, "老饭店")
        self.assertEqual(result.stop_reason, "plan")

    def test_local_failure_keeps_successful_sibling_facts(self):
        context = planning_context()
        good, bad = subtask("good", objective="成功查询"), subtask("bad", objective="局部失败")
        source, fact = place_fact(fact_id="good-fact")

        def make_result(task, child_context):
            if task.task_id == "bad":
                raise RuntimeError("secret response content")
            return execution(task, child_context, FactStore(sources=(source,), places=(fact,)), ())

        decider = ScriptedDecider([
            decision(context, "delegate", tasks=(resolved(good), resolved(bad))),
            decision(context, "plan"),
        ])
        result = asyncio.run(make_service(
            decider,
            runner_factory=lambda: ImmediateRunner(result_factory=make_result),
        ).run_async(context))

        self.assertEqual(result.stop_reason, "plan")
        self.assertEqual({item.status for item in result.task_results}, {"complete", "failed"})
        self.assertEqual([item.fact_id for item in result.fact_store.places], ["good-fact"])
        self.assertNotIn("secret response content", str(result.model_dump()))

    def test_local_planning_error_preserves_safe_code_in_issue(self):
        context = planning_context()
        task = subtask("preflight-failure", role="dining", period="dinner")
        decider = ScriptedDecider([
            decision(context, "delegate", tasks=(resolved(task),)),
            decision(context, "plan"),
        ])

        def fail_preflight(_task, _child_context):
            raise PlanningError("CANDIDATE_REF_INVALID", "敏感诊断不应泄露", step="dining_preflight")

        service = PlanningDispatchService(
            decider=decider,
            decision_context_factory=lambda **values: SimpleNamespace(**values),
            runner_factories={"dining": lambda: ImmediateRunner(result_factory=fail_preflight)},
        )
        result = asyncio.run(service.run_async(context))

        issue = next(item for item in result.issues if item.reference_id == task.task_id)
        self.assertEqual(issue.code, "CANDIDATE_REF_INVALID")
        self.assertNotIn("敏感诊断不应泄露", str(result.model_dump()))
        self.assertEqual(result.task_states[0].status, "failed")

    def test_version_mismatch_rejects_child_result_and_preserves_initial_facts(self):
        source, existing = place_fact(fact_id="initial-fact")
        context = planning_context(facts=FactStore(sources=(source,), places=(existing,)))
        task = subtask("mismatch")
        child_source, child_fact = place_fact(
            fact_id="child-fact", poi_id="child-poi", source_id="child-source",
        )
        decider = ScriptedDecider([decision(context, "delegate", tasks=(resolved(task),))])
        result = asyncio.run(make_service(
            decider,
            runner_factory=lambda: ImmediateRunner(result_factory=lambda item, child: execution(
                item, child, FactStore(sources=(child_source,), places=(child_fact,)), (), revision=8,
            )),
        ).run_async(context))

        self.assertEqual(result.stop_reason, "technical_error")
        self.assertEqual(result.error.code, "TASK_RESULT_IDENTITY_MISMATCH")
        self.assertEqual([item.fact_id for item in result.fact_store.places], ["initial-fact"])
        self.assertEqual(result.task_results, ())

    def test_semantically_duplicate_task_stops_as_no_progress(self):
        context = planning_context()
        first = resolved(subtask("task-random-1", objective="查找静安景点"), local_ref="local-random-1")
        repeated = resolved(subtask("task-random-2", objective="查找静安景点"), local_ref="local-random-2")
        decider = ScriptedDecider([
            decision(context, "delegate", tasks=(first,)),
            decision(context, "delegate", tasks=(repeated,)),
        ])
        tracker = {}
        result = asyncio.run(make_service(
            decider, runner_factory=lambda: ImmediateRunner(tracker),
        ).run_async(context))

        self.assertEqual(result.stop_reason, "no_progress")
        self.assertEqual(tracker["calls"], ["task-random-1"])
        self.assertEqual(next(item for item in result.task_states if item.task_id == "task-random-2").status, "duplicate")

    def test_duplicate_task_is_skipped_while_new_sibling_still_runs(self):
        context = planning_context()
        first = resolved(subtask("task-random-1", objective="找市中心景点"))
        repeated = resolved(subtask("task-random-2", objective="找市中心景点"), local_ref="new-local-ref")
        new_task = resolved(subtask("task-new", period="afternoon", objective="找下午景点"))
        decider = ScriptedDecider([
            decision(context, "delegate", tasks=(first,)),
            decision(context, "delegate", tasks=(repeated, new_task)),
            decision(context, "plan"),
        ])
        tracker = {}
        result = asyncio.run(make_service(
            decider, runner_factory=lambda: ImmediateRunner(tracker),
        ).run_async(context))

        self.assertEqual(tracker["calls"], ["task-random-1", "task-new"])
        self.assertEqual(next(item for item in result.task_states if item.task_id == "task-random-2").status, "duplicate")
        self.assertIn("task-new", {item.task_id for item in result.task_results})

    def test_repeated_unknown_weather_date_is_attempted_once(self):
        context = planning_context()
        requested_day = context.planning_input.days[0].day_date
        calls = []

        class WeatherService:
            async def run_async(self, weather_context):
                dates = tuple(item.day_date for item in weather_context.planning_input.days)
                calls.append(dates)
                return unknown_weather_execution(weather_context, dates)

        decider = ScriptedDecider([
            decision(context, "delegate", weather_dates=(requested_day,)),
            decision(context, "delegate", weather_dates=(requested_day,)),
        ])
        result = asyncio.run(PlanningDispatchService(
            decider=decider,
            decision_context_factory=lambda **values: SimpleNamespace(**values),
            weather_service_factory=WeatherService,
        ).run_async(context))

        self.assertEqual(result.stop_reason, "no_progress")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], tuple(item.day_date for item in context.planning_input.days))
        self.assertEqual(result.weather_results[0].travel_dates, (requested_day,))
        self.assertEqual(result.weather_attempted_dates, (requested_day,))
        self.assertIn("天气未知", " ".join(result.missing_information))

    def test_conflicting_source_id_rejects_later_fact_store_without_overwrite(self):
        source, initial = place_fact(fact_id="initial-fact")
        context = planning_context(facts=FactStore(sources=(source,), places=(initial,)))
        conflicting_source = source.model_copy(update={"effective_params": {"keywords": "different search"}})
        _, child = place_fact(fact_id="child-fact", poi_id="child-poi", source_id=source.tool_call_id)
        child = child.model_copy(update={"field_sources": {
            "poi_id": source.tool_call_id, "name": source.tool_call_id,
            "location": source.tool_call_id, "city": source.tool_call_id,
        }})
        task = resolved(subtask("source-conflict"))
        decider = ScriptedDecider([decision(context, "delegate", tasks=(task,))])
        result = asyncio.run(make_service(
            decider,
            runner_factory=lambda: ImmediateRunner(result_factory=lambda item, child_context: execution(
                item, child_context, FactStore(sources=(conflicting_source,), places=(child,)), (),
            )),
        ).run_async(context))

        self.assertEqual(result.stop_reason, "technical_error")
        self.assertEqual(result.error.code, "FACT_SOURCE_ID_CONFLICT")
        self.assertEqual([item.fact_id for item in result.fact_store.places], ["initial-fact"])

    def test_conflicting_fact_id_rejects_later_fact_store_without_overwrite(self):
        source, initial = place_fact(fact_id="same-fact", poi_id="initial-poi")
        context = planning_context(facts=FactStore(sources=(source,), places=(initial,)))
        child_source, child = place_fact(
            fact_id="same-fact", poi_id="child-poi", source_id="child-source",
        )
        task = resolved(subtask("fact-conflict"))
        decider = ScriptedDecider([decision(context, "delegate", tasks=(task,))])
        result = asyncio.run(make_service(
            decider,
            runner_factory=lambda: ImmediateRunner(result_factory=lambda item, child_context: execution(
                item, child_context, FactStore(sources=(child_source,), places=(child,)), (),
            )),
        ).run_async(context))

        self.assertEqual(result.stop_reason, "technical_error")
        self.assertEqual(result.error.code, "FACT_ID_CONFLICT")
        self.assertEqual([(item.fact_id, item.poi_id) for item in result.fact_store.places], [("same-fact", "initial-poi")])

    def test_fact_id_conflict_across_fact_types_is_explicit(self):
        source, initial = place_fact(fact_id="shared-id", poi_id="initial-poi")
        context = planning_context(facts=FactStore(sources=(source,), places=(initial,)))
        weather_source = FactSource(
            tool_call_id="weather-source", tool_name="maps_weather", api_version="v3",
            queried_at=datetime.fromisoformat("2026-10-10T01:00:00+00:00"),
            effective_params={"city": "上海"},
        )
        weather_fact = WeatherFact(fact_id="shared-id", city="上海", source=weather_source)
        task = resolved(subtask("cross-type-conflict"))
        decider = ScriptedDecider([decision(context, "delegate", tasks=(task,))])
        result = asyncio.run(make_service(
            decider,
            runner_factory=lambda: ImmediateRunner(result_factory=lambda item, child_context: execution(
                item, child_context,
                FactStore(sources=(weather_source,), weather=(weather_fact,)), (),
            )),
        ).run_async(context))

        self.assertEqual(result.stop_reason, "technical_error")
        self.assertEqual(result.error.code, "FACT_ID_CONFLICT")
        self.assertEqual([(item.fact_id, item.poi_id) for item in result.fact_store.places], [("shared-id", "initial-poi")])
        self.assertEqual(result.fact_store.weather, ())

    def test_candidate_projection_unions_role_scoped_slots_and_requirements(self):
        planning_input = make_input()
        morning = planning_input.days[0].slots[1].slot_id
        afternoon = planning_input.days[0].slots[3].slot_id
        source, fact = place_fact()
        candidates = (
            candidate("candidate-a", fact, slots=(morning,), requirements=("req-a",)),
            candidate("candidate-b", fact, slots=(afternoon,), requirements=("req-b",)),
        )
        task = subtask(
            "projection", slots=(morning, afternoon), requirement_ids=("req-a", "req-b"),
        )
        context = planning_context(planning_input, facts=FactStore(sources=(source,), places=(fact,)), candidates=candidates)
        tracker = {}
        decider = ScriptedDecider([
            decision(context, "delegate", tasks=(resolved(task),)),
            decision(context, "plan"),
        ])
        result = asyncio.run(make_service(
            decider, runner_factory=lambda: ImmediateRunner(tracker),
        ).run_async(context))

        projection = tracker["contexts"]["projection"].candidate_refs
        self.assertEqual(len(projection), 1)
        self.assertEqual(set(projection[0].slot_ids), {morning, afternoon})
        self.assertEqual(set(projection[0].requirement_ids), {"req-a", "req-b"})
        self.assertEqual(result.stop_reason, "plan")

    def test_dining_projection_filters_candidates_to_current_area_slots_and_named_binding(self):
        from app.models.planning_v4 import AreaHint, FactStore
        from app.services.planning_dispatch_service import PlanningDispatchService

        base = make_input()
        dinner = base.days[0].slots[4].slot_id
        named = HardRequirement(
            requirement_id="req-named", kind="must_include", text="必吃老饭店",
            source=RequirementSource(field="remarks", quote="必吃老饭店"),
        )
        request = base.request.model_copy(update={"remarks": "必吃老饭店"})
        requirements = base.requirements.model_copy(update={"hard_requirements": (named,)})
        planning_input = base.model_copy(update={"request": request, "requirements": requirements})

        anchor_source, anchor = place_fact(
            "anchor-fact", "anchor", source_id="anchor-source", lng=121.49, lat=31.24,
        )
        close_source, close_place = place_fact(
            "close-fact", "close-meal", source_id="close-source", lng=121.494, lat=31.24,
        )
        close_place = close_place.model_copy(update={
            "name": "附近餐馆", "type_name": "餐饮服务;中餐厅", "typecode": "050100",
            "field_sources": {**close_place.field_sources, "type_name": close_source.tool_call_id,
                              "typecode": close_source.tool_call_id},
        })
        far_source, far_place = place_fact(
            "far-fact", "far-meal", source_id="far-source", lng=121.51, lat=31.24,
        )
        far_place = far_place.model_copy(update={
            "name": "远处餐馆", "type_name": "餐饮服务;中餐厅", "typecode": "050100",
            "field_sources": {**far_place.field_sources, "type_name": far_source.tool_call_id,
                              "typecode": far_source.tool_call_id},
        })
        exact_source, exact_place = place_fact(
            "exact-fact", "exact-meal", source_id="exact-source", lng=121.52, lat=31.24,
        )
        exact_place = exact_place.model_copy(update={
            "name": "老饭店", "type_name": "餐饮服务;中餐厅", "typecode": "050100",
            "field_sources": {**exact_place.field_sources, "type_name": exact_source.tool_call_id,
                              "typecode": exact_source.tool_call_id},
        })
        wrong_source, wrong_place = place_fact(
            "wrong-fact", "wrong-meal", source_id="wrong-source", lng=121.53, lat=31.24,
        )
        wrong_place = wrong_place.model_copy(update={
            "name": "别家餐馆", "type_name": "餐饮服务;中餐厅", "typecode": "050100",
            "field_sources": {**wrong_place.field_sources, "type_name": wrong_source.tool_call_id,
                              "typecode": wrong_source.tool_call_id},
        })
        facts = FactStore(
            sources=(anchor_source, close_source, far_source, exact_source, wrong_source),
            places=(anchor, close_place, far_place, exact_place, wrong_place),
        )
        area = AreaHint(
            area_hint_id="area-current", label="当前餐次区域", anchor_poi_ids=(anchor.poi_id,),
            center=anchor.location, basis_fact_ids=(anchor.fact_id,), basis="verified_place_facts",
        )
        originals = (
            candidate("near-unrestricted", close_place, category="meal"),
            candidate("far-unrestricted", far_place, category="meal"),
            candidate("far-slot-bound", far_place, category="meal", slots=(dinner,)),
            candidate("exact-named", exact_place, category="meal", requirements=("req-named",)),
            candidate("wrong-named", wrong_place, category="meal", requirements=("req-named",)),
        )
        context = planning_context(
            planning_input, facts=facts, candidates=originals, areas=(area,),
        )
        task = subtask(
            "dining-current-area", role="dining", period="dinner",
            requirement_ids=("req-named",), area_hint_ids=(area.area_hint_id,),
        )
        resolved_task = resolved(
            task,
            meal_areas=(DiningMealAreaBinding(
                slot_id=dinner, area_hint_id=area.area_hint_id, anchor_poi_id=anchor.poi_id,
            ),),
            must_eat=(SimpleNamespace(requirement_id="req-named", restaurant_name="老饭店"),),
        )
        state = SimpleNamespace(context=context, candidate_refs=list(originals), fact_store=facts)

        projected = PlanningDispatchService()._project_candidates(state, task, resolved_task=resolved_task)
        by_poi = {item.poi_id: item for item in projected}

        self.assertEqual(by_poi[close_place.poi_id].slot_ids, (dinner,))
        self.assertNotEqual(by_poi[close_place.poi_id].candidate_id, "near-unrestricted")
        self.assertEqual(by_poi[exact_place.poi_id].requirement_ids, ("req-named",))
        self.assertEqual(by_poi[exact_place.poi_id].slot_ids, ())
        self.assertNotIn(far_place.poi_id, by_poi, "范围外旧候选不能令当前餐次整项 preflight 失败")
        self.assertNotIn(wrong_place.poi_id, by_poi, "无餐次适用性且店名不符时不能保留命名绑定")
        self.assertEqual(tuple(state.candidate_refs), originals, "过滤仅作用于子任务投影")
        self.assertEqual(facts.places, (anchor, close_place, far_place, exact_place, wrong_place))
        repeated = PlanningDispatchService()._project_candidates(state, task, resolved_task=resolved_task)
        self.assertEqual(projected, repeated, "投影候选身份必须稳定")

    def test_projected_candidate_echo_uses_new_id_and_preserves_seven_slot_source(self):
        planning_input = make_input()
        all_slots = (
            f"{planning_input.days[0].day_id}-afternoon",
            f"{planning_input.days[0].day_id}-evening",
            f"{planning_input.days[1].day_id}-morning",
            f"{planning_input.days[1].day_id}-afternoon",
            f"{planning_input.days[1].day_id}-evening",
            f"{planning_input.days[2].day_id}-morning",
            f"{planning_input.days[2].day_id}-afternoon",
        )
        source, fact = place_fact()
        original = candidate("seven-slot-source", fact, slots=all_slots)
        context = planning_context(
            planning_input,
            facts=FactStore(sources=(source,), places=(fact,)),
            candidates=(original,),
        )
        task = subtask("one-slot", slots=(all_slots[0],))
        tracker = {}
        decider = ScriptedDecider([
            decision(context, "delegate", tasks=(resolved(task),)),
            decision(context, "plan"),
        ])
        result = asyncio.run(make_service(
            decider,
            runner_factory=lambda: ImmediateRunner(tracker, result_factory=lambda item, child: execution(
                item, child, FactStore(), child.candidate_refs,
            )),
        ).run_async(context))

        projected = tracker["contexts"]["one-slot"].candidate_refs[0]
        self.assertNotEqual(projected.candidate_id, original.candidate_id)
        self.assertEqual(projected.slot_ids, (all_slots[0],))
        self.assertEqual(result.stop_reason, "plan")
        self.assertEqual(next(item for item in result.candidate_refs
                              if item.candidate_id == original.candidate_id), original)
        self.assertIn(projected, result.candidate_refs)

    def test_unrestricted_and_restricted_same_poi_project_to_current_two_slots_and_merge(self):
        planning_input = make_input()
        two_slots = (
            f"{planning_input.days[0].day_id}-afternoon",
            f"{planning_input.days[0].day_id}-evening",
        )
        source, fact = place_fact()
        unrestricted = candidate("unrestricted-source", fact)
        restricted = candidate("restricted-source", fact, slots=(two_slots[0],))
        context = planning_context(
            planning_input,
            facts=FactStore(sources=(source,), places=(fact,)),
            candidates=(unrestricted, restricted),
        )
        task = subtask("two-slot-projection", slots=two_slots)
        tracker = {}
        decider = ScriptedDecider([
            decision(context, "delegate", tasks=(resolved(task),)),
            decision(context, "plan"),
        ])

        result = asyncio.run(make_service(
            decider,
            runner_factory=lambda: ImmediateRunner(tracker, result_factory=lambda item, child: execution(
                item, child, FactStore(), child.candidate_refs,
            )),
        ).run_async(context))

        projected = tracker["contexts"][task.task_id].candidate_refs
        self.assertEqual(len(projected), 1)
        self.assertEqual(set(projected[0].slot_ids), set(two_slots))
        self.assertNotIn(projected[0].candidate_id, {unrestricted.candidate_id, restricted.candidate_id})
        self.assertEqual(result.stop_reason, "plan")
        self.assertIsNone(result.error)
        self.assertEqual({item.candidate_id for item in result.candidate_refs}, {
            unrestricted.candidate_id, restricted.candidate_id, projected[0].candidate_id,
        })
        self.assertEqual(result.task_results[0].candidates, projected)
        self.assertEqual(result.task_results[0].candidates[0].place_fact_id, fact.fact_id)
        self.assertEqual(result.fact_store.places, (fact,))

    def test_unchanged_candidate_projection_keeps_original_id(self):
        planning_input = make_input()
        all_slots = (
            f"{planning_input.days[0].day_id}-afternoon",
            f"{planning_input.days[0].day_id}-evening",
            f"{planning_input.days[1].day_id}-morning",
            f"{planning_input.days[1].day_id}-afternoon",
            f"{planning_input.days[1].day_id}-evening",
            f"{planning_input.days[2].day_id}-morning",
            f"{planning_input.days[2].day_id}-afternoon",
        )
        source, fact = place_fact()
        original = candidate("unchanged-source", fact, slots=all_slots)
        context = planning_context(
            planning_input,
            facts=FactStore(sources=(source,), places=(fact,)),
            candidates=(original,),
        )
        task = subtask("seven-slots", slots=all_slots)
        task = task.model_copy(update={
            "scope": TaskScope(
                city="上海",
                day_ids=tuple(day.day_id for day in planning_input.days),
                slot_ids=all_slots,
            ),
        })
        tracker = {}
        decider = ScriptedDecider([
            decision(context, "delegate", tasks=(resolved(task),)),
            decision(context, "plan"),
        ])
        result = asyncio.run(make_service(
            decider,
            runner_factory=lambda: ImmediateRunner(tracker, result_factory=lambda item, child: execution(
                item, child, FactStore(), child.candidate_refs,
            )),
        ).run_async(context))

        projected = tracker["contexts"]["seven-slots"].candidate_refs[0]
        self.assertEqual(projected, original)
        self.assertEqual(tuple(item.candidate_id for item in result.candidate_refs), (original.candidate_id,))
        self.assertEqual(result.stop_reason, "plan")

    def test_lodging_candidate_projection_preserves_area_association(self):
        context = planning_context()
        source, fact = place_fact()
        hint = AreaHint(
            area_hint_id="stay-area", label="外滩", anchor_poi_ids=(fact.poi_id,),
            center=fact.location, basis_fact_ids=(fact.fact_id,), basis="verified_place_facts",
        )
        lodging_candidate = candidate(
            "lodging-candidate", fact, category="lodging", area_hint_id=hint.area_hint_id,
        )
        task = subtask("lodging", role="lodging", area_hint_ids=(hint.area_hint_id,))
        context = planning_context(
            facts=FactStore(sources=(source,), places=(fact,)), candidates=(lodging_candidate,), areas=(hint,),
        )
        tracker = {}
        decider = ScriptedDecider([
            decision(context, "delegate", tasks=(resolved(task),)),
            decision(context, "plan"),
        ])
        service = PlanningDispatchService(
            decider=decider,
            decision_context_factory=lambda **values: SimpleNamespace(**values),
            runner_factories={"lodging": lambda: ImmediateRunner(tracker)},
        )

        state = SimpleNamespace(context=context, fact_store=context.fact_store,
                                candidate_refs=context.candidate_refs, area_hints=context.area_hints)
        projected = service._project_candidates(state, task, resolved_task=resolved(task))
        self.assertEqual(len(projected), 1)
        self.assertEqual(projected[0].area_hint_id, hint.area_hint_id)

    def test_task_result_candidate_must_close_over_valid_child_fact_and_source(self):
        context = planning_context()
        task = subtask("bad-candidate")
        source, fact = place_fact()
        dangling = candidate("dangling", fact.model_copy(update={"fact_id": "not-in-store"}))
        decider = ScriptedDecider([decision(context, "delegate", tasks=(resolved(task),))])
        result = asyncio.run(make_service(
            decider,
            runner_factory=lambda: ImmediateRunner(result_factory=lambda item, child: execution(
                item, child, FactStore(sources=(source,), places=(fact,)), (dangling,),
            )),
        ).run_async(context))

        self.assertEqual(result.stop_reason, "technical_error")
        self.assertEqual(result.error.code, "TASK_CANDIDATE_SOURCE_INVALID")
        self.assertEqual(result.fact_store.places, ())
        self.assertEqual(result.candidate_refs, ())

    def test_endpoint_runner_receives_only_explicitly_requested_queries(self):
        context = planning_context(make_input(arrival_query="上海虹桥站", departure_query="浦东机场"))
        calls = []

        class EndpointService:
            async def run_async(self, endpoint_context):
                request = endpoint_context.planning_input.request
                calls.append((request.arrival_place_query, request.departure_place_query))
                return EndpointVerificationExecution(
                    request_id=endpoint_context.request_id,
                    run_id=endpoint_context.run_id,
                    context_revision=endpoint_context.context_revision,
                    base_revision=endpoint_context.base_revision,
                    city="上海",
                    arrival=EndpointVerification(
                        kind="arrival", user_query="上海虹桥站", status="error", reason="search_error",
                    ),
                    departure=EndpointVerification(
                        kind="departure", user_query=None, status="missing", reason="query_missing",
                    ),
                    fact_store=FactStore(),
                )

        decider = ScriptedDecider([
            decision(context, "delegate", arrival=True),
            decision(context, "plan", missing=("离开地点尚未核实",)),
        ])
        result = asyncio.run(PlanningDispatchService(
            decider=decider,
            decision_context_factory=lambda **values: SimpleNamespace(**values),
            endpoint_service_factory=EndpointService,
        ).run_async(context))

        self.assertEqual(calls, [("上海虹桥站", None)])
        self.assertEqual(result.stop_reason, "plan")
        self.assertEqual(result.endpoint_attempted, ("arrival",))
        self.assertEqual(len(result.endpoint_results), 1)

    def test_master_decider_receives_fixed_request_snapshot(self):
        context = planning_context()
        calls = []

        class PlanLLM:
            def invoke_with_usage(self, messages, **kwargs):
                calls.append(kwargs)
                return json.dumps({
                    "action": "plan",
                    "reason": "可用信息已收集，转入每日计划阶段。",
                    "missing_information": ["天气仍未知"],
                }, ensure_ascii=False), SimpleNamespace(
                    prompt_tokens=12, completion_tokens=8, total_tokens=20,
                )

        result = asyncio.run(PlanningDispatchService(
            decider=MasterDecider(llm=PlanLLM()),
            total_timeout_seconds=1.5,
        ).run_async(context))

        self.assertEqual(result.stop_reason, "plan")
        self.assertEqual(result.action, "plan")
        self.assertEqual(result.request_id, context.request_id)
        self.assertEqual(result.context_revision, context.context_revision)
        self.assertEqual(result.missing_information, ("天气仍未知",))
        self.assertLessEqual(calls[0]["request_timeout"], 1.5)

    def test_master_decider_receives_attempted_weather_and_safe_failure_summary(self):
        context = planning_context(make_input(arrival_query="上海虹桥站"))
        day = context.planning_input.days[0].day_date

        class TwoPassLLM:
            def __init__(self):
                self.calls = []
                self.responses = [
                    json.dumps({
                        "action": "delegate", "reason": "查询首日天气并核实用户填写的到达地点。",
                        "weather_dates": [day.isoformat()], "verify_arrival_place": True,
                    }),
                    json.dumps({"action": "plan", "reason": "天气仍未知，转入计划。", "missing_information": ["天气未知"]}),
                ]

            def invoke_with_usage(self, messages, **kwargs):
                self.calls.append((messages, kwargs))
                return self.responses.pop(0), None

        class FailedWeatherService:
            async def run_async(self, weather_context):
                return unknown_weather_execution(weather_context, (day,))

        class FailedEndpointService:
            async def run_async(self, endpoint_context):
                return EndpointVerificationExecution(
                    request_id=endpoint_context.request_id,
                    run_id=endpoint_context.run_id,
                    context_revision=endpoint_context.context_revision,
                    base_revision=endpoint_context.base_revision,
                    city="上海",
                    arrival=EndpointVerification(
                        kind="arrival", user_query="上海虹桥站", status="error", reason="search_error:network",
                    ),
                    departure=EndpointVerification(
                        kind="departure", user_query=None, status="missing", reason="query_missing",
                    ),
                    fact_store=FactStore(),
                )

        llm = TwoPassLLM()
        result = asyncio.run(PlanningDispatchService(
            decider=MasterDecider(llm=llm),
            weather_service_factory=FailedWeatherService,
            endpoint_service_factory=FailedEndpointService,
        ).run_async(context))

        self.assertEqual(result.stop_reason, "plan")
        self.assertEqual(result.weather_attempted_dates, (day,))
        self.assertEqual(result.endpoint_attempted, ("arrival",))
        second_input = json.loads(llm.calls[1][0][1]["content"])
        history = second_input["backend_query_history"]
        self.assertEqual(history["weather_attempted_dates"], [day.isoformat()])
        summaries = {item["kind"]: item for item in history["safe_result_summaries"]}
        self.assertEqual(summaries["weather"]["status"], "error")
        self.assertEqual(summaries["weather"]["error_category"], "network")
        self.assertEqual(summaries["arrival"]["status"], "error")
        self.assertEqual(
            second_input["backend_query_capabilities"]["verify_arrival_place"]["allowed"], False,
        )
        self.assertEqual(
            second_input["backend_query_capabilities"]["allowed_weather_dates"],
            [item.day_date.isoformat() for item in context.planning_input.days[1:]],
        )
        weather_schema = second_input["decision_json_schema"]["$defs"]["DelegatePayload"]["properties"]["weather_dates"]
        self.assertNotIn(day.isoformat(), weather_schema["items"]["enum"])
        self.assertEqual(
            second_input["decision_json_schema"]["$defs"]["DelegatePayload"]["properties"]["verify_arrival_place"]["const"],
            False,
        )

    def test_dispatch_deadline_cancels_late_master_repair_request(self):
        context = planning_context()
        started = threading.Event()
        finished = threading.Event()

        class SlowMalformedLLM:
            def __init__(self):
                self.calls = 0

            def invoke_with_usage(self, messages, **kwargs):
                self.calls += 1
                started.set()
                threading.Event().wait(0.12)
                finished.set()
                return "not json", None

        llm = SlowMalformedLLM()
        service = PlanningDispatchService(
            decider=MasterDecider(llm=llm),
            total_timeout_seconds=0.04,
        )

        async def run_to_deadline():
            dispatch = asyncio.create_task(service.run_async(context))
            await asyncio.to_thread(started.wait, 1)
            result = await dispatch
            await asyncio.to_thread(finished.wait, 1)
            return result

        result = asyncio.run(run_to_deadline())

        self.assertEqual(result.stop_reason, "deadline")
        self.assertEqual(llm.calls, 1, "超出调度deadline后不得新增JSON格式修复模型调用")

    def test_deadline_keeps_completed_sibling_and_marks_slow_task_missing(self):
        context = planning_context()
        quick = subtask("quick", objective="快速完成")
        slow = subtask("slow", period="afternoon", objective="等待中的查询")
        source, fact = place_fact(fact_id="quick-fact")
        started = asyncio.Event()

        class MixedRunner:
            async def run_async(self, task, child_context):
                if task.task_id == "slow":
                    started.set()
                    await asyncio.Event().wait()
                await asyncio.sleep(0.01)
                return execution(task, child_context, FactStore(sources=(source,), places=(fact,)), ())

        decider = ScriptedDecider([decision(context, "delegate", tasks=(resolved(slow), resolved(quick)))])
        service = PlanningDispatchService(
            decider=decider,
            decision_context_factory=lambda **values: SimpleNamespace(**values),
            runner_factories={"attractions": MixedRunner},
            total_timeout_seconds=0.25,
        )
        result = asyncio.run(service.run_async(context))

        self.assertTrue(started.is_set())
        self.assertEqual(result.stop_reason, "deadline")
        self.assertEqual([item.fact_id for item in result.fact_store.places], ["quick-fact"])
        self.assertIn("quick", {item.task_id for item in result.task_results})
        self.assertEqual(next(item for item in result.task_states if item.task_id == "slow").status, "waiting")
        self.assertTrue(any("技术期限" in item for item in result.missing_information))

    def test_task_local_timeout_does_not_end_dispatch_deadline(self):
        context = planning_context()
        timed_out = subtask("local-timeout", objective="单项超时")
        good = subtask("after-timeout", period="afternoon", objective="继续查询")

        class MixedRunner:
            async def run_async(self, task, child_context):
                if task.task_id == "local-timeout":
                    raise asyncio.TimeoutError
                return execution(task, child_context, FactStore(), ())

        decider = ScriptedDecider([
            decision(context, "delegate", tasks=(resolved(timed_out), resolved(good))),
            decision(context, "plan"),
        ])
        result = asyncio.run(PlanningDispatchService(
            decider=decider,
            decision_context_factory=lambda **values: SimpleNamespace(**values),
            runner_factories={"attractions": MixedRunner},
            total_timeout_seconds=10,
        ).run_async(context))

        self.assertEqual(result.stop_reason, "plan")
        self.assertEqual({item.task_id for item in result.task_results}, {"local-timeout", "after-timeout"})
        self.assertEqual(next(item for item in result.task_results if item.task_id == "local-timeout").stop_reason, "deadline")

    def test_cancellation_propagates_and_cancels_running_runner(self):
        context = planning_context()
        task = subtask("cancelled")
        started = asyncio.Event()
        cancelled = asyncio.Event()

        class WaitingRunner:
            async def run_async(self, task, child_context):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        decider = ScriptedDecider([decision(context, "delegate", tasks=(resolved(task),))])
        service = PlanningDispatchService(
            decider=decider,
            decision_context_factory=lambda **values: SimpleNamespace(**values),
            runner_factories={"attractions": WaitingRunner},
        )

        async def run_and_cancel():
            dispatch = asyncio.create_task(service.run_async(context))
            await asyncio.wait_for(started.wait(), timeout=1)
            dispatch.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await dispatch
            await asyncio.wait_for(cancelled.wait(), timeout=1)

        asyncio.run(run_and_cancel())
        self.assertEqual(len(decider.contexts), 1)
