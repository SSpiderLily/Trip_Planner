import json
import unittest
from app.agents.native_daily_plan_agent import NativeDailyPlanAgent
from app.services.llm_service import NativeLLMCompletion
from app.services.native_tool_loop import NativeToolLoopError
from app.planning.core import generate_draft
from test_planning_core_contracts import snapshot,load_case
from test_planning_core import Runtime

class FakeModel:
    def __init__(self,*submissions):self.submissions=list(submissions);self.messages=[]
    def invoke_native(self,messages,**kwargs):
        self.messages.append(messages)
        value=self.submissions.pop(0)
        message={'role':'assistant','content':value} if isinstance(value,str) else {'role':'assistant','content':None,'tool_calls':[{'id':f'call{len(self.messages)}','type':'function','function':{'name':'submit_daily_plan','arguments':json.dumps(value)}}]}
        return NativeLLMCompletion(message=message,usage=None,finish_reason='stop' if isinstance(value,str) else 'tool_calls')

class AgentTests(unittest.TestCase):
    def test_accept_ends_without_extra_model_call(self):
        model=FakeModel(load_case()['submission'])
        result=generate_draft(snapshot(),generator=NativeDailyPlanAgent(model),runtime=Runtime())
        self.assertEqual(len(model.messages),1)
        self.assertEqual(len(model.messages[0]),2)
        self.assertEqual(result.draft.plan_id,'plan-sole-synthetic')
    def test_invalid_reference_then_corrected(self):
        wrong=load_case()['submission'];wrong['days'][0]['activities'][0]['candidate_ref']='fake-poi'
        model=FakeModel(wrong,load_case()['submission'])
        generate_draft(snapshot(),generator=NativeDailyPlanAgent(model),runtime=Runtime())
        self.assertEqual(len(model.messages),2)
        self.assertIn('rejected',model.messages[1][-1]['content'])
    def test_plain_text_is_not_a_submission(self):
        with self.assertRaises(NativeToolLoopError):
            generate_draft(snapshot(),generator=NativeDailyPlanAgent(FakeModel('完成行程')),runtime=Runtime())
    def test_partial_plan_accepted_without_collection(self):
        model=FakeModel(load_case('missing_meal')['submission'])
        result=generate_draft(snapshot('missing_meal'),generator=NativeDailyPlanAgent(model),runtime=Runtime())
        self.assertTrue(any(x.code=='missing_meal' for x in result.report.issues))
        self.assertEqual(len(model.messages),1)
    def test_callback_error_stops_before_another_model_call(self):
        class Broken(Runtime):
            def report(self,stage,*args,**kwargs):
                if stage=='validation':raise RuntimeError('callback failed')
        model=FakeModel(load_case()['submission'])
        with self.assertRaisesRegex(RuntimeError,'规划进度回调失败'):
            generate_draft(snapshot(),generator=NativeDailyPlanAgent(model),runtime=Broken())
        self.assertEqual(len(model.messages),1)
    def test_cancel_during_model_call_discards_submission(self):
        runtime=Runtime()
        class LateModel(FakeModel):
            def invoke_native(self,*args,**kwargs):
                result=super().invoke_native(*args,**kwargs);runtime.cancelled=True;return result
        from app.models.planning_errors import PlanningError
        with self.assertRaises(PlanningError) as cm:
            generate_draft(snapshot(),generator=NativeDailyPlanAgent(LateModel(load_case()['submission'])),runtime=runtime)
        self.assertEqual(cm.exception.code,'CANCELLED')
    def test_callback_validation_error_is_not_model_rejection(self):
        from pydantic import ValidationError
        from app.models.planning_runtime import PreviewProgress
        from app.models.planning_errors import PlanningError
        class Broken(Runtime):
            failed=False
            def report(self,stage,*args,**kwargs):
                if stage=='validation' and not self.failed:
                    self.failed=True
                    PreviewProgress(stage='invalid-stage')
        model=FakeModel(load_case()['submission'],load_case()['submission'])
        with self.assertRaises(PlanningError) as cm:
            generate_draft(snapshot(),generator=NativeDailyPlanAgent(model),runtime=Broken())
        self.assertEqual(cm.exception.code,'PROGRESS_CALLBACK_FAILED')
        self.assertEqual(len(model.messages),1)
