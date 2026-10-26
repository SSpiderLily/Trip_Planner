"""新证据边界不能丢弃原始执行结果；损坏投影必须与旧入口同样拒绝。"""
from dataclasses import replace
import unittest
from unittest.mock import Mock
from app.models.planning_evidence import WeatherEvidence, EndpointEvidence
from app.planning.collection_evidence import validate_weather_evidence, validate_endpoint_evidence
from app.services.native_weather_service import NativeWeatherService, validate_native_weather_result
from app.services.native_endpoint_service import NativeEndpointService, validate_native_endpoint_result
from app.services.amap_contract_adapter import AmapContractAdapter
from test_native_weather import planning, raw_weather, forecast
from test_native_endpoints import planning as endpoint_planning, raw, poi


class EvidenceTests(unittest.TestCase):
    def test_weather_roundtrip_and_tamper_match_legacy(self):
        for payload in (raw_weather([forecast('2026-10-20')]), raw_weather([]), {'result_status':'error'}):
            with self.subTest(payload=payload):
                p = planning()
                old = NativeWeatherService(adapter=AmapContractAdapter(Mock(return_value=payload))).run(p, run_id='task')
                evidence = WeatherEvidence.model_validate({k:getattr(old,k) for k in ('run_id','planning_input','executions','fact_store')})
                evidence = WeatherEvidence.model_validate_json(evidence.model_dump_json())
                validate_native_weather_result(p,'task',old)
                validate_weather_evidence(p,'task',evidence)
                bad_execution = old.executions[0].model_copy(update={'run_id':'other'})
                bad_old = replace(old, executions=(bad_execution,))
                bad_new = evidence.model_copy(update={'executions':(bad_execution,)})
                for validate, value in ((validate_native_weather_result,bad_old),(validate_weather_evidence,bad_new)):
                    with self.assertRaisesRegex(Exception,'天气事实结果无效'):
                        validate(p,'task',value)

    def test_endpoints_roundtrip_and_tamper_match_legacy(self):
        for payload in (raw([poi()]), raw([]), {'result_status':'error'}):
            with self.subTest(payload=payload):
                p=endpoint_planning()
                old=NativeEndpointService(adapter=AmapContractAdapter(Mock(return_value=payload))).run(p,run_id='task')
                evidence=EndpointEvidence.model_validate({k:getattr(old,k) for k in ('run_id','planning_input','executions','arrival','departure','fact_store')})
                evidence=EndpointEvidence.model_validate_json(evidence.model_dump_json())
                validate_native_endpoint_result(p,'task',old)
                validate_endpoint_evidence(p,'task',evidence)
                bad=old.executions[0].model_copy(update={'city':'北京'})
                for validate, value in ((validate_native_endpoint_result,replace(old,executions=(bad,))),
                                        (validate_endpoint_evidence,evidence.model_copy(update={'executions':(bad,)}))):
                    with self.assertRaisesRegex(Exception,'到离地点事实结果无效'):
                        validate(p,'task',value)

    def test_legacy_invalid_type_keeps_projection_diagnostic_stage(self):
        for validate, p, code, stage in (
            (validate_native_weather_result,planning(),'WEATHER_RESULT_INVALID','weather_projection'),
            (validate_native_endpoint_result,endpoint_planning(),'ENDPOINT_RESULT_INVALID','endpoint_projection')):
            with self.assertRaises(Exception) as cm:validate(p,'task',object())
            self.assertEqual(cm.exception.code,code)
            self.assertEqual(cm.exception.step,stage)
