"""有界的三角色工作流。开放备注只由模型理解；程序只处理结构、事实和计算。"""
import json
import hashlib
import math
import re
from datetime import date, timedelta
from pydantic import ValidationError
from .execution import PlanningAgent, PlanningError, decode_result, check_tool_result, find_items, forecast_by_date
from ..models.itinerary import Collection, Conditions, Draft, Itinerary, Place
from ..services.observation_service import span, ObservedLLM
from ..services.llm_service import get_llm
from ..services.amap_service import get_amap_mcp_tool
from ..services.planning_maps import PlanningMaps
from ..services.route_options_service import RouteOptionsService, choose_mode
from ..services.schedule_service import schedule_day, opening_conflict
from ..services.cost_service import queried_reference_cost, summarize_day_costs, summarize_trip
from ..config import get_settings

COLLECT_PROMPT = '''你是旅行候选搜集专家。只输出符合给定 schema 的 JSON，不输出工具调用文本。
程序执行你的 searches，下一次调用会把实际候选与查询反馈给你。不要虚构搜索结果。
首次在搜索前理解用户完整备注，返回 conditions 与 searches；后续保持已确认 conditions，不重新解释需求。
语义理解完全由你负责：必去、预算、交通等从备注理解，不以关键词机械分类。
显式城市、日期、住处优先；冲突写 interpretation_notes。原始备注不能丢失。
默认普通成人，公共交通结合步行，09:00—19:00。默认偏好为公共交通；短途可推荐步行。预算按每人独住口径理解为全程住宿、餐饮和门票，不含交通。
用户说明其他预算口径而无法可靠换算时，不编造人均上限，在 interpretation_notes 说明。
带老人、孩子、少走路等本轮只放 reminder_only_requests，并明确未针对这些要求调整路线。
must_visit_requests 只放用户明确要求到访的具体地点（景点或餐厅等），每条给唯一 requirement_id。
“老城散步”“品尝当地美食”这类一般偏好不能列为必去地点；写入 preferences 或 interpretation_notes。不去的地点也不能列为必去。
预算未提及则为 null，不从“便宜”等模糊词杜撰金额。价格数值只能来自地图查询；禁止给门票、餐饮或住宿估价。已有条件时 conditions=null，不重复输出条件。
景点阶段只搜 sightseeing，结合全部偏好、必去需求与天数，每次最多3个精准搜索词。
住宿餐饮阶段依据已有日程分布搜 lodging 和 meal，推荐商圈/美食街/住宿区域，不强制酒店。
用户已填住处则优先用该住处原文搜索，不能换成其他酒店。支持阶段最多3个查询。
查询失败可在剩余额度内换搜索词；候选足够时 searches=[]。不得请求超出额度的调用。

以下仅为结构示例。城市、日期、备注、需求ID和搜索词须按本次输入填写，不得照抄示例。
示例展示可选字段；可省略有默认值的字段，但不能省略用户条件或丢失用户备注。
首次搜集（此例用户备注为“必须去示例公园”，未填预算和到离地点）：
```json
{
  "conditions": {
    "city": "示例城市", "start_date": "2030-05-01", "end_date": "2030-05-01", "travel_days": 1,
    "preferences": [], "transportation": "transit", "daily_time_budget_minutes": 600,
    "must_visit_requests": [{"requirement_id": "r1", "text": "示例公园"}],
    "budget_per_adult": null, "budget_per_person": null, "remarks": "必须去示例公园",
    "interpretation_notes": [], "reminder_only_requests": [],
    "arrival_at": "2030-05-01T09:00:00+08:00", "departure_at": "2030-05-01T19:00:00+08:00",
    "arrival_place_id": null, "departure_place_id": null, "arrival_place": null, "departure_place": null
  },
  "searches": [{"keywords": "示例公园", "category": "sightseeing"}]
}
```
已有条件，补充景点查询（conditions=null，不重复解释用户意图）：
```json
{"conditions": null, "searches": [{"keywords": "另一处景点", "category": "sightseeing"}]}
```
已有条件，住宿餐饮阶段查询（只能依据当前日程和用户住处选择搜索词）：
```json
{"conditions": null, "searches": [{"keywords": "顺路用餐区域", "category": "meal"}, {"keywords": "附近住宿区域", "category": "lodging"}]}
```
候选足够，停止查询（空列表不是null）：
```json
{"conditions": null, "searches": []}
```'''

