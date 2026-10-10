import json
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pydantic import ValidationError

from app.agents.requirement_interpreter import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_TIMEOUT_SECONDS,
    RequirementInterpreter,
    RequirementDraft,
)
from app.agents.execution import PlanningError
from app.models.planning_v4.input import RequirementSummary, TripBudget, TripRequest
from app.services.llm_service import UsageAwareLLM
from app.services.observation_service import ObservationService
from app.services.task_repository import TaskRepository
from scripts.run_requirement_interpreter import shanghai_request


def sample_summary(*, city_field=None, source_quote="必须去外滩"):
    result = {
        "core_goals": ["上海代表性景点", "城市漫步与美食"],
        "hard_requirements": [{
            "requirement_id": "M1",
            "kind": "must_visit",
            "text": "外滩",
            "source": {"field": "remarks", "quote": source_quote},
        }],
        "preferences": ["公共交通结合步行", "节奏适中", "尽量少折返", "顺路安排午餐和晚餐"],
        "unknowns": ["arrival_place", "departure_place", "lodging", "budget"],
        "assumptions": [],
    }
    if city_field is not None:
        result["city"] = city_field
    return result


class FakeLLM:
    model = "offline-fixture"

    def __init__(self, responses, usage=None):
        self.responses = list(responses)
        self.usage = usage or {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130}
        self.calls = []

    def invoke_with_usage(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response, self.usage


class RequirementInterpreterTests(unittest.TestCase):
    def test_design_page_request_and_summary_preserve_original_input(self):
        request = shanghai_request()
        original = request.model_dump(mode="json")
        llm = FakeLLM([json.dumps(sample_summary(), ensure_ascii=False)])

        interpreter = RequirementInterpreter(llm=llm)
        summary = interpreter.interpret(request)

        self.assertEqual(request.model_dump(mode="json"), original)
        self.assertEqual(request.city, "上海")
        self.assertEqual(request.arrival_at.isoformat(), "2026-10-10T09:00:00+08:00")
        self.assertEqual(request.departure_at.isoformat(), "2026-10-12T19:00:00+08:00")
        self.assertIsNone(request.budget.amount)
        self.assertEqual(request.budget.scope, "whole_trip")
        self.assertEqual(request.budget.party_basis, "unspecified")
        self.assertEqual(request.remarks, "想看上海代表性景点，必须去外滩。以公共交通结合步行为主，节奏适中，尽量少折返；安排顺路午餐和晚餐。")
        self.assertEqual(summary.request_id, request.request_id)
        self.assertTrue(summary.requirements_id.startswith("requirements_"))
        self.assertEqual(summary.hard_requirements[0].source.field, "remarks")
        self.assertEqual(summary.hard_requirements[0].source.quote, "必须去外滩")
        self.assertFalse(hasattr(summary, "city"))
        self.assertEqual(interpreter.model_calls, 1)
        self.assertEqual(interpreter.last_usage, {"input_tokens": 100, "output_tokens": 30, "total_tokens": 130})

        payload = json.loads(llm.calls[0]["messages"][1]["content"])
        self.assertEqual(payload["trip_request"], original)
        self.assertIn("不表示禁止骑行", llm.calls[0]["messages"][0]["content"])

    def test_nonempty_whole_trip_budget_and_unspecified_party_basis_stay_in_request(self):
        request = shanghai_request().model_copy(update={
            "budget": TripBudget(amount=Decimal("3200"), party_basis="unspecified", party_size=None),
        })
        llm = FakeLLM([json.dumps(sample_summary(), ensure_ascii=False)])

        summary = RequirementInterpreter(llm=llm).interpret(request)

        sent = json.loads(llm.calls[0]["messages"][1]["content"])["trip_request"]
        self.assertEqual(sent["budget"]["amount"], "3200")
        self.assertEqual(sent["budget"]["scope"], "whole_trip")
        self.assertEqual(sent["budget"]["party_basis"], "unspecified")
        self.assertIsNone(sent["budget"]["party_size"])
        self.assertEqual(request.budget.amount, Decimal("3200"))
        self.assertIsNone(request.budget.party_size)
        self.assertFalse(hasattr(summary, "budget"))

    def test_hard_requirement_source_must_match_original_field_and_semantic_errors_are_not_repaired(self):
        llm = FakeLLM([json.dumps(sample_summary(source_quote="外滩一定要去"), ensure_ascii=False)])
        with self.assertRaises(PlanningError) as raised:
            RequirementInterpreter(llm=llm).interpret(shanghai_request())
        self.assertEqual(raised.exception.code, "REQUIREMENT_SOURCE_INVALID")
        self.assertEqual(raised.exception.step, "requirement_source_validation")
        self.assertEqual(len(llm.calls), 1)

    def test_explicit_positive_and_negative_intentions_are_hard_requirements_but_preferences_stay_soft(self):
        request = shanghai_request().model_copy(update={
            "remarks": "必须去外滩，但不要去动物园。以公共交通结合步行为主，尽量少折返。",
        })
        response = {
            "core_goals": ["城市漫步"],
            "hard_requirements": [
                {"requirement_id": "M1", "kind": "must_visit", "text": "去外滩",
                 "source": {"field": "remarks", "quote": "必须去外滩"}},
                {"requirement_id": "M2", "kind": "must_avoid", "text": "去动物园",
                 "source": {"field": "remarks", "quote": "不要去动物园"}},
            ],
            "preferences": ["公共交通结合步行", "尽量少折返"],
            "unknowns": [],
            "assumptions": [],
        }
        summary = RequirementInterpreter(
            llm=FakeLLM([json.dumps(response, ensure_ascii=False)]),
        ).interpret(request)

        self.assertEqual([item.kind for item in summary.hard_requirements], ["must_visit", "must_avoid"])
        self.assertEqual([item.source.quote for item in summary.hard_requirements], ["必须去外滩", "不要去动物园"])
        self.assertEqual(summary.preferences, ("公共交通结合步行", "尽量少折返"))
        self.assertNotIn("公共交通", [item.text for item in summary.hard_requirements])
        self.assertNotIn("折返", [item.text for item in summary.hard_requirements])

    def test_structural_error_gets_only_one_targeted_repair(self):
        malformed_first = json.dumps(sample_summary(city_field="北京"), ensure_ascii=False)
        corrected_second = json.dumps(sample_summary(), ensure_ascii=False)
        llm = FakeLLM([malformed_first, corrected_second])
        interpreter = RequirementInterpreter(llm=llm, timeout_seconds=45)

        summary = interpreter.interpret(shanghai_request())

        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(summary.request_id, "DEMO_REQ")
        self.assertFalse(hasattr(summary, "city"))
        self.assertTrue(all(call["kwargs"]["request_timeout"] == 45 for call in llm.calls))
        self.assertTrue(all(call["kwargs"]["max_retries"] == 0 for call in llm.calls))
        self.assertEqual(interpreter.model_calls, 2)
        self.assertEqual(interpreter.last_usage, {"input_tokens": 200, "output_tokens": 60, "total_tokens": 260})
        repair_user_message = llm.calls[1]["messages"][-1]["content"]
        self.assertIn("只修复格式/结构", repair_user_message)

    def test_second_structural_error_stops_without_third_model_call(self):
        llm = FakeLLM(["not-json", "still-not-json", json.dumps(sample_summary(), ensure_ascii=False)])
        interpreter = RequirementInterpreter(llm=llm)
        with self.assertRaises(PlanningError) as raised:
            interpreter.interpret(shanghai_request())
        self.assertEqual(raised.exception.code, "REQUIREMENT_OUTPUT_INVALID")
        self.assertEqual(raised.exception.step, "requirement_output_validation")
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(interpreter.last_usage, {"input_tokens": 200, "output_tokens": 60, "total_tokens": 260})

    def test_empty_object_and_damaged_fence_are_repaired_once(self):
        for malformed in ("{}", "```", "```json\n{}"):
            with self.subTest(malformed=malformed):
                llm = FakeLLM([malformed, json.dumps(sample_summary(), ensure_ascii=False)])
                result = RequirementInterpreter(llm=llm).interpret(shanghai_request())
                self.assertEqual(result.request_id, "DEMO_REQ")
                self.assertEqual(len(llm.calls), 2)

    def test_failed_call_without_usage_marks_attempt_usage_unknown(self):
        llm = FakeLLM(["not-json", TimeoutError("SENSITIVE_TIMEOUT_BODY")])
        interpreter = RequirementInterpreter(llm=llm)
        with self.assertRaises(PlanningError):
            interpreter.interpret(shanghai_request())
        self.assertEqual(interpreter.model_calls, 2)
        self.assertIsNone(interpreter.last_usage)

    def test_timeout_and_authentication_errors_are_not_repaired_or_logged(self):
        for failure, expected_code in (
            (TimeoutError("SENSITIVE_TIMEOUT_BODY"), "MODEL_TIMEOUT"),
            (type("AuthenticationError", (RuntimeError,), {})("SENSITIVE_AUTH_BODY"), "MODEL_AUTHENTICATION_FAILED"),
        ):
            with self.subTest(code=expected_code):
                llm = FakeLLM([failure, json.dumps(sample_summary(), ensure_ascii=False)])
                with self.assertRaises(PlanningError) as raised:
                    RequirementInterpreter(llm=llm).interpret(shanghai_request())
                self.assertEqual(raised.exception.code, expected_code)
                self.assertEqual(raised.exception.step, "requirement_model_call")
                self.assertEqual(len(llm.calls), 1)
                self.assertNotIn("SENSITIVE_", str(raised.exception))

    def test_model_cannot_add_or_change_user_fields(self):
        llm = FakeLLM([
            json.dumps(sample_summary(city_field="北京"), ensure_ascii=False),
            json.dumps(sample_summary(), ensure_ascii=False),
        ])
        request = shanghai_request()
        result = RequirementInterpreter(llm=llm).interpret(request)
        self.assertEqual(request.city, "上海")
        self.assertEqual(result.request_id, "DEMO_REQ")
        self.assertFalse(hasattr(result, "city"))

    def test_output_schema_rejects_structured_request_fields(self):
        with self.assertRaises(ValidationError):
            RequirementDraft.model_validate({**sample_summary(), "arrival_at": "2030-01-01T00:00:00+08:00"})

    def test_caller_managed_observation_records_summary_and_usage_but_not_raw_response(self):
        response = json.dumps(sample_summary(), ensure_ascii=False, indent=2)
        llm = FakeLLM([response])
        with tempfile.TemporaryDirectory() as directory:
            repo = TaskRepository(Path(directory) / "offline.sqlite3")
            repo.initialize()
            repo.create("offline-task", {})
            with ObservationService(repo).task("offline-task"):
                summary = RequirementInterpreter(llm=llm).interpret(shanghai_request())

            rows = repo.spans("offline-task")
            self.assertTrue(rows)
            stored = [repo.span("offline-task", row["span_id"]) for row in rows]
            outputs = [json.dumps(item["output_data"], ensure_ascii=False) for item in stored]
            self.assertTrue(any("requirements_" in output for output in outputs if output))
            self.assertTrue(all(response not in output for output in outputs if output))
            usage_row = next(item for item in stored if item["name"] == "llm.requirement_interpretation")
            self.assertEqual((usage_row["input_tokens"], usage_row["output_tokens"], usage_row["total_tokens"]), (100, 30, 130))
            self.assertEqual(summary.request_id, "DEMO_REQ")

    def test_caller_managed_observation_never_records_raw_model_error_text(self):
        llm = FakeLLM([TimeoutError("SENSITIVE_TIMEOUT_BODY")])
        with tempfile.TemporaryDirectory() as directory:
            repo = TaskRepository(Path(directory) / "offline-error.sqlite3")
            repo.initialize()
            repo.create("offline-error-task", {})
            with ObservationService(repo).task("offline-error-task"):
                with self.assertRaises(PlanningError):
                    RequirementInterpreter(llm=llm).interpret(shanghai_request())

            row = next(item for item in repo.spans("offline-error-task") if item["name"] == "llm.requirement_interpretation")
            detail = repo.span("offline-error-task", row["span_id"])
            self.assertEqual(detail["error"]["code"], "MODEL_TIMEOUT")
            self.assertNotIn("SENSITIVE_TIMEOUT_BODY", json.dumps(detail, ensure_ascii=False))

    def test_timeout_configuration_is_bounded_and_default_is_120_seconds(self):
        self.assertEqual(DEFAULT_TIMEOUT_SECONDS, 120)
        self.assertEqual(MAX_TIMEOUT_SECONDS, 120)
        for invalid in (0, -1, 121, float("inf"), True):
            with self.subTest(timeout=invalid), self.assertRaises(ValueError):
                RequirementInterpreter(llm=FakeLLM([]), timeout_seconds=invalid)

    def test_usage_state_resets_before_model_initialization(self):
        interpreter = RequirementInterpreter()
        interpreter.last_usage = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
        with patch("app.agents.requirement_interpreter.get_llm", side_effect=RuntimeError("SENSITIVE_INIT_TEXT")):
            with self.assertRaises(PlanningError) as raised:
                interpreter.interpret(shanghai_request())
        self.assertEqual(raised.exception.code, "MODEL_INITIALIZATION_FAILED")
        self.assertIsNone(interpreter.last_usage)
        self.assertEqual(interpreter.model_calls, 0)


class RequirementCliTests(unittest.TestCase):
    def test_cli_stdout_is_only_json_even_when_llm_initialization_prints(self):
        from scripts import run_requirement_interpreter

        summary = RequirementSummary(requirements_id="requirements_test", request_id="DEMO_REQ")

        class NoisyInterpreter:
            def __init__(self, **kwargs):
                self.last_usage = {"input_tokens": 4, "output_tokens": 3, "total_tokens": 7}
                self.model_calls = 1

            def interpret(self, request):
                print("LLM initialization banner")
                return summary

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(run_requirement_interpreter, "RequirementInterpreter", NoisyInterpreter):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = run_requirement_interpreter.main([])

        self.assertEqual(result, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["requirements"]["request_id"], "DEMO_REQ")
        self.assertEqual(payload["usage"]["total_tokens"], 7)
        self.assertGreaterEqual(payload["elapsed_seconds"], 0)
        self.assertNotIn("LLM initialization banner", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")


class UsageAwareTimeoutTests(unittest.TestCase):
    def test_request_timeout_and_zero_retries_are_applied_at_http_client_layer(self):
        create = Mock(return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="answer"))],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2, total_tokens=5),
        ))
        request_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with_options = Mock(return_value=request_client)
        llm = UsageAwareLLM.__new__(UsageAwareLLM)
        llm.model, llm.temperature, llm.max_tokens = "fixture", 0.7, None
        llm._client = SimpleNamespace(with_options=with_options)

        text, usage = llm.invoke_with_usage(
            [{"role": "user", "content": "hello"}],
            request_timeout=120,
            max_retries=0,
            max_tokens=50,
        )

        self.assertEqual(text, "answer")
        self.assertEqual(usage.total_tokens, 5)
        with_options.assert_called_once_with(timeout=120, max_retries=0)
        create.assert_called_once()
        self.assertEqual(create.call_args.kwargs["max_tokens"], 50)
        self.assertNotIn("request_timeout", create.call_args.kwargs)
        self.assertNotIn("max_retries", create.call_args.kwargs)

    def test_existing_call_path_uses_original_client_without_options(self):
        create = Mock(return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="answer"))],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2, total_tokens=5),
        ))
        llm = UsageAwareLLM.__new__(UsageAwareLLM)
        llm.model, llm.temperature, llm.max_tokens = "fixture", 0.7, None
        llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

        text, usage = llm.invoke_with_usage([{"role": "user", "content": "hello"}], max_tokens=50)

        self.assertEqual(text, "answer")
        self.assertEqual(usage.total_tokens, 5)
        create.assert_called_once()
        self.assertEqual(create.call_args.kwargs["max_tokens"], 50)


if __name__ == "__main__":
    unittest.main()
