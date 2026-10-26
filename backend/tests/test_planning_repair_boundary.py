"""P4：独立单日模型、规范化补查与失败隔离。"""
import asyncio
import unittest
from threading import Event
from unittest.mock import Mock
from app.models.planning_execution import PlanningSnapshot
from app.models.planning_runtime import PlanningRunIdentity
from app.services.planning_runtime_adapter import SynchronousPlanningRuntime
from test_simplified_day_repair import ready_case
from test_day_repair_service import setup_missing_dinner, proposal_day_from_current
from test_native_attraction_collection import FakeModel, call


def repair_case(ready=True):
    p,f,c,d,r,slot = ready_case() if ready else setup_missing_dinner()
    snapshot=PlanningSnapshot(identity=PlanningRunIdentity(task_id=d.source_run_id,plan_id=d.plan_id),
        planning_input=p,facts=f,candidates=c)
    return snapshot,d,r,slot


def replacement(messages, fill=True):
    import json
    payload=json.loads(messages[-1]['content']); day=payload['current_day']
    from app.models.planning_v4 import DraftDay
    candidate=next((c for c in payload['candidates'] if c['candidate_id']=='candidate-dinner-repair'),None)
    from app.models.planning_v4 import CandidateRef
    # 使用可信fixture候选，模型只返回候选ID。
    s,_,_,slot=repair_case()
    c=next(c for c in s.candidates if c.candidate_id=='candidate-dinner-repair')
    value=proposal_day_from_current(DraftDay.model_validate(day),added_candidate=c if fill else None,
        added_slot=slot.slot_id if fill else None)
    return call('submit_day_revision',{'replacement_day':value.model_dump(mode='json')},'revision')


class RepairBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def run_repair(self,s,d,r,model,collection=None,runtime=None):
        from app.services.planning_repair_service import repair_draft
        from app.agents.native_day_revision_agent import NativeDayRevisionAgent
        return await repair_draft(s,d,r,revision_generator=NativeDayRevisionAgent(model),
            collection=collection or Mock(),runtime=runtime or SynchronousPlanningRuntime())

    async def test_candidates_revise_only_target_day_and_keep_accepted_content(self):
        s,d,r,_=repair_case(); before=s.model_dump(); collector=Mock();model=FakeModel(replacement)
        out=await self.run_repair(s,d,r,model,collector)
        self.assertEqual(out.status,'complete');collector.supplement.assert_not_called()
        self.assertEqual(out.draft.days[0],d.days[0]);self.assertEqual(s.model_dump(),before)
        old=[a for a in d.days[1].activities if a.kind!='free_time']
        self.assertEqual([a for a in out.draft.days[1].activities if a.activity_id in {a.activity_id for a in old}],old)
        self.assertEqual(len(model.requests),1);self.assertEqual(len(model.requests[0]['messages']),2)
        self.assertEqual(out.repair_states[1].completed_rounds,1)

    async def test_three_business_rounds_leave_partial_draft(self):
        s,d,r,_=repair_case();model=FakeModel(*[lambda m: replacement(m,False)]*3)
        out=await self.run_repair(s,d,r,model)
        self.assertEqual(out.status,'partial');self.assertEqual(out.draft,d)
        self.assertEqual(out.repair_states[1].completed_rounds,3)
        self.assertEqual(len(model.requests),3)

    async def test_revision_model_receives_verified_or_unknown_endpoint_evidence(self):
        import json
        from app.models.planning_evidence import EndpointEvidence
        from app.services.native_endpoint_service import NativeEndpointService
        from app.services.amap_contract_adapter import AmapContractAdapter
        from app.services.day_repair_service import _merge_facts
        from app.planning.validation import validate_snapshot
        for status in ('verified', 'ambiguous', 'not_found', 'error', 'missing'):
            with self.subTest(status=status):
                s, d, r, _ = repair_case()
                request = s.planning_input.request.model_copy(update={
                    'departure_place_query': None if status == 'missing' else '上海虹桥站'})
                planning = s.planning_input.model_copy(update={'request': request})
                def query(*_):
                    if status == 'error':
                        raise ConnectionError('private-map-error')
                    places = [] if status == 'not_found' else [
                        {'id': 'review-station', 'name': '上海虹桥站', 'cityname': '上海市',
                         'location': '121.327,31.20', 'address': '闵行区'}]
                    if status == 'ambiguous':
                        places.append({**places[0], 'id': 'review-station-other'})
                    return {'result_status': 'ok' if places else 'empty', 'pois': places}
                endpoints = NativeEndpointService(adapter=AmapContractAdapter(query)).run(
                    planning, run_id=s.identity.task_id)
                evidence = EndpointEvidence.model_validate({key: getattr(endpoints, key)
                    for key in ('run_id', 'planning_input', 'executions', 'arrival', 'departure', 'fact_store')})
                s = s.model_copy(update={'planning_input': planning, 'endpoint_evidence': evidence,
                    'facts': _merge_facts(s.facts, evidence.fact_store)})
                validate_snapshot(s)
                model = FakeModel(replacement)
                out = await self.run_repair(s, d, r, model)
                self.assertEqual(out.status, 'complete')
                payload = json.loads(model.requests[0]['messages'][-1]['content'])
                self.assertIn('endpoints', payload)
                endpoint = payload['endpoints']['departure']
                self.assertEqual(endpoint['status'], status)
                self.assertEqual(endpoint['availability'], 'known' if status == 'verified' else 'unknown')
                self.assertEqual(endpoint['day_id'], s.planning_input.days[-1].day_id)
                if status == 'verified':
                    self.assertEqual(endpoint['place']['poi_id'], 'review-station')
                    self.assertEqual(endpoint['place']['location'], {'lng': 121.327, 'lat': 31.2, 'coordinate_system': 'GCJ-02'})
                    source_ids = {source['tool_call_id'] for source in payload['endpoints']['sources']}
                    self.assertLessEqual(set(endpoint['place']['source_refs']), source_ids)
                    self.assertNotIn(endpoint['fact_id'], {c['place_fact_id'] for c in payload['candidates']})
                else:
                    self.assertIsNone(endpoint['fact_id'])
                    self.assertIsNone(endpoint['place'])
                self.assertEqual(out.draft.days[0], d.days[0])

    async def test_collection_failure_is_technical_and_cannot_change_draft(self):
        from app.models.planning_errors import PlanningError
        s,d,r,_=repair_case(False); collector=Mock()
        collector.supplement.side_effect=PlanningError('REPAIR_MAP_FAILED','safe')
        model=FakeModel();out=await self.run_repair(s,d,r,model,collector)
        self.assertEqual(out.status,'technical_error');self.assertEqual(out.draft,d);self.assertEqual(out.facts,s.facts)
        self.assertEqual(collector.supplement.call_count,3);self.assertFalse(model.requests)
        self.assertEqual(out.repair_states[1].completed_rounds,0)

    async def test_cancelled_late_revision_never_publishes(self):
        from app.models.planning_errors import PlanningError
        s,d,r,_=repair_case();event=Event()
        def late(messages):
            value=replacement(messages);event.set();return value
        with self.assertRaises(PlanningError) as error:
            await self.run_repair(s,d,r,FakeModel(late),runtime=SynchronousPlanningRuntime(cancel_event=event))
        self.assertEqual(error.exception.code,'CANCELLED')