PLAN_PROMPT = '''你是旅行行程安排专家。只输出给定 schema 的完整 JSON。
使用已核实候选，place 必须携带 source_id 及完整名称、地址、经纬度；不可创造候选以外的地点。
严格按用户城市与连续日期，所有天数都保留。活动按实际先后顺序排列，ID在整份行程中唯一。
sightseeing 游览，meal 用餐，free_time 自由活动；活动按实际先后顺序排列，不固定景点数量。
普通完整游览日可安排上午和下午内容；首日按到达时间及30分钟接驳缓冲计算可用窗口，末日按离开时间及30分钟缓冲计算。晚到或早离导致时间不足时，当天可以少排或不排景点，不为填满页面强加自由活动或餐饮。
普通完整日的午餐与晚餐优先提供当地特色及顺路用餐区域；首末日时间不足时不强加餐饮。早餐不强制，夜游本阶段不生成。
第一次 stage=layout 时先组合景点形成日程分布，住宿与餐饮可以暂缺；stage=final 时补齐已有候选中的住处和餐饮。
候选不足用 free_time 保留自由活动，不重复景点填满。自由活动 place=null。
已核实必去景点尽量全部保留，并用 requirement_ids 关联已提供的需求；无法核实的必去不编造地点。
用户住宿 source=user，user_input 原文；无法唯一核实时 place=null。否则推荐一个区域作为全程基点，source=recommended，说明区域与理由；最终版从已核实住宿候选中选择 place 作为区域参考点。当天往返且没有住宿候选时仍保留 lodging_base 对象，其区域、理由和 place 可以为null。
只参考地图已查到的开放时间；营业时间、预约条件未知时不声称开放或可预约。费用数值由程序从高德查询结果填入；模型只输出下述未知费用结构，不估价。
同一景点不重复；每天交通游玩加两餐和休息总时长以600分钟为上限，内容不必排满。
地点选择、室内外安排参考给定的日期天气。开放/预约条件未知时不得声称确定开放。
费用不允许估价，模型不得生成任何价格数值。lodging_base.reference_cost 必须是对象，未知时 amount=null、status=missing；不能将整个对象写成null。
活动 estimated_cost 必须是对象，未知时 amount=null、basis=未知；不能将整个对象写成null。活动 reference_cost 可以为null，与住宿 reference_cost 的规则不同。
只给活动参考游玩时长，交通与合计由程序查询和计算。description 用一句话说明当天安排依据。
预算不足优先调整普通景点与餐饮，保留必去；所有备注均需阅读，程序不会为你理解开放意图。

以下是完整行程草稿的结构示例，修改和结构修复也返回此结构，不返回局部补丁或最终接口的交通、天气、费用汇总。
示例地点、ID、地址和坐标均为虚构占位；实际 place 必须复制本次 candidates 中已核实地点的字段，requirement_ids 必须来自本次 conditions。
日期、活动数量、时段、区域和时长按本次条件安排，不照抄示例。可省略有默认值的字段；若显式输出，仍须遵守其类型。[]表示空列表，null只用于schema允许为空的字段。
stage=layout 初步排程（未选住宿，先排景点）：
```json
{
  "lodging_base": {
    "source": "recommended", "user_input": null, "area_name": null, "recommendation_reason": null, "place": null,
    "reference_cost": {"amount": null, "currency": "CNY", "unit": null, "source": null, "status": "missing"}
  },
  "days": [{
    "date": "2030-05-01", "description": "先围绕已核实必去景点安排",
    "activities": [{
      "activity_id": "a1", "type": "sightseeing", "period": "morning", "title": "游览示例公园",
      "description": "优先安排必去景点", "duration_minutes": 90,
      "place": {"source": "amap", "source_id": "example_sight", "name": "示例公园", "address": "示例地址", "longitude": 120.0, "latitude": 30.0},
      "estimated_cost": {"amount": null, "basis": "未知"}, "reference_cost": null,
      "requirement_ids": ["r1"], "opening_hours": null, "photos": []
    }]
  }]
}
```
stage=final 完整排程（示例展示游览、午餐、自由活动和晚餐，数量按实际条件决定）：
```json
{
  "lodging_base": {
    "source": "recommended", "user_input": null, "area_name": "示例住宿区域", "recommendation_reason": "靠近已选活动",
    "place": {"source": "amap", "source_id": "example_lodging", "name": "示例住宿区域参考点", "address": "示例住宿地址", "longitude": 120.0, "latitude": 30.0, "is_area_reference": true},
    "reference_cost": {"amount": null, "currency": "CNY", "unit": null, "source": null, "status": "missing"}
  },
  "days": [{
    "date": "2030-05-01", "description": "围绕必去景点安排顺路用餐并预留休息",
    "activities": [
      {
        "activity_id": "a1", "type": "sightseeing", "period": "morning", "title": "游览示例公园", "description": "保留必去景点", "duration_minutes": 90,
        "place": {"source": "amap", "source_id": "example_sight", "name": "示例公园", "address": "示例地址", "longitude": 120.0, "latitude": 30.0},
        "estimated_cost": {"amount": null, "basis": "未知"}, "reference_cost": null, "requirement_ids": ["r1"], "opening_hours": null, "photos": []
      },
      {
        "activity_id": "a2", "type": "meal", "period": "lunch", "title": "顺路午餐", "description": "在附近用餐区域选择当地餐饮", "duration_minutes": 60,
        "place": {"source": "amap", "source_id": "example_meal", "name": "示例用餐区域参考点", "address": "示例用餐地址", "longitude": 120.01, "latitude": 30.01, "is_area_reference": true},
        "estimated_cost": {"amount": null, "basis": "未知"}, "reference_cost": null, "requirement_ids": [], "opening_hours": null, "photos": []
      },
      {
        "activity_id": "a3", "type": "free_time", "period": "afternoon", "title": "附近自由活动", "description": "在上一地点附近自行安排", "duration_minutes": 30,
        "place": null, "estimated_cost": {"amount": null, "basis": "未知"}, "reference_cost": null, "requirement_ids": [], "opening_hours": null, "photos": []
      },
      {
        "activity_id": "a4", "type": "meal", "period": "dinner", "title": "附近晚餐", "description": "在附近用餐区域选择晚餐", "duration_minutes": 60,
        "place": {"source": "amap", "source_id": "example_meal", "name": "示例用餐区域参考点", "address": "示例用餐地址", "longitude": 120.01, "latitude": 30.01, "is_area_reference": true},
        "estimated_cost": {"amount": null, "basis": "未知"}, "reference_cost": null, "requirement_ids": [], "opening_hours": null, "photos": []
      }
    ]
  }]
}
```
以下仅为可替换的子结构示例，输出时必须嵌入完整 lodging_base、days 草稿，不能单独返回片段。
用户住处无法唯一定位（保留用户原文，不能换酒店）：
```json
{"source": "user", "user_input": "用户填写的住处原文", "area_name": null, "recommendation_reason": null, "place": null, "reference_cost": {"amount": null, "currency": "CNY", "unit": null, "source": null, "status": "missing"}}
```
晚到或早离没有活动窗口（保留当天，activities为空列表，不为null）：
```json
{"date": "2030-05-01", "description": "可用时间不足，当天不安排活动", "activities": []}
```'''

