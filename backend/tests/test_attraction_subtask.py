import asyncio
from dataclasses import replace
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from app.agents.execution import PlanningError
from app.agents.attraction_subtask import (
    AttractionExecutionContext,
    AttractionSubtaskRunner,
)
from app.models.planning_v4 import (
    FactStore,
    HardRequirement,
    PlaceFact,
    RequirementSummary,
    RuleSet,
    Subtask,
    TripRequest,
)
from app.models.planning_v4.input import RequirementSource
from app.models.planning_v4.tasks import TaskScope
from app.models.planning_v4.tasks import TaskDependency
from app.services.planning_input_builder import PlanningInputBuildRequest, build_planning_input
from app.services.mcp_tool_executor import MCPToolExecutor
from app.services.observation_service import ObservationService
from app.services.task_repository import TaskRepository


WINDOWS = (
    {"period": "breakfast", "start_minute": 8 * 60, "end_minute": 9 * 60},
    {"period": "morning", "start_minute": 9 * 60, "end_minute": 12 * 60},
    {"period": "lunch", "start_minute": 12 * 60, "end_minute": 14 * 60},
    {"period": "afternoon", "start_minute": 14 * 60, "end_minute": 17 * 60},
    {"period": "dinner", "start_minute": 17 * 60, "end_minute": 19 * 60},
    {"period": "evening", "start_minute": 19 * 60, "end_minute": 22 * 60},
)
SLOT_ID = "slot-2026-10-10-afternoon"
M1_TEXT = "必须去外滩"


def planning_input(*, with_requirement=True):
    request = TripRequest(
        request_id="f03-request",
        city="上海",
        arrival_at=datetime.fromisoformat("2026-10-10T09:00:00+08:00"),
        departure_at=datetime.fromisoformat("2026-10-12T19:00:00+08:00"),
        timezone="Asia/Shanghai",
        remarks=M1_TEXT if with_requirement else "",
    )
    requirements = RequirementSummary(
        requirements_id="f03-summary",
        request_id=request.request_id,
        hard_requirements=(
            HardRequirement(
                requirement_id="M1",
                kind="must_visit",
                text=M1_TEXT,
                source=RequirementSource(field="remarks", quote=M1_TEXT),
            ),
        ) if with_requirement else (),
    )
    rules = RuleSet(
        rule_version="f03-test-v1",
        windows=WINDOWS,
        full_day_start_minute=8 * 60,
        return_buffer_default_s=3600,
        departure_day_minimum_usable_s=10800,
    )
    return build_planning_input(PlanningInputBuildRequest(
        request=request,
        requirements=requirements,
        rules=rules,
    ))


def subtask(*, requirement_ids=("M1",), scope=None):
    return Subtask(
        task_id="task-attractions-1",
        run_id="run-f03",
        context_revision=1,
        base_revision=1,
        role="attractions",
        objective="核实必去外滩，并为下午找到景点候选",
        scope=scope or TaskScope(
            city="上海",
            day_ids=("day-2026-10-10",),
            slot_ids=(SLOT_ID,),
        ),
        requirement_ids=requirement_ids,
        candidate_target=1,
    )


def place(poi_id="p1", name="外滩", **overrides):
    result = {
        "id": poi_id,
        "name": name,
        "location": "121.4903,31.2417",
        "address": "上海市黄浦区中山东一路",
        "cityname": "上海市",
        "type": "风景名胜;公园广场;城市广场",
        "typecode": "110000",
    }
    result.update(overrides)
    return result


def map_result(places=(), *, status="ok"):
    return {
        "result_status": status,
        "count": str(len(places)),
        "pois": list(places),
        "result_source": {
            "api_version": "v3",
            "queried_at": "2026-10-11T00:00:00+00:00",
        },
    }


class FakeLLM:
    def __init__(self, replies, *, fail_at=None):
        self.replies = list(replies)
        self.fail_at = fail_at
        self.messages = []
        self.calls = 0

    def invoke_with_usage(self, messages, **kwargs):
        self.calls += 1
        self.messages.append(messages)
        if self.fail_at == self.calls:
            raise RuntimeError("private provider response must not leak")
        text, usage = self.replies.pop(0)
        return text, usage


def usage(prompt=10, completion=5, total=15):
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}


def search_action(keywords="外滩"):
    return json.dumps({"action": "search", "keywords": keywords}, ensure_ascii=False)


