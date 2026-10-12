"""真实观测仓库上的离线回归：错误细节、脱敏、结束原因和统计。"""
import ast
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from app.agents.execution import PlanningError
from app.agents.master_decider import MasterDecider
from app.agents.requirement_interpreter import RequirementInterpreter
from app.services.llm_service import UsageAwareLLM
from app.services.observation_service import ObservationService, span
from app.services.task_repository import TaskRepository
from app.services.task_diagnostics import build_task_report, render_task_report
from app.services.model_diagnostics import provider_error_diagnostics
from test_requirement_interpreter import sample_summary, shanghai_request
from test_weather_query_service import make_context, success_payload, forecast
from app.services.weather_query_service import WeatherQueryService
from app.services.amap_contract_adapter import AmapContractAdapter
from app.services.mcp_tool_executor import MCPToolExecutor


class ModelDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "tasks.sqlite3"
        self.repo = TaskRepository(self.path)
        self.repo.initialize()
        self.repo.create("fixture", {})
        self.repo.start("fixture")
        self.observer = ObservationService(self.repo)

    def rows(self):
        with sqlite3.connect(self.path) as db:
            return [dict(zip(("id", "parent", "name", "status", "output"), row))
                    for row in db.execute("select span_id,parent_span_id,name,status,output_data from spans")]

    def llm(self, *, content=None, reason="stop", error=None):
        model = UsageAwareLLM.__new__(UsageAwareLLM)
        model.model, model.temperature = "fixture", 0
        client = Mock()
        client.with_options.return_value = client
        client.chat.completions.create = Mock(
            side_effect=error, return_value=SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=reason)],
                usage=SimpleNamespace(prompt_tokens=5, completion_tokens=7, total_tokens=12,
                                      completion_tokens_details=SimpleNamespace(reasoning_tokens=3))))
        model._client = client
        return model

    def test_requirement_keeps_completion_metadata_when_business_output_overwrites_it(self):
        model = self.llm(content=json.dumps(sample_summary()))
        with self.observer.task("fixture"):
            RequirementInterpreter(llm=model).interpret(shanghai_request())
        row = next(r for r in self.rows() if r["name"] == "llm.requirement_interpretation")
        output = json.loads(row["output"])
        self.assertEqual(output["status"], "valid")
        self.assertEqual(output["model_call"]["finish_reason"], "stop")
        self.assertEqual(output["model_call"]["usage_counts"]["reasoning"], 3)
        self.assertNotIn("必须去外滩", row["output"])

    def test_provider_error_survives_caller_wrapping_without_raw_payload(self):
        Error = type("APIStatusError", (Exception,), {})
        original = Error("private-key private-response https://bad/?key=private-key")
        original.status_code = 502
        original.body = {"error": {"code": "server_error", "message": "private-response"}}
        original.request_id = "req-12345678"
        model = self.llm(error=original)
        with self.observer.task("fixture"):
            with self.assertRaises(PlanningError):
                RequirementInterpreter(llm=model).interpret(shanghai_request())
        row = next(r for r in self.rows() if r["name"] == "llm.requirement_interpretation")
        meta = json.loads(row["output"])["model_call"]
        self.assertEqual(meta["http_status"], 502)
        self.assertEqual(meta["provider_code"], "server_error")
        self.assertEqual(meta["request_id"], "req-12345678")
        self.assertEqual(meta["exception_chain"], ["APIStatusError"])
        self.assertNotIn("private-", json.dumps(self.rows()))

    def test_unknown_codes_ids_and_cyclic_exception_chain_are_safe(self):
        error = RuntimeError("private")
        error.body = {"error": {"code": "sk-private-secret"}}
        error.request_id = "req-sk-private-secret"
        error.__cause__ = error
        result = provider_error_diagnostics(error)
        self.assertEqual(result["provider_code"], "other")
        self.assertIsNone(result["request_id"])
        self.assertEqual(result["exception_chain"], ["OtherException"])
        self.assertNotIn("private", json.dumps(result))

    def test_schema_error_records_paths_without_model_supplied_field_names(self):
        model = self.llm(content='{"private-response-secret": 1}', reason="length")
        with self.observer.task("fixture"):
            with self.assertRaises(PlanningError):
                RequirementInterpreter(llm=model).interpret(shanghai_request())
        outputs = [json.loads(r["output"]) for r in self.rows() if r["name"] == "llm.requirement_interpretation"]
        self.assertEqual(len(outputs), 2)
        for output in outputs:
            self.assertTrue(output["diagnostics"])
            self.assertEqual(output["model_call"]["finish_reason"], "length")
            self.assertNotIn("private-response-secret", json.dumps(output))

    def test_every_master_rejection_has_a_fixed_condition(self):
        import app.agents.master_decider as module
        tree = ast.parse(Path(module.__file__).read_text())
        messages = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_reject":
                self.assertIsInstance(node.args[1], ast.Constant)
                messages.add((node.args[0].value, node.args[1].value))
        with self.observer.task("fixture"):
            for code, message in messages:
                with self.assertRaises(PlanningError):
                    MasterDecider._reject(code, message)
        for row in self.rows():
            detail = json.loads(row["output"])["diagnostics"][0]
            self.assertTrue(detail["guidance"])
            self.assertTrue(detail["path"])
        conditions = [json.loads(r["output"])["diagnostics"][0]["code"] for r in self.rows()]
        self.assertIn("duplicate_fact_reference", conditions)
        self.assertIn("unknown_fact_reference", conditions)
        self.assertIn("meal_area_scope_mismatch", conditions)

    def test_weather_parent_and_mcp_child_are_saved(self):
        payload = success_payload([forecast("2026-10-10", dayweather="晴")])
        executor = MCPToolExecutor(lambda *_: payload)
        self.addCleanup(executor.close)
        adapter = AmapContractAdapter(lambda name, args: executor.run(name, args, timeout_seconds=1))
        with self.observer.task("fixture"):
            WeatherQueryService(adapter=adapter).run(make_context())
        rows = self.rows()
        weather = next(r for r in rows if r["name"] == "weather.query")
        tool = next(r for r in rows if r["name"] == "tool.mcp_call")
        self.assertEqual(tool["parent"], weather["id"])
        self.assertFalse(self.observer.incomplete)

    def test_error_distribution_does_not_count_parent_rethrows(self):
        model = self.llm(error=ConnectionError("private"))
        with self.observer.task("fixture"):
            for _ in range(2):
                with self.assertRaises(Exception):
                    with span("repair.revise", "validation") as parent:
                        try:
                            with span("llm.fixture", "llm") as record:
                                model.invoke([])
                        except Exception:
                            parent.output = {"valid": False, "error_code": "EXECUTION_FAILED"}
                            raise
            with span("llm.completed", "llm") as record:
                self.llm(content="private-response", reason="length").invoke([])
                record.output = {"status": "returned"}
            with span("validation.daily_plan_completion", "validation") as record:
                record.output = {"valid": False, "reason": "truncated"}
        report = build_task_report(self.path, "fixture")
        counts = report["error_distribution"]
        self.assertEqual(sum(x["count"] for x in counts), 3)
        self.assertEqual(counts[0]["count"], 2)
        self.assertEqual(counts[0]["stage"], "llm.fixture")
        self.assertNotIn("private-response", render_task_report(report))
        self.assertIn("length", render_task_report(report))
