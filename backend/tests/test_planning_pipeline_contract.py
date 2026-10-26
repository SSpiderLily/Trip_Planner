"""P5公开结果边界：正式预览不得读取采集器历史。"""
import unittest
from native_planning_fixtures import fixture_planner

class PlanningPipelineContractTests(unittest.TestCase):
    def test_formal_planner_returns_serializable_public_outcome(self):
        from app.models.planning_outcome import PlanningOutcome
        from app.services.native_preview_adapter import NativePreviewAdapter
        request,planner,_=fixture_planner(repair=True)
        outcome=planner.run(request,task_id='p5-task',plan_id='p5-plan')
        self.assertIsInstance(outcome,PlanningOutcome)
        self.assertEqual(outcome.status,'complete')
        self.assertEqual(outcome.initial_result.draft.revision,1)
        self.assertEqual(outcome.final_result.draft.revision,2)
        serialized=outcome.model_dump_json()
        for key in ('master_history','protocol','children','collector'):
            self.assertNotIn('"'+key+'"',serialized)
        restored=PlanningOutcome.model_validate_json(serialized)
        preview=NativePreviewAdapter().convert(restored,request=request,task_id='p5-task',plan_id='p5-plan')
        self.assertEqual(preview.snapshot.preview.draft.revision,2)

    def test_technical_outcome_with_draft_never_delivers(self):
        from app.models.planning_outcome import PlanningOutcome
        from app.services.native_preview_adapter import NativePreviewAdapter,NativePreviewError
        request,planner,_=fixture_planner(repair=False)
        outcome=planner.run(request,task_id='p5-task',plan_id='p5-plan')
        bad=outcome.model_copy(update={'status':'technical_error','error_code':'MODEL_CALL_FAILED'})
        with self.assertRaises(NativePreviewError):
            NativePreviewAdapter().convert(bad,request=request,task_id='p5-task',plan_id='p5-plan')

    def test_initial_report_revision_mismatch_is_rejected(self):
        from app.services.native_preview_adapter import NativePreviewAdapter,NativePreviewError
        request,planner,_=fixture_planner(repair=False)
        outcome=planner.run(request,task_id='p5-task',plan_id='p5-plan')
        invalid=outcome.model_copy(update={'initial_result':outcome.initial_result.model_copy(update={
            'report':outcome.initial_result.report.model_copy(update={'based_on_revision':99})})})
        with self.assertRaises(NativePreviewError):
            NativePreviewAdapter().convert(invalid,request=request,task_id='p5-task',plan_id='p5-plan')

    def test_status_cannot_claim_complete_with_required_gaps(self):
        from app.services.native_preview_adapter import NativePreviewAdapter,NativePreviewError
        from test_native_preview_adapter import native_result,public_outcome
        outcome=public_outcome(native_result(partial=True))
        forged=outcome.model_copy(update={'status':'complete','repair':outcome.repair.model_copy(update={'status':'complete'})})
        with self.assertRaises(NativePreviewError):
            NativePreviewAdapter().convert(forged,request=outcome.planning_input.request,task_id='task',plan_id='plan')
