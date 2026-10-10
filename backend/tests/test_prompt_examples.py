"""校验优化前各阶段的 JSON 示例及实际角色消息链路。"""
import copy
import json
import re
import unittest
from pydantic import ValidationError
from app.agents.route_planner import (
    RouteTripPlanner, COLLECT_PROMPT, PLAN_PROMPT, REVISE_PROMPT, REPAIR_PROMPT,
)
from app.models.itinerary import Collection, Draft, Day, Lodging, ReferenceCost, Cost
from app.models.schemas import TripRequest
from test_route_workflow import FakeLLM, FakeMCP


def examples(prompt):
    return [json.loads(value) for value in re.findall(r"```json\s*\n(.*?)\n```", prompt, re.S)]


class PromptExamplesTest(unittest.TestCase):
    def test_collection_examples_cover_first_followup_and_stop(self):
        values = [Collection.model_validate(value) for value in examples(COLLECT_PROMPT)]
        self.assertTrue(any(value.conditions is not None for value in values))
        self.assertTrue(any(value.conditions is None and value.searches for value in values))
        self.assertTrue(any(not value.searches for value in values))
        self.assertEqual({query.category for value in values for query in value.searches},
                         {'sightseeing', 'meal', 'lodging'})

    def test_arrange_and_revision_examples_match_draft_contract(self):
        for prompt in (PLAN_PROMPT, REVISE_PROMPT):
            values = examples(prompt)
            self.assertTrue(values)
            for value in values:
                contract = Draft if 'lodging_base' in value else Day if 'activities' in value else Lodging
                with self.subTest(contract=contract.__name__):
                    contract.model_validate(value)
            drafts = [Draft.model_validate(value) for value in values if 'lodging_base' in value]
            self.assertEqual({activity.type for value in drafts for day in value.days for activity in day.activities},
                             {'sightseeing', 'meal', 'free_time'})
            self.assertTrue(any(not value['activities'] for value in values if 'activities' in value))
            self.assertTrue(any(value.get('source') == 'user' and value['place'] is None for value in values))

    def test_unknown_cost_examples_and_invalid_null_objects(self):
        reference, estimated = examples(REPAIR_PROMPT)
        self.assertIsNone(ReferenceCost.model_validate(reference).amount)
        self.assertIsNone(Cost.model_validate(estimated).amount)
        original = next(value for value in examples(PLAN_PROMPT) if 'lodging_base' in value)
        for field in ('lodging', 'activity'):
            bad = copy.deepcopy(original)
            if field == 'lodging':
                bad['lodging_base']['reference_cost'] = None
            else:
                bad['days'][0]['activities'][0]['estimated_cost'] = None
            with self.subTest(field=field), self.assertRaises(ValidationError):
                Draft.model_validate(bad)

    def test_actual_messages_include_examples_through_repair_and_revision(self):
        llm = FakeLLM()
        llm.long_day, llm.invalid_first = True, True
        original, received = llm.invoke, []
        def invoke(messages, **kwargs):
            received.append((messages[0]['content'], json.loads(messages[-1]['content']), kwargs))
            return original(messages, **kwargs)
        llm.invoke = invoke
        result = RouteTripPlanner(llm, FakeMCP()).plan_trip(TripRequest(
            city='北京', arrival_at='2026-10-01T09:00:00+08:00', departure_at='2026-10-01T19:00:00+08:00'))
        self.assertEqual(result.schema_version, 3)
        self.assertEqual({data['stage'] for _, data, _ in received},
                         {'sights', 'layout', 'support', 'final', 'revision'})
        self.assertEqual(sum(bool(data.get('repair')) for _, data, _ in received), 1)
        for prompt, data, kwargs in received:
            self.assertTrue(examples(prompt))
            self.assertEqual(data['schema']['title'], 'Collection' if data['stage'] in ('sights', 'support') else 'Draft')
            self.assertNotIn('extra_body', kwargs)
            if data.get('repair'):
                self.assertEqual(examples(data['repair']), examples(REPAIR_PROMPT))


if __name__ == '__main__':
    unittest.main()
