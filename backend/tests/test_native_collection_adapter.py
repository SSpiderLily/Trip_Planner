"""替换资料提供器不能要求生成器认识采集历史或私有状态。"""
from dataclasses import replace
from types import SimpleNamespace
from threading import Event
import unittest
from unittest.mock import Mock
from app.services.native_collection_adapter import NativeRequirementAdapter, NativeCollectionSession
from app.agents.native_multiagent_collection import NativeMultiagentCollectionResult
from app.models.planning_execution import PlanningSnapshot
from app.models.planning_errors import PlanningError
from app.planning.core import generate_draft
from app.services.planning_runtime_adapter import SynchronousPlanningRuntime
from app.services.native_weather_service import NativeWeatherService
from app.services.native_endpoint_service import NativeEndpointService
from app.services.amap_contract_adapter import AmapContractAdapter
from app.services.planning_input_builder import build_planning_input, PlanningInputBuildRequest
from app.services.planning_v4_rules import default_planning_v4_rules
from test_planning_core import Generator
from test_planning_core_contracts import snapshot


def prepared():
    s=snapshot()
    p=build_planning_input(PlanningInputBuildRequest(request=s.planning_input.request,
        requirements=s.planning_input.requirements,rules=default_planning_v4_rules()))
    return s.model_copy(update={'planning_input':p})


def collection(s):
    mapper=AmapContractAdapter(Mock(return_value={'result_status':'ok','forecasts':[],'pois':[]}))
    weather=NativeWeatherService(adapter=mapper).run(s.planning_input,run_id=s.identity.task_id)
    endpoints=NativeEndpointService(adapter=mapper).run(s.planning_input,run_id=s.identity.task_id)
    return NativeMultiagentCollectionResult(s.identity.task_id,'private summary',s.candidates,s.facts,(),s.area_hints,
        0,0,s.planning_input,protocol=None,weather=weather,endpoints=endpoints)


class AdapterTests(unittest.TestCase):
    def test_requirement_boundary_only_returns_input(self):
        s=prepared(); interpreter=Mock()
        interpreter.run.return_value=SimpleNamespace(summary=s.planning_input.requirements,
            protocol=SimpleNamespace(messages=[{'content':'SECRET_HISTORY'}]))
        p=NativeRequirementAdapter(interpreter=interpreter).run(s.planning_input.request,
            runtime=SynchronousPlanningRuntime())
        self.assertEqual(p,s.planning_input)
        self.assertFalse(hasattr(p,'protocol'))

    def test_same_core_accepts_normalized_provider_without_private_history(self):
        s=prepared(); state=collection(s); collector=Mock();collector.run.return_value=state
        session=NativeCollectionSession(collector=collector)
        normalized=session.collect(s.planning_input,identity=s.identity,runtime=SynchronousPlanningRuntime())
        kwargs=collector.run.call_args.kwargs
        self.assertNotIn('messages',kwargs)
        self.assertEqual(kwargs['requirements'],s.planning_input.requirements)
        self.assertFalse(any(k in normalized.model_dump() for k in ('children','protocol','query_cache')))
        self.assertEqual(normalized.schema_version,2)
        # 固定与采集提供器给出相同资料时，同一个生成器产出相同草稿/报告。
        for value in (s,normalized):
            result=generate_draft(value,generator=Generator(),runtime=SynchronousPlanningRuntime())
            self.assertEqual(result.draft.days[0].day_id,s.planning_input.days[0].day_id)
        self.assertEqual(generate_draft(s,generator=Generator(),runtime=SynchronousPlanningRuntime()),
                         generate_draft(normalized,generator=Generator(),runtime=SynchronousPlanningRuntime()))
        self.assertEqual(PlanningSnapshot.model_validate_json(normalized.model_dump_json()),normalized)

    def test_input_identity_or_evidence_mismatch_is_rejected(self):
        s=prepared(); state=collection(s)
        for bad in (replace(state,run_id='other'),replace(state,planning_input=snapshot().planning_input),
                    replace(state,weather=None),replace(state,endpoints=None)):
            with self.subTest(bad=bad.run_id):
                collector=Mock();collector.run.return_value=bad
                with self.assertRaises(Exception):
                    NativeCollectionSession(collector=collector).collect(s.planning_input,identity=s.identity,
                        runtime=SynchronousPlanningRuntime())

    def test_cancel_or_failure_does_not_publish_or_reuse_state(self):
        s=prepared(); event=Event();runtime=SynchronousPlanningRuntime(cancel_event=event)
        collector=Mock()
        def late(*args,**kwargs):
            event.set();return collection(s)
        collector.run.side_effect=late
        session=NativeCollectionSession(collector=collector)
        with self.assertRaises(PlanningError):session.collect(s.planning_input,identity=s.identity,runtime=runtime)
        event.clear();collector.run.side_effect=RuntimeError('private')
        with self.assertRaises(PlanningError) as cm:session.collect(s.planning_input,identity=s.identity,runtime=runtime)
        self.assertEqual(cm.exception.code,'COLLECTION_FAILED')
        collector.run.side_effect=None;collector.run.return_value=collection(s)
        session.collect(s.planning_input,identity=s.identity,runtime=runtime)
        self.assertNotIn('initial_state',collector.run.call_args.kwargs)

