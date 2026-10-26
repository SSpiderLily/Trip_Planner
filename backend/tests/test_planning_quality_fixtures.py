"""Quality 样本能力与意图，不指定唯一标准行程。"""
import json
from pathlib import Path
import unittest
from app.models.planning_execution import PlanningSnapshot, ProposalSubmission
from app.planning.validation import validate_snapshot, accept_submission

QUALITY = Path(__file__).parent / 'fixtures/planning_core/quality'
CASES = ('normal', 'no_breakfast_evidence', 'insufficient_candidates', 'specified_hotel',
         'unknown_cost_weather', 'explicit_repeat', 'rest_preference')

def load_quality(name='normal'):
    return json.loads((QUALITY / (name + '.json')).read_text())

class QualityFixtureTests(unittest.TestCase):
    def test_all_cases_have_closed_references_and_replayable_proposal(self):
        for name in CASES:
            with self.subTest(case=name):
                self.assertTrue((QUALITY / (name + '.json')).exists(), name)
                data = load_quality(name)
                self.assertEqual(data['case_id'], name)
                self.assertEqual(data['source_type'], 'synthetic')
                self.assertTrue(data['intent'])
                snap = PlanningSnapshot.model_validate(data['snapshot'])
                validate_snapshot(snap)
                result = accept_submission(snap, ProposalSubmission.model_validate(data['submission']))
                self.assertEqual(len(result.draft.days), 2)
                self.assertEqual(data['expected']['required_slots'], [
                    slot.slot_id for day in snap.planning_input.days for slot in day.slots
                    if slot.eligibility == 'required'])
                self.assertTrue(all(p.opening_hours_raw is None for p in snap.facts.places))
    def test_normal_has_distinct_experiences_breakfast_main_meals_and_hotel(self):
        self.assertTrue((QUALITY / 'normal.json').exists())
        data = load_quality()
        candidates = data['snapshot']['candidates']
        self.assertGreaterEqual(len({c['poi_id'] for c in candidates if c['category']=='sightseeing'}), 4)
        places = data['snapshot']['facts']['places']
        self.assertTrue(any(p['type_name']=='早餐服务；面点' for p in places))
        self.assertTrue(any(p['type_name']=='中式正餐' for p in places))
        self.assertEqual(sum(c['category']=='lodging' for c in candidates), 1)
    def test_contrasts_have_the_intended_evidence_boundaries(self):
        self.assertTrue((QUALITY / 'no_breakfast_evidence.json').exists())
        no_breakfast = load_quality('no_breakfast_evidence')['snapshot']
        self.assertFalse(any('早餐' in p['type_name'] for p in no_breakfast['facts']['places']))
        short = load_quality('insufficient_candidates')
        self.assertTrue(any(d['unfilled_slot_ids'] for d in short['submission']['days']))
        specified = load_quality('specified_hotel')['snapshot']
        self.assertEqual(specified['planning_input']['request']['lodging_query'], '合成指定酒店')
        unknown = load_quality('unknown_cost_weather')['snapshot']
        self.assertEqual(unknown['facts']['weather'], [])
        self.assertTrue(all(p['cost']['value'] is None for p in unknown['facts']['places']))
        self.assertIn('重复游览', load_quality('explicit_repeat')['snapshot']['planning_input']['request']['remarks'])
        self.assertIn('休息', load_quality('rest_preference')['snapshot']['planning_input']['request']['remarks'])

    def test_case_identities_cannot_mix_reviews_across_cases(self):
        identities=[tuple(load_quality(name)['snapshot']['identity'].values()) for name in CASES]
        self.assertEqual(len(set(identities)),7)