REVISE_PROMPT = PLAN_PROMPT + '''\n你是修改专家：依据 problems 修改完整行程，仅使用已有候选，不能请求新的地点查询。
用户住处固定，推荐区域可从已有候选调整。保留已核实必去景点，尽量保持未改变活动的 ID。
只修改必要部分，解决超时、重复、超预算等问题；不要引入新的已知冲突。
输出沿用上面的完整行程草稿示例：保留 lodging_base 和全部日期的 days，返回修改后的完整 activities，不只返回被修改的活动。'''

REPAIR_PROMPT = '''前次输出结构校验失败，请严格按 schema 和系统提示词中的完整行程草稿示例返回完整 JSON。
修复字段类型并保留有效安排，不通过删除活动、日期或用户条件绕过校验。住宿 reference_cost 和活动 estimated_cost 不能为null。
未知费用的正确字段值如下；嵌入完整草稿，不单独返回这些片段。
lodging_base.reference_cost 的值：
```json
{"amount": null, "currency": "CNY", "unit": null, "source": null, "status": "missing"}
```
每个活动 estimated_cost 的值：
```json
{"amount": null, "basis": "未知"}
```
活动 reference_cost 允许为null。错误类型：'''


def parse_json(text):
    text = text.strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
    return json.loads(text)


