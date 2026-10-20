"""正式应用工厂与原生API链路：独立临时SQLite，全部模型和地图使用替身。"""
import asyncio
from contextlib import ExitStack, contextmanager, redirect_stdout
from datetime import datetime
import io
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api import main
from app.api.routes.trip import router
from app.config import Settings
from app.models.planning_runtime import PreviewProgress
from app.services.amap_contract_adapter import AmapContractAdapter
from app.services.current_route_service import CurrentRouteService
from app.services.native_preview_adapter import NativePreviewPipeline, NativePreviewError
import app.services.native_preview_adapter as native_module
from native_planning_fixtures import fixture_planner


class RouteMap:
    def __init__(self, fail=False): self.calls = []; self.fail = fail
    def __call__(self, name, args):
        self.calls.append((name, dict(args)))
        if self.fail: return {'result_status':'error', 'error_category':'provider_error', 'error_code':'MOCK_PROVIDER_ERROR'}
        transit = 'transit' in name; bike = 'bicycling' in name
        effective = {'origin':args.get('origin',args.get('origin_coordinates')),
                     'destination':args.get('destination',args.get('destination_coordinates'))}
        if transit:
            effective.update({key:args[key] for key in ('city','cityd','date','time','strategy')}); effective['extensions']='base'
        row = {'duration':'300', 'segments':[]} if transit else {'duration':'900','steps':[]}
        payload = {'route':{'transits':[row]}} if transit else {'data':{'paths':[row]}} if bike else {'route':{'paths':[row]}}
        return {**payload, 'result_status':'ok', 'result_source':{'api_version':'v4' if bike else 'v3',
            'queried_at':'2026-10-13T00:00:00+08:00', 'effective_params':effective}}


