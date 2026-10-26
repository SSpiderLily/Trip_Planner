import unittest
from app.agents.daily_plan_generator import DailyPlanGenerator, DailyPlanModelOutput, _InvalidReferences
from app.models.planning_execution import ProposalSubmission, PlanningSnapshot
from app.models.planning_errors import PlanningError
from app.planning.validation import accept_submission
from test_planning_core_contracts import snapshot,load_case

class EquivalenceTests(unittest.TestCase):
    def test_binding_weather_and_missing_slots(self):
        value=snapshot();data=load_case()['submission'];data['days'][0]['activities']=[]
        data['days'][1]['weather_advice']='确定晴天'
        output=DailyPlanModelOutput.model_validate(data)
        old=DailyPlanGenerator(llm=object())
        proposal=old._validate_proposal_output(output,value.planning_input,value.candidates,value.facts,value.area_hints)
        draft=old._build_draft(proposal,value.planning_input,value.candidates,value.facts,value.area_hints,value.identity.task_id,plan_id=value.identity.plan_id)
        result=accept_submission(value,ProposalSubmission.model_validate(data))
        self.assertEqual(draft,result.draft)
        self.assertTrue(result.draft.days[0].unfilled_slot_ids)
        self.assertEqual(result.draft.days[1].weather_advice,'该日期暂无预报，天气建议未知。')
    def test_invalid_unfilled_has_same_error_code(self):
        for slots in [('missing',),('slot-2026-10-10-afternoon',)*2]:
            data=load_case()['submission'];data['days'][0]['unfilled_slot_ids']=slots
            value=snapshot(); old=DailyPlanGenerator(llm=object())
            with self.assertRaises(_InvalidReferences) as before:
                old._validate_proposal_output(DailyPlanModelOutput.model_validate(data),value.planning_input,value.candidates,value.facts,value.area_hints)
            with self.assertRaises(_InvalidReferences) as after:
                accept_submission(value,ProposalSubmission.model_validate(data))
            self.assertEqual(before.exception.codes,after.exception.codes)
    def test_task_identity_mismatch_is_rejected(self):
        for field,wrong in [('run_id','other-run'),('request_id','other-request')]:
            data=load_case(); task={'task_id':'child','run_id':data['snapshot']['identity']['task_id'],'request_id':data['snapshot']['planning_input']['request']['request_id'],'context_revision':1,'status':'complete','stop_reason':'target_met'}
            task[field]=wrong;data['snapshot']['task_results']=[task]
            with self.assertRaises(PlanningError) as cm:
                accept_submission(PlanningSnapshot.model_validate(data['snapshot']),ProposalSubmission.model_validate(data['submission']))
            self.assertEqual(cm.exception.code,'PLAN_REFERENCE_INVALID')