def number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        match = re.fullmatch(r'\s*[¥￥]?\s*(\d+(?:\.\d+)?)\s*(?:元|人民币)?\s*(?:/人)?\s*', value)
        if not match:
            return None
        value = match.group(1)
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError):
        return None


def problem(code, message, *, category='needs_confirmation', day=None, activity=None, leg=None):
    return {'code': code, 'category': category, 'date': day, 'activity_id': activity,
            'leg_id': leg, 'message': message}


def stable_issue_id(issue):
    source = '|'.join(str(issue.get(key) or '') for key in ('code', 'date', 'activity_id', 'leg_id', 'message'))
    return 'issue_' + hashlib.sha256(source.encode()).hexdigest()[:16]


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
        state['route_service'] = RouteOptionsService(lambda name, args: self.tool(name, args),
                                                     query_limit=self.settings.planner_route_query_limit)
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
        for issue in current['issues']:
            issue['issue_id'] = stable_issue_id(issue)
        return Itinerary.model_validate(current)

    @staticmethod
    def conflicts(result):
        return {(p['code'], p.get('date'), p.get('activity_id')) for p in result['issues'] if p['category'] == 'needs_adjustment'}

    def context(self, state, stage, **extra):
        return {'stage': stage, 'request': state['request'].model_dump(mode='json'),
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
                conditions.arrival_at = req.arrival_at.isoformat() if req.arrival_at else None
                conditions.departure_at = req.departure_at.isoformat() if req.departure_at else None
                conditions.arrival_place_id = req.arrival_place_id
                conditions.departure_place_id = req.departure_place_id
                if req.budget_per_person is not None:
                    conditions.budget_per_person = req.budget_per_person
                    conditions.budget_per_adult = req.budget_per_person
                elif conditions.budget_per_person is None:
                    # 保留模型从完整自然语言备注理解出的预算；不由程序扫描关键词。
                    conditions.budget_per_person = conditions.budget_per_adult
                if req.preferences:
                    conditions.preferences = req.preferences
                ids = [r.requirement_id for r in conditions.must_visit_requests]
                if len(ids) != len(set(ids)):
                    raise PlanningError('INVALID_CONDITIONS', '需求标识重复')
                state['conditions'] = conditions
                conditions.arrival_place = self.resolve_endpoint(req.arrival_place_id, req.city, state) if req.arrival_place_id else None
                conditions.departure_place = self.resolve_endpoint(req.departure_place_id, req.city, state) if req.departure_place_id else None
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
                        if not isinstance(item, dict):
                            continue
                        identity = item.get('id')
                        if not identity:
                            continue
                        if identity in state['pool']:
                            cats = state['pool'][identity]['categories']
                            if query.category not in cats:
                                cats.append(query.category)
                            continue
                        biz = item.get('biz_ext') if isinstance(item.get('biz_ext'), dict) else {}
                        has_price = item.get('cost') is not None or biz.get('cost') is not None
                        has_hours = bool(item.get('opentime') or item.get('opentime_today'))
                        needs_detail = (not item.get('location') or not has_price or not self._photos(item)
                                        or (query.category == 'sightseeing' and not has_hours))
                        if needs_detail and state['used'] < boundary:
                            state['used'] += 1
                            try:
                                detail = self._unwrap_detail(self.tool('maps_search_detail', {'id': identity}))
                                if detail.get('id') == identity:
                                    item = {**item, **detail}
                            except Exception:
                                # Search facts remain usable when coordinates were already returned;
                                # queried cost/opening fields then stay explicitly unknown.
                                pass
                        if not item.get('location'):
                            continue
                        try:
                            coordinates = item['location']
                            if isinstance(coordinates, dict):
                                lng = float(coordinates.get('longitude', coordinates.get('lng')))
                                lat = float(coordinates.get('latitude', coordinates.get('lat')))
                            else:
                                lng, lat = (float(v) for v in str(coordinates).split(',', 1))
                            place = Place(source_id=identity, name=item['name'], address=item.get('address') or '', longitude=lng, latitude=lat)
                        except (ValueError, TypeError, KeyError, ValidationError):
                            continue
                        state['pool'][identity] = {**place.model_dump(), 'categories': [query.category],
                                                   'photos': self._photos(item), 'tel': item.get('tel'),
                                                   **self._reference_metadata(item, query.category)}
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
            data['repair'] = REPAIR_PROMPT + type(exc).__name__
            return self.model(self.planner, 'repair_' + stage, data, Draft)

    def ground(self, place, state):
        if place is None:
            return None
        candidate = state['pool'].get(place.source_id)
        if candidate is None:
            raise PlanningError('UNKNOWN_PLACE', '行程引用了未经核实的地点')
        return Place(**{**candidate, 'is_area_reference': place.is_area_reference})

    @staticmethod
    def lodging_reference(area_name, draft, state):
        """模型漏选区域参考点时，从已核实住宿候选中选取贴近日程的地点。"""
        candidates = [item for item in state['pool'].values()
                      if 'lodging' in item.get('categories', []) and item.get('longitude') is not None
                      and item.get('latitude') is not None]
        if not candidates:
            return None
        terms = [part.rstrip('一带附近周边商圈') for part in re.split(r'[-·/，,、\s]+', area_name or '')]
        terms = [part for part in terms if len(part) >= 2]
        selected = [state['pool'].get(activity.place.source_id)
                    for day in draft.days for activity in day.activities if activity.place]
        points = [(item['longitude'], item['latitude']) for item in selected
                  if item and item.get('longitude') is not None and item.get('latitude') is not None]
        center = (sum(x for x, _ in points) / len(points), sum(y for _, y in points) / len(points)) if points else None

        def rank(item):
            label = str(item.get('name') or '') + str(item.get('address') or '')
            match = sum(term in label for term in terms)
            distance = ((item['longitude'] - center[0]) ** 2 + (item['latitude'] - center[1]) ** 2) if center else 0
            return (-match, distance, str(item['source_id']))

        return Place(**{**min(candidates, key=rank), 'is_area_reference': True})

    def resolve_endpoint(self, identity, city, state):
        """只接受地图详情可核实且能在目的地城市搜索到的到离地点。"""
        try:
            detail = self._unwrap_detail(self.tool('maps_search_detail', {'id': identity}))
            if isinstance(detail.get('poi'), dict):
                detail = detail['poi']
            elif isinstance(detail.get('detail'), dict):
                detail = detail['detail']
            if detail.get('id') != identity:
                return None
            city_name = str(detail.get('cityname') or detail.get('city') or '')
            if city_name and city_name not in city and city not in city_name:
                return None
            if not city_name:
                search = self.tool('maps_text_search', {'keywords': detail.get('name', ''), 'city': city, 'citylimit': 'true'})
                if not any(p.get('id') == identity for p in (find_items(search, 'pois') or []) if isinstance(p, dict)):
                    return None
            coordinates = detail.get('location')
            if isinstance(coordinates, dict):
                lng = float(coordinates.get('longitude', coordinates.get('lng')))
                lat = float(coordinates.get('latitude', coordinates.get('lat')))
            else:
                lng, lat = (float(value) for value in str(coordinates).split(',', 1))
            place = Place(source_id=identity, name=detail['name'], address=detail.get('address') or '', longitude=lng, latitude=lat)
            state['pool'][identity] = {**place.model_dump(), 'categories': ['endpoint'], 'photos': self._photos(detail),
                                       'tel': detail.get('tel'), **self._reference_metadata(detail)}
            return place
        except Exception:
            return None

    @staticmethod
    def _unwrap_detail(value):
        if not isinstance(value, dict):
            return {}
        for key in ('poi', 'detail'):
            if isinstance(value.get(key), dict):
                return value[key]
        rows = find_items(value, 'pois') or []
        if rows and isinstance(rows[0], dict):
            return rows[0]
        if isinstance(value.get('data'), dict):
            return value['data']
        return value

    @staticmethod
    def _photos(item):
        values = item.get('photos') or []
        if not isinstance(values, list):
            return []
        photos = []
        for photo in values:
            url = photo.get('url') if isinstance(photo, dict) else photo
            if isinstance(url, str) and url.strip():
                photos.append(url.strip())
        return photos

    @staticmethod
    def _reference_metadata(item, category=None):
        biz = item.get('biz_ext') if isinstance(item.get('biz_ext'), dict) else {}
        value = item.get('cost') if item.get('cost') is not None else biz.get('cost')
        parsed = number(value)
        raw_unit = str(item.get('cost_unit') or biz.get('cost_unit') or value or '').lower().replace(' ', '')
        if category == 'lodging' and re.search(r'(?:元/晚|元/夜|/晚|/夜|/night|每晚|每夜)', raw_unit):
            basis = 'room_night'
        elif parsed is not None:
            basis = 'reference'
        else:
            basis = None
        return {'reference_cost': parsed, 'cost_basis': basis,
                'opening_hours': item.get('opentime') or item.get('opentime_today')}

    def evaluate(self, draft, state):
        with span('validation.itinerary_v3', 'validation') as record:
            result = self._evaluate(draft.model_copy(deep=True), state)
            result = self._upgrade_v3(result, state)
            record.output = {'issue_count': len(result['issues']), 'days': len(result['days'])}
            return result

    def _upgrade_v3(self, legacy, state):
        """由同一组已核实事实组装 v3；丢弃模型费用和旧交通费用口径。"""
        conditions = legacy['planning_conditions']
        request = state['request']
        base = legacy['lodging_base']
        base_place = base.get('place')
        base_candidate = state['pool'].get((base_place or {}).get('source_id'), {})
        base['reference_cost'] = queried_reference_cost(base_candidate, 'lodging')
        days = []
        issues = [issue for issue in legacy['issues'] if issue['code'] not in {
            'ROUTE_UNKNOWN', 'TIME_EXCEEDED', 'COST_UNKNOWN', 'BUDGET_EXCEEDED', 'DAY_INCOMPLETE', 'OPENING_UNKNOWN'
        }]
        for index, old_day in enumerate(legacy['days']):
            activities = []
            for old_activity in old_day['activities']:
                activity = dict(old_activity)
                category = 'tickets' if activity.get('type') == 'sightseeing' else 'meals' if activity.get('type') == 'meal' else None
                candidate = state['pool'].get((activity.get('place') or {}).get('source_id'), {})
                activity['reference_cost'] = queried_reference_cost(candidate, category) if category else None
                activity['opening_hours'] = candidate.get('opening_hours')
                activity['photos'] = candidate.get('photos') or []
                if activity.get('type') == 'sightseeing' and not activity['opening_hours']:
                    issues.append(problem('OPENING_UNKNOWN', '未查询到开放时间', day=old_day['date'], activity=activity['activity_id']))
                activity.pop('estimated_cost', None)
                activity['start_at'] = None
                activity['end_at'] = None
                activities.append(activity)
            legs = [self._v3_leg(leg) for leg in old_day['legs']]
            day = {'day_id': 'day-' + old_day['date'], 'date': old_day['date'], 'description': old_day['description'],
                   'activities': activities, 'legs': legs, 'weather': old_day.get('weather')}
            schedule_day(day, conditions)
            for activity in activities:
                if opening_conflict(activity) is True:
                    issues.append(problem('OPENING_CONFLICT', '按当前参考时间可能与已查询开放时间冲突',
                                          category='needs_adjustment', day=day['date'], activity=activity['activity_id']))
            day['cost_summary'] = summarize_day_costs(
                activities, lodging_base=base,
                lodging_nights=max(0, (date.fromisoformat(request.end_date)-date.fromisoformat(request.start_date)).days) if index == 0 else 0,
                include_lodging_unknown=index == 0)
            if day['time_summary']['possible_overrun_minutes']:
                issue = problem('TIME_EXCEEDED', f"按当前预估，可能超时约 {day['time_summary']['possible_overrun_minutes']} 分钟", category='needs_adjustment', day=day['date'])
                issues.append(issue)
            if day['time_summary']['unknown_leg_count']:
                issues.append(problem('ROUTE_UNKNOWN', '部分路段没有可用耗时，暂不能完整判断当天安排', day=day['date']))
            if not day['cost_summary']['complete']:
                issues.append(problem('COST_UNKNOWN', '未查询到真实数据，费用仅汇总已知项目', day=day['date']))
            days.append(day)
        summary = summarize_trip(days, conditions.get('budget_per_person'))
        if summary['over_budget_known']:
            issues.append(problem('BUDGET_EXCEEDED', '已知参考费用已超过预算，不代表未知项目的总费用', category='needs_adjustment'))
        if request.arrival_place_id and not conditions.get('arrival_place'):
            issues.append(problem('ARRIVAL_PLACE_UNKNOWN', '到达地点详情未能核实，未计算该端接驳', day=request.start_date))
        if request.departure_place_id and not conditions.get('departure_place'):
            issues.append(problem('DEPARTURE_PLACE_UNKNOWN', '离开地点详情未能核实，未计算该端接驳', day=request.end_date))
        for issue in issues:
            issue['issue_id'] = stable_issue_id(issue)
        for day in days:
            day['issues'] = [issue for issue in issues if issue.get('date') in (None, day['date'])]
        return {'schema_version': 3, 'planning_conditions': conditions, 'lodging_base': base,
                'days': days, 'cost_summary': summary, 'issues': issues}

    @staticmethod
    def _v3_leg(leg):
        mode = leg.get('selected_mode') or leg.get('mode') or 'transit'
        options = leg.get('options') or {mode: {
            'mode': mode, 'duration_minutes': leg.get('duration_minutes'), 'distance_m': leg.get('distance_meters'),
            'status': 'available' if leg.get('duration_minutes') is not None else 'failed', 'source': leg.get('data_basis', 'unknown'),
            'queried_at': None}}
        selected = options.get(mode, {})
        return {'leg_id': leg.get('leg_id') or f"{leg.get('from_activity_id')}->{leg.get('to_activity_id')}",
                'from_activity_id': leg.get('from_activity_id'), 'to_activity_id': leg.get('to_activity_id'),
                'origin': leg.get('origin'), 'destination': leg.get('destination'), 'options': options,
                'selected_mode': mode, 'selection_source': leg.get('selection_source', 'preference'),
                'fastest_mode': leg.get('fastest_mode'), 'duration_minutes': selected.get('duration_minutes'),
                'distance_m': selected.get('distance_m'), 'note': leg.get('note', '')}

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
            if not base.area_name or not base.recommendation_reason:
                raise PlanningError('INVALID_LODGING', '推荐住宿区域必须有名称和理由')
            if not base.place:
                base.place = self.lodging_reference(base.area_name, draft, state)
            if not base.place and request.travel_days > 1:
                raise PlanningError('INVALID_LODGING', '过夜行程的住宿区域必须有已核实参考点')
            if base.place:
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
        if not base.place and request.travel_days > 1:
            issues.append(problem('LODGING_UNKNOWN', '用户住处无法定位，往返交通未知'))
        if state['weather_error']:
            issues.append(problem('WEATHER_FAILED', state['weather_error']))
        for day in draft.days:
            activities, legs = [], []
            arrival_today = day.date == request.start_date
            departure_today = day.date == request.end_date
            if arrival_today:
                anchor_id, anchor_place = ('arrival', conditions.arrival_place) if conditions.arrival_place_id else (None, None)
            else:
                anchor_id, anchor_place = 'lodging', base.place
            for activity in day.activities:
                if activity.activity_id in seen_ids or activity.activity_id in {'lodging', 'arrival', 'departure'}:
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
                    if identity in seen_places:
                        issues.append(problem('REPEATED_PLACE', '同一景点重复安排', category='needs_adjustment', day=day.date, activity=activity.activity_id))
                    seen_places.add(identity)
                if activity.type != 'free_time' and anchor_id is not None:
                    leg = self.route(anchor_id, anchor_place, activity.activity_id, activity.place, day.date, len(legs), state)
                    legs.append(leg)
                    anchor_id, anchor_place = activity.activity_id, activity.place
                elif activity.type != 'free_time':
                    anchor_id, anchor_place = activity.activity_id, activity.place
                activities.append(activity.model_dump())
            if departure_today:
                target_id, target_place = ('departure', conditions.departure_place) if conditions.departure_place_id else (None, None)
            else:
                target_id, target_place = 'lodging', base.place
            if activities and anchor_id is not None and target_id is not None and anchor_id != target_id:
                legs.append(self.route(anchor_id, anchor_place, target_id, target_place, day.date, len(legs), state))
            for period in ('lunch', 'dinner'):
                if not any(a.type == 'meal' and a.period == period for a in day.activities):
                    issues.append(problem('MEAL_MISSING', '午餐安排缺失' if period == 'lunch' else '晚餐安排缺失', day=day.date))
            weather = state['forecasts'].get(day.date)
            if weather is None and not state['weather_error']:
                issues.append(problem('WEATHER_UNCOVERED', '预报未覆盖当天，天气未知', day=day.date))
            days.append({'date': day.date, 'description': day.description, 'activities': activities, 'legs': legs,
                         'optional_plans': [], 'weather': weather})
        if not seen_places:
            raise PlanningError('INSUFFICIENT_INFORMATION', '行程中没有已核实景点')
        for requirement in conditions.must_visit_requests:
            if requirement.requirement_id not in fulfilled:
                issues.append(problem('MUST_VISIT_MISSING', '未安排的必去地点：' + requirement.text, category='needs_adjustment', activity=requirement.requirement_id))
        return {'schema_version': 2, 'planning_conditions': conditions.model_dump(), 'lodging_base': base.model_dump(),
                'days': days, 'issues': issues}

    def route(self, from_id, origin, to_id, destination, day, index, state):
        preferred = state['conditions'].transportation
        origin_data = origin.model_dump() if origin else None
        destination_data = destination.model_dump() if destination else None
        options = state['route_service'].options(origin_data, destination_data, state['request'].city,
                                                  include_driving=preferred == 'driving')
        fastest_mode = choose_mode(options, preferred)
        preferred_option = options.get(preferred, {})
        walking_option, transit_option = options.get('walking', {}), options.get('transit', {})
        short_walk = (preferred == 'transit' and walking_option.get('status') == 'available'
                      and walking_option.get('duration_minutes') is not None
                      and walking_option['duration_minutes'] <= 15
                      and transit_option.get('status') == 'available'
                      and transit_option.get('duration_minutes') is not None
                      and walking_option['duration_minutes'] < transit_option['duration_minutes'])
        if short_walk:
            selected_mode, selection_source = 'walking', 'short_walk'
        elif preferred_option.get('status') == 'available' and preferred_option.get('duration_minutes') is not None:
            selected_mode, selection_source = preferred, 'preference'
        else:
            selected_mode, selection_source = fastest_mode or preferred, 'fastest_fallback'
        notes = []
        if short_walk:
            notes.append('短途步行少于15分钟，作为公共交通接驳参考')
        elif selection_source == 'fastest_fallback' and fastest_mode:
            notes.append('首选方式暂无可用耗时，暂按已查询到的最快方式展示')
        elif selection_source == 'fastest_fallback':
            notes.append('未查询到真实路线耗时，所选方式仅作参考')
        if (origin and origin.is_area_reference) or (destination and destination.is_area_reference):
            notes.append('住宿区域仅为参考点，实际住处或餐厅位置可能增加接驳时间')
        selected = options.get(selected_mode, {})
        return {'leg_id': f"{(origin.source_id if origin else from_id)}->{(destination.source_id if destination else to_id)}",
                'from_activity_id': from_id, 'to_activity_id': to_id, 'origin': origin_data, 'destination': destination_data,
                'options': options, 'selected_mode': selected_mode, 'selection_source': selection_source,
                'fastest_mode': fastest_mode, 'duration_minutes': selected.get('duration_minutes'),
                'distance_meters': selected.get('distance_m'), 'data_basis': selected.get('source', 'unknown'),
                'estimated_cost': {'amount': None, 'basis': '交通费用不计入预算'},
                'note': '；'.join(notes)}
