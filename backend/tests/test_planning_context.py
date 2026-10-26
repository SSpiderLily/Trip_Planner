"""初版投影保留原文/来源/未知，不替模型决定唯一行程。单日修订属P4。"""
import unittest
from app.models.planning_execution import PlanningSnapshot, ProposalSubmission
from app.planning.context import build_context
from app.planning.validation import accept_submission
from test_planning_quality_fixtures import load_quality, CASES

class ContextTests(unittest.TestCase):
    def test_opening_hours_raw_and_source_are_preserved_without_parsing(self):
        data=load_quality()['snapshot']
        data['facts']['places'][0]['opening_hours_raw']='以现场公告为准；节日另行通知'
        data['facts']['places'][0]['field_sources']['opening_hours_raw']='source-synthetic-map'
        value=PlanningSnapshot.model_validate(data)
        projected=build_context(value).reference_information
        place=projected['place_facts'][0]
        self.assertIn('opening_hours_raw',place)
        self.assertEqual(place['opening_hours_raw'],'以现场公告为准；节日另行通知')
        self.assertEqual(place['field_sources']['opening_hours_raw'],'source-synthetic-map')
    def test_original_request_sources_and_unknowns_survive_projection(self):
        for name in CASES:
            with self.subTest(case=name):
                snap=PlanningSnapshot.model_validate(load_quality(name)['snapshot'])
                before=snap.model_dump(mode='json')
                context=build_context(snap).reference_information
                self.assertEqual(context['planning_input']['request'],before['planning_input']['request'])
                self.assertEqual(context['planning_input']['requirements'],before['planning_input']['requirements'])
                self.assertEqual(context['fact_sources'],before['facts']['sources'])
                for place in context['place_facts']:
                    original=next(p for p in before['facts']['places'] if p['fact_id']==place['fact_id'])
                    for key in ('cost','field_sources','source_refs','missing_fields'):
                        self.assertEqual(place[key],original[key])
                self.assertEqual(snap.model_dump(mode='json'),before)
                self.assertNotIn('messages',context)
                self.assertEqual(context['planning_input']['boundary_facts']['departure_activity_end_upper_bound_at'],
                                 '2026-10-11T18:00:00+08:00')
    def test_semantic_quality_does_not_add_online_rejection_rules(self):
        # 正餐用作早餐、重复游览仍按原结构规则接受，质量结论由独立人工评估给出。
        data=load_quality('explicit_repeat')
        data['submission']['days'][1]['activities'][0]['candidate_ref']='candidate-meal'
        snap=PlanningSnapshot.model_validate(data['snapshot'])
        result=accept_submission(snap,ProposalSubmission.model_validate(data['submission']))
        self.assertEqual(len(result.draft.days[1].activities),5)
        self.assertEqual(result.draft.days[1].activities[0].candidate_ref,'candidate-meal')
    def test_native_submission_boundary_for_quality_cases(self):
        from app.agents.native_daily_plan_agent import NativeDailyPlanAgent
        from app.planning.core import generate_draft
        from test_native_daily_plan_agent import FakeModel
        from test_planning_core import Runtime
        for name in ('normal','no_breakfast_evidence','explicit_repeat','rest_preference'):
            with self.subTest(case=name):
                data=load_quality(name);model=FakeModel(data['submission'])
                result=generate_draft(PlanningSnapshot.model_validate(data['snapshot']),
                                      generator=NativeDailyPlanAgent(model),runtime=Runtime())
                self.assertEqual(len(model.messages),1)
                self.assertEqual(result.identity.task_id,'run-quality-'+name)