class CollectionRepairBoundaryTests(unittest.TestCase):
    def setup_session(self):
        from test_native_collection_adapter import prepared, collection
        from app.services.native_collection_adapter import NativeCollectionSession
        s=prepared();state=collection(s);collector=Mock();collector.run.return_value=state
        session=NativeCollectionSession(collector=collector);runtime=SynchronousPlanningRuntime()
        snap=session.collect(s.planning_input,identity=s.identity,runtime=runtime)
        return session,collector,snap,None,runtime

    def test_supplement_rejects_wrong_session_before_provider(self):
        from app.models.planning_errors import PlanningError
        session,collector,s,result,runtime=self.setup_session()
        # 构造另一个请求的修订范围，不能把资料并入当前会话。
        from app.services.day_repair_service import DayRepairCollectionRequest
        other,d,r,_=repair_case(False)
        req=DayRepairCollectionRequest(planning_input=other.planning_input,draft=d,day=d.days[1],
            issues=r.issues,assignments=(),round_id='wrong',fact_store=other.facts,candidates=other.candidates)
        with self.assertRaises(PlanningError):session.supplement(req,runtime=runtime)
        self.assertEqual(collector.run.call_count,1)


def session_with_gap(*, sightseeing_gap=False):
    from dataclasses import replace
    from test_native_trip_planner import collection, plan
    from app.services.native_collection_adapter import NativeCollectionSession
    from app.planning.context import build_context
    from app.planning.validation import accept_submission
    from app.models.planning_execution import ProposalSubmission
    from app.services.day_repair_service import DayRepairCollectionRequest, _collection_scope
    from app.services.planning_v4_validation import is_required_place_gap
    state=collection();slot='slot-2026-10-21-breakfast'
    candidates=tuple(c.model_copy(update={'slot_ids':tuple(v for v in c.slot_ids if v!=slot)})
                     if c.category=='meal' else c for c in state.candidates)
    if sightseeing_gap:
        candidates=tuple(c for c in candidates if c.category!='sightseeing')
    by_id={c.candidate_id:c for c in candidates}
    children=tuple(replace(child,task_result=child.task_result.model_copy(update={
        'candidates':tuple(by_id[c.candidate_id] for c in child.task_result.candidates if c.candidate_id in by_id)})) for child in state.children)
    state=replace(state,candidates=candidates,children=children)
    collector=Mock();session=NativeCollectionSession(collector=collector);runtime=SynchronousPlanningRuntime()
    identity=PlanningRunIdentity(task_id=state.run_id,plan_id='repair-plan')
    from planning_collection_fixtures import normalized_collection
    state=normalized_collection(state)
    collector.run.return_value=state
    s=session.collect(state.planning_input,identity=identity,runtime=runtime)
    collector.reset_mock()
    import json
    result=accept_submission(s,ProposalSubmission.model_validate(plan([
        {'role':'user','content':json.dumps(build_context(s).reference_information)}])))
    day=result.draft.days[1];issues=tuple(i for i in result.report.issues if is_required_place_gap(s.planning_input,i))
    _,targets=_collection_scope(s.planning_input,s.planning_input.days[1],day,issues,s.facts,s.candidates)
    req=DayRepairCollectionRequest(planning_input=s.planning_input,draft=result.draft,day=day,issues=issues,
        assignments=(),round_id='round',targets=targets,fact_store=s.facts,candidates=s.candidates,
        area_hints=s.area_hints,task_results=s.task_results)
    return session,collector,s,result,req,runtime


