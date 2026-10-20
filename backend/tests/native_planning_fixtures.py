"""原生规划观测/API测试共用的供应商替身，不导入其他测试模块。"""
from datetime import datetime
import json
from app.agents.native_trip_planner import NativeTripPlanner
from app.models.planning_v4 import TripRequest
from app.services.amap_contract_adapter import AmapContractAdapter
from app.services.llm_service import NativeLLMCompletion
from app.services.native_endpoint_service import NativeEndpointService
from app.services.native_weather_service import NativeWeatherService

def call(name, args, call_id):
    return NativeLLMCompletion({'role': 'assistant', 'content': None, 'reasoning_content': 'private-reasoning',
        'tool_calls': [{'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}}]},
        {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}, 'tool_calls')


def done():
    return NativeLLMCompletion({'role': 'assistant', 'content': 'private-freeform-response'}, None, 'stop')


def user_data(messages):
    return json.loads(next(m['content'] for m in reversed(messages) if m['role'] == 'user'))


class Model:
    def __init__(self, model, *responses):
        self.model = model
        self.responses = list(responses)
        self.calls = 0
    def invoke_native(self, messages, **kwargs):
        self.calls += 1
        answer = self.responses.pop(0)
        return answer(messages) if callable(answer) else answer


def task_args():
    return {'city': '上海', 'dates': ['2026-10-20'], 'objective': '查询模拟候选', 'preferences': [],
        'constraints': [], 'candidate_target': 1, 'hard_requirements': []}


def dispatch_support(messages):
    # 真实上一轮工具结果已经提供p-sights。
    meals = [{'date': '2026-10-20', 'period': p, 'anchor_poi_id': 'p-sights'} for p in ('lunch', 'dinner')]
    first = call('find_restaurants', {**task_args(), 'meals': meals}, 'dining-task')
    second = call('recommend_lodging', {**task_args(), 'anchor_poi_id': 'p-sights'}, 'lodging-task')
    first.message['tool_calls'].extend(second.message['tool_calls'])
    return first


def spatial_queries(messages):
    data = user_data(messages)
    scopes = data.get('meal_scopes', data.get('area_scopes'))
    calls = [call('maps_around_search', {'scope_id': item['scope_id'], 'keywords': '餐馆' if 'meal_scopes' in data else '酒店',
        'expand': False}, 'query-'+str(i)) for i, item in enumerate(scopes)]
    calls[0].message['tool_calls'] = [c for item in calls for c in item.message['tool_calls']]
    return calls[0]


def spatial_submit(messages):
    data = user_data(messages)
    if 'meal_scopes' in data:
        return call('submit_restaurants', {'selections': [{'poi_id': 'p-food',
            'slot_ids': [s['scope_id'] for s in data['meal_scopes']], 'recommendation_reason': '模拟餐馆'}]}, 'food-submit')
    return call('submit_lodging', {'selections': [{'poi_id': 'p-hotel', 'area_hint_id': data['area_scopes'][0]['scope_id'],
        'recommendation_reason': '模拟酒店'}]}, 'hotel-submit')


def daily_plan(messages):
    data = user_data(messages); output = []
    for day in data['planning_input']['days']:
        activities = []
        options = next(o for o in data['day_slot_options'] if o['day_id'] == day['day_id'])
        periods = ['breakfast', 'morning', 'lunch', 'afternoon', 'dinner', 'evening']
        slots = sorted([*options['available_meal_slot_ids'], *options['available_sightseeing_slot_ids']], key=lambda s: periods.index(s.split('-')[-1]))
        for slot in slots:
            candidate = next(c for c in data['candidates'] if slot in c['applicable_slot_ids'])
            activities.append({'proposal_ref': 'new-'+slot, 'candidate_ref': candidate['candidate_id'],
                'slot_id': slot, 'kind': 'meal' if candidate['category'] == 'meal' else 'sightseeing'})
        hotel = next(c for c in data['candidates'] if c['category'] == 'lodging')
        activities.pop(0)  # 故意缺一个时段；真实DayRepairService采用已有候选修订。
        output.append({'day_id': day['day_id'], 'day_date': day['day_date'], 'theme': '模拟初版', 'activities': activities,
            'lodging': {'candidate_ref': hotel['candidate_id'], 'area_hint_id': hotel['area_hint_id']}})
    return call('submit_daily_plan', {'days': output}, 'plan-submit')


def revise(messages):
    data = user_data(messages); day = data['current_day']
    activities = [{key: item[key] for key in ('slot_id', 'kind', 'candidate_ref', 'requirement_ids', 'recommendation_reason')}
        | {'proposal_ref': 'keep-'+item['activity_id'], 'existing_activity_id': item['activity_id']} for item in day['activities']]
    for issue in data['issues']:
        candidate = next(c for c in data['candidates'] if issue['target_id'] in c['applicable_slot_ids'])
        activities.append({'proposal_ref': 'fill-'+issue['target_id'], 'candidate_ref': candidate['candidate_id'],
            'slot_id': issue['target_id'], 'kind': 'meal' if candidate['category'] == 'meal' else 'sightseeing'})
    periods = ['breakfast', 'morning', 'lunch', 'afternoon', 'dinner', 'evening']
    activities.sort(key=lambda a: periods.index(a['slot_id'].split('-')[-1]))
    return call('submit_day_revision', {'replacement_day': {'day_id': day['day_id'], 'day_date': day['day_date'],
        'theme': '模拟修订', 'activities': activities, 'lodging': {k: v for k, v in day['lodging'].items()
        if k in ('candidate_ref', 'area_hint_id')}, 'unfilled_slot_ids': []}}, 'revision-submit')


def fixture_planner(barrier=None, reference_cost=None):
    req = TripRequest(request_id='mock-observation', city='上海',
        arrival_at=datetime.fromisoformat('2026-10-20T09:00:00+08:00'), departure_at=datetime.fromisoformat('2026-10-20T20:00:00+08:00'))
    master = Model('kimi-k3', call('submit_requirements', {key: [] for key in (
        'core_goals', 'hard_requirements', 'lodging_changes', 'preferences', 'unknowns', 'assumptions')}, 'requirements-submit'), done(),
        call('find_attractions', task_args(), 'sights-task'), dispatch_support, done(), daily_plan, done(), revise, done())
    sights = Model('glm-5.3-flash', call('maps_text_search', {'city': '上海', 'keywords': '模拟景点'}, 'sights-query'),
        call('submit_attractions', {'selections': [{'poi_id': 'p-sights', 'recommendation_reason': '模拟景点'}]}, 'sights-submit'), done())
    dining, lodging = (Model('glm-5.3-flash', spatial_queries, spatial_submit, done()) for _ in range(2))
    seen = set()
    def maps(name, args):
        assert args["extensions"] == "all", "候选查询必须携带消费扩展字段"
        if name == 'maps_around_search' and barrier is not None and args['types'] not in seen:
            seen.add(args['types']); barrier.wait(5)
        category = 'sights' if name == 'maps_text_search' else 'food' if args['types'] == '050000' else 'hotel'
        return {'result_status': 'ok', 'pois': [{'id': 'p-'+category, 'name': '模拟'+category, 'location': '121.49,31.24',
            'biz_ext': {'cost': reference_cost}, 'cityname': '上海市', 'type': '风景名胜' if category == 'sights' else '餐饮服务' if category == 'food' else '住宿服务',
            'typecode': '110000' if category == 'sights' else '050100' if category == 'food' else '100100'}]}
    empty = AmapContractAdapter(lambda *_: {'result_status': 'empty', 'city': '上海', 'forecasts': [], 'pois': []})
    planner = NativeTripPlanner(master_llm=master, attraction_llm=sights, dining_llm=dining, lodging_llm=lodging,
        adapter=AmapContractAdapter(maps), weather_service=NativeWeatherService(adapter=empty), endpoint_service=NativeEndpointService(adapter=empty))
    return req, planner, (master, sights, dining, lodging)