class RevisionContextTests(unittest.TestCase):
    def test_single_day_keeps_request_sources_and_preserve_constraints_without_history(self):
        from test_planning_repair_boundary import repair_case
        from app.services.day_repair_service import DayRevisionRequest, derive_activity_policy
        from app.planning.context import build_revision_context
        s,d,r,_=repair_case();day=d.days[1]
        issues=tuple(i for i in r.issues if i.day_id==day.day_id and i.triggers_repair)
        policy=derive_activity_policy(d,day.day_id,issues)
        req=DayRevisionRequest(planning_input=s.planning_input,draft=d,day=day,report=r,issues=issues,
            assignments=(),round_id='round',preserve_activity_ids=policy.preserve_ids,
            modifiable_activity_ids=policy.modifiable_ids,removable_activity_ids=policy.removable_ids,
            fact_store=s.facts,candidates=s.candidates)
        before=req.model_dump();payload=build_revision_context(req).reference_information
        self.assertEqual(len(payload['planning_input']['days']),1)
        self.assertEqual(payload['planning_input']['request'],s.planning_input.request.model_dump(mode='json'))
        self.assertEqual(payload['activity_policy']['preserve_activity_ids'],list(policy.preserve_ids))
        self.assertEqual(payload['current_day'],day.model_dump(mode='json'))
        self.assertTrue(payload['issues']);self.assertNotIn('messages',payload);self.assertNotIn('draft',payload)
        sources={v['tool_call_id'] for v in payload['fact_sources']}
        for place in payload['place_facts']:
            self.assertLessEqual(set(place['source_refs'])|set(place['field_sources'].values()),sources)
        self.assertEqual(req.model_dump(),before)

    def test_preserved_lodging_and_weather_references_are_closed(self):
        from test_native_trip_planner import collection, plan
        from app.agents.daily_plan_generator import DailyPlanGenerator
        from app.agents.daily_plan_activity_repair import DailyPlanModelOutput
        from app.services.day_repair_service import DayRevisionRequest, derive_activity_policy
        from app.services.planning_v4_validation import validate_daily_plan
        from app.planning.context import build_revision_context
        state=collection();rules=DailyPlanGenerator();p=state.planning_input
        output=plan(rules._messages(p,state.candidates,state.fact_store,state.area_hints,()))
        output['days'][1]['activities'].pop(0)
        proposal=rules._validate_proposal_output(DailyPlanModelOutput.model_validate(output),p,state.candidates,state.fact_store,state.area_hints)
        draft=rules._build_draft(proposal,p,state.candidates,state.fact_store,state.area_hints,state.run_id)
        report=validate_daily_plan(p,draft,state.fact_store,state.candidates);day=draft.days[1]
        issues=tuple(i for i in report.issues if i.triggers_repair and i.day_id==day.day_id)
        policy=derive_activity_policy(draft,day.day_id,issues)
        req=DayRevisionRequest(planning_input=p,draft=draft,day=day,report=report,issues=issues,
            assignments=(),round_id='round',fact_store=state.fact_store,candidates=state.candidates,
            area_hints=state.area_hints,preserve_activity_ids=policy.preserve_ids)
        value=build_revision_context(req).reference_information
        self.assertIn(day.lodging.place_fact_id,{f['fact_id'] for f in value['place_facts']})
        self.assertEqual([d['day_id'] for d in value['planning_input']['days']],[day.day_id])
        self.assertTrue(all(set(row)=={'day_id','poi_id','recommendation_reason'} for row in value['other_day_sightseeing']))

    def test_no_candidate_task_keeps_target_coverage_and_missing_notes(self):
        from test_planning_repair_boundary import repair_case
        from app.services.day_repair_service import DayRevisionRequest
        from app.models.planning_v4 import TaskResult, TaskCoverage
        from app.models.planning_execution import CollectionTaskNote
        from app.planning.context import build_revision_context
        s,d,r,slot=repair_case()
        target='slot:'+slot.slot_id
        other='slot:'+s.planning_input.days[0].slots[0].slot_id
        task=TaskResult(task_id='missing-meal',run_id=d.source_run_id,request_id=d.request_id,
            context_revision=1,status='partial',stop_reason='no_match',
            coverage=(TaskCoverage(target_id=target,status='missing',reason='无匹配'),),
            missing=tuple(TaskCoverage(target_id=t,status='missing',reason='无匹配') for t in (target,other)))
        unrelated=task.model_copy(update={'task_id':'other-day','coverage':(),
            'missing':(TaskCoverage(target_id=other,status='missing',reason='无匹配'),)})
        req=DayRevisionRequest(planning_input=s.planning_input,draft=d,day=d.days[1],report=r,
            issues=r.issues,assignments=(),round_id='notes',fact_store=s.facts,candidates=s.candidates,
            task_results=(task,unrelated))
        payload=build_revision_context(req,task_notes=(CollectionTaskNote(task_id=task.task_id,
            unmet_needs=('无法确认餐馆无障碍入口',)),)).reference_information
        self.assertEqual([t['task_id'] for t in payload['task_results']],['missing-meal'])
        result=payload['task_results'][0]
        self.assertEqual(result['unmet_needs'],['无法确认餐馆无障碍入口'])
        self.assertEqual([c['target_id'] for c in result['coverage']],[target])
        self.assertEqual([c['target_id'] for c in result['missing']],[target])

    def test_candidate_task_keeps_prefixed_coverage(self):
        from test_planning_repair_boundary import repair_case
        from app.services.day_repair_service import DayRevisionRequest
        from app.models.planning_v4 import TaskResult, TaskCoverage
        from app.planning.context import build_revision_context
        s,d,r,slot=repair_case()
        candidate=next(c for c in s.candidates if c.candidate_id=='candidate-dinner-repair')
        target='slot:'+slot.slot_id
        task=TaskResult(task_id='covered-meal',run_id=d.source_run_id,request_id=d.request_id,
            context_revision=1,status='complete',stop_reason='target_met',candidates=(candidate,),
            coverage=(TaskCoverage(target_id=target,status='covered'),))
        req=DayRevisionRequest(planning_input=s.planning_input,draft=d,day=d.days[1],report=r,
            issues=r.issues,assignments=(),round_id='coverage',fact_store=s.facts,candidates=s.candidates,
            task_results=(task,))
        result=build_revision_context(req).reference_information['task_results'][0]
        self.assertEqual(result['candidate_ids'],[candidate.candidate_id])
        self.assertEqual([c['target_id'] for c in result['coverage']],[target])

    def test_revision_projects_target_forecast_with_its_embedded_source(self):
        from test_planning_repair_boundary import repair_case
        from app.models.planning_v4.facts import WeatherFact, WeatherForecast
        from app.services.day_repair_service import DayRevisionRequest
        from app.planning.context import build_revision_context
        s,d,r,_=repair_case();day=d.days[1]
        source=s.facts.sources[0].model_copy(update={'tool_call_id':'weather-source','tool_name':'maps_weather'})
        weather=WeatherFact(fact_id='weather-fact',city=s.planning_input.request.city,source=source,
            forecasts=(WeatherForecast(forecast_date=d.days[0].day_date,day_weather='晴'),
                       WeatherForecast(forecast_date=day.day_date,day_weather='雨')))
        facts=s.facts.model_copy(update={'sources':(*s.facts.sources,source),'weather':(weather,)})
        req=DayRevisionRequest(planning_input=s.planning_input,draft=d,day=day,report=r,
            issues=r.issues,assignments=(),round_id='weather',fact_store=facts,candidates=s.candidates)
        before=req.model_dump();payload=build_revision_context(req).reference_information
        self.assertEqual(len(payload['weather_facts']),1)
        projected=payload['weather_facts'][0]
        self.assertEqual(projected['forecasts'],[weather.forecasts[1].model_dump(mode='json')])
        self.assertEqual(projected['source'],source.model_dump(mode='json'))
        self.assertIn(source.model_dump(mode='json'),payload['fact_sources'])
        self.assertEqual(req.model_dump(),before)