class RealCollectorBoundaryTests(unittest.TestCase):
    def test_interpreter_collector_core_use_separate_contexts(self):
        from native_planning_fixtures import fixture_planner
        from app.agents.native_daily_plan_agent import NativeDailyPlanAgent
        from app.models.planning_runtime import PlanningRunIdentity
        req, planner, models=fixture_planner(repair=False)
        runtime=SynchronousPlanningRuntime()
        captured=[]
        original=models[0].invoke_native
        def capture(messages,**kwargs):
            captured.append(messages)
            return original(messages,**kwargs)
        models[0].invoke_native=capture
        p=NativeRequirementAdapter(interpreter=planner.requirement_interpreter).run(req,runtime=runtime)
        session=NativeCollectionSession(collector=planner.collector)
        s=session.collect(p,identity=PlanningRunIdentity(task_id='p3-task',plan_id='p3-plan'),runtime=runtime)
        result=generate_draft(s,generator=NativeDailyPlanAgent(models[0]),runtime=runtime)
        self.assertEqual(len(captured[1]),2)  # 采集从本阶段system/user开始。
        self.assertEqual(len(captured[-1]),2)  # 生成不携带采集工具历史。
        self.assertNotIn('private-freeform-response',str(captured[-1]))
        self.assertEqual(len(s.task_results),3)
        self.assertEqual({c.category for c in s.candidates},{'sightseeing','meal','lodging'})
        self.assertFalse(any(issue.triggers_repair for issue in result.report.issues))
        self.assertEqual(session.collect(p,identity=s.identity,runtime=runtime),s)

    def test_four_states_keep_missing_and_technical_error_distinct(self):
        from native_planning_fixtures import fixture_planner, call, Model
        from app.models.planning_runtime import PlanningRunIdentity
        for mode in ('undispatched','empty','failed','candidates'):
            with self.subTest(mode=mode):
                req,planner,models=fixture_planner(repair=False)
                if mode=='undispatched':models[0].responses.pop(2)
                elif mode=='empty':
                    planner.collector.models['dining']=Model('fake',call('submit_restaurants',
                        {'selections':[],'unmet_needs':['没有查询结果']},'empty'))
                elif mode=='failed':
                    planner.collector.models['dining']=Model('fake',lambda _: (_ for _ in ()).throw(RuntimeError('fixture')))
                runtime=SynchronousPlanningRuntime()
                p=NativeRequirementAdapter(interpreter=planner.requirement_interpreter).run(req,runtime=runtime)
                s=NativeCollectionSession(collector=planner.collector).collect(p,
                    identity=PlanningRunIdentity(task_id='p3-task',plan_id='p3-plan'),runtime=runtime)
                meals=[r for r in s.task_results if any(c.category=='meal' for c in r.candidates)]
                if mode=='undispatched':self.assertEqual(len(s.task_results),1)
                elif mode=='candidates':self.assertTrue(meals)
                else:
                    dining_result=s.task_results[1]
                    self.assertFalse(dining_result.candidates)
                    self.assertEqual(dining_result.status,'failed' if mode=='failed' else 'partial')
                    self.assertTrue(any(note.task_id==dining_result.task_id and note.unmet_needs for note in s.task_notes))

