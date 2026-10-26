"""独立链路必须落到原SQLite，模型消息/异常原文不得进入观测。"""
import json
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch
from app.models.planning_runtime import PlanningRunIdentity
from app.services.collected_planning_service import run_collected_request
from app.services.observation_service import ObservationService
from app.services.task_repository import TaskRepository
from app.services.native_collection_adapter import NativeRequirementAdapter, NativeCollectionSession
from app.services.planning_runtime_adapter import SynchronousPlanningRuntime
from app.agents.native_daily_plan_agent import NativeDailyPlanAgent
from native_planning_fixtures import fixture_planner

class CollectedObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.repo=TaskRepository(Path(self.tmp.name)/'tasks.db');self.repo.initialize()
        self.obs=ObservationService(self.repo)

    def run_case(self,task='observed',generator=None,runtime=None):
        req,planner,models=fixture_planner(repair=False)
        return run_collected_request(req,identity=PlanningRunIdentity(task_id=task,plan_id=task+'-plan'),
            requirements=NativeRequirementAdapter(interpreter=planner.requirement_interpreter),
            collection=NativeCollectionSession(collector=planner.collector),
            generator=generator or NativeDailyPlanAgent(models[0]),runtime=runtime or SynchronousPlanningRuntime(),observation=self.obs)

    def details(self,task='observed'):
        return [self.repo.span(task,s['span_id']) for s in self.repo.spans(task)]

    def test_all_stage_and_nested_model_records_use_existing_repository(self):
        record=self.run_case();rows=self.details();names={s['name'] for s in rows}
        self.assertTrue(record['delivered']);self.assertEqual(record['identity']['task_id'],'observed')
        for stage in ('requirements','collection','generation','validation','evaluation'):
            self.assertIn('planning.modular.'+stage,names)
        self.assertEqual(self.repo.status('observed')['status'],'succeeded')
        ids={r['span_id'] for r in rows}
        self.assertTrue(all(r['parent_span_id'] in ids for r in rows if r['parent_span_id']))
        calls=[s for s in rows if s['operation_type']=='llm']
        self.assertEqual(len(calls),11)
        self.assertTrue(all(s['input_data']['task_id']=='observed' and s['input_data']['plan_id']=='observed-plan' for s in calls))
        text=json.dumps(rows)
        for secret in ('private-reasoning','private-freeform-response'):
            self.assertNotIn(secret,text)
        self.assertFalse(record['observation']['incomplete'])

    def test_failed_call_keeps_nested_transport_cause_and_unknown_usage(self):
        from test_native_model_retry import model,connection_error
        llm,_,_=model(*(connection_error() for _ in range(3)))
        with patch('app.services.native_tool_loop.NativeToolLoop._wait_before_retry'):
            r=self.run_case(generator=NativeDailyPlanAgent(llm))
        self.assertFalse(r['delivered']);self.assertIsNotNone(r['snapshot'])
        self.assertEqual(self.repo.status('observed')['error_step'],'generation')
        self.assertEqual(self.repo.status('observed')['status'],'failed')
        failed=[s for s in self.details() if s['operation_type']=='llm' and s['status']=='failed']
        self.assertEqual(len(failed),3)
        for row in failed:
            self.assertIsNone(row['total_tokens'])
            self.assertIn('RemoteProtocolError',row['output_data']['model_call']['exception_chain'])
        self.assertEqual(len(r['failure_details']),3)
        self.assertNotIn('private-remote-error',json.dumps(r['failure_details']))

    def test_cancelled_run_is_failed_and_no_state_is_published_as_success(self):
        event=Event();event.set()
        r=self.run_case(runtime=SynchronousPlanningRuntime(cancel_event=event))
        self.assertFalse(r['delivered']);self.assertEqual(r['error_code'],'CANCELLED')
        self.assertEqual(self.repo.status('observed')['status'],'failed')
        self.assertNotIn('planning.modular.collection',{s['name'] for s in self.details()})

    def test_span_write_failure_marks_incomplete_without_changing_planning(self):
        with patch.object(self.repo,'begin_span',side_effect=RuntimeError('private-storage')):
            r=self.run_case()
        self.assertTrue(r['delivered']);self.assertTrue(r['observation']['incomplete'])
        self.assertTrue(self.repo.status('observed')['observation_incomplete'])

    def test_attaching_observation_store_does_not_recover_another_active_task(self):
        self.repo.create('other-active',{});self.repo.start('other-active')
        second=TaskRepository(self.repo.path);second.initialize(recover_running=False)
        self.assertEqual(second.status('other-active')['status'],'running')

    def test_parallel_runs_keep_parent_links_and_identity_separate(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda task:self.run_case(task),('one','two')))
        self.assertTrue(all(r['delivered'] for r in results))
        for task in ('one','two'):
            rows=self.details(task);ids={row['span_id'] for row in rows}
            self.assertTrue(all(row['parent_span_id'] in ids for row in rows if row['parent_span_id']))
            self.assertTrue(all(row['input_data'].get('task_id',task)==task for row in rows if isinstance(row['input_data'],dict)))

    def test_rejected_submission_can_correct_without_failing_whole_task(self):
        from test_planning_core_contracts import snapshot,load_case
        from app.models.planning_execution import ProposalSubmission
        from app.models.planning_errors import InvalidPlanningReferences
        s=snapshot();sub=ProposalSubmission.model_validate(load_case()['submission'])
        class Requirements:
            def run(self,*args,**kwargs):return s.planning_input
        class Collection:
            def collect(self,*args,**kwargs):return s
        class Correcting:
            def generate(self,context,*,accept,runtime):
                day=sub.days[0];activity=day.activities[0].model_copy(update={'candidate_ref':'foreign'})
                bad=sub.model_copy(update={'days':(day.model_copy(update={'activities':(activity,*day.activities[1:])}),*sub.days[1:])})
                try:accept(bad)
                except Exception:pass
                return accept(sub)
        r=run_collected_request(s.planning_input.request,identity=s.identity,requirements=Requirements(),
            collection=Collection(),generator=Correcting(),runtime=SynchronousPlanningRuntime(),observation=self.obs)
        self.assertTrue(r['delivered'])
        validations=[row for row in self.details(s.identity.task_id) if row['name']=='planning.modular.validation']
        self.assertEqual([row['status'] for row in validations],['failed','succeeded'])
        self.assertEqual(self.repo.status(s.identity.task_id)['status'],'succeeded')

    def test_duplicate_identity_does_not_append_or_change_previous_task(self):
        from app.models.planning_errors import PlanningError
        self.run_case();before=self.details()
        with self.assertRaises(PlanningError) as cm:self.run_case()
        self.assertEqual(cm.exception.code,'OBSERVATION_IDENTITY_CONFLICT')
        self.assertEqual(self.details(),before)
        self.assertEqual(self.repo.status('observed')['status'],'succeeded')

    def test_cancellation_during_evaluation_cannot_publish_success(self):
        from app.planning.evaluation import evaluate_draft
        event=Event();runtime=SynchronousPlanningRuntime(cancel_event=event)
        def late(*args,**kwargs):
            result=evaluate_draft(*args,**kwargs);event.set();return result
        with patch('app.services.collected_planning_service.evaluate_draft',side_effect=late):
            r=self.run_case(runtime=runtime)
        self.assertFalse(r['delivered']);self.assertEqual(r['error_code'],'CANCELLED')
        self.assertEqual(self.repo.status('observed')['status'],'failed')

    def test_existing_task_span_and_metrics_http_endpoints_read_modular_run(self):
        import asyncio,httpx
        from fastapi import FastAPI
        from app.api.routes.trip import router
        from app.services.task_service import TaskService
        self.run_case();app=FastAPI();app.include_router(router,prefix='/api')
        app.state.task_service=TaskService(self.repo,self.obs,lambda:None)
        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as client:
                self.assertEqual((await client.get('/api/trip/tasks/observed')).json()['status'],'succeeded')
                spans=await client.get('/api/trip/tasks/observed/spans');self.assertEqual(spans.status_code,200)
                selected=next(row for row in spans.json() if row['name']=='planning.modular.evaluation')
                detail=await client.get('/api/trip/tasks/observed/spans/'+selected['span_id'])
                self.assertEqual(detail.status_code,200)
                self.assertEqual((await client.get('/api/trip/tasks/observed/metrics')).status_code,200)
                self.assertEqual((await client.get('/api/trip/tasks/foreign/spans/'+selected['span_id'])).status_code,404)
        asyncio.run(check())

    def test_p4_revision_records_request_stage_and_parent_identity(self):
        from app.agents.native_day_revision_agent import NativeDayRevisionAgent
        req,planner,models=fixture_planner(repair=True)
        r=run_collected_request(req,identity=PlanningRunIdentity(task_id='p4-observed',plan_id='p4-plan'),
            requirements=NativeRequirementAdapter(interpreter=planner.requirement_interpreter),
            collection=NativeCollectionSession(collector=planner.collector),generator=NativeDailyPlanAgent(models[0]),
            revision_generator=NativeDayRevisionAgent(models[0]),runtime=SynchronousPlanningRuntime(),observation=self.obs)
        self.assertTrue(r['delivered']);rows=self.details('p4-observed');ids={row['span_id'] for row in rows}
        self.assertTrue(all(row['parent_span_id'] in ids for row in rows if row['parent_span_id']))
        revision=[row for row in rows if row['operation_type']=='llm' and row['input_data'].get('stage')=='revision']
        self.assertEqual(len(revision),1)
        self.assertEqual(revision[0]['input_data']['task_id'],'p4-observed')
        self.assertEqual(revision[0]['input_data']['plan_id'],'p4-plan')
        self.assertEqual(self.repo.status('p4-observed')['status'],'succeeded')
        self.assertNotIn('private-reasoning',json.dumps(rows))