class NativePlanningApiTests(unittest.TestCase):
    @contextmanager
    def application(self, *, fail_routes=False, broken_planner=False):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            config = Settings(_env_file=None, task_db_path=str(Path(folder)/'tasks.db'),
                preview_db_path=str(Path(folder)/'previews.db'), destination_candidate_db_path=str(Path(folder)/'destinations.db'),
                planner_enforce_timeouts=False)
            route_map = RouteMap(fail_routes); routes = CurrentRouteService(adapter=AmapContractAdapter(route_map))
            gate = Event(); original = routes.run_async
            async def gated(*args, **kwargs):
                while not gate.is_set(): await asyncio.sleep(.005)
                return await original(*args, **kwargs)
            routes.run_async = gated
            planners = []
            def make_planner():
                req, planner, models = fixture_planner(reference_cost="88.50"); planners.append((planner, models))
                if broken_planner:
                    def fail(*a,**k): raise RuntimeError('private-model-exception')
                    models[0].invoke_native = fail
                return planner
            old = stack.enter_context(patch.object(main, 'get_trip_planner_agent', side_effect=AssertionError('legacy generation forbidden')))
            stack.enter_context(patch.object(main, 'settings', config))
            stack.enter_context(patch.object(main, 'validate_config'))
            stack.enter_context(patch.object(main, 'print_config'))
            stack.enter_context(patch.object(native_module, 'NativeTripPlanner', side_effect=make_planner))
            stack.enter_context(patch.object(native_module, 'CurrentRouteService', return_value=routes))
            stack.enter_context(patch.object(main, 'CurrentRouteService', return_value=routes))
            stack.enter_context(redirect_stdout(io.StringIO()))
            app = FastAPI(lifespan=main.lifespan); app.include_router(router, prefix='/api')
            with TestClient(app) as client:
                try: yield app, client, route_map, gate, planners, old
                finally: gate.set()

    def submit(self, client):
        req, _, _ = fixture_planner()
        response = client.post('/api/trip/v4/tasks', json=req.model_dump(mode='json'))
        self.assertEqual(response.status_code, 202, response.text)
        accepted=response.json(); headers={'Authorization':'Bearer '+accepted['access_token']}
        return accepted, headers

    def wait_ready(self, client, accepted, headers, expected='ready'):
        for _ in range(300):
            response=client.get('/api/trip/v4/tasks/'+accepted['task_id'], headers=headers)
            self.assertEqual(response.status_code,200); state=response.json()
            if state['status'] in ('ready','failed','interrupted'):
                self.assertEqual(state['status'],expected,state); return state
            time.sleep(.005)
        self.fail('模拟任务未结束')

    def preview(self, client, accepted, headers):
        response=client.get('/api/trip/v4/tasks/'+accepted['task_id']+'/preview',headers=headers)
        self.assertEqual(response.status_code,200,response.text); return response.json()

    def wait_routes(self, client, accepted, headers, calculation_id):
        for _ in range(400):
            response=client.get('/api/trip/v4/tasks/'+accepted['task_id']+'/edit-jobs/'+calculation_id,headers=headers)
            self.assertEqual(response.status_code,200,response.text)
            result=response.json()
            if result['job']['status'] in ('completed','failed','superseded'): return result
            time.sleep(.005)
        self.fail('模拟交通任务未结束')

    def test_formal_factory_202_native_generation_traffic_edit_save_restore_and_observation(self):
        with self.application() as (app,client,maps,gate,planners,old):
            self.assertIsInstance(app.state.planning_v4_pipeline,NativePreviewPipeline)
            self.assertEqual(app.state.planning_v4_pipeline.total_timeout_seconds,main.settings.planner_v4_total_timeout_seconds)
            accepted,headers=self.submit(client); self.wait_ready(client,accepted,headers)
            prefix='/api/trip/v4/tasks/'+accepted['task_id']
            original=self.preview(client,accepted,headers)
            self.assertEqual(original['cost']['days'][0]['lodging']['amount'], '88.50')
            self.assertEqual(original['cost']['days'][0]['lodging']['unit'], 'per_person_per_night_reference')
            self.assertEqual(original['plan_revision'],2); self.assertEqual(original['store_version'],1)
            self.assertEqual(original['preview']['draft']['source_run_id'],accepted['task_id'])
            self.assertEqual(original['preview']['calculation']['current_routes'],'pending'); self.assertFalse(maps.calls)
            self.assertEqual(client.get(prefix+'/preview').status_code,401)
            self.assertNotEqual(client.get(prefix+'/preview',headers={'Authorization':'Bearer wrong'}).status_code,200)
            self.assertEqual(client.post(prefix+'/final/save',headers=headers,json={'save_id':'pending',
                'source_store_version':original['store_version']}).status_code,200)
            gate.set(); self.wait_routes(client,accepted,headers,original['preview']['calculation']['calculation_id'])
            routed=self.preview(client,accepted,headers)
            self.assertEqual(routed['preview']['calculation']['current_routes'],'completed')
            leg=routed['preview']['draft']['legs'][0]
            self.assertEqual({m['mode'] for m in leg['mode_results'] if m['status']=='available'},{'transit','bicycling','walking'})
            walk=next(m for m in leg['mode_results'] if m['mode']=='walking')['option_ids'][0]
            edit=client.post(prefix+'/edits',headers=headers,json={'expected_store_version':routed['store_version'], 'command':{
                'plan_id':accepted['plan_id'],'day_id':leg['day_id'],'base_revision':routed['plan_revision'],'command_id':'walk',
                'operations':[{'type':'select_leg_mode','day_id':leg['day_id'],'leg_id':leg['leg_id'],'mode':'walking','option_id':walk}]}})
            self.assertEqual(edit.status_code,202,edit.text)
            envelope=edit.json()['envelope']
            self.wait_routes(client,accepted,headers,envelope['preview']['calculation']['calculation_id']) if envelope['preview']['calculation']['current_routes']=='pending' else None
            selected=self.preview(client,accepted,headers)
            self.assertEqual(selected['preview']['draft']['legs'][0]['selection_source'],'user')
            self.assertEqual(selected['preview']['draft']['legs'][0]['selected_mode'],'walking')
            saved=client.post(prefix+'/final/save',headers=headers,json={'save_id':'selected-save','source_store_version':selected['store_version']})
            self.assertEqual(saved.status_code,200,saved.text)
            day=selected['preview']['draft']['days'][0]; removed=day['activities'][0]
            command={'plan_id':accepted['plan_id'],'day_id':day['day_id'],'base_revision':selected['plan_revision'], 'command_id':'remove',
                'operations':[{'type':'remove_activity','day_id':day['day_id'],'activity_id':removed['activity_id']}]}
            edit=client.post(prefix+'/edits',headers=headers,json={'command':command,'expected_store_version':selected['store_version']})
            self.assertEqual(edit.status_code,202,edit.text); edited=edit.json()['envelope']
            self.wait_routes(client,accepted,headers,edited['preview']['calculation']['calculation_id'])
            edited=self.preview(client,accepted,headers)
            self.assertEqual(edited['plan_revision'],selected['plan_revision']+1)
            stale=client.post(prefix+'/edits',headers=headers,json={'command':{**command,'command_id':'stale-edit'},'expected_store_version':selected['store_version']})
            self.assertEqual(stale.status_code,409)
            restored=client.post(prefix+'/final/restore',headers=headers,json={'restore_id':'restore','expected_store_version':edited['store_version'],
                'expected_plan_revision':edited['plan_revision'],'expected_save_id':'selected-save'})
            self.assertEqual(restored.status_code,200,restored.text); restored=restored.json()['envelope']
            self.assertEqual(restored['cost']['days'][0]['lodging'], selected['cost']['days'][0]['lodging'])
            self.assertEqual(restored['plan_revision'],edited['plan_revision']+1)
            self.assertEqual(restored['preview']['draft']['days'],selected['preview']['draft']['days'])
            records=client.get('/api/trip/tasks/'+accepted['task_id']+'/spans').json()
            self.assertTrue(any(r['name']=='native.repair.revision' for r in records)); old.assert_not_called()
            self.assertEqual(len(planners),1)
            # 相邻交通不得重新调用规划模型；已完成规划包含三个子模型。
            self.assertTrue(all(m.calls>0 for m in planners[0][1]))

    def test_202_returns_before_planning_finishes_and_service_rejects_duplicate_active_task(self):
        with self.application() as (app,client,maps,gate,planners,old):
            entered=Event(); release=Event(); factory=app.state.planning_v4_pipeline.planner_factory
            def blocked_factory():
                planner=factory(); model=planners[-1][1][0]; invoke=model.invoke_native
                def blocked(*args,**kwargs):
                    entered.set()
                    if not release.wait(3): raise AssertionError('mock gate not released')
                    return invoke(*args,**kwargs)
                model.invoke_native=blocked
                return planner
            app.state.planning_v4_pipeline.planner_factory=blocked_factory
            try:
                accepted,headers=self.submit(client); self.assertTrue(entered.wait(1))
                prefix='/api/trip/v4/tasks/'+accepted['task_id']
                state=client.get(prefix,headers=headers).json()
                self.assertFalse(state['preview_ready']); self.assertEqual(state['stage'],'planning_input')
                self.assertEqual(client.get(prefix+'/preview',headers=headers).status_code,409)
                req,_,_=fixture_planner()
                self.assertEqual(client.post('/api/trip/v4/tasks',json=req.model_dump(mode='json')).status_code,409)
                self.assertEqual(len(planners),1)
                release.set(); self.wait_ready(client,accepted,headers); old.assert_not_called()
            finally: release.set()

    def test_failed_model_has_safe_failure_no_preview_and_no_legacy_fallback(self):
        with self.application(broken_planner=True) as (app,client,maps,gate,planners,old):
            accepted,headers=self.submit(client); state=self.wait_ready(client,accepted,headers,'failed')
            self.assertFalse(state['preview_ready']); self.assertFalse(maps.calls); old.assert_not_called()
            response=client.get('/api/trip/v4/tasks/'+accepted['task_id']+'/preview',headers=headers)
            self.assertEqual(response.status_code,409)
            self.assertNotIn('private-model-exception',str(state))
            self.assertEqual(state['error_code'],'MODEL_CALL_FAILED')

    def test_route_failure_keeps_preview_edit_and_save(self):
        with self.application(fail_routes=True) as (app,client,maps,gate,planners,old):
            accepted,headers=self.submit(client); self.wait_ready(client,accepted,headers); original=self.preview(client,accepted,headers)
            gate.set(); self.wait_routes(client,accepted,headers,original['preview']['calculation']['calculation_id'])
            latest=self.preview(client,accepted,headers); prefix='/api/trip/v4/tasks/'+accepted['task_id']
            self.assertTrue(maps.calls)
            self.assertTrue(all(m['status']=='error' for leg in latest['preview']['draft']['legs'] for m in leg['mode_results']))
            day=latest['preview']['draft']['days'][0]
            edit=client.post(prefix+'/edits',headers=headers,json={'expected_store_version':latest['store_version'],'command':{
                'plan_id':accepted['plan_id'],'day_id':day['day_id'],'base_revision':latest['plan_revision'],'command_id':'failed-routes-edit',
                'operations':[{'type':'remove_activity','day_id':day['day_id'],'activity_id':day['activities'][0]['activity_id']}]}})
            self.assertEqual(edit.status_code,202,edit.text); edited=edit.json()['envelope']
            self.wait_routes(client,accepted,headers,edited['preview']['calculation']['calculation_id'])
            latest=self.preview(client,accepted,headers)
            response=client.post(prefix+'/final/save',headers=headers,json={'save_id':'failed-routes-save','source_store_version':latest['store_version']})
            self.assertEqual(response.status_code,200,response.text); old.assert_not_called()

    def test_invalid_request_does_not_start_planning(self):
        with self.application() as (app,client,maps,gate,planners,old):
            response=client.post('/api/trip/v4/tasks',json={'city':'上海'})
            self.assertEqual(response.status_code,422); self.assertEqual(planners,[]); self.assertFalse(maps.calls)

    def test_preview_commit_failure_never_reports_ready(self):
        with self.application() as (app,client,maps,gate,planners,old):
            with patch.object(app.state.preview_repository,'complete_job',side_effect=RuntimeError('private-db-error')):
                accepted,headers=self.submit(client); state=self.wait_ready(client,accepted,headers,'failed')
            self.assertFalse(state['preview_ready']); self.assertFalse(maps.calls); old.assert_not_called()


class NativeDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_total_timeout_cancels_planner_and_refuses_delivery(self):
        signals=[]; adapter=Mock()
        class Planner:
            async def run_async(self, request, **kwargs):
                signals.append(kwargs['cancel_event']); await asyncio.Event().wait()
        pipeline=NativePreviewPipeline(planner_factory=Planner, adapter=adapter, enforce_timeouts=True,
            total_timeout_seconds=.02,initial_collection_timeout_seconds=1)
        req,_,_=fixture_planner()
        with self.assertRaises(NativePreviewError) as caught: await pipeline.run(req,task_id='task',plan_id='plan')
        self.assertEqual(caught.exception.code,'PIPELINE_DEADLINE'); self.assertTrue(signals[0].is_set()); adapter.convert.assert_not_called()

    async def test_initial_collection_timeout_is_separate_and_cancels_planner(self):
        signals=[]
        class Planner:
            async def run_async(self, request, **kwargs):
                signals.append(kwargs['cancel_event'])
                await kwargs['progress'](PreviewProgress(stage='dispatch'))
                await asyncio.Event().wait()
        req,_,_=fixture_planner()
        with self.assertRaises(NativePreviewError) as caught:
            await NativePreviewPipeline(planner_factory=Planner,enforce_timeouts=True,total_timeout_seconds=1,
                initial_collection_timeout_seconds=.02).run(req,task_id='task',plan_id='plan')
        self.assertEqual(caught.exception.code,'INITIAL_COLLECTION_TIMEOUT'); self.assertTrue(signals[0].is_set())

    async def test_disabled_timeout_uses_full_native_mock_chain(self):
        req,planner,_=fixture_planner(); routes=CurrentRouteService(adapter=AmapContractAdapter(RouteMap()))
        result=await NativePreviewPipeline(planner_factory=lambda:planner,current_route_service_factory=lambda:routes,
            enforce_timeouts=False,total_timeout_seconds=.000001,initial_collection_timeout_seconds=.000001).run(req,task_id='task',plan_id='plan')
        self.assertEqual(result.snapshot.preview.draft.revision,2); await result.route_context.aclose()

    async def test_collection_completion_restores_total_budget_for_daily_planning(self):
        class Planner:
            async def run_async(self, request, **kwargs):
                for completed in (0,1): await kwargs['progress'](PreviewProgress(stage='dispatch',completed=completed,total=1))
                await asyncio.sleep(.04)
                raise NativePreviewError('MOCK_AFTER_PROPOSAL','proposal')
        req,_,_=fixture_planner()
        with self.assertRaises(NativePreviewError) as caught:
            await NativePreviewPipeline(planner_factory=Planner,enforce_timeouts=True,total_timeout_seconds=1,
                initial_collection_timeout_seconds=.01).run(req,task_id='task',plan_id='plan')
        self.assertEqual(caught.exception.code,'MOCK_AFTER_PROPOSAL')

    async def test_invalid_deadline_configuration_rejected(self):
        for value in (True,0,-1,float('inf'),float('nan')):
            with self.assertRaises(ValueError): NativePreviewPipeline(total_timeout_seconds=value)


if __name__=='__main__': unittest.main()
