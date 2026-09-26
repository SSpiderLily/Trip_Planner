"""有界的三角色工作流。开放备注只由模型理解；程序只处理结构、事实和计算。"""
import json
import math
from datetime import date, timedelta
from pydantic import ValidationError
from .execution import PlanningAgent, PlanningError, decode_result, check_tool_result, find_items, forecast_by_date
from ..models.itinerary import Collection, Conditions, Draft, Itinerary, Place
from ..services.observation_service import span, ObservedLLM
from ..services.llm_service import get_llm
from ..services.amap_service import get_amap_mcp_tool
from ..services.planning_maps import PlanningMaps
from ..config import get_settings

COLLECT_PROMPT = '''你是旅行候选搜集专家。只输出符合给定 schema 的 JSON，不输出工具调用文本。
程序执行你的 searches，下一次调用会把实际候选与查询反馈给你。不要虚构搜索结果。
首次在搜索前理解用户完整备注，返回 conditions 与 searches；后续保持已确认 conditions，不重新解释需求。
语义理解完全由你负责：必去、预算、交通等从备注理解，不以关键词机械分类。
显式城市、日期、住处优先；冲突写 interpretation_notes。原始备注不能丢失。
默认普通成人，公共交通结合步行，10小时/天。预算是每成人全程门票餐饮市内交通、不含酒店与往返大交通。
用户说明其他预算口径而无法可靠换算时，不编造人均上限，在 interpretation_notes 说明。
带老人、孩子、少走路等本轮只放 reminder_only_requests，并明确未针对这些要求调整路线。
must_visit_requests 只放用户明确要求到访的具体地点（景点或餐厅等），每条给唯一 requirement_id。
“老城散步”“品尝当地美食”这类一般偏好不能列为必去地点；写入 preferences 或 interpretation_notes。不去的地点也不能列为必去。
预算未提及则为 null，不从“便宜”等模糊词杜撰金额。已有条件时 conditions=null，不重复输出条件。
景点阶段只搜 sightseeing，结合全部偏好、必去需求与天数，每次最多3个精准搜索词。
住宿餐饮阶段依据已有日程分布搜 lodging 和 meal，推荐商圈/美食街/住宿区域，不强制酒店。
用户已填住处则优先用该住处原文搜索，不能换成其他酒店。支持阶段最多3个查询。
查询失败可在剩余额度内换搜索词；候选足够时 searches=[]。不得请求超出额度的调用。'''

PLAN_PROMPT = '''你是旅行行程安排专家。只输出给定 schema 的完整 JSON。
使用已核实候选，place 必须携带 source_id 及完整名称、地址、经纬度；不可创造候选以外的地点。
严格按用户城市与连续日期，所有天数都保留。活动按实际先后顺序排列，ID在整份行程中唯一。
sightseeing 游览，meal 用餐，free_time 自由活动。上午、下午有主要内容，不固定景点数量。
每天午餐与晚餐优先提供当地特色及顺路用餐区域，早餐不强制，夜游本阶段不生成。
第一次 stage=layout 时先组合景点形成日程分布，住宿与餐饮可以暂缺；stage=final 时补齐已有候选中的住处和餐饮。
候选不足用 free_time 保留自由活动，不重复景点填满。自由活动 place=null。
已核实必去景点尽量全部保留，并用 requirement_ids 关联已提供的需求；无法核实的必去不编造地点。
用户住宿 source=user，user_input 原文；无法唯一核实时 place=null。否则推荐一个区域作为全程基点，source=recommended，说明区域与理由。
同一景点不重复；每天交通游玩加两餐和休息总时长以600分钟为上限，内容不必排满。
地点选择、室内外安排参考给定的日期天气。开放/预约条件未知时不得声称确定开放。
费用估计标明 basis（模型估算），不知道则 amount=null，不能随便填0。按每成人口径。
只给活动时间与活动费用估计，交通与合计由程序补齐。description 用一句话说明当天安排依据。
预算不足优先调整普通景点与餐饮，保留必去；所有备注均需阅读，程序不会为你理解开放意图。'''

