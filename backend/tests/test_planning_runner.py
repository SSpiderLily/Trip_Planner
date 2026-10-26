"""评估入口显式选例，一例一次流程；失败及物理尝试不能消失。"""
import contextlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from scripts import run_planning_core as runner
from test_planning_quality_fixtures import QUALITY

class RunnerTests(unittest.TestCase):
    def test_explicit_cases_run_in_order_without_network_or_config(self):
        out=io.StringIO()
        with patch('socket.socket.connect',side_effect=AssertionError('offline')),contextlib.redirect_stdout(out):
            code=runner.main(['--mode','fake','--cases','normal','insufficient_candidates'])
        self.assertEqual(code,0)
        runs=json.loads(out.getvalue())['runs']
        self.assertEqual([r['case_id'] for r in runs],['normal','insufficient_candidates'])
        self.assertEqual([r['evaluation']['deterministic']['covered_required_slots'] for r in runs],[9,4])
        self.assertTrue(all(r['delivered'] for r in runs))
        self.assertTrue(all(r['run']['physical_model_attempts']==0 for r in runs))
        self.assertTrue(all(r['run']['usage'] is None for r in runs))
    def test_failure_is_recorded_and_later_case_still_runs(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);bad=json.loads((QUALITY/'normal.json').read_text());bad['submission']['days'][0]['activities'][0]['candidate_ref']='bad'
            (root/'bad.json').write_text(json.dumps(bad));(root/'normal.json').write_bytes((QUALITY/'normal.json').read_bytes())
            out=io.StringIO()
            with contextlib.redirect_stdout(out):code=runner.main(['--mode','fake','--fixture-dir',tmp,'--cases','bad','normal'])
        runs=json.loads(out.getvalue())['runs']
        self.assertEqual(code,1)
        self.assertFalse(runs[0]['delivered']);self.assertIsNone(runs[0]['evaluation'])
        self.assertEqual(set(runs[0]['human_quality'].values()),{'unknown'})
        self.assertTrue(runs[1]['delivered'])
        self.assertNotIn('bad',runs[0]['error_code'])
    def test_live_records_physical_calls_missing_usage_and_stops_on_accept(self):
        from test_native_daily_plan_agent import FakeModel
        data=json.loads((QUALITY/'normal.json').read_text())
        wrong=json.loads(json.dumps(data['submission']));wrong['days'][0]['activities'][0]['candidate_ref']='wrong'
        model=FakeModel(wrong,data['submission'])
        record=runner.run_case(QUALITY/'normal.json',mode='live',model_factory=lambda:model)
        self.assertTrue(record['delivered'])
        self.assertEqual(record['run']['physical_model_attempts'],2)
        self.assertEqual(len(record['run']['attempts']),2)
        self.assertIsNone(record['run']['usage'])
        self.assertNotIn('messages',json.dumps(record))
    def test_live_failure_keeps_attempts_without_raw_error_or_empty_draft(self):
        class Broken:
            def invoke_native(self,*args,**kwargs):raise RuntimeError('private-secret')
        record=runner.run_case(QUALITY/'normal.json',mode='live',model_factory=Broken)
        self.assertFalse(record['delivered']);self.assertIsNone(record['result'])
        self.assertEqual(record['run']['physical_model_attempts'],1)
        self.assertEqual(record['run']['attempts'][0]['status'],'failed')
        self.assertIsNone(record['run']['usage'])
        self.assertNotIn('private-secret',json.dumps(record))
    def test_usage_sum_never_turns_missing_field_into_zero(self):
        recorder=runner.MeasuredModel(None)
        recorder.attempts=[{'usage':{'input_tokens':2,'output_tokens':3,'total_tokens':5}},
                           {'usage':{'input_tokens':7,'output_tokens':None,'total_tokens':None}}]
        self.assertEqual(recorder.usage(),{'input_tokens':9,'output_tokens':None,'total_tokens':None})
    def test_single_file_keeps_original_cli_shape(self):
        out=io.StringIO()
        with contextlib.redirect_stdout(out):code=runner.main(['--mode','fake','--input-file',str(QUALITY/'normal.json')])
        value=json.loads(out.getvalue());self.assertEqual(code,0)
        self.assertEqual(len(value['result']['draft']['days']),2)
        self.assertEqual(value['source_type'],'synthetic')
    def test_duplicate_or_path_case_rejected_before_any_execution(self):
        for cases in (['normal','normal'],['../normal']):
            with self.subTest(cases=cases),contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                runner.main(['--mode','fake','--cases',*cases])

    def test_connection_retry_attempt_and_returned_usage_are_both_recorded(self):
        from test_native_daily_plan_agent import FakeModel
        from test_native_model_retry import connection_error
        from app.services.llm_service import NativeLLMCompletion
        data=json.loads((QUALITY/'normal.json').read_text())
        class RetryOnce(FakeModel):
            failed=False
            def invoke_native(self,*args,**kwargs):
                if not self.failed:
                    self.failed=True
                    raise connection_error()
                result=super().invoke_native(*args,**kwargs)
                return NativeLLMCompletion(message=result.message,finish_reason=result.finish_reason,
                    usage={'prompt_tokens':11,'completion_tokens':7,'total_tokens':18})
        with patch('app.services.native_tool_loop.retry_delay_seconds',return_value=0):
            record=runner.run_case(QUALITY/'normal.json',mode='live',model_factory=lambda:RetryOnce(data['submission']))
        self.assertTrue(record['delivered'])
        self.assertEqual(record['run']['physical_model_attempts'],2)
        self.assertEqual([a['status'] for a in record['run']['attempts']],['failed','returned'])
        self.assertEqual(record['run']['attempts'][1]['usage']['total_tokens'],18)
        self.assertIsNone(record['run']['usage'])  # 失败请求的计费用量未知，不能只相加成功请求。

    def test_run_records_safe_model_configuration_and_known_usage(self):
        from test_native_daily_plan_agent import FakeModel
        from app.services.llm_service import NativeLLMCompletion
        data=json.loads((QUALITY/'normal.json').read_text())
        class WithUsage(FakeModel):
            model='fixture-master'
            temperature=0.2
            reasoning_effort='low'
            api_key='private-secret'
            def invoke_native(self,*args,**kwargs):
                result=super().invoke_native(*args,**kwargs)
                return NativeLLMCompletion(message=result.message,finish_reason=result.finish_reason,
                    usage={'prompt_tokens':11,'completion_tokens':7,'total_tokens':18})
        record=runner.run_case(QUALITY/'normal.json',mode='live',model_factory=lambda:WithUsage(data['submission']))
        self.assertIn('model_configuration',record['run'])
        self.assertEqual(record['run']['model_configuration'],
                         {'model':'fixture-master','temperature':0.2,'reasoning_effort':'low'})
        self.assertEqual(record['run']['usage'],{'input_tokens':11,'output_tokens':7,'total_tokens':18})
        self.assertNotIn('private-secret',json.dumps(record))
