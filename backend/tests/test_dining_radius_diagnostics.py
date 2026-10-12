"""餐饮搜索范围与拒绝诊断的离线回归；所有供应商返回均为合成数据。"""
import json
import socket
from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_dining_subtask import (
    FakeLLM, active_meal_slots, dining_context, action_search, action_finish,
    selection, vendor_result, restaurant,
)
from app.agents.dining_subtask import DiningSubtaskRunner
from app.services.amap_contract_adapter import AmapContractAdapter
from app.services.observation_service import ObservationService
from app.services.task_repository import TaskRepository


class DiningRadiusDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(socket.socket, 'connect', side_effect=AssertionError('offline test'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.slot, self.other = active_meal_slots(2, 'dinner')
        self.task, self.context = dining_context({
            self.slot: ('area-a', 'anchor-a', 121.49, 31.24),
            self.other: ('area-b', 'anchor-b', 121.49, 31.24),
        })

    def runner(self, replies, *, expanded=False):
        self.calls = []
        def tools(name, args):
            self.calls.append(dict(args))
            rows = [] if expanded and args['radius'] == '1000' else [
                restaurant('meal-a', '合成餐馆', 121.49, 31.26 if expanded else 31.241)]
            return vendor_result(name, args, rows)
        llm = FakeLLM(replies)
        return DiningSubtaskRunner(llm=llm, adapter=AmapContractAdapter(tool_runner=tools)), llm

    def test_expanded_candidate_reaches_model_and_finish_without_expanding_other_meal(self):
        runner, llm = self.runner([
            action_search(self.slot), action_finish([selection('meal-a', [self.slot])]),
        ], expanded=True)
        result = runner.run(self.task, self.context)
        candidates = json.loads(llm.messages[1][1]['content'])['available_candidates']
        self.assertEqual(candidates[0]['eligible_slot_ids'], [self.slot])
        self.assertEqual([x['radius'] for x in self.calls], ['1000', '3000'])
        self.assertEqual(result.task_result.candidates[0].slot_ids, (self.slot,))
        self.assertNotEqual(result.task_result.stop_reason, 'invalid_output')
        # 重用runner执行新任务不能继承上个任务扩大的范围。
        runner.llm = llm
        llm.replies = [action_finish([])]
        runner.run(self.task, self.context)
        self.assertEqual(runner._searched_radius_by_slot, {})

    def test_finish_rejection_conditions_are_distinct(self):
        cases = [
            (selection('absent', [self.slot]), 'selected_poi_not_available', 'poi_id'),
            (selection('meal-a', ['outside']), 'selected_slot_outside_task', 'slot_ids'),
            (selection('meal-a'), 'selection_binding_missing', 'slot_ids'),
            (selection('meal-a', [self.slot], ['unknown']), 'selected_requirement_outside_task', 'requirement_ids'),
        ]
        for chosen, condition, field in cases:
            with self.subTest(condition=condition):
                runner, _ = self.runner([action_search(self.slot), action_finish([chosen])])
                result = runner.run(self.task, self.context)
                summary = runner._safe_result_summary(result.task_result)
                self.assertEqual(result.task_result.stop_reason, 'invalid_output')
                self.assertTrue(any(d['condition'] == condition and d['path'] == 'selections[0].' + field
                                    for d in summary['diagnostics']))

    def test_expanded_radius_does_not_authorize_other_meal(self):
        runner, _ = self.runner([action_search(self.slot), action_finish([selection('meal-a', [self.other])])], expanded=True)
        result = runner.run(self.task, self.context)
        self.assertEqual(result.task_result.stop_reason, 'invalid_output')
        self.assertIn('selected_slot_not_eligible', [d['condition'] for d in runner._output_rejections])

    def test_invalid_search_and_keyword_diagnostics(self):
        for reply, condition, path in [
            (action_search('outside'), 'search_slot_outside_task', 'slot_id'),
            (json.dumps({'action': 'verify_restaurant', 'requirement_id': 'outside'}),
             'named_restaurant_requirement_missing', 'requirement_id'),
            (action_search(self.slot, 'https://example.invalid/private-secret'), 'query_keyword_forbidden', 'keywords'),
        ]:
            with self.subTest(condition=condition):
                runner, _ = self.runner([reply])
                runner.run(self.task, self.context)
                self.assertEqual(runner._output_rejections[0]['condition'], condition)
                self.assertEqual(runner._output_rejections[0]['path'], path)
                self.assertEqual(self.calls, [])

    def test_schema_diagnostics_persist_without_private_values(self):
        invalid = json.dumps({'action': 'search', 'keywords': 'private-secret', 'private-secret': 1})
        runner, _ = self.runner(['not json private-secret', invalid])
        with tempfile.TemporaryDirectory() as tmp:
            repo = TaskRepository(Path(tmp) / 'tasks.sqlite3')
            repo.initialize()
            repo.create('offline-dining', {})
            with ObservationService(repo).task('offline-dining'):
                result = runner.run(self.task, self.context)
            with closing(sqlite3.connect(repo.path)) as conn:
                rows = conn.execute('select name, output_data from spans').fetchall()
            serialized = json.dumps(rows)
            self.assertNotIn('private-secret', serialized)
            self.assertIn('action_json_invalid', serialized)
            self.assertIn('action_schema_invalid', serialized)
            summaries = [json.loads(output) for name, output in rows if name == 'agent.dining_result']
            self.assertEqual(summaries[0]['stop_reason'], 'invalid_output')
            self.assertTrue(any(d.get('repair_exhausted') for d in summaries[0]['diagnostics']))
            self.assertTrue(any(d['path'] == 'search.slot_id' for d in summaries[0]['diagnostics']))

    def test_radius_still_rejects_restaurant_beyond_three_km(self):
        def tools(name, args):
            return vendor_result(name, args, [restaurant('far', '远处合成餐馆', 121.49, 31.28)])
        llm = FakeLLM([action_search(self.slot), action_finish([selection('far', [self.slot])])])
        runner = DiningSubtaskRunner(llm=llm, adapter=AmapContractAdapter(tool_runner=tools))
        result = runner.run(self.task, self.context)
        self.assertEqual(json.loads(llm.messages[1][1]['content'])['available_candidates'], [])
        self.assertEqual(result.task_result.stop_reason, 'invalid_output')
        self.assertEqual(result.task_result.candidates, ())

    def test_duplicate_references_have_specific_condition_and_repaired_output_can_succeed(self):
        chosen = selection('meal-a', [self.slot, self.slot])
        runner, _ = self.runner([action_search(self.slot), action_finish([chosen]),
                                 action_finish([selection('meal-a', [self.slot])])])
        result = runner.run(self.task, self.context)
        self.assertNotEqual(result.task_result.stop_reason, 'invalid_output')
        self.assertEqual(runner._output_rejections[0]['condition'], 'selection_duplicate_slots')
        self.assertEqual(runner._output_rejections[0]['path'], 'finish.selections[0]')
        self.assertFalse(runner._output_rejections[0]['repair_exhausted'])