class SupplementTransactionTests(unittest.TestCase):
    def test_no_result_is_business_no_progress_and_has_no_old_messages(self):
        from dataclasses import replace
        from app.services.native_tool_loop import NativeToolLoopResult
        from app.models.planning_v4 import TaskResult
        from app.agents.native_attraction_agent import NativeCandidateExecution
        session,collector,s,result,req,runtime=session_with_gap()
        def no_result(*args,**kw):
            self.assertNotIn('messages',kw)
            seed=kw['initial_state']
            child=NativeCandidateExecution(TaskResult(task_id='no-match',run_id=s.identity.task_id,
                request_id=s.planning_input.request.request_id,context_revision=8,status='partial',stop_reason='no_match'),
                s.facts,unmet_needs=('缺少早餐候选',))
            return replace(seed,children=(*seed.children,child),protocol=NativeToolLoopResult('',(),(),0))
        collector.run.side_effect=no_result
        output=session.supplement(req,runtime=runtime)
        self.assertEqual(output.stop_reason,'no_progress');self.assertEqual(output.fact_store,s.facts)
        self.assertEqual(session.task_notes[-1].unmet_needs,('缺少早餐候选',))
        self.assertEqual(result.draft.days[0],req.draft.days[0])

    def test_late_cancelled_result_does_not_publish_facts_or_notes(self):
        from dataclasses import replace
        from app.models.planning_errors import PlanningError
        session,collector,s,result,req,runtime=session_with_gap()
        before=session._snapshot.model_dump();notes=session.task_notes
        def late(*args,**kw):
            runtime.cancel_event.set()
            return kw['initial_state']
        collector.run.side_effect=late
        with self.assertRaises(PlanningError) as raised:session.supplement(req,runtime=runtime,cancel_event=runtime.cancel_event)
        self.assertEqual(raised.exception.code,'CANCELLED')
        self.assertEqual(session._snapshot.model_dump(),before);self.assertEqual(session.task_notes,notes)
        self.assertEqual(result.draft,req.draft)

    def test_provider_cannot_overwrite_accepted_facts(self):
        from dataclasses import replace
        from app.models.planning_errors import PlanningError
        session,collector,s,result,req,runtime=session_with_gap();before=session._snapshot.model_dump()
        def corrupt(*args,**kw):
            seed=kw['initial_state'];places=list(seed.fact_store.places)
            places[0]=places[0].model_copy(update={'name':'tampered'})
            return replace(seed,fact_store=seed.fact_store.model_copy(update={'places':tuple(places)}))
        collector.run.side_effect=corrupt
        with self.assertRaises(PlanningError):session.supplement(req,runtime=runtime)
        self.assertEqual(session._snapshot.model_dump(),before);self.assertEqual(result.draft,req.draft)

    def test_wrong_plan_or_outside_day_target_is_rejected_before_provider(self):
        from app.models.planning_errors import PlanningError
        session,collector,s,result,req,runtime=session_with_gap()
        for bad in (req.model_copy(update={'draft':req.draft.model_copy(update={'plan_id':'other'})}),
                    req.model_copy(update={'targets':(req.targets[0].model_copy(update={'day_id':req.draft.days[0].day_id}),)})):
            with self.assertRaises(PlanningError):session.supplement(bad,runtime=runtime)
        collector.run.assert_not_called()

class RepairInputBindingTests(unittest.IsolatedAsyncioTestCase):
    run_repair = RepairBoundaryTests.run_repair
    async def test_snapshot_identity_mismatch_is_rejected_before_model(self):
        from app.models.planning_errors import PlanningError
        s,d,r,_=repair_case();model=FakeModel(replacement)
        bad=s.model_copy(update={'identity':s.identity.model_copy(update={'plan_id':'other-plan'})})
        with self.assertRaises(PlanningError):await self.run_repair(bad,d,r,model)
        self.assertFalse(model.requests)

    async def test_async_cancellation_signals_inflight_collection_before_late_result(self):
        import threading
        from app.services.planning_repair_service import repair_draft
        session,collector,s,result,req,runtime=session_with_gap()
        entered,release,finished=threading.Event(),threading.Event(),threading.Event()
        before=session._snapshot.model_dump()
        def late(*args,**kw):
            entered.set()
            try:
                if not release.wait(2):raise TimeoutError('gate')
                return kw['initial_state']
            finally:finished.set()
        collector.run.side_effect=late
        task=asyncio.create_task(repair_draft(s,result.draft,result.report,
            revision_generator=Mock(),collection=session,runtime=runtime))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait,2));task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
            self.assertTrue(runtime.cancel_event.is_set())
        finally:release.set()
        self.assertTrue(await asyncio.to_thread(finished.wait,2))
        await asyncio.sleep(0)
        self.assertEqual(session._snapshot.model_dump(),before)