def finish_action(poi_id="p1", *, slot_ids=(SLOT_ID,), requirement_ids=("M1",)):
    return json.dumps({
        "action": "finish",
        "selections": [{
            "poi_id": poi_id,
            "slot_ids": list(slot_ids),
            "requirement_ids": list(requirement_ids),
            "recommendation_reason": "用户明确指定的景点",
        }],
    }, ensure_ascii=False)


class AttractionSubtaskTests(unittest.TestCase):
    def setUp(self):
        self.tool_calls = []

    def make_context(self, *, with_requirement=True):
        planning = planning_input(with_requirement=with_requirement)
        return AttractionExecutionContext(
            request_id=planning.request.request_id,
            run_id="run-f03",
            context_revision=1,
            base_revision=1,
            planning_input=planning,
        )

    def make_tool(self, responder):
        def invoke(tool_name, arguments):
            self.tool_calls.append((tool_name, dict(arguments)))
            return responder(tool_name, arguments)
        return MCPToolExecutor(invoke)

    def run_subtask(self, llm, responder, *, timeout=2.0, tool_timeout=0.2, sleep=lambda _: None,
                    task=None, context=None):
        executor = self.make_tool(responder)
        try:
            runner = AttractionSubtaskRunner(
                llm=llm,
                tool_executor=executor,
                subtask_timeout_seconds=timeout,
                tool_timeout_seconds=tool_timeout,
                sleep=sleep,
            )
            return runner, runner.run(task or subtask(), context or self.make_context())
        finally:
            executor.close(wait=True)

    def test_search_and_finish_returns_complete_fact_backed_candidate(self):
        llm = FakeLLM([(search_action(), usage()), (finish_action(), usage())])
        runner, result = self.run_subtask(
            llm,
            lambda *_: map_result([place()]),
        )

        task_result = result.task_result
        self.assertEqual(task_result.status, "complete")
        self.assertEqual(task_result.stop_reason, "target_met")
        self.assertEqual(task_result.request_id, "f03-request")
        self.assertEqual(task_result.base_revision, 1)
        self.assertEqual(task_result.candidates[0].poi_id, "p1")
        self.assertEqual(task_result.candidates[0].requirement_ids, ("M1",))
        self.assertEqual(result.usage, {
            "input_tokens": 20, "output_tokens": 10, "total_tokens": 30,
        })
        self.assertEqual(runner.model_calls, 2)
        self.assertEqual(len(result.fact_store.sources), 1)
        self.assertEqual(len(self.tool_calls), 1)
        self.assertEqual(self.tool_calls[0][0], "maps_text_search")
        self.assertEqual(self.tool_calls[0][1], {
            "keywords": "外滩", "city": "上海", "citylimit": "true", "page": "1",
            "offset": "20", "extensions": "base",
        })

    def test_model_prompt_projects_known_fact_values_and_task_scope(self):
        llm = FakeLLM([(search_action(), usage()), (finish_action(), usage())])
        self.run_subtask(llm, lambda *_: map_result([place()]))

        prompt = json.loads(llm.messages[1][1]["content"])
        candidate = prompt["available_candidates"][0]
        self.assertEqual(candidate["poi_id"], "p1")
        self.assertTrue(candidate["fact_id"].startswith("place:"))
        self.assertEqual(candidate["city"], "上海市")
        self.assertEqual(candidate["location"], {
            "lng": 121.4903, "lat": 31.2417, "coordinate_system": "GCJ-02",
        })
        self.assertIsNone(candidate["opening_hours_raw"])
        self.assertEqual(candidate["cost"], {
            "raw": None, "value": None, "unit": "per_person_reference",
        })
        self.assertEqual(len(candidate["source_refs"]), 1)
        self.assertEqual(prompt["subtask"]["slots"][0]["date"], "2026-10-10")
        self.assertEqual(prompt["subtask"]["requirement_ids"][0]["source_quote"], M1_TEXT)

    def test_same_name_different_pois_are_ambiguous(self):
        llm = FakeLLM([(search_action(), usage()), (finish_action(), usage())])
        _, result = self.run_subtask(
            llm,
            lambda *_: map_result([place("p1"), place("p2")]),
        )

        self.assertEqual(result.task_result.status, "partial")
        self.assertEqual(result.task_result.stop_reason, "ambiguous")
        requirement = next(item for item in result.task_result.coverage if item.target_id == "M1")
        self.assertEqual(requirement.status, "unknown")
        self.assertEqual(result.task_result.candidates[0].requirement_ids, ())

    def test_model_cannot_select_a_forged_or_out_of_city_poi(self):
        forged_llm = FakeLLM([
            (search_action(), usage()),
            (finish_action("not-seen"), usage()),
        ])
        _, forged = self.run_subtask(forged_llm, lambda *_: map_result([place()]))
        self.assertEqual(forged.task_result.stop_reason, "invalid_output")
        self.assertEqual(forged.task_result.status, "partial")
        self.assertEqual(forged.task_result.candidates[0].slot_ids, ())

        out_of_city_llm = FakeLLM([(search_action(), usage())])
        _, out_of_city = self.run_subtask(
            out_of_city_llm,
            lambda *_: map_result([place("p-shenzhen", "外滩", cityname="深圳市")]),
        )
        self.assertEqual(out_of_city.task_result.stop_reason, "no_progress")
        self.assertEqual(out_of_city.task_result.candidates, ())

    def test_reused_candidate_refs_obey_current_task_constraints(self):
        llm = FakeLLM([(search_action(), usage()), (finish_action(), usage())])
        _, first = self.run_subtask(llm, lambda *_: map_result([place()]))
        candidate = first.task_result.candidates[0]
        context = replace(
            self.make_context(),
            fact_store=first.fact_store,
            candidate_refs=(candidate,),
        )

        duplicate_poi = candidate.model_copy(update={"candidate_id": "candidate_duplicate"})
        duplicate_id = candidate.model_copy(update={"poi_id": "p2", "place_fact_id": "missing-fact"})
        excluded_task = subtask().model_copy(update={"excluded_poi_ids": (candidate.poi_id,)})
        duplicate_poi_context = replace(context, candidate_refs=(candidate, duplicate_poi))
        duplicate_id_context = replace(context, candidate_refs=(candidate, duplicate_id))

        planning = context.planning_input
        avoid_quote = "不要去禁地公园"
        request = planning.request.model_copy(update={
            "remarks": f"{M1_TEXT}；{avoid_quote}",
        })
        avoid = HardRequirement(
            requirement_id="A1",
            kind="must_avoid",
            text=avoid_quote,
            source=RequirementSource(field="remarks", quote=avoid_quote),
        )
        requirements = planning.requirements.model_copy(update={
            "hard_requirements": (*planning.requirements.hard_requirements, avoid),
        })
        restricted_planning = planning.model_copy(update={
            "request": request,
            "requirements": requirements,
        })
        renamed_places = tuple(
            item.model_copy(update={"name": "禁地公园"})
            for item in context.fact_store.places
        )
        restricted_context = replace(
            context,
            planning_input=restricted_planning,
            fact_store=context.fact_store.model_copy(update={"places": renamed_places}),
        )

        cases = (
            ("excluded", excluded_task, context),
            ("duplicate_poi", subtask(), duplicate_poi_context),
            ("duplicate_candidate_id", subtask(), duplicate_id_context),
            ("must_avoid", subtask(), restricted_context),
        )
        for name, task, current_context in cases:
            with self.subTest(name=name):
                model = FakeLLM([])
                executor = self.make_tool(lambda *_: self.fail("invalid reused candidate must fail preflight"))
                try:
                    runner = AttractionSubtaskRunner(llm=model, tool_executor=executor)
                    with self.assertRaises(PlanningError) as raised:
                        runner.run(task, current_context)
                finally:
                    executor.close(wait=True)
                self.assertEqual(raised.exception.code, "CANDIDATE_REF_INVALID")
                self.assertEqual(model.calls, 0)

    def test_empty_search_is_no_match_not_provider_error(self):
        llm = FakeLLM([(search_action(), usage())])
        _, result = self.run_subtask(llm, lambda *_: map_result(status="empty"))

        self.assertEqual(result.task_result.status, "partial")
        self.assertEqual(result.task_result.stop_reason, "no_match")
        self.assertEqual(result.task_result.candidates, ())
        self.assertEqual(result.tool_calls[0].status, "empty")

    def test_auth_error_is_not_retried_but_retryable_error_gets_one_retry(self):
        for category, expected_calls, expected_status in (
            ("auth", 1, "failed"),
            ("timeout", 2, "complete"),
        ):
            with self.subTest(category=category):
                self.tool_calls = []
                llm = FakeLLM([(search_action(), usage()), (finish_action(), usage())])
                count = 0

                def respond(*_):
                    nonlocal count
                    count += 1
                    if count == 1:
                        return {
                            "result_status": "error",
                            "result_error": {"category": category, "provider_code": "fixture"},
                            "result_source": {"api_version": "v3"},
                        }
                    return map_result([place()])

                _, result = self.run_subtask(llm, respond)
                self.assertEqual(len(self.tool_calls), expected_calls)
                self.assertEqual(result.task_result.status, expected_status)

    def test_detail_for_a_different_poi_is_rejected(self):
        llm = FakeLLM([(search_action(), usage())])

        def respond(tool_name, _arguments):
            if tool_name == "maps_text_search":
                return map_result([place("p1", location=None)])
            return map_result([place("p2", "另一地点")])

        _, result = self.run_subtask(llm, respond)

        self.assertEqual([name for name, _ in self.tool_calls], ["maps_text_search", "maps_search_detail"])
        self.assertEqual(result.task_result.status, "failed")
        self.assertEqual(result.task_result.stop_reason, "tool_error")
        self.assertEqual({item.poi_id for item in result.fact_store.places}, {"p1"})

    def test_detail_enriches_only_the_searched_poi_and_keeps_both_sources(self):
        llm = FakeLLM([(search_action(), usage()), (finish_action(), usage())])

        def respond(tool_name, _arguments):
            if tool_name == "maps_text_search":
                return map_result([place("p1", location=None)])
            return map_result([place("p1", cityname=None, type=None, typecode=None)])

        _, result = self.run_subtask(llm, respond)

        self.assertEqual(result.task_result.status, "complete")
        candidate = result.task_result.candidates[0]
        combined = next(item for item in result.fact_store.places if item.fact_id == candidate.place_fact_id)
        self.assertTrue(combined.fact_id.startswith("derived:attraction:"))
        self.assertEqual(combined.poi_id, "p1")
        self.assertIsNotNone(combined.location)
        self.assertEqual(len(combined.source_refs), 2)
        self.assertEqual(len(result.fact_store.sources), 2)

    def test_new_call_id_with_identical_poi_fields_is_no_progress(self):
        llm = FakeLLM([
            (search_action("外滩"), usage()),
            (search_action("外滩景区"), usage()),
        ])
        _, result = self.run_subtask(llm, lambda *_: map_result([place()]))

        self.assertEqual(len(self.tool_calls), 2)
        self.assertEqual(len(result.fact_store.sources), 2)
        self.assertEqual(result.task_result.stop_reason, "no_progress")

    def test_format_repair_budget_is_shared_across_decision_rounds(self):
        invalid = '{"action":"search","keywords":"外滩","request_id":"forged"}'
        llm = FakeLLM([
            (invalid, usage()),
            (search_action(), usage()),
            (invalid, usage()),
        ])
        runner, result = self.run_subtask(llm, lambda *_: map_result([place()]))

        self.assertEqual(result.task_result.stop_reason, "invalid_output")
        self.assertEqual(runner.format_repairs, 1)
        self.assertEqual(runner.model_calls, 3)

    def test_restaurant_result_is_not_an_attraction_or_progress(self):
        llm = FakeLLM([(search_action("餐馆"), usage())])
        _, result = self.run_subtask(
            llm,
            lambda *_: map_result([place("r1", "某餐馆", type="餐饮服务;中餐厅", typecode="050100")]),
        )

        self.assertEqual(result.task_result.status, "partial")
        self.assertEqual(result.task_result.stop_reason, "no_progress")
        self.assertEqual(result.task_result.candidates, ())
        self.assertEqual(len(result.fact_store.places), 1)

    def test_model_error_after_search_preserves_unassigned_candidate(self):
        llm = FakeLLM([(search_action(), usage())], fail_at=2)
        _, result = self.run_subtask(llm, lambda *_: map_result([place()]))

        self.assertEqual(result.task_result.status, "partial")
        self.assertEqual(result.task_result.stop_reason, "model_error")
        self.assertEqual(len(result.task_result.candidates), 1)
        self.assertEqual(result.task_result.candidates[0].slot_ids, ())
        self.assertEqual(result.task_result.candidates[0].requirement_ids, ())

    def test_invalid_model_output_gets_only_one_format_repair(self):
        invalid = '{"action":"search","keywords":"外滩","request_id":"forged"}'
        llm = FakeLLM([(invalid, usage()), (invalid, usage())])
        runner, result = self.run_subtask(llm, lambda *_: self.fail("no map call expected"))

        self.assertEqual(result.task_result.status, "failed")
        self.assertEqual(result.task_result.stop_reason, "invalid_output")
        self.assertEqual(runner.model_calls, 2)
        self.assertEqual(runner.format_repairs, 1)
        self.assertEqual(self.tool_calls, [])

    def test_missing_usage_makes_aggregate_unknown(self):
        llm = FakeLLM([(search_action(), usage()), (finish_action(), None)])
        _, result = self.run_subtask(llm, lambda *_: map_result([place()]))

        self.assertEqual(result.task_result.status, "complete")
        self.assertEqual(result.usage, {
            "input_tokens": None, "output_tokens": None, "total_tokens": None,
        })

    def test_observation_spans_store_safe_summary_and_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = TaskRepository(Path(directory) / "f03-observation.sqlite3")
            repository.initialize()
            repository.create("observation-task", {})
            repository.start("observation-task")
            llm = FakeLLM([(search_action(), usage()), (finish_action(), usage())])
            executor = self.make_tool(lambda *_: map_result([place()]))
            runner = AttractionSubtaskRunner(llm=llm, tool_executor=executor)
            try:
                with ObservationService(repository).task("observation-task"):
                    result = runner.run(subtask(), self.make_context())
            finally:
                executor.close(wait=True)

            spans = repository.spans("observation-task")
            root = next(item for item in spans if item["name"] == "agent.attraction_subtask")
            root_detail = repository.span("observation-task", root["span_id"])
            self.assertEqual(root["status"], "succeeded")
            self.assertEqual(root_detail["output_data"]["status"], "complete")
            self.assertEqual(root_detail["output_data"]["stop_reason"], "target_met")
            self.assertNotIn("recommendation_reason", json.dumps(root_detail["output_data"]))

            llm_spans = [item for item in spans if item["name"] == "llm.attraction_decision"]
            self.assertEqual(len(llm_spans), 2)
            self.assertTrue(all(item["status"] == "succeeded" for item in llm_spans))
            self.assertEqual(
                [(item["input_tokens"], item["output_tokens"], item["total_tokens"])
                 for item in llm_spans],
                [(10, 5, 15), (10, 5, 15)],
            )
            tool_span = next(item for item in spans if item["name"] == "tool.mcp_call")
            tool_detail = repository.span("observation-task", tool_span["span_id"])
            self.assertEqual(tool_detail["output_data"]["status"], "ok")
            self.assertEqual(result.task_result.status, "complete")

    def test_preflight_rejects_unready_dependency_before_model_or_tool(self):
        llm = FakeLLM([])
        context = self.make_context()
        task = subtask()
        task = task.model_copy(update={
            "dependencies": (TaskDependency(kind="task", reference_id="task-weather"),),
        })
        executor = self.make_tool(lambda *_: self.fail("no tool call expected"))
        try:
            runner = AttractionSubtaskRunner(llm=llm, tool_executor=executor)
            with self.assertRaises(PlanningError) as raised:
                runner.run(task, context)
        finally:
            executor.close(wait=True)

        self.assertEqual(raised.exception.code, "DEPENDENCY_UNMET")
        self.assertEqual(llm.calls, 0)
        self.assertEqual(self.tool_calls, [])

    def test_preflight_rejects_stale_versions_invalid_slot_and_requirement(self):
        base_context = self.make_context()
        base_task = subtask()
        cases = (
            ("request", base_task, replace(base_context, request_id="other-request"), "REQUEST_MISMATCH"),
            ("run", base_task, replace(base_context, run_id="other-run"), "RUN_MISMATCH"),
            ("context_revision", base_task,
             replace(base_context, context_revision=2), "CONTEXT_REVISION_MISMATCH"),
            ("base_revision", base_task.model_copy(update={"base_revision": 2}),
             base_context, "BASE_REVISION_MISMATCH"),
            ("slot", base_task.model_copy(update={
                "scope": TaskScope(city="上海", slot_ids=("slot-2026-10-10-breakfast",)),
            }), base_context, "SCOPE_SLOT_SKIPPED"),
            ("requirement", base_task.model_copy(update={"requirement_ids": ("M999",)}),
             base_context, "SCOPE_REQUIREMENT_INVALID"),
        )
        for name, task, context, expected_code in cases:
            with self.subTest(name=name):
                llm = FakeLLM([])
                executor = self.make_tool(lambda *_: self.fail("preflight must stop before MCP"))
                try:
                    runner = AttractionSubtaskRunner(llm=llm, tool_executor=executor)
                    with self.assertRaises(PlanningError) as raised:
                        runner.run(task, context)
                finally:
                    executor.close(wait=True)
                self.assertEqual(raised.exception.code, expected_code)
                self.assertEqual(llm.calls, 0)
                self.assertEqual(self.tool_calls, [])

    def test_preflight_rejects_unclosed_fact_source_and_mismatched_requirement_source(self):
        valid_context = self.make_context()
        unclosed = PlaceFact(
            fact_id="place-unclosed",
            poi_id="p1",
            name="外滩",
            source_refs=("missing-tool-call",),
        )
        bad_facts_context = replace(valid_context, fact_store=FactStore(places=(unclosed,)))

        request = valid_context.planning_input.request.model_copy(update={"remarks": "另一个备注"})
        bad_planning = valid_context.planning_input.model_copy(update={"request": request})
        bad_requirement_context = replace(valid_context, planning_input=bad_planning)
        for context, expected_code in (
            (bad_facts_context, "FACT_SOURCE_UNCLOSED"),
            (bad_requirement_context, "REQUIREMENT_SOURCE_MISMATCH"),
        ):
            llm = FakeLLM([])
            executor = self.make_tool(lambda *_: self.fail("preflight must stop before MCP"))
            try:
                runner = AttractionSubtaskRunner(llm=llm, tool_executor=executor)
                with self.assertRaises(PlanningError) as raised:
                    runner.run(subtask(), context)
            finally:
                executor.close(wait=True)
            self.assertEqual(raised.exception.code, expected_code)
            self.assertEqual(llm.calls, 0)
            self.assertEqual(self.tool_calls, [])

    def test_deadline_stops_a_model_that_ignores_request_timeout(self):
        started = threading.Event()
        release = threading.Event()

        class SlowLLM:
            def invoke_with_usage(self, messages, **kwargs):
                started.set()
                release.wait(1)
                return search_action(), usage()

        executor = self.make_tool(lambda *_: self.fail("model result arrived too late"))
        runner = AttractionSubtaskRunner(
            llm=SlowLLM(), tool_executor=executor, subtask_timeout_seconds=0.08,
        )
        try:
            before = time.monotonic()
            result = runner.run(subtask(), self.make_context())
            elapsed = time.monotonic() - before
            self.assertTrue(started.is_set())
            self.assertLess(elapsed, 0.5)
            self.assertEqual(result.task_result.stop_reason, "deadline")
            self.assertEqual(self.tool_calls, [])
        finally:
            release.set()
            executor.close(wait=True)

    def test_async_runner_cancellation_removes_its_queued_mcp_call(self):
        lock = threading.Lock()
        actual_ids = []
        four_started = threading.Event()
        release_workers = threading.Event()
        model_called = threading.Event()
        runner_queued = threading.Event()

        def blocking_tool(_name, arguments):
            with lock:
                actual_ids.append(arguments.get("id", "runner-search"))
                if len(actual_ids) == 4:
                    four_started.set()
            release_workers.wait(1)
            return {"result_status": "ok"}

        class SearchingLLM:
            def invoke_with_usage(self, _messages, **_kwargs):
                model_called.set()
                return search_action(), usage()

        class SignalingExecutor(MCPToolExecutor):
            def _submit(self, tool_name, arguments, timeout_seconds, cancel_event=None):
                submitted = super()._submit(tool_name, arguments, timeout_seconds, cancel_event)
                if arguments.get("keywords") == "外滩":
                    runner_queued.set()
                return submitted

        executor = SignalingExecutor(blocking_tool)
        callers = ThreadPoolExecutor(max_workers=4)
        active = [
            callers.submit(executor.run, "maps_text_search", {"id": str(index)}, timeout_seconds=2)
            for index in range(4)
        ]
        try:
            self.assertTrue(four_started.wait(1))
            runner = AttractionSubtaskRunner(llm=SearchingLLM(), tool_executor=executor)

            async def cancel_runner():
                task = asyncio.create_task(runner.run_async(subtask(), self.make_context()))
                self.assertTrue(await asyncio.to_thread(model_called.wait, 1))
                self.assertTrue(await asyncio.to_thread(runner_queued.wait, 1))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

            asyncio.run(cancel_runner())
            release_workers.set()
            for future in active:
                future.result(timeout=1)
            self.assertTrue(runner._run_lock.acquire(timeout=1))
            self.assertEqual(len(actual_ids), 4)
            self.assertNotIn("runner-search", actual_ids)
            runner._run_lock.release()
        finally:
            release_workers.set()
            callers.shutdown(wait=True)
            executor.close(wait=True)


if __name__ == "__main__":
    unittest.main()