REVISE_PROMPT = PLAN_PROMPT + '''\n你是修改专家：依据 problems 修改完整行程，仅使用已有候选，不能请求新的地点查询。
用户住处固定，推荐区域可从已有候选调整。保留已核实必去景点，尽量保持未改变活动的 ID。
只修改必要部分，解决超时、重复、超预算等问题；不要引入新的已知冲突。'''


def parse_json(text):
    text = text.strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
    return json.loads(text)


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError):
        return None


def summary(values):
    return {'known_total': round(sum(v for v in values if v is not None), 2),
            'unknown_count': sum(v is None for v in values), 'complete': all(v is not None for v in values)}


def problem(code, message, *, category='needs_confirmation', day=None, activity=None, leg=None):
    return {'code': code, 'category': category, 'date': day, 'activity_id': activity,
            'leg_id': leg, 'message': message}


class RouteTripPlanner:
    def __init__(self, llm=None, mcp=None):
        self.settings = get_settings()
        self.llm = ObservedLLM(llm if llm is not None else get_llm())
        self.amap_tool = mcp if mcp is not None else PlanningMaps(get_amap_mcp_tool(), self.settings.planner_tool_timeout_seconds)
        self.amap_tools = self.amap_tool.get_expanded_tools()
        self.collector = PlanningAgent('候选搜集', self.llm, system_prompt=COLLECT_PROMPT, enable_tool_calling=False)
        self.planner = PlanningAgent('行程安排', self.llm, system_prompt=PLAN_PROMPT, enable_tool_calling=False)
        self.reviser = PlanningAgent('行程修改', self.llm, system_prompt=REVISE_PROMPT, enable_tool_calling=False)
        self.agents = [self.collector, self.planner, self.reviser]

    def model(self, agent, name, data, model_type):
        with span('agent.' + name, 'agent', data) as record:
            response = agent.run(json.dumps({'schema': model_type.model_json_schema(), **data}, ensure_ascii=False), max_tokens=8192)
            record.output = response
            with span('validation.' + name, 'validation') as validation:
                result = model_type.model_validate(parse_json(response))
                validation.output = {'valid': True}
            return result

    def tool(self, name, arguments):
        with span('tool.amap_' + name, 'tool', arguments) as record:
            value = self.amap_tool.run({'action': 'call_tool', 'tool_name': name, 'arguments': arguments})
            record.output = value
            data = decode_result(value)
            check_tool_result(data)
            return data

    def plan_trip(self, request):
        # 所有任务状态为局部变量；角色 run 每次独立上下文，跨任务不复用候选或查询缓存。
        state = {'request': request, 'pool': {}, 'used': 0, 'feedback': [], 'route_cache': {}, 'routes_used': 0}
        with span('planning.collect_sights', 'planning'):
            self.collect(state, 'sights')
        if not any('sightseeing' in p['categories'] for p in state['pool'].values()):
            raise PlanningError('INSUFFICIENT_INFORMATION', '没有查到可定位的景点')
        forecasts, weather_error = {}, None
        try:
            payload = self.tool('maps_weather', {'city': request.city})
            forecasts = forecast_by_date(find_items(payload, 'casts') or find_items(payload, 'forecasts') or [])
        except Exception:
            weather_error = '天气查询失败或没有有效预报，未根据天气调整行程'
        state.update(forecasts=forecasts, weather_error=weather_error)
        layout = self.draft(state, 'layout')
        with span('planning.collect_support', 'planning'):
            self.collect(state, 'support', layout)
        draft = self.draft(state, 'final', layout)
        current = self.evaluate(draft, state)
        feedback = current['issues']
        for attempt in range(2):
            if not any(p['category'] == 'needs_adjustment' for p in feedback):
                break
            try:
                proposal = self.model(self.reviser, 'revision', self.context(state, 'revision', draft=draft.model_dump(), problems=feedback), Draft)
                candidate = self.evaluate(proposal, state)
                with span('validation.revision', 'validation') as review:
                    old = self.conflicts(current)
                    if not self.conflicts(candidate).issubset(old):
                        raise PlanningError('REVISION_REJECTED', '修改引入新的已知冲突')
                    review.output = {'accepted': True}
                draft, current = proposal, candidate
                feedback = current['issues']
            except Exception:
                feedback = current['issues'] + [problem('REVISION_INVALID', '上次修改无效或引入新冲突，请保留事实并修复', category='needs_adjustment')]
                if attempt == 1:
                    current['issues'].append(problem('REVISION_INCOMPLETE', '自动调整未完成，保留上一份通过基本校验的行程'))
        for index, issue in enumerate(current['issues']):
            issue['issue_id'] = f'issue_{index + 1}'
        return Itinerary.model_validate(current)

    @staticmethod
    def conflicts(result):
        return {(p['code'], p.get('date'), p.get('activity_id')) for p in result['issues'] if p['category'] == 'needs_adjustment'}

    def context(self, state, stage, **extra):
        return {'stage': stage, 'request': state['request'].model_dump(),
                'conditions': state.get('conditions', Conditions()).model_dump(),
                'candidates': list(state['pool'].values()), 'weather': state.get('forecasts', {}), **extra}

    def collect(self, state, stage, layout=None):
        maximum = self.settings.planner_place_query_limit
        reserve = min(12, max(1, maximum // 3))
        boundary = maximum - reserve if stage == 'sights' else maximum
        for _ in range(2):
            remaining = boundary - state['used']
            if remaining <= 0:
                break
            response = self.model(self.collector, 'collect_' + stage, self.context(
                state, stage, remaining_queries=remaining, feedback=state['feedback'],
                draft=layout.model_dump() if layout else None), Collection)
            if 'conditions' not in state:
                if response.conditions is None:
                    raise PlanningError('INVALID_CONDITIONS', '模型没有提供规划条件')
                conditions = response.conditions
                req = state['request']
                # 仅复制显式字段、计算日期；不扫描或分类用户备注。
                conditions.city, conditions.start_date, conditions.end_date = req.city, req.start_date, req.end_date
                conditions.travel_days, conditions.remarks = req.travel_days, req.free_text_input or ''
                conditions.daily_time_budget_minutes = 600
                if req.preferences:
                    conditions.preferences = req.preferences
                ids = [r.requirement_id for r in conditions.must_visit_requests]
                if len(ids) != len(set(ids)):
                    raise PlanningError('INVALID_CONDITIONS', '需求标识重复')
                state['conditions'] = conditions
            if not response.searches:
                break
            for query in response.searches:
                if (stage == 'sights') != (query.category == 'sightseeing') or state['used'] >= boundary:
                    continue
                state['used'] += 1
                try:
                    result = self.tool('maps_text_search', {'keywords': query.keywords, 'city': state['request'].city, 'citylimit': 'true'})
                    entries = find_items(result, 'pois') or []
                    added = 0
                    for item in entries[:3]:
                        identity = item.get('id')
                        if not identity:
                            continue
                        if identity in state['pool']:
                            cats = state['pool'][identity]['categories']
                            if query.category not in cats:
                                cats.append(query.category)
                            continue
                        if not item.get('location'):
                            if state['used'] >= boundary:
                                break
                            state['used'] += 1
                            try:
                                item = self.tool('maps_search_detail', {'id': identity})
                                if item.get('id') != identity:
                                    continue
                            except Exception:
                                continue
                        try:
                            lng, lat = (float(v) for v in str(item['location']).split(','))
                            place = Place(source_id=identity, name=item['name'], address=item.get('address') or '', longitude=lng, latitude=lat)
                        except (ValueError, TypeError, KeyError, ValidationError):
                            continue
                        state['pool'][identity] = {**place.model_dump(), 'categories': [query.category]}
                        added += 1
                    state['feedback'].append({'query': query.keywords, 'new_places': added})
                except Exception:
                    state['feedback'].append({'query': query.keywords, 'error': '查询失败，已计入额度'})

    def draft(self, state, stage, prior=None):
        data = self.context(state, stage, draft=prior.model_dump() if prior else None)
        try:
            return self.model(self.planner, 'arrange_' + stage, data, Draft)
        except (ValidationError, ValueError, TypeError) as exc:
            if state.get('repair_used'):
                raise
            state['repair_used'] = True
            # 一次结构修复，不重新搜索，不把第三方异常或模型响应拼入日志。
            data['repair'] = '前次输出结构校验失败，请严格按 schema 返回完整 JSON；错误类型：' + type(exc).__name__
            return self.model(self.planner, 'repair_' + stage, data, Draft)

    def ground(self, place, state):
        if place is None:
            return None
        candidate = state['pool'].get(place.source_id)
        if candidate is None:
            raise PlanningError('UNKNOWN_PLACE', '行程引用了未经核实的地点')
        return Place(**{**candidate, 'is_area_reference': place.is_area_reference})

    def evaluate(self, draft, state):
        with span('validation.itinerary_v2', 'validation') as record:
            result = self._evaluate(draft.model_copy(deep=True), state)
            record.output = {'issue_count': len(result['issues']), 'days': len(result['days'])}
            return result

    def _evaluate(self, draft, state):
        request, conditions = state['request'], state['conditions']
        expected = [(date.fromisoformat(request.start_date) + timedelta(days=i)).isoformat() for i in range(request.travel_days)]
        if [d.date for d in draft.days] != expected:
            raise PlanningError('INVALID_ITINERARY', '日期与请求不一致')
        base = draft.lodging_base
        if request.lodging:
            if base.source != 'user' or base.user_input != request.lodging:
                raise PlanningError('INVALID_LODGING', '不能替换用户指定住处')
        elif base.source != 'recommended':
            raise PlanningError('INVALID_LODGING', '没有提供住处时必须推荐住宿区域')
        base.place = self.ground(base.place, state)
        if base.source == 'recommended':
            if not base.place or not base.area_name or not base.recommendation_reason:
                raise PlanningError('INVALID_LODGING', '推荐住宿区域必须有已核实参考点和理由')
            base.place.is_area_reference = True
        # 第一次确认后，用户住处的已核实位置固定，修改角色不能换酒店。
        if 'fixed_lodging' in state:
            base = state['fixed_lodging'].model_copy(deep=True)
        elif base.source == 'user':
            state['fixed_lodging'] = base.model_copy(deep=True)
        issues, days, seen_ids, seen_places, fulfilled = [], [], set(), set(), set()
        req_ids = {r.requirement_id for r in conditions.must_visit_requests}
        for note in conditions.interpretation_notes:
            issues.append(problem('INTERPRETATION_NOTE', note))
        for note in conditions.reminder_only_requests:
            issues.append(problem('REMINDER_ONLY', note + '：仅作提醒，未据此调整路线'))
        if not base.place:
            issues.append(problem('LODGING_UNKNOWN', '用户住处无法定位，往返交通未知'))
        if state['weather_error']:
            issues.append(problem('WEATHER_FAILED', state['weather_error']))
        for day in draft.days:
            activities, legs = [], []
            anchor_id, anchor_place = 'lodging', base.place
            sight_count = 0
            for activity in day.activities:
                if activity.activity_id in seen_ids or activity.activity_id == 'lodging':
                    raise PlanningError('INVALID_ACTIVITY', '活动标识重复或保留标识被占用')
                seen_ids.add(activity.activity_id)
                if not set(activity.requirement_ids).issubset(req_ids):
                    raise PlanningError('INVALID_REQUIREMENT', '活动关联未知需求')
                activity.place = self.ground(activity.place, state)
                if activity.place and activity.type != 'free_time':
                    fulfilled.update(activity.requirement_ids)
                if activity.type == 'sightseeing' and not activity.place:
                    raise PlanningError('UNKNOWN_PLACE', '游览活动没有已核实地点')
                if activity.place and activity.type == 'sightseeing':
                    identity = activity.place.source_id
                    if 'sightseeing' not in state['pool'][identity]['categories']:
                        raise PlanningError('INVALID_PLACE_TYPE', '游览地点未作为景点候选搜集')
                    sight_count += 1
                    if identity in seen_places:
                        issues.append(problem('REPEATED_PLACE', '同一景点重复安排', category='needs_adjustment', day=day.date, activity=activity.activity_id))
                    seen_places.add(identity)
                    issues.append(problem('OPENING_UNKNOWN', '开放时间及预约要求待确认', day=day.date, activity=activity.activity_id))
                if activity.type != 'free_time':
                    leg = self.route(anchor_id, anchor_place, activity.activity_id, activity.place, day.date, len(legs), state)
                    legs.append(leg)
                    anchor_id, anchor_place = activity.activity_id, activity.place
                activities.append(activity.model_dump())
            if anchor_id != 'lodging':
                legs.append(self.route(anchor_id, anchor_place, 'lodging', base.place, day.date, len(legs), state))
            if not sight_count:
                issues.append(problem('DAY_INCOMPLETE', '当天缺少可核实景点，保留自由活动', category='needs_adjustment', day=day.date))
            for period in ('lunch', 'dinner'):
                if not any(a.type == 'meal' and a.period == period for a in day.activities):
                    issues.append(problem('MEAL_MISSING', '午餐安排缺失' if period == 'lunch' else '晚餐安排缺失', day=day.date))
            for leg in legs:
                if leg['duration_minutes'] is None:
                    issues.append(problem('ROUTE_UNKNOWN', '该路段交通耗时未知：' + leg['note'], day=day.date, leg=leg['leg_id']))
            # 固定预留30分钟机动；自由活动另计，不以0掩盖未知的交通和停留时间。
            times = summary([a.duration_minutes for a in day.activities] + [l['duration_minutes'] for l in legs] + [30])
            times.update(budget_minutes=600, buffer_minutes=30)
            times['status'] = 'exceeded' if times['known_total'] > 600 else ('within_budget' if times['complete'] else 'incomplete')
            if times['known_total'] > 600:
                issues.append(problem('TIME_EXCEEDED', f"已知安排至少超出每日上限 {round(times['known_total'] - 600)} 分钟", category='needs_adjustment', day=day.date))
            costs = summary([a.estimated_cost.amount for a in day.activities] + [l['estimated_cost']['amount'] for l in legs])
            if not costs['complete']:
                issues.append(problem('COST_UNKNOWN', '存在未知费用，合计仅为已知部分', day=day.date))
            weather = state['forecasts'].get(day.date)
            if weather is None and not state['weather_error']:
                issues.append(problem('WEATHER_UNCOVERED', '预报未覆盖当天，天气未知', day=day.date))
            days.append({'date': day.date, 'description': day.description, 'activities': activities, 'legs': legs,
                         'optional_plans': [], 'weather': weather, 'time_summary': times, 'cost_summary': costs})
        if not seen_places:
            raise PlanningError('INSUFFICIENT_INFORMATION', '行程中没有已核实景点')
        for requirement in conditions.must_visit_requests:
            if requirement.requirement_id not in fulfilled:
                issues.append(problem('MUST_VISIT_MISSING', '未安排的必去地点：' + requirement.text, category='needs_adjustment', activity=requirement.requirement_id))
        total = summary([day['cost_summary']['known_total'] for day in days])
        total['unknown_count'] = sum(day['cost_summary']['unknown_count'] for day in days)
        total['complete'] = total['unknown_count'] == 0
        total.update(currency='CNY', basis='每位成人全程门票、餐饮和市内交通；不含住宿及往返目的地交通')
        if conditions.budget_per_adult is not None and total['known_total'] > conditions.budget_per_adult:
            issues.append(problem('BUDGET_EXCEEDED', f"已知费用至少超预算 {round(total['known_total'] - conditions.budget_per_adult, 2)} 元", category='needs_adjustment'))
        return {'schema_version': 2, 'planning_conditions': conditions.model_dump(), 'lodging_base': base.model_dump(),
                'days': days, 'cost_summary': total, 'issues': issues}

    def route(self, from_id, origin, to_id, destination, day, index, state):
        mode = state['conditions'].transportation
        if mode == 'transit' and origin and destination:
            # 对已核实坐标做确定性几何计算，不识别或解析用户备注。
            dx = (origin.longitude - destination.longitude) * math.cos(math.radians(origin.latitude))
            dy = origin.latitude - destination.latitude
            if math.hypot(dx, dy) * 111000 <= 1200:
                mode = 'walking'
        leg = {'leg_id': f'{day}_leg_{index}', 'from_activity_id': from_id, 'to_activity_id': to_id,
               'origin': origin.model_dump() if origin else None, 'destination': destination.model_dump() if destination else None,
               'mode': mode, 'duration_minutes': None, 'distance_meters': None, 'data_basis': 'unknown',
               'estimated_cost': {'amount': None, 'basis': '交通费用未知'}, 'note': ''}
        if not origin or not destination:
            leg['note'] = '起点或终点无法定位'
            return leg
        if origin.source_id == destination.source_id:
            leg.update(mode='walking', duration_minutes=0, distance_meters=0, data_basis='same_place',
                       estimated_cost={'amount': 0, 'basis': '同一地点无需转移'}, note='同一已核实地点；区域内部移动另行预留')
            return leg
        key = (origin.source_id, destination.source_id, mode)
        if key in state['route_cache']:
            return {**leg, **state['route_cache'][key]}
        if state['routes_used'] >= self.settings.planner_route_query_limit:
            leg['note'] = '已达到本次路线查询上限'
            return leg
        state['routes_used'] += 1
        try:
            params = {'origin': f'{origin.longitude},{origin.latitude}', 'destination': f'{destination.longitude},{destination.latitude}'}
            tool_mode = 'transit_integrated' if mode == 'transit' else mode
            if mode == 'transit':
                params.update(city=state['request'].city, cityd=state['request'].city)
            result = self.tool('maps_direction_' + tool_mode + '_by_coordinates', params)
            options = find_items(result, 'transits' if mode == 'transit' else 'paths') or []
            options = [option for option in options if number(option.get('duration')) is not None]
            if not options:
                raise PlanningError('ROUTE_EMPTY', '没有有效路线')
            route = min(options, key=lambda option: float(option['duration']))
            leg.update(duration_minutes=math.ceil(float(route['duration']) / 60), distance_meters=number(route.get('distance')), data_basis='route_query')
            if mode == 'walking':
                leg['estimated_cost'] = {'amount': 0, 'basis': '步行无交通票价'}
            elif number(route.get('cost')) is not None:
                leg['estimated_cost'] = {'amount': float(route['cost']), 'basis': '地图路线查询'}
            leg['note'] = '区域参考点，实际住处或餐厅位置可能增加步行' if origin.is_area_reference or destination.is_area_reference else ''
        except Exception:
            leg['note'] = '路线查询失败或未返回有效结果'
        fields = ('mode', 'duration_minutes', 'distance_meters', 'data_basis', 'estimated_cost', 'note')
        state['route_cache'][key] = {k: leg[k] for k in fields}
        return leg
