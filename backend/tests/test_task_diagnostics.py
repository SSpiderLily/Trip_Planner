"""单次任务只读诊断：关联、证据缺口和采集失败语义。"""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app.services.planning_diagnostics import diagnostic_stage, repair_stage
from app.services.observation_service import ObservationService, span
from app.services.task_diagnostics import build_task_report, render_task_report
from app.services.task_repository import TaskRepository


class TaskDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "tasks.sqlite3"
        self.repo = TaskRepository(self.path)
        self.repo.initialize()
        self.repo.create("task-a", {"private_request": "must-not-export"})
        self.repo.start("task-a")
        self.repo.create("task-b", {})

    def report(self):
        with patch("socket.socket.connect", side_effect=AssertionError("offline only")):
            return build_task_report(self.path, "task-a")

    def test_read_is_snapshot_only_and_does_not_recover_running_tasks(self):
        before = self.path.read_bytes()
        report = self.report()
        self.assertEqual(report["task"]["status"], "running")
        self.assertEqual(self.repo.status("task-a")["status"], "running")
        self.assertEqual(before, self.path.read_bytes())
        self.assertIsNone(report["delivery"])
        self.assertTrue(report["evidence_gaps"])
        self.assertNotIn("private_request", json.dumps(report))
        self.assertNotIn("task-b", json.dumps(report))
        self.assertIn("未知", render_task_report(report))

    def test_missing_database_or_task_is_not_created(self):
        absent = self.path.parent / "absent.sqlite3"
        with self.assertRaises(sqlite3.OperationalError):
            build_task_report(absent, "none")
        self.assertFalse(absent.exists())
        with self.assertRaises(LookupError):
            build_task_report(self.path, "none")

    def test_first_rejection_uses_failure_time_not_parent_start_and_keeps_round(self):
        from app.services.planning_v4_validator import ContractValidationError, ReferenceIssue
        with ObservationService(self.repo).task("task-a"):
            with self.assertRaises(ContractValidationError):
                with diagnostic_stage("planning.v4.run", "planning", {"plan_id": "plan-a"}):
                    with repair_stage("recalculate", {"day_id": "day-2", "round_id": "round-2", "round_number": 2}):
                        with repair_stage("route_validation", {}):
                            raise ContractValidationError((ReferenceIssue("dangling_route_fact", "draft.legs[0].mode_results[1]", "must-not-export"),))
        report = self.report()
        first = report["first_observed_rejection"]
        self.assertEqual(first["name"], "repair.route_validation")
        self.assertEqual(first["round_id"], "round-2")
        self.assertEqual(first["plan_id"], "plan-a")
        self.assertEqual(first["diagnostics"][0]["code"], "dangling_route_fact")
        self.assertNotIn("must-not-export", json.dumps(report))

    def test_successful_call_with_technical_error_is_still_reported_as_rejected(self):
        with ObservationService(self.repo).task("task-a"):
            with repair_stage("collect", {"round_id": "round-1"}) as record:
                record.output = {"valid": False, "error_code": "MASTER_TASK_PREFERENCE_INVALID"}
            with span("llm", "llm", {"messages": ["must-not-export"]}) as record:
                record.output = {"response": "must-not-export"}
            with span("planning.v4.progress", "planning", {"stage": "proposal"}) as record:
                record.output = {"outcome": "reported"}
        report = self.report()
        first = report["first_observed_rejection"]
        self.assertEqual(first["execution_status"], "succeeded")
        self.assertEqual(first["outcome"], "rejected")
        self.assertNotIn("must-not-export", json.dumps(report))
        rendered = render_task_report(report)
        self.assertIn("阶段 proposal", rendered)
        self.assertIn("调用=succeeded，结果=rejected", rendered)

    def test_master_preference_conditions_are_distinguishable_without_raw_values(self):
        from app.agents.execution import PlanningError
        from app.agents.master_decider import MasterDecider
        with ObservationService(self.repo).task("task-a"):
            for message in ("任务偏好引用重复", "任务偏好必须完整引用输入中的原文"):
                with self.assertRaises(PlanningError):
                    MasterDecider._reject("MASTER_TASK_PREFERENCE_INVALID", message)
        report = self.report()
        conditions = [e["diagnostics"][0]["code"] for e in report["events"]]
        self.assertEqual(conditions, ["duplicate_preference_reference", "preference_reference_not_in_input"])

    def test_legacy_errors_do_not_invent_conditions_or_delivery(self):
        with ObservationService(self.repo).task("task-a"):
            with span("planning.dispatch", "planning", {}) as record:
                record.output = {"error_code": "MASTER_TASK_AREA_UNKNOWN", "reason": "must-not-export"}
        self.repo.fail("task-a", "MASTER_TASK_AREA_UNKNOWN", "must-not-export", "dispatch")
        report = self.report()
        self.assertIsNone(report["delivery"])
        self.assertEqual(report["first_observed_rejection"]["diagnostics"], [])
        self.assertTrue(any("具体条件" in gap for gap in report["evidence_gaps"]))
        self.assertEqual(report["repair_rounds"], [])
        self.assertNotIn("must-not-export", json.dumps(report))
        self.assertIn("任务错误：MASTER_TASK_AREA_UNKNOWN @ dispatch", render_task_report(report))

    def test_truncated_and_incomplete_evidence_is_flagged(self):
        with ObservationService(self.repo, content_limit=128).task("task-a"):
            with span("llm", "llm", {}) as record:
                record.output = {"response": "must-not-export" * 1000}
        self.repo.mark_incomplete("task-a")
        report = self.report()
        self.assertTrue(any("截断" in gap for gap in report["evidence_gaps"]))
        self.assertTrue(any("采集器" in gap for gap in report["evidence_gaps"]))
        self.assertNotIn("must-not-export", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
