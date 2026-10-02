"""无状态签名日快照与局部编辑；只允许服务端发出的行程继续编辑。"""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import re
import secrets
import time
from contextvars import ContextVar
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from uuid import uuid4

from ..agents.execution import check_tool_result, decode_result, find_items
from ..services.planning_maps import PlanningMaps
from ..config import get_settings
from .cost_service import queried_reference_cost, summarize_day_costs, summarize_trip
from .route_options_service import RouteOptionsService, choose_mode
from .schedule_service import schedule_day, opening_conflict


class DayEditError(RuntimeError):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code, self.status = code, status


class DayEditService:
    MAX_TOKEN_CHARS = 100_000
    MAX_SNAPSHOT_BYTES = 72_000
    MAX_OPERATIONS = 20
    _deadline = ContextVar('day_edit_deadline', default=None)

    def __init__(self, repository, *, signing_key: bytes | None = None, query_tool=None, poi_resolver=None):
        self.repository = repository
        self.signing_key = signing_key or secrets.token_bytes(32)
        self.query_tool = query_tool
        self.poi_resolver = poi_resolver

    def attach_tokens(self, task_id: str, result: dict) -> dict:
        response = copy.deepcopy(result)
        if response.get('schema_version') != 3:
            return response
        for day in response.get('days', []):
            day.pop('edit_token', None)
            day['edit_available'] = True
            try:
                day['edit_token'] = self._sign_snapshot(task_id, day, [])
            except DayEditError:
                day['edit_token'] = None
                day['edit_available'] = False
                issues = day.setdefault('issues', [])
                issues.append(self._make_issue('EDIT_UNAVAILABLE', '当天内容过多，暂时无法编辑；行程仍可查看和导出', day=day.get('date')))
                for index, issue in enumerate(issues, 1):
                    issue['issue_id'] = self._stable_issue_id(day.get('date', ''), issue, index)
        return response

    def _sign_snapshot(self, task_id: str, day: dict, revoked_ids: list[str]) -> str:
        snapshot = copy.deepcopy(day)
        snapshot.pop('edit_token', None)
        payload = json.dumps({'task_id': task_id, 'date': day['date'], 'day': snapshot,
                              'revoked_requirement_ids': sorted(set(revoked_ids))},
                             ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        if len(payload) > self.MAX_SNAPSHOT_BYTES:
            raise DayEditError('EDIT_SNAPSHOT_TOO_LARGE', '当天行程内容过大，暂时无法编辑', 413)
        encoded = base64.urlsafe_b64encode(payload).decode().rstrip('=')
        signature = hmac.new(self.signing_key, encoded.encode(), hashlib.sha256).hexdigest()
        return f'{encoded}.{signature}'

    def _decode(self, token: str, task_id: str, day_date: str) -> tuple[dict, list[str]]:
        try:
            encoded, signature = token.split('.', 1)
            expected = hmac.new(self.signing_key, encoded.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            raw = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4))
            payload = json.loads(raw)
            if payload.get('task_id') != task_id or payload.get('date') != day_date:
                raise ValueError
            return payload['day'], payload.get('revoked_requirement_ids', [])
        except Exception as exc:
            raise DayEditError('EDIT_TOKEN_INVALID', '编辑凭证无效或服务已重启，请重新载入行程', 409) from exc

    def recalculate(self, body) -> dict:
        task_id, day_date = body.task_id, body.date
        status = self.repository.status(task_id)
        if status is None:
            raise DayEditError('TASK_NOT_FOUND', '原行程已不存在或已清理', 404)
        if status['status'] != 'succeeded':
            raise DayEditError('TASK_NOT_READY', '原行程尚未生成完成，无法编辑', 409)
        original = self.repository.result(task_id)
        stored_request = self.repository.request(task_id)
        if not original or original.get('schema_version') != 3:
            raise DayEditError('EDIT_VERSION_UNSUPPORTED', '该行程版本只支持查看，请重新生成后编辑', 409)
        orig_day = next((item for item in original.get('days', []) if item.get('date') == day_date), None)
        if orig_day is None:
            raise DayEditError('DAY_NOT_FOUND', '目标日期不属于此行程', 404)
        day, revoked = self._decode(body.edit_token, task_id, day_date)
        requirement_ids = {item.get('requirement_id') for item in original.get('planning_conditions', {}).get('must_visit_requests', [])}
        if not set(revoked).issubset(requirement_ids):
            raise DayEditError('EDIT_TOKEN_INVALID', '编辑凭证内容无效，请重新载入行程', 409)
        operations = [item.model_dump(exclude_none=True) if hasattr(item, 'model_dump') else dict(item)
                      for item in body.operations]
        if not 1 <= len(operations) <= self.MAX_OPERATIONS:
            raise DayEditError('INVALID_OPERATIONS', '一次编辑最多包含20项操作', 422)
        deadline_token = self._deadline.set(time.monotonic() + 90)
        try:
            route_service = self._new_route_service()
            for operation in operations:
                self._apply_operation(day, operation, revoked, original, stored_request, route_service)
            self._rebuild_day(day, original, stored_request, route_service, revoked)
        finally:
            self._deadline.reset(deadline_token)
        all_days = [copy.deepcopy(item) for item in original.get('days', [])]
        all_days = [day if item.get('date') == day_date else item for item in all_days]
        conditions = original.get('planning_conditions', {})
        lodging_nights = max(0, (date.fromisoformat(conditions['end_date']) - date.fromisoformat(conditions['start_date'])).days)
        day_index = next(i for i, item in enumerate(all_days) if item.get('date') == day_date)
        day['cost_summary'] = summarize_day_costs(
            day.get('activities', []), lodging_base=original.get('lodging_base'),
            lodging_nights=lodging_nights if day_index == 0 else 0, include_lodging_unknown=day_index == 0)
        all_days[day_index] = day
        summary = summarize_trip(all_days, conditions.get('budget_per_person'))
        if not day['cost_summary']['complete']:
            day['issues'].append(self._make_issue('COST_UNKNOWN', '未查询到真实数据，费用仅汇总已知项目', day=day_date))
        if summary.get('over_budget_known'):
            day['issues'].append(self._make_issue('BUDGET_EXCEEDED', '已知参考费用已超过预算', category='needs_adjustment', day=day_date))
        for index, issue in enumerate(day['issues'], 1):
            issue['issue_id'] = self._stable_issue_id(day_date, issue, index)
        day['edit_token'] = self._sign_snapshot(task_id, day, revoked)
        return {'request_id': body.request_id, 'client_revision': body.client_revision,
                'date': day_date, 'day': day, 'cost_summary': summary}

    def _apply_operation(self, day, operation, revoked, original, stored_request, route_service):
        activities = day.get('activities', [])
        kind = operation['type']
        if kind == 'add_poi':
            identity = operation['poi_id']
            client_id = operation['client_activity_id']
            if any(item.get('activity_id') == client_id for item in activities):
                raise DayEditError('ACTIVITY_ID_EXISTS', '新增景点标识已在当天使用', 409)
            if any((item.get('place') or {}).get('source_id') == identity for item in activities):
                raise DayEditError('POI_ALREADY_IN_DAY', '该地点已加入当天', 409)
            place = self._resolve_poi(identity, original['planning_conditions']['city'])
            activity = {'activity_id': client_id, 'type': 'sightseeing', 'period': 'afternoon',
                        'title': place['name'], 'description': '', 'duration_minutes': 90,
                        'place': place, 'requirement_ids': [], 'opening_hours': place.get('opening_hours'),
                        'photos': place.get('photos') or [], 'reference_cost': queried_reference_cost(place, 'tickets'),
                        'start_at': None, 'end_at': None}
            index = self._best_insertion_index(day, activity, original, stored_request, route_service)
            activities.insert(index, activity)
            return
        if kind == 'delete_activity':
            activity = next((item for item in activities if item.get('activity_id') == operation['activity_id']), None)
            if activity is None:
                raise DayEditError('ACTIVITY_NOT_FOUND', '该活动已不存在', 409)
            required = {item.get('requirement_id') for item in original['planning_conditions'].get('must_visit_requests', [])}
            revoked.extend(identity for identity in activity.get('requirement_ids', [])
                           if identity in required and identity not in revoked)
            activities.remove(activity)
            if not any(item.get('type') == 'sightseeing' for item in activities):
                activities[:] = [item for item in activities
                                 if item.get('type') != 'meal' or bool(item.get('requirement_ids'))]
            return
        if kind == 'move_activity':
            index = next((i for i, item in enumerate(activities) if item.get('activity_id') == operation['activity_id']), None)
            if index is None:
                raise DayEditError('ACTIVITY_NOT_FOUND', '该活动已不存在', 409)
            target = index + (-1 if operation['direction'] == 'up' else 1)
            if 0 <= target < len(activities):
                activities[index], activities[target] = activities[target], activities[index]
            return
        if kind == 'select_leg_mode':
            leg = next((item for item in day.get('legs', []) if item.get('from_activity_id') == operation['from_activity_id']
                        and item.get('to_activity_id') == operation['to_activity_id']), None)
            if leg is None:
                raise DayEditError('LEG_NOT_FOUND', '该路段已变化，请重试', 409)
            option = (leg.get('options') or {}).get(operation['mode'])
            if not option or option.get('status') != 'available' or option.get('duration_minutes') is None:
                raise DayEditError('ROUTE_MODE_UNAVAILABLE', '该路段没有此方式的可用耗时', 422)
            leg['selected_mode'] = operation['mode']
            leg['selection_source'] = 'manual'
            return
        raise DayEditError('INVALID_OPERATION', '不支持的编辑操作', 422)

    def _resolve_poi(self, identity: str, city: str) -> dict:
        try:
            if self.poi_resolver:
                value = self.poi_resolver(identity, city)
            else:
                from .amap_service import AmapService
                detail_data = self._call_mcp('maps_search_detail', {'id': identity})
                detail = self._unwrap_detail(detail_data)
                if detail.get('id') != identity or not detail.get('name'):
                    value = None
                else:
                    city_name = str(detail.get('cityname') or detail.get('city') or '')
                    if city_name and city_name not in city and city not in city_name:
                        value = None
                    else:
                        search = self._call_mcp('maps_text_search', {'keywords': detail['name'], 'city': city, 'citylimit': 'true'})
                        matches = find_items(search, 'pois') or []
                        verified = any(isinstance(item, dict) and item.get('id') == identity for item in matches)
                        normalized = AmapService._poi(detail) if verified else None
                        value = normalized.model_dump() if normalized else None
            if hasattr(value, 'model_dump'):
                value = value.model_dump()
            if not isinstance(value, dict) or value.get('id') != identity or not value.get('name'):
                raise DayEditError('POI_NOT_FOUND_IN_CITY', '未能在当前目的地城市核实该地点', 422)
            location = value.get('location') or {}
            if not isinstance(location, dict) or location.get('longitude') is None or location.get('latitude') is None:
                raise DayEditError('POI_LOCATION_MISSING', '该地点没有可用地图坐标', 422)
            return {'source': 'amap', 'source_id': identity, 'name': value['name'],
                    'address': value.get('address') or '', 'longitude': float(location['longitude']),
                    'latitude': float(location['latitude']), 'tel': value.get('tel'),
                    'photos': value.get('photos') or [], 'opening_hours': value.get('opening_hours'),
                    'reference_cost': value.get('reference_cost'), 'cost_basis': value.get('cost_basis')}
        except DayEditError:
            raise
        except Exception as exc:
            raise DayEditError('POI_LOOKUP_FAILED', '地图地点查询失败，请稍后重试', 502) from exc

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

    def _call_mcp(self, name, arguments):
        from .amap_service import get_amap_mcp_tool
        deadline = self._deadline.get()
        remaining = deadline - time.monotonic() if deadline else get_settings().planner_tool_timeout_seconds
        if remaining <= 0.5:
            raise TimeoutError
        tool = PlanningMaps(get_amap_mcp_tool(), min(get_settings().planner_tool_timeout_seconds, remaining))
        value = tool.run({'action': 'call_tool', 'tool_name': name, 'arguments': arguments})
        data = decode_result(value)
        check_tool_result(data)
        return data if isinstance(data, dict) else {}

    def _new_route_service(self):
        settings = get_settings()
        return RouteOptionsService(self._query_route, query_limit=min(settings.planner_route_query_limit, 80))

    def _query_route(self, tool_name, arguments):
        if self.query_tool:
            return self.query_tool(tool_name, arguments)
        try:
            from .amap_service import get_amap_mcp_tool
            deadline = self._deadline.get()
            remaining = deadline - time.monotonic() if deadline else get_settings().planner_tool_timeout_seconds
            if remaining <= 0.5:
                raise TimeoutError
            seconds = min(get_settings().planner_tool_timeout_seconds, remaining)
            tool = PlanningMaps(get_amap_mcp_tool(), seconds)
            value = tool.run({'action': 'call_tool', 'tool_name': tool_name, 'arguments': arguments})
            data = decode_result(value)
            check_tool_result(data)
            return data
        except Exception as exc:
            raise DayEditError('ROUTE_QUERY_FAILED', '地图路线查询失败', 502) from exc

    def _rebuild_day(self, day, original, stored_request, route_service, revoked):
        conditions = original.get('planning_conditions', {})
        old_legs = list(day.get('legs', []))
        old_by_identity = {}
        for leg in old_legs:
            key = self._endpoint_key(leg.get('origin'), leg.get('destination'))
            old_by_identity[key] = leg
        route_points = [activity for activity in day.get('activities', [])
                        if activity.get('type') != 'free_time' and activity.get('place')]
        rebuilt = []
        previous_id = None
        previous_place = None
        is_first = day['date'] == conditions.get('start_date')
        is_last = day['date'] == conditions.get('end_date')
        if not route_points:
            day['legs'] = []
            schedule_day(day, conditions)
            day['issues'] = self._day_issues(day, conditions, original, old_legs, revoked)
            return
        if not is_first:
            previous_id, previous_place = 'lodging', (original.get('lodging_base') or {}).get('place')
        elif conditions.get('arrival_place_id'):
            previous_id, previous_place = 'arrival', conditions.get('arrival_place')
            if previous_place is None:
                previous_place = {'source_id': 'arrival', 'name': '到达地点', 'longitude': None, 'latitude': None}
        for activity in route_points:
            place = activity['place']
            if previous_id is not None:
                rebuilt.append(self._build_leg(previous_id, previous_place, activity['activity_id'], place,
                                               old_by_identity, route_service, conditions))
            previous_id, previous_place = activity['activity_id'], place
        if is_last:
            target_id, target_place = ('departure', conditions.get('departure_place')) if conditions.get('departure_place_id') else (None, None)
            if target_id and target_place is None:
                target_place = {'source_id': 'departure', 'name': '离开地点', 'longitude': None, 'latitude': None}
        else:
            target_id, target_place = 'lodging', (original.get('lodging_base') or {}).get('place')
        if previous_id is not None and target_id is not None:
            rebuilt.append(self._build_leg(previous_id, previous_place, target_id, target_place,
                                           old_by_identity, route_service, conditions))
        day['legs'] = rebuilt
        schedule_day(day, conditions)
        day['issues'] = self._day_issues(day, conditions, original, old_legs, revoked)

    @staticmethod
    def _endpoint_key(origin, destination):
        return ((origin or {}).get('source_id'), (destination or {}).get('source_id'))

    def _build_leg(self, from_id, origin, to_id, destination, old_by_identity, route_service, conditions):
        old = old_by_identity.get(self._endpoint_key(origin, destination))
        if old:
            leg = copy.deepcopy(old)
            leg['from_activity_id'], leg['to_activity_id'] = from_id, to_id
            return leg
        include_driving = conditions.get('transportation') == 'driving'
        options = route_service.options(origin, destination, conditions.get('city', ''), include_driving=include_driving)
        preferred = conditions.get('transportation') or 'transit'
        fastest = choose_mode(options, preferred)
        selected = fastest or preferred
        choice = options.get(selected, {})
        return {'leg_id': f"{(origin or {}).get('source_id', from_id)}->{(destination or {}).get('source_id', to_id)}",
                'from_activity_id': from_id, 'to_activity_id': to_id, 'origin': origin, 'destination': destination,
                'options': options, 'selected_mode': selected, 'selection_source': 'fastest',
                'fastest_mode': fastest, 'duration_minutes': choice.get('duration_minutes'),
                'distance_m': choice.get('distance_m'), 'note': ''}

    def _day_issues(self, day, conditions, original, old_legs, revoked):
        derived = {'ROUTE_UNKNOWN', 'TIME_EXCEEDED', 'COST_UNKNOWN', 'OPENING_UNKNOWN', 'OPENING_CONFLICT', 'MUST_VISIT_MISSING'}
        issues = [copy.deepcopy(issue) for issue in original.get('issues', [])
                  if issue.get('date') == day['date'] and issue.get('code') not in derived]
        for activity in day.get('activities', []):
            if activity.get('type') != 'sightseeing':
                continue
            if not activity.get('opening_hours'):
                issues.append(self._make_issue('OPENING_UNKNOWN', '未查询到开放时间', day=day['date'], activity=activity['activity_id']))
            elif self._opening_conflict(activity):
                issues.append(self._make_issue('OPENING_CONFLICT', '按当前参考时间可能与已查询开放时间冲突', category='needs_adjustment',
                                           day=day['date'], activity=activity['activity_id']))
        for leg in day.get('legs', []):
            mode = leg.get('selected_mode')
            option = (leg.get('options') or {}).get(mode, {})
            if option.get('status') != 'available' or option.get('duration_minutes') is None:
                issues.append(self._make_issue('ROUTE_UNKNOWN', '该路段所选方式暂无耗时数据', day=day['date'], leg=leg['leg_id']))
        if day.get('time_summary', {}).get('possible_overrun_minutes'):
            minutes = day['time_summary']['possible_overrun_minutes']
            issues.append(self._make_issue('TIME_EXCEEDED', f'按当前预估，可能超时约 {minutes} 分钟', category='needs_adjustment', day=day['date']))
        present_requirements = {identity for activity in day.get('activities', []) for identity in activity.get('requirement_ids', [])}
        for requirement in conditions.get('must_visit_requests', []):
            identity = requirement.get('requirement_id')
            if identity and identity not in present_requirements and identity not in revoked:
                # Only retain or report requirements originally scheduled on this date.
                was_here = any(identity in activity.get('requirement_ids', [])
                               for activity in next((entry.get('activities', []) for entry in original.get('days', [])
                                                     if entry.get('date') == day['date']), []))
                if was_here:
                    issues.append(self._make_issue('MUST_VISIT_MISSING', '必去地点未安排', category='needs_adjustment',
                                               day=day['date'], activity=identity))
        for index, issue in enumerate(issues, 1):
            issue['issue_id'] = self._stable_issue_id(day['date'], issue, index)
        return issues

    @staticmethod
    def _make_issue(code, message, *, category='needs_confirmation', day=None, activity=None, leg=None):
        return {'code': code, 'category': category, 'date': day, 'activity_id': activity, 'leg_id': leg, 'message': message}

    @staticmethod
    def _stable_issue_id(day, issue, index):
        source = '|'.join(str(issue.get(key) or '') for key in ('code', 'date', 'activity_id', 'leg_id', 'message'))
        return 'issue_' + hashlib.sha256(source.encode()).hexdigest()[:16]

    @staticmethod
    def _opening_conflict(activity):
        return opening_conflict(activity) is True

    def _best_insertion_index(self, day, activity, original, stored_request, route_service):
        activities = day.get('activities', [])
        if not activities:
            return 0
        conditions = original.get('planning_conditions', {})
        lodging = (original.get('lodging_base') or {}).get('place')
        arrival = conditions.get('arrival_place')
        departure = conditions.get('departure_place')
        candidates = []
        for index in range(len(activities) + 1):
            before = next((item for item in reversed(activities[:index]) if item.get('place')), None)
            after = next((item for item in activities[index:] if item.get('place')), None)
            origin = (before or {}).get('place')
            destination = (after or {}).get('place')
            if origin is None and day['date'] != conditions.get('start_date'):
                origin = lodging
            elif origin is None and conditions.get('arrival_place_id'):
                origin = arrival or {'source_id': 'arrival', 'longitude': None, 'latitude': None}
            if destination is None and day['date'] != conditions.get('end_date'):
                destination = lodging
            elif destination is None and conditions.get('departure_place_id'):
                destination = departure or {'source_id': 'departure', 'longitude': None, 'latitude': None}
            proxy = self._detour_proxy(origin, activity['place'], destination)
            candidates.append((proxy, index, origin, destination))
        candidates.sort(key=lambda row: (row[0], row[1]))
        ranked = []
        for proxy, index, origin, destination in candidates[:6]:
            route_cost = self._insertion_route_cost(route_service, day, index, activity, origin, destination,
                                                     original['planning_conditions'])
            ranked.append((route_cost, proxy, index))
        ranked.sort(key=lambda row: (row[0][0], row[0][1], row[0][2], row[1], row[2]))
        return ranked[0][2] if ranked else 0

    @staticmethod
    def _distance_proxy(a, b):
        try:
            dx = (float(a['longitude']) - float(b['longitude'])) * 111_000
            dy = (float(a['latitude']) - float(b['latitude'])) * 111_000
            return (dx * dx + dy * dy) ** 0.5
        except (TypeError, ValueError, KeyError):
            return float('inf')

    def _detour_proxy(self, origin, inserted, destination):
        if origin is None:
            return self._distance_proxy(inserted, destination) if destination else 0
        if destination is None:
            return self._distance_proxy(origin, inserted)
        return (self._distance_proxy(origin, inserted) + self._distance_proxy(inserted, destination)
                - self._distance_proxy(origin, destination))

    def _insertion_route_cost(self, route_service, day, index, activity, origin, destination, conditions):
        inserted = activity['place']
        segments = [(origin, inserted), (inserted, destination)]
        total, unknown, incoming_duration = 0, 0, None
        for segment_index, (start, end) in enumerate(segments):
            if not start or not end:
                continue
            options = route_service.options(start, end, conditions.get('city', ''),
                                            include_driving=conditions.get('transportation') == 'driving')
            mode = choose_mode(options)
            duration = options.get(mode, {}).get('duration_minutes') if mode else None
            if duration is None:
                unknown += 1
            else:
                total += duration
                if segment_index == 0:
                    incoming_duration = duration
        if origin and destination:
            prior = next((leg for leg in day.get('legs', [])
                          if (leg.get('origin') or {}).get('source_id') == origin.get('source_id')
                          and (leg.get('destination') or {}).get('source_id') == destination.get('source_id')), None)
            if prior:
                old = (prior.get('options') or {}).get(prior.get('selected_mode'), {}).get('duration_minutes')
                if old is not None:
                    total -= old
        open_penalty = 0
        if activity.get('opening_hours') and incoming_duration is not None:
            previous = next((item for item in reversed(day.get('activities', [])[:index]) if item.get('end_at')), None)
            day_date = day['date']
            if previous:
                starts_at = datetime.fromisoformat(previous['end_at']) + timedelta(minutes=incoming_duration)
            elif conditions.get('arrival_at') and conditions.get('start_date') == day_date:
                starts_at = datetime.fromisoformat(conditions['arrival_at'].replace('Z', '+00:00')).astimezone(ZoneInfo('Asia/Shanghai')) + timedelta(minutes=30 + incoming_duration)
            else:
                starts_at = datetime.fromisoformat(day_date).replace(hour=9, tzinfo=ZoneInfo('Asia/Shanghai')) + timedelta(minutes=incoming_duration)
            candidate = dict(activity, start_at=starts_at.isoformat(timespec='minutes'),
                             end_at=(starts_at + timedelta(minutes=activity.get('duration_minutes') or 0)).isoformat(timespec='minutes'))
            open_penalty = int(opening_conflict(candidate) is True)
        return unknown, open_penalty, total
