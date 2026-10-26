"""固定资料契约与旧实现提取前的 golden 对照。"""
import json
from pathlib import Path
import unittest
from pydantic import ValidationError
from app.models.planning_errors import PlanningError, InvalidPlanningReferences
from app.services.planning_v4_validator import ContractValidationError
from app.models.planning_execution import PlanningSnapshot, ProposalSubmission
from app.planning.validation import accept_submission

FIXTURES = Path(__file__).parent / 'fixtures/planning_core'
def load_case(name='normal'):
    return json.loads((FIXTURES / (name+'.json')).read_text())
def snapshot(name='normal'):
    return PlanningSnapshot.model_validate(load_case(name)['snapshot'])

class PlanningContractTests(unittest.TestCase):
    def test_six_cases_match_pre_extraction_golden(self):
        for path in FIXTURES.glob('*.json'):
            with self.subTest(case=path.stem):
                data=load_case(path.stem)
                value=PlanningSnapshot.model_validate(data['snapshot'])
                result=accept_submission(value,ProposalSubmission.model_validate(data['submission']))
                self.assertEqual(result.draft.model_dump(mode='json'),data['expected']['draft'])
                self.assertEqual(result.report.model_dump(mode='json'),data['expected']['report'])
    def test_input_is_not_modified(self):
        data=load_case(); value=PlanningSnapshot.model_validate(data['snapshot'])
        before=value.model_dump(mode='json')
        accept_submission(value,ProposalSubmission.model_validate(data['submission']))
        self.assertEqual(value.model_dump(mode='json'),before)
        data['snapshot']['facts']['sources'][0]['effective_params']['city']='篡改'
        self.assertEqual(value.model_dump(mode='json'),before)
    def test_invalid_candidate_and_source_are_rejected(self):
        for field,value in [('place_fact_id','missing'),('poi_id','wrong')]:
            data=load_case(); data['snapshot']['candidates'][0][field]=value
            with self.assertRaises((PlanningError, InvalidPlanningReferences, ContractValidationError, ValidationError)):
                accept_submission(PlanningSnapshot.model_validate(data['snapshot']),ProposalSubmission.model_validate(data['submission']))
        data=load_case(); data['snapshot']['facts']['sources']=[]
        with self.assertRaises((PlanningError, InvalidPlanningReferences, ContractValidationError, ValidationError)):
            accept_submission(PlanningSnapshot.model_validate(data['snapshot']),ProposalSubmission.model_validate(data['submission']))
    def test_wrong_city_rejected_on_selection(self):
        data=load_case();data['snapshot']['facts']['places'][0]['city']='北京'
        with self.assertRaises((PlanningError, InvalidPlanningReferences, ContractValidationError, ValidationError)):
            accept_submission(PlanningSnapshot.model_validate(data['snapshot']),ProposalSubmission.model_validate(data['submission']))
    def test_specified_hotel_cannot_be_replaced_with_another_known_hotel(self):
        data=load_case('specified_hotel'); value=data['snapshot']
        place=dict(value['facts']['places'][-1]);place.update(fact_id='fact-other-hotel',poi_id='poi-other-hotel',name='其他合成酒店')
        value['facts']['places'].append(place)
        candidate=dict(value['candidates'][-1]);candidate.update(candidate_id='candidate-other-hotel',place_fact_id=place['fact_id'],poi_id=place['poi_id'])
        value['candidates'].append(candidate)
        data['submission']['days'][0]['lodging']={'candidate_ref':candidate['candidate_id']}
        with self.assertRaises((InvalidPlanningReferences,ContractValidationError)):
            accept_submission(PlanningSnapshot.model_validate(value),ProposalSubmission.model_validate(data['submission']))
    def test_missing_specified_hotel_stays_unknown(self):
        data=load_case('specified_hotel')
        data['snapshot']['candidates']=[c for c in data['snapshot']['candidates'] if c['category']!='lodging']
        result=accept_submission(PlanningSnapshot.model_validate(data['snapshot']),ProposalSubmission.model_validate(data['submission']))
        self.assertTrue(all(day.lodging.selection_source=='unknown' for day in result.draft.days))
    def test_model_cannot_supply_backend_identity(self):
        data=load_case()['submission'];data['request_id']='forged'
        with self.assertRaises(ValidationError):ProposalSubmission.model_validate(data)
    def test_context_is_stable_across_processes(self):
        import os,subprocess,sys
        code='''
import json
from test_planning_core_contracts import load_case
from app.models.planning_execution import PlanningSnapshot
from app.planning.context import build_context
data=load_case()['snapshot']
forecast=dict(data['facts']['weather'][0]['forecasts'][0])
forecast['forecast_date']='2026-10-11'
data['facts']['weather'][0]['forecasts'].append(forecast)
print(json.dumps(build_context(PlanningSnapshot.model_validate(data)).model_dump(mode='json'),sort_keys=True))
'''
        outputs=[]
        for seed in ('1','2'):
            env=dict(os.environ,PYTHONHASHSEED=seed,PYTHONPATH='.:tests')
            result=subprocess.run([sys.executable,'-c',code],cwd=FIXTURES.parents[2],env=env,capture_output=True,text=True,check=True)
            outputs.append(result.stdout)
        self.assertEqual(outputs[0],outputs[1])
