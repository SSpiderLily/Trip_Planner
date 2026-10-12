"""合成上海三日：真实 F06/F09 到预览，外部模型、采集和路线全部隔离。"""
import asyncio
import json
import unittest
from unittest.mock import patch

from app.agents.daily_plan_generator import DailyPlanGenerator
from app.agents.execution import PlanningError
from app.services.daily_plan_adapter import DailyPlanGeneratorAdapter
from app.services.planning_dispatch_service import PlanningDispatchResult
from app.services.planning_v4_pipeline import PlanningV4Pipeline
from app.services.planning_v4_validator import validate_preview

from test_daily_plan_generator import (
    FakeLLM, make_input, make_facts, make_candidates, make_areas, make_model_output,
)
from test_planning_v4_pipeline import FakeInterpreter, FakeRouteService


class DailyPlanRecoveryPipelineTests(unittest.TestCase):
    def test_unrepaired_activity_reaches_preview_with_preserved_visits_and_slot_notice(self):
        request = make_input().request
        facts, candidates, areas = make_facts(), make_candidates(), make_areas()
        foreign_place = facts.places[0].model_copy(update={
            "fact_id": "fact-foreign", "poi_id": "poi-foreign", "city": "北京",
        })
        facts = facts.model_copy(update={"places": (*facts.places, foreign_place)})
        candidates = (*candidates, candidates[0].model_copy(update={
            "candidate_id": "candidate-foreign", "place_fact_id": "fact-foreign", "poi_id": "poi-foreign",
        }))
        tracker = {}
        day_id = "day-2026-10-11"
        morning = "slot-2026-10-11-morning"
        afternoon = "slot-2026-10-11-afternoon"
        lunch = "slot-2026-10-11-lunch"
        evening = "slot-2026-10-11-evening"

        class Dispatch:
            async def run_async(self, context):
                return PlanningDispatchResult(
                    request_id=context.request_id, run_id=context.run_id,
                    context_revision=context.context_revision, base_revision=context.base_revision,
                    action="plan", stop_reason="plan", reason="offline synthetic facts",
                    fact_store=facts, candidate_refs=candidates, area_hints=areas,
                )

        class ActualF06:
            async def generate(self, planning_input, *args, **kwargs):
                activities = (
                    {"proposal_ref": "good-morning", "candidate_ref": "candidate-bund",
                     "slot_id": morning, "kind": "sightseeing"},
                    {"proposal_ref": "bad-morning", "candidate_ref": "unknown-candidate",
                     "slot_id": morning, "kind": "sightseeing"},
                    {"proposal_ref": "bad-lunch", "candidate_ref": None,
                     "slot_id": lunch, "kind": "meal"},
                    {"proposal_ref": "good-afternoon", "candidate_ref": "candidate-bund",
                     "slot_id": afternoon, "kind": "sightseeing"},
                    {"proposal_ref": "bad-evening", "candidate_ref": "candidate-foreign",
                     "slot_id": evening, "kind": "sightseeing"},
                )
                llm = FakeLLM([
                    make_model_output(planning_input, activities={day_id: activities}),
                    "invalid local repair response",
                ])
                execution = await DailyPlanGeneratorAdapter(DailyPlanGenerator(llm=llm)).generate(
                    planning_input, *args, **kwargs,
                )
                tracker["llm"] = llm
                tracker["execution"] = execution
                return execution

            async def revise_day(self, *args, **kwargs):
                # 真正的按日三轮状态机运行；修订服务只返回一个确定的离线失败。
                tracker["business_revisions"] = tracker.get("business_revisions", 0) + 1
                raise PlanningError("OFFLINE_REVISION_UNAVAILABLE", "合成修订暂不可用")

        pipeline = PlanningV4Pipeline(
            requirement_interpreter_factory=lambda _timeout: FakeInterpreter(tracker),
            dispatch_service_factory=lambda _timeout: Dispatch(),
            daily_plan_adapter_factory=lambda _timeout: ActualF06(),
            current_route_service_factory=lambda: FakeRouteService(tracker),
        )
        with patch("socket.socket.connect", side_effect=AssertionError("offline test forbids network")):
            result = asyncio.run(pipeline.run(
                request, task_id="offline-recovery-task", plan_id="offline-recovery-plan",
                progress=lambda _update: None,
            ))

        preview = result.snapshot.preview
        validate_preview(preview)
        self.assertEqual(len(tracker["llm"].calls), 2)
        self.assertEqual(len(preview.draft.days), 3)
        day = next(item for item in preview.draft.days if item.day_id == day_id)
        self.assertEqual([item.slot_id for item in day.activities], [morning, afternoon])
        self.assertTrue(all("duration_s" not in item.model_dump() for item in day.activities))
        self.assertEqual([item.poi_id for item in day.activities], ["poi-bund", "poi-bund"])
        self.assertEqual(len({item.activity_id for item in day.activities}), 2)
        self.assertEqual(day.activities, tracker["execution"].draft.days[1].activities)
        self.assertIn(lunch, day.unfilled_slot_ids)
        self.assertIn(evening, day.unfilled_slot_ids)

        issues = preview.validation.issues
        self.assertTrue(any(issue.code == "missing_meal" and issue.target_id == lunch for issue in issues))
        notices = [issue for issue in issues if issue.code == "INITIAL_ACTIVITY_OMITTED"]
        self.assertEqual({issue.target_id for issue in notices}, {morning, lunch, evening})
        self.assertTrue(all(not issue.triggers_repair for issue in notices))
        self.assertTrue(all(issue.based_on_revision == preview.draft.revision for issue in notices))
        day_report = next(item for item in preview.validation.days if item.day_id == day_id)
        self.assertTrue({issue.issue_id for issue in notices} <= set(day_report.issue_ids))
        self.assertTrue(all(state.completed_rounds <= 3 for state in preview.validation.repair_states))
        self.assertGreater(tracker.get("business_revisions", 0), 0)
        # 存储序列化后仍有可见提示，不依赖只存在于内存中的模型回答。
        restored = type(result.snapshot).model_validate_json(result.snapshot.model_dump_json())
        self.assertEqual(restored.preview.validation, preview.validation)
        self.assertNotIn("unknown-candidate", json.dumps([n.model_dump() for n in notices]))


if __name__ == "__main__":
    unittest.main()