class SessionIsolationTests(unittest.TestCase):
    def test_successful_session_rejects_cross_run_and_copies_results(self):
        s=prepared();collector=Mock();collector.run.return_value=collection(s)
        session=NativeCollectionSession(collector=collector);runtime=SynchronousPlanningRuntime()
        value=session.collect(s.planning_input,identity=s.identity,runtime=runtime)
        value.facts.sources[0].effective_params['city']='tampered'
        self.assertNotEqual(session.collect(s.planning_input,identity=s.identity,runtime=runtime).facts,value.facts)
        other=s.identity.model_copy(update={'task_id':'other'})
        with self.assertRaisesRegex(PlanningError,'跨请求'):
            session.collect(s.planning_input,identity=other,runtime=runtime)
        self.assertEqual(collector.run.call_count,1)

    def test_evidence_fact_conflict_is_not_published(self):
        from app.planning.validation import validate_snapshot
        s=prepared();collector=Mock();collector.run.return_value=collection(s)
        value=NativeCollectionSession(collector=collector).collect(s.planning_input,identity=s.identity,runtime=SynchronousPlanningRuntime())
        weather=value.weather_evidence
        # 相同事实ID但更改快照内容，证据不能充当一个可绕过的verified标记。
        if value.facts.weather:
            bad_weather=value.facts.weather[0].model_copy(update={'city':'北京'})
            bad=value.model_copy(update={'weather_evidence':weather.model_copy(update={
                'fact_store':weather.fact_store.model_copy(update={'weather':(bad_weather,)})})})
            with self.assertRaises(Exception):validate_snapshot(bad)

    def test_task_note_must_reference_its_own_task(self):
        from app.models.planning_execution import CollectionTaskNote
        from app.planning.validation import validate_snapshot
        s=prepared().model_copy(update={'task_notes':(CollectionTaskNote(task_id='foreign-task',unmet_needs=('unknown',)),)})
        with self.assertRaises(Exception):validate_snapshot(s)

class TaskNotesDeliveryTests(unittest.TestCase):
    def test_collection_notes_reach_native_planning_model_without_history(self):
        from native_planning_fixtures import fixture_planner
        from app.agents.native_daily_plan_agent import NativeDailyPlanAgent
        from app.models.planning_runtime import PlanningRunIdentity
        req, planner, models = fixture_planner(repair=False)
        runtime = SynchronousPlanningRuntime()
        planning = NativeRequirementAdapter(interpreter=planner.requirement_interpreter).run(req, runtime=runtime)
        original_collect = planner.collector.run
        note = '餐馆缺少无障碍入口证据，请保留未知'
        def collect(*args, **kwargs):
            state = original_collect(*args, **kwargs)
            children = list(state.children)
            children[1] = replace(children[1], unmet_needs=(note,))
            return replace(state, children=tuple(children))
        collector = Mock()
        collector.run.side_effect = collect
        snapshot = NativeCollectionSession(collector=collector).collect(planning,
            identity=PlanningRunIdentity(task_id='notes-task', plan_id='notes-plan'), runtime=runtime)
        before = snapshot.model_dump(mode='json')
        captured = []
        original_invoke = models[0].invoke_native
        def capture(messages, **kwargs):
            captured.append(messages)
            return original_invoke(messages, **kwargs)
        models[0].invoke_native = capture
        result = generate_draft(snapshot, generator=NativeDailyPlanAgent(models[0]), runtime=runtime)
        import json
        payload = json.loads(captured[0][1]['content'])
        task = next(t for t in payload['task_results'] if t['task_id'] == snapshot.task_notes[1].task_id)
        self.assertEqual(task['unmet_needs'], [note])
        self.assertEqual(len(captured[0]), 2)
        self.assertNotIn('protocol', payload)
        self.assertEqual(snapshot.model_dump(mode='json'), before)
        self.assertFalse(any(i.triggers_repair for i in result.report.issues))
