"""离线评估不得把结构通过当作人工质量通过。"""
import unittest
from copy import deepcopy
from app.models.planning_execution import PlanningSnapshot, ProposalSubmission
from app.planning.validation import accept_submission
from test_planning_quality_fixtures import load_quality

class EvaluationTests(unittest.TestCase):
    def setUp(self):
        data=load_quality()
        self.snapshot=PlanningSnapshot.model_validate(data['snapshot'])
        self.result=accept_submission(self.snapshot, ProposalSubmission.model_validate(data['submission']))
    def evaluate(self, result=None, **kwargs):
        from app.planning.evaluation import evaluate_draft
        return evaluate_draft(self.snapshot, result or self.result, **kwargs)
    def test_complete_coverage_leaves_all_human_quality_unknown(self):
        report=self.evaluate()
        self.assertTrue(report.deterministic.delivered)
        self.assertTrue(report.deterministic.references_valid)
        self.assertEqual(report.deterministic.required_slots,9)
        self.assertEqual(report.deterministic.covered_required_slots,9)
        self.assertTrue(report.deterministic.lodging_bound)
        self.assertEqual(set(report.human.model_dump().values()),{'unknown'})
    def test_partial_draft_is_delivered_but_not_complete(self):
        from app.planning.evaluation import evaluate_draft
        data=load_quality('insufficient_candidates');snap=PlanningSnapshot.model_validate(data['snapshot'])
        result=accept_submission(snap,ProposalSubmission.model_validate(data['submission']))
        report=evaluate_draft(snap,result)
        self.assertTrue(report.deterministic.delivered)
        self.assertEqual(report.deterministic.covered_required_slots,4)
        self.assertFalse(report.deterministic.required_coverage_complete)
    def test_cross_identity_and_context_are_rejected(self):
        paths=[('identity','task_id'),('identity','plan_id'),('draft','plan_id'),('draft','request_id'),
               ('draft','source_run_id'),('draft','rule_version'),('report','plan_id'),('report','based_on_revision')]
        for section,key in paths:
            with self.subTest(section=section,key=key):
                data=self.result.model_dump();data[section][key]=999 if key=='based_on_revision' else 'other'
                with self.assertRaises(ValueError): self.evaluate(type(self.result).model_validate(data))
        data=self.result.model_dump();data['planning_input']['request']['remarks']='changed'
        with self.assertRaises(ValueError):self.evaluate(type(self.result).model_validate(data))
    def test_tampered_activity_is_not_counted_as_valid_coverage(self):
        data=self.result.model_dump();data['draft']['days'][0]['activities'][0]['poi_id']='forged'
        report=self.evaluate(type(self.result).model_validate(data))
        self.assertFalse(report.deterministic.references_valid)
        self.assertFalse(report.deterministic.required_coverage_complete)
    def test_lodging_binding_is_recomputed_from_snapshot(self):
        data=self.result.model_dump();data['draft']['days'][1]['lodging']=None
        report=self.evaluate(type(self.result).model_validate(data))
        self.assertFalse(report.deterministic.lodging_bound)
    def test_human_review_is_explicit_and_bound_to_current_result(self):
        from app.models.planning_evaluation import QualityReview, QualityRatings
        review=QualityReview(identity=self.result.identity,request_id='request-quality-normal',rule_version='rules-f06-v1',
            revision=1,result_fingerprint=self.evaluate().result_fingerprint,ratings=QualityRatings(breakfast_suitability='fail',rest_preference='pass'))
        report=self.evaluate(review=review)
        self.assertEqual(report.human.breakfast_suitability,'fail')
        self.assertEqual(report.human.rest_preference,'pass')
        self.assertEqual(report.human.repeat_reasonableness,'unknown')
        for field,value in [('revision',2),('request_id','other'),('rule_version','other')]:
            with self.assertRaises(ValueError):self.evaluate(review=review.model_copy(update={field:value}))
    def test_evaluation_does_not_mutate_input_or_call_network(self):
        from unittest.mock import patch
        before=deepcopy(self.result.model_dump())
        with patch('socket.socket.connect',side_effect=AssertionError('offline')):
            self.evaluate()
        self.assertEqual(self.result.model_dump(),before)

    def test_wrong_kind_in_required_slot_does_not_count_as_coverage(self):
        data=self.result.model_dump()
        data['draft']['days'][0]['activities'][0].update(kind='sightseeing',candidate_ref='candidate-bund',
                                                      poi_id='poi-bund',place_fact_id='fact-bund')
        report=self.evaluate(type(self.result).model_validate(data))
        self.assertFalse(report.deterministic.required_coverage_complete)
        self.assertLess(report.deterministic.covered_required_slots,9)

    def test_wrong_city_is_not_a_valid_reference_even_when_fact_copies_agree(self):
        from app.planning.evaluation import evaluate_draft
        from app.models.planning_errors import InvalidPlanningReferences
        data=load_quality()
        fact_id=self.result.draft.days[0].activities[0].place_fact_id
        for place in data['snapshot']['facts']['places']:
            if place['fact_id']==fact_id:place['city']='北京市'
        snapshot=PlanningSnapshot.model_validate(data['snapshot'])
        with self.assertRaises(InvalidPlanningReferences):
            accept_submission(snapshot,ProposalSubmission.model_validate(data['submission']))
        result=self.result.model_copy(update={'facts':snapshot.facts})
        evaluation=evaluate_draft(snapshot,result)
        self.assertFalse(evaluation.deterministic.references_valid)
        self.assertEqual(evaluation.deterministic.covered_required_slots,0)
        self.assertFalse(evaluation.deterministic.required_coverage_complete)

    def test_human_review_cannot_be_reused_for_different_text_at_same_revision(self):
        from app.models.planning_evaluation import QualityReview, QualityRatings
        from app.planning.evaluation import evaluate_draft
        # 人工评价绑定公开评估输出的内容指纹，不能移植到同身份的另一份草稿。
        first=evaluate_draft(self.snapshot,self.result)
        binding={key:getattr(first,key) for key in ('identity','request_id','rule_version','revision','result_fingerprint')}
        review=QualityReview(**binding,ratings=QualityRatings(unsupported_promises='pass'))
        data=load_quality()
        data['submission']['days'][0]['activities'][0]['recommendation_reason']='保证全程预算够用并准时赶上返程。'
        another=accept_submission(self.snapshot,ProposalSubmission.model_validate(data['submission']))
        self.assertEqual(another.identity,self.result.identity)
        self.assertEqual(another.draft.revision,self.result.draft.revision)
        with self.assertRaisesRegex(ValueError,'EVALUATION_REVIEW_MISMATCH'):
            evaluate_draft(self.snapshot,another,review=review)