class SupplementCandidateScopeTests(unittest.TestCase):
    def run_candidate(self, slot_ids, *, reject):
        from dataclasses import replace
        from app.models.planning_errors import PlanningError
        from app.models.planning_v4 import TaskResult
        from app.agents.native_attraction_agent import NativeCandidateExecution
        session,collector,s,result,req,runtime=session_with_gap()
        before=session._snapshot.model_copy(deep=True); old_state=session._state
        candidate=next(c for c in s.candidates if c.category=='meal').model_copy(update={
            'candidate_id':'new-meal','slot_ids':slot_ids})
        def provider(*args,**kw):
            seed=kw['initial_state']
            child=NativeCandidateExecution(TaskResult(task_id='supplement',run_id=s.identity.task_id,
                request_id=s.planning_input.request.request_id,context_revision=8,status='complete',
                stop_reason='target_met',candidates=(candidate,)),s.facts)
            return replace(seed,candidates=(*seed.candidates,candidate),children=(*seed.children,child))
        collector.run.side_effect=provider
        if reject:
            with self.assertRaises(PlanningError) as error:session.supplement(req,runtime=runtime)
            self.assertEqual(error.exception.code,'REPAIR_TASK_OUTSIDE_SCOPE')
            self.assertEqual(session._snapshot,before)
            self.assertIs(session._state,old_state)
        else:
            out=session.supplement(req,runtime=runtime)
            self.assertEqual(out.stop_reason,'target_met')
            self.assertIn(candidate,out.candidates)
            for original in s.candidates:self.assertIn(original,out.candidates)
        self.assertEqual(result.draft,req.draft)

    def test_outside_date_meal_is_rejected_without_publishing(self):
        self.run_candidate(('slot-2026-10-20-breakfast',),reject=True)

    def test_other_meal_on_target_date_is_rejected(self):
        self.run_candidate(('slot-2026-10-21-dinner',),reject=True)

    def test_mixed_target_and_outside_slots_are_rejected(self):
        self.run_candidate(('slot-2026-10-21-breakfast','slot-2026-10-20-breakfast'),reject=True)

    def test_unscoped_new_meal_is_rejected(self):
        self.run_candidate((),reject=True)

    def test_target_meal_is_accepted_and_existing_candidates_remain(self):
        self.run_candidate(('slot-2026-10-21-breakfast',),reject=False)

    def test_native_unscoped_sightseeing_is_accepted_only_for_attraction_gap(self):
        from dataclasses import replace
        from test_native_trip_planner import collection
        from app.models.planning_errors import PlanningError
        # collection()运行真实原生景点Agent及替身模型，保留其空时段输出契约。
        original=collection()
        child=next(c for c in original.children if any(v.category=='sightseeing' for v in c.task_result.candidates))
        candidate=next(c for c in child.task_result.candidates if c.category=='sightseeing')
        self.assertEqual(candidate.slot_ids,())
        for has_gap in (False,True):
            with self.subTest(attraction_gap=has_gap):
                session,collector,s,result,req,runtime=session_with_gap(sightseeing_gap=has_gap)
                place=next(p for p in s.facts.places if p.poi_id==candidate.poi_id)
                candidate_new=candidate.model_copy(update={'candidate_id':'supplement-attraction',
                    'place_fact_id':place.fact_id})
                new_child=replace(child,task_result=child.task_result.model_copy(update={
                    'task_id':'supplement-attraction-task','run_id':s.identity.task_id,
                    'request_id':s.planning_input.request.request_id,'candidates':(candidate_new,)}))
                before=session._snapshot.model_copy(deep=True)
                def provider(*args,**kw):
                    seed=kw['initial_state']
                    return replace(seed,candidates=(*seed.candidates,candidate_new),children=(*seed.children,new_child))
                collector.run.side_effect=provider
                if has_gap:
                    out=session.supplement(req,runtime=runtime)
                    self.assertIn(candidate_new,out.candidates)
                    self.assertEqual(result.draft,req.draft)
                else:
                    with self.assertRaises(PlanningError) as error:session.supplement(req,runtime=runtime)
                    self.assertEqual(error.exception.code,'REPAIR_TASK_OUTSIDE_SCOPE')
                    self.assertEqual(session._snapshot,before)
