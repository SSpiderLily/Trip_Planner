"""必须记录实际SDK参数，显式覆盖/失败时仍保留，不能脱敏掉额度。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import httpx
from openai import BadRequestError
from app.services.observation_service import ObservationService
from app.services.task_repository import TaskRepository
from app.services.native_tool_loop import NativeToolLoop,NativeToolLoopError
from test_native_model_retry import model,response

class ModelRequestObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.repo=TaskRepository(Path(self.tmp.name)/'tasks.db');self.repo.initialize();self.repo.create('call',{})
        self.obs=ObservationService(self.repo)

    def details(self):
        return [self.repo.span('call',s['span_id']) for s in self.repo.spans('call') if s['operation_type']=='llm']

    def test_recorded_overrides_match_sdk_request_with_medium_effort(self):
        llm,client,requests=model(response())
        llm.reasoning_effort='low';llm.output_token_limit=1000000;llm.output_token_parameter='max_tokens'
        with self.obs.task('call'):
            NativeToolLoop(llm,[]).run([],temperature=0.3,reasoning_effort='medium',max_completion_tokens=12345)
        config=self.details()[0]['output_data']['model_configuration']
        self.assertEqual(config,{'model':'fixture-model','temperature':0.3,'reasoning_effort':'medium',
            'output_limits':[{'parameter':'max_completion_tokens','value':12345}]})
        self.assertEqual(requests[0]['max_completion_tokens'],12345)
        self.assertNotIn('max_tokens',requests[0])
        self.assertEqual(self.details()[0]['input_data']['reasoning_effort'],'medium')

    def test_failure_still_records_actual_parameters_and_safe_http_diagnostics(self):
        err=BadRequestError('private-provider-text',response=httpx.Response(400,request=httpx.Request('POST','https://private.invalid')),
            body={'error':{'code':'invalid_parameter','message':'private-provider-text'}})
        llm,_,_=model(err);llm.output_token_limit=1000000;llm.output_token_parameter='max_tokens'
        with self.obs.task('call'),self.assertRaises(NativeToolLoopError):NativeToolLoop(llm,[]).run([])
        row=self.details()[0];out=row['output_data']
        self.assertEqual(out['model_configuration']['output_limits'],[{'parameter':'max_tokens','value':1000000}])
        self.assertEqual(out['model_call']['http_status'],400)
        self.assertEqual(out['model_call']['provider_code'],'invalid_parameter')
        self.assertIn('BadRequestError',out['model_call']['exception_chain'])
        self.assertIsNone(row['total_tokens'])
        self.assertNotIn('private-provider-text',json.dumps(row));self.assertNotIn('private.invalid',json.dumps(row))
