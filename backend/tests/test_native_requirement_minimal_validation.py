"""仅A1—A4的原生需求契约；真实循环和消费者，模型/地图使用替身。"""
import json
import unittest
from pydantic import ValidationError
from app.agents.native_requirement_interpreter import NativeRequirementInterpreter
from app.models.planning_v4.input import TripRequest
from app.services.native_tool_loop import NativeToolLoopError
from native_planning_fixtures import fixture_planner
from test_native_requirement_interpreter import Model, call, request


class MinimalRequirementsTests(unittest.TestCase):
    def interpret(self, args, req=None):
        model = Model(call(args), AssertionError('accepted之后不应确认'))
        result = NativeRequirementInterpreter(llm=model).run(req or request())
        self.assertEqual(result.protocol.model_calls, 1)
        return result, model

    def test_only_structural_fields_no_sources_and_no_required_six_lists(self):
        result, model = self.interpret({'core_goals': [{'text': '城市漫步'}],
            'hard_requirements': [{'kind': 'must_visit', 'text': '外滩'}]})
        self.assertEqual(result.summary.core_goals, ('城市漫步',))
        self.assertIsNone(result.summary.hard_requirements[0].source)
        self.assertEqual(result.summary.preferences, ())
        self.assertNotIn('source', model.requests[0]['tools'][0]['function']['parameters'].get('required', []))

    def test_removed_policies_do_not_reject_or_repair_model_output(self):
        args = {'core_goals': [{'text': '  ', 'source': {'field': 'budget', 'quote': '"amount": null'}}],
            'preferences': [{'text': ''}],
            'hard_requirements': [{'kind': 'must_include', 'text': '指定住处', 'source': {'field': 'lodging_query', 'quote': '不存在'}}]*2,
            'unknowns': ['city','anything','anything'], 'assumptions': ['模型自带假设'],
            'request_id': 'foreign', 'requirements_id': 'foreign', 'city': '北京', 'budget': {'amount': 1}}
        req = request(); before = req.model_dump(mode='json')
        result, _ = self.interpret(args, req)
        self.assertEqual(req.model_dump(mode='json'), before)
        self.assertEqual(result.summary.request_id, req.request_id)
        self.assertNotEqual(result.summary.requirements_id, 'foreign')
        self.assertEqual(result.summary.core_goals, ('  ',))
        self.assertEqual(result.summary.preferences, ('',))
        self.assertEqual(len(result.summary.hard_requirements), 2)
        self.assertEqual(len({x.requirement_id for x in result.summary.hard_requirements}), 2)
        self.assertTrue(all(x.source is None for x in result.summary.hard_requirements))
        self.assertEqual((result.summary.unknowns,result.summary.assumptions), ((),()))

    def test_empty_text_and_duplicate_items_are_not_content_validation_errors(self):
        result,_=self.interpret({'hard_requirements':[{'kind':'must_avoid','text':''}]*2})
        self.assertEqual([r.text for r in result.summary.hard_requirements], ['',''])

    def test_bad_known_types_and_categories_still_require_correction(self):
        for bad in ({'core_goals':'text'}, {'core_goals':[{'text':123}]},
                    {'hard_requirements':[{'kind':'unknown','text':'外滩'}]}, {'lodging_changes':[{'start_date':'bad'}]}):
            with self.subTest(bad=bad):
                model=Model(call(bad,'bad'),call({},'fixed'))
                result=NativeRequirementInterpreter(llm=model).run(request())
                error=json.loads(model.requests[1]['messages'][-1]['content'])
                self.assertEqual(error['error']['code'],'INVALID_TOOL_ARGUMENTS')
                self.assertEqual(result.protocol.model_calls,2)

    def test_lodging_keeps_only_date_range_and_collision_checks(self):
        req=request()
        good={'start_date':'2026-10-21','city':'杭州','lodging_query':'未出现在原文的新酒店',
              'source':{'field':'city','quote':'完全不匹配'}}
        result,_=self.interpret({'lodging_changes':[good]},req)
        self.assertIsNone(result.summary.lodging_changes[0].source)
        self.assertEqual(result.summary.lodging_changes[0].city,'杭州')
        for changes in ([{**good,'start_date':'2026-10-23'}],[good,good]):
            with self.subTest(changes=changes):
                model=Model(call({'lodging_changes':changes},'bad'),call({'lodging_changes':[good]},'fixed'))
                NativeRequirementInterpreter(llm=model).run(req)
                self.assertEqual(json.loads(model.requests[1]['messages'][-1]['content'])['data']['code'],
                                 'REQUIREMENT_LODGING_DATES_INVALID')

    def test_plain_text_without_submission_is_still_not_a_committed_result(self):
        from test_native_requirement_interpreter import final
        with self.assertRaises(NativeToolLoopError) as raised:
            NativeRequirementInterpreter(llm=Model(final())).run(request())
        self.assertEqual(raised.exception.code,'MASTER_DID_NOT_SUBMIT_REQUIREMENTS')

    def test_original_request_dates_and_city_still_checked(self):
        data=request().model_dump(mode='json')
        for change in ({'city':' '},{'departure_at':data['arrival_at']},{'arrival_at':'bad'}):
            with self.subTest(change=change),self.assertRaises(ValidationError):
                TripRequest.model_validate({**data,**change})

    def test_formal_planner_and_candidate_consumers_accept_unquoted_requirements(self):
        req,planner,models=fixture_planner(repair=False)
        master=models[0]
        message=master.responses[0]
        args=json.loads(message.message['tool_calls'][0]['function']['arguments'])
        args['hard_requirements']=[{'kind':'must_visit','text':'模拟景点'}, {'kind':'must_avoid','text':'动物园'}]
        message.message['tool_calls'][0]['function']['arguments']=json.dumps(args)
        dispatch=master.responses[1]
        task=json.loads(dispatch.message['tool_calls'][0]['function']['arguments'])
        task['hard_requirements']=[{'kind':'must_visit','text':'模拟景点'}]
        dispatch.message['tool_calls'][0]['function']['arguments']=json.dumps(task)
        result=planner.run(req)
        self.assertEqual(result.repair.status,'complete')
        self.assertTrue(all(r.source is None for r in result.planning_input.requirements.hard_requirements))
        self.assertEqual(len(result.snapshot.task_results),3)
        self.assertEqual(result.draft.revision,1)

    def test_dispatch_consumes_each_duplicate_requirement_identity_once(self):
        req, planner, models = fixture_planner(repair=False)
        events = []
        planner.collector.emit = lambda event, data: events.append((event, data))
        items = [{'kind': 'must_visit', 'text': '模拟景点'}] * 2
        for response in models[0].responses[:2]:
            args = json.loads(response.message['tool_calls'][0]['function']['arguments'])
            args['hard_requirements'] = items
            response.message['tool_calls'][0]['function']['arguments'] = json.dumps(args)
        result = planner.run(req)
        expected = {r.requirement_id for r in result.planning_input.requirements.hard_requirements}
        actual = next(data['task']['requirement_ids'] for event, data in events if event == 'master.task')
        self.assertEqual(set(actual), expected)
        self.assertEqual(len(actual), 2)

    def test_dispatch_schemas_allow_every_accepted_text_length(self):
        from app.agents.native_attraction_collection import AttractionRequirement
        from app.agents.native_multiagent_collection import DiningRequirement
        for schema, kind in ((AttractionRequirement, 'must_visit'), (DiningRequirement, 'must_include')):
            for text in ('', '园', '字' * 201):
                with self.subTest(schema=schema.__name__, text_length=len(text)):
                    self.assertEqual(schema(kind=kind, text=text).text, text)
