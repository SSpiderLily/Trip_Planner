import asyncio
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api.routes.trip import router
from app.services.task_repository import TaskRepository
from app.services.task_service import TaskService, TaskError
from app.services.observation_service import ObservationService, span, bounded
from test_planning_contract import request
from app.models.schemas import TripPlan, DayPlan


def result(req):
    return TripPlan(city=req.city, start_date=req.start_date, end_date=req.end_date,
        days=[DayPlan(date=req.start_date,day_index=0,description='游览',transportation='步行',accommodation='酒店')],overall_suggestions='建议')


class FakePlanner:
    def plan_trip(self, req):
        with span('agent.fake','agent',{'city':req.city}) as record:
            record.output='完成'
        return result(req)


class RuntimeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.repo=TaskRepository(Path(self.tmp.name)/'tasks.db')
        self.repo.initialize()
        self.obs=ObservationService(self.repo)
        self.service=TaskService(self.repo,self.obs,FakePlanner)

    async def asyncTearDown(self):
        await self.service.close()
        self.tmp.cleanup()

    async def test_success_persists_tree_result_and_light_status(self):
        accepted=await self.service.submit(request())
        await self.service.close()
        status=await self.service.status(accepted['task_id'])
        self.assertEqual(status['status'],'succeeded')
        self.assertNotIn('request_data',status)
        self.assertEqual((await self.service.result(accepted['task_id']))['city'],'北京')
        rows=self.repo.spans(accepted['task_id'])
        self.assertEqual(len(rows),3)
        self.assertTrue(all(row['status']=='succeeded' for row in rows))

    async def test_two_simultaneous_submissions_only_one_accepted(self):
        gate=threading.Event()
        class Slow:
            def plan_trip(self,req):
                gate.wait(3)
                return result(req)
        self.service.planner_factory=Slow
        try:
            results=await asyncio.gather(self.service.submit(request()),self.service.submit(request()),return_exceptions=True)
            self.assertEqual(sum(isinstance(r,TaskError) and r.code=='TASK_BUSY' for r in results),1)
            self.assertEqual(len(self.repo.list_tasks()),1)
        finally:
            gate.set()

    async def test_registration_failure_never_calls_planner(self):
        with patch.object(self.repo,'create',side_effect=sqlite3.OperationalError()),patch.object(self.service,'planner_factory') as factory:
            with self.assertRaises(TaskError):
                await self.service.submit(request())
            factory.assert_not_called()
            self.assertIsNone(self.service.active_id)

    async def test_recording_failure_does_not_fail_business(self):
        with patch.object(self.repo,'begin_span',side_effect=sqlite3.OperationalError()):
            accepted=await self.service.submit(request())
            await self.service.close()
        status=await self.service.status(accepted['task_id'])
        self.assertEqual(status['status'],'succeeded')
        self.assertTrue(status['observation_incomplete'])

    async def test_result_save_failure_not_success(self):
        with patch.object(self.repo,'succeed',side_effect=sqlite3.OperationalError()):
            accepted=await self.service.submit(request())
            await self.service.close()
        status=await self.service.status(accepted['task_id'])
        self.assertEqual(status['status'],'failed')
        self.assertEqual(status['error_code'],'RESULT_SAVE_FAILED')
        self.assertIsNone(self.repo.result(accepted['task_id']))

    async def test_recovery_and_cascade(self):
        self.repo.create('old',{});self.repo.start('old')
        self.repo.begin_span(('parent','old',None,'planning','planning','2020','{}',0))
        self.repo.begin_span(('child','old','parent','llm.invoke','llm','2020','{}',0))
        self.repo.recover()
        status=self.repo.status('old')
        self.assertEqual(status['status'],'interrupted')
        self.assertIsNone(status['finished_at'])
        self.assertTrue(all(s['status']=='interrupted' for s in self.repo.spans('old')))
        with self.repo.connection() as conn:
            conn.execute("DELETE FROM tasks WHERE task_id='old'")
        self.assertEqual(self.repo.spans('old'),[])

    async def test_cross_task_parent_rejected(self):
        self.repo.create('a',{});self.repo.create('b',{})
        self.repo.begin_span(('parent','a',None,'planning','planning','2020','{}',0))
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.begin_span(('child','b','parent','llm.invoke','llm','2020','{}',0))

    async def test_capacity_keeps_active_task(self):
        self.repo.create('active',{});self.repo.max_bytes=1
        self.repo.cleanup()
        self.assertIsNotNone(self.repo.status('active'))
        with self.obs.task('active'),span('agent.test','agent','input'):
            pass
        self.assertTrue(self.repo.status('active')['observation_incomplete'])

    async def test_result_transaction_rolls_back_on_insert_error(self):
        self.repo.create('rollback', {})
        self.repo.start('rollback')
        with self.repo.connection() as conn:
            conn.execute("CREATE TRIGGER reject_result BEFORE INSERT ON task_results BEGIN SELECT RAISE(ABORT, 'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.succeed('rollback', {})
        self.assertEqual(self.repo.status('rollback')['status'], 'running')
        self.assertIsNone(self.repo.result('rollback'))

    async def test_cancelled_submitter_does_not_cancel_execution(self):
        started, release = threading.Event(), threading.Event()
        original = self.repo.create
        def delayed_create(*args):
            started.set()
            release.wait(3)
            original(*args)
        with patch.object(self.repo, 'create', side_effect=delayed_create):
            caller = asyncio.create_task(self.service.submit(request()))
            await asyncio.to_thread(started.wait, 3)
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await caller
            release.set()
            await self.service.close()
        self.assertEqual(self.repo.list_tasks()[0]['status'], 'succeeded')

    async def test_total_write_failure_visible_without_backup_result(self):
        with patch.object(self.repo,'succeed',side_effect=sqlite3.OperationalError()), patch.object(self.repo,'fail',side_effect=sqlite3.OperationalError()):
            accepted = await self.service.submit(request())
            await self.service.close()
        status = await self.service.status(accepted['task_id'])
        self.assertEqual(status['status'], 'failed')
        self.assertEqual(status['persisted_status'], 'running')
        self.assertEqual(status['persistence_error']['code'], 'RESULT_SAVE_FAILED')
        self.assertEqual((await self.service.list_tasks('failed'))[0]['status'], 'failed')
        self.assertEqual(await self.service.list_tasks('running'), [])
        with patch.object(self.repo,'status',side_effect=sqlite3.OperationalError()):
            self.assertEqual((await self.service.status(accepted['task_id']))['status'], 'failed')

    async def test_redaction_and_utf8_truncation(self):
        with patch.dict(os.environ,{'LLM_API_KEY':'test-secret-value'}):
            data,cut=bounded({'token':'abc','message':'test-secret-value '+'旅'*500},256)
        self.assertTrue(cut)
        self.assertLessEqual(len(data.encode()),256)
        self.assertNotIn('test-secret-value',data);self.assertNotIn('abc',data)
        json.loads(data)
        quoted, _ = bounded('响应包含 {"api_key":"example-private-value"}', 1024)
        self.assertNotIn('example-private-value', quoted)


class ApiTest(unittest.TestCase):
    def test_input_rejected_before_submission(self):
        app=FastAPI();app.include_router(router,prefix='/api')
        with TestClient(app) as client:
            data=request().model_dump();data['travel_days']=2
            self.assertEqual(client.post('/api/trip/tasks',json=data).status_code,422)

class FullChainTest(unittest.TestCase):
    def test_real_agent_loop_records_model_tool_rounds(self):
        from unittest.mock import Mock
        from app.agents.trip_planner_agent import MultiAgentTripPlanner
        from test_trip_planner_agent import FakeMCPTool
        llm=Mock()
        llm.model='fixture'
        llm.invoke.side_effect=[
            '[TOOL_CALL:amap_maps_text_search:keywords=景点,city=北京]', '景点总结',
            '[TOOL_CALL:amap_maps_weather:city=北京]', '天气总结',
            '[TOOL_CALL:amap_maps_text_search:keywords=酒店,city=北京]', '酒店总结',
            result(request()).model_dump_json()]
        with patch('app.agents.trip_planner_agent.MCPTool',FakeMCPTool),patch('app.agents.trip_planner_agent.get_llm',return_value=llm):
            planner=MultiAgentTripPlanner()
        for tool in planner.amap_tools:
            tool.run=Mock(return_value=json.dumps({'forecasts':[{'casts':[{'date':'2026-09-25','dayweather':'晴','nightweather':'晴','daytemp':'20','nighttemp':'10'}]}]} if tool.name.endswith('weather') else {'pois':[{'name':'真实测试地点'}]}))
        with tempfile.TemporaryDirectory() as directory:
            repo=TaskRepository(Path(directory)/'db');repo.initialize();repo.create('chain',{})
            obs=ObservationService(repo)
            with obs.task('chain'):
                plan=planner.plan_trip(request())
            rows=repo.spans('chain')
            self.assertEqual(sum(s['operation_type']=='llm' for s in rows),7)
            self.assertEqual(sum(s['operation_type']=='tool' for s in rows),3)
            self.assertTrue(all(s['status']=='succeeded' for s in rows))
            self.assertEqual(plan.weather_info[0].day_weather,'天气未知')
            self.assertIsNone(plan.weather_info[0].day_temp)
            model_spans=[s for s in rows if s['operation_type']=='llm']
            second=repo.span('chain',model_spans[1]['span_id'])
            self.assertIn('工具执行结果',json.dumps(second['input_data'],ensure_ascii=False))
            self.assertEqual(len({s['parent_span_id'] for s in model_spans}),4)

class HttpLifecycleTest(unittest.TestCase):
    def test_accept_busy_live_query_success_and_terminal_error(self):
        from contextlib import asynccontextmanager
        import time
        gate = threading.Event()
        class SlowPlanner:
            def plan_trip(self, req):
                with span('agent.wait', 'agent'):
                    gate.wait(5)
                return result(req)
        with tempfile.TemporaryDirectory() as directory:
            repo = TaskRepository(Path(directory)/'db')
            @asynccontextmanager
            async def lifespan(app):
                repo.initialize()
                app.state.task_service = TaskService(repo,ObservationService(repo),SlowPlanner)
                yield
                gate.set()
                await app.state.task_service.close()
            app=FastAPI(lifespan=lifespan); app.include_router(router,prefix='/api')
            with TestClient(app) as client:
                accepted=client.post('/api/trip/tasks',json=request().model_dump())
                self.assertEqual(accepted.status_code,202)
                tid=accepted.json()['task_id']; url='/api/trip/tasks/'+tid
                self.assertEqual(client.post('/api/trip/tasks',json=request().model_dump()).status_code,409)
                self.assertEqual(client.get(url).status_code,200)
                self.assertEqual(client.get(url+'/result').json()['detail']['code'],'RESULT_NOT_READY')
                self.assertEqual(client.get('/api/trip/tasks/missing').status_code,404)
                gate.set()
                for _ in range(100):
                    status=client.get(url).json()
                    if status['status']=='succeeded': break
                    time.sleep(.01)
                self.assertEqual(status['status'],'succeeded')
                self.assertEqual(client.get(url+'/result').json()['data']['city'],'北京')
                self.assertTrue(client.get(url+'/spans').json())
                repo.create('failed',{});repo.fail('failed','TEST_FAILURE','测试失败')
                self.assertEqual(client.get('/api/trip/tasks/failed').status_code,200)
                self.assertEqual(client.get('/api/trip/tasks/failed/result').json()['detail']['code'],'TASK_FAILED')
