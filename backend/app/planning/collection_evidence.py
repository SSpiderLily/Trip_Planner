"""天气及端点证据的纯复核；旧服务与新快照消费同一规则。"""
from app.models.planning_errors import PlanningError
from app.models.planning_evidence import WeatherEvidence, EndpointEvidence
from app.models.planning_v4 import FactStore
from app.services.planning_lodging_policy import city_for_days
from .weather_projection import WeatherQueryContext, _project_result
from .endpoint_projection import (EndpointVerificationExecution, EndpointVerificationContext,
    _EndpointQueryRecord, _project_execution, _exact_candidates, _city_conflicts,
    _reconcile_verified_endpoints, _fact_store)


def weather_scopes(planning):
    cities = {}
    for day in planning.days:
        cities.setdefault(city_for_days(planning, (day.day_id,)), []).append(day.day_id)
    return tuple((city, tuple(day_ids)) for city, day_ids in cities.items())


def _merge_weather(executions):
    sources, weather = {}, {}
    for execution in executions:
        for rows, values, key_name in ((sources, execution.fact_store.sources, "tool_call_id"),
                                       (weather, execution.fact_store.weather, "fact_id")):
            for value in values:
                key = getattr(value, key_name)
                if key in rows and rows[key] != value:
                    raise ValueError("天气事实标识冲突")
                rows[key] = value
    return FactStore(sources=tuple(sources.values()), weather=tuple(weather.values()))


def validate_weather_evidence(planning, run_id, result):
    """消费者再次核对身份、城市/日期、投影与来源，拒绝外部注入错误状态。"""
    try:
        if not isinstance(result, WeatherEvidence) or result.run_id != run_id or result.planning_input != planning:
            raise ValueError("天气运行身份不一致")
        scopes = weather_scopes(planning)
        if len(result.executions) != len(scopes):
            raise ValueError("天气城市范围不完整")
        for (city, day_ids), execution in zip(scopes, result.executions):
            context = WeatherQueryContext.from_planning_input(planning, run_id=run_id,
                context_revision=1, day_ids=day_ids)
            dates = tuple(day.day_date for day in planning.days if day.day_id in day_ids)
            diagnostic = execution.diagnostic_tool_result
            if (diagnostic.source.tool_name != "maps_weather"
                    or diagnostic.source.tool_call_id != diagnostic.tool_call_id
                    or (diagnostic.data is not None and diagnostic.data.source != diagnostic.source)):
                raise ValueError("天气来源不一致")
            expected = _project_result(context, city, dates, diagnostic)
            if execution != expected:
                raise ValueError("天气投影与原始结果不一致")
        if result.fact_store != _merge_weather(result.executions):
            raise ValueError("天气合并事实不一致")
    except (AttributeError, TypeError, ValueError, KeyError):
        raise PlanningError("WEATHER_RESULT_INVALID", "天气事实结果无效", step="weather_projection") from None


def endpoint_scopes(planning):
    groups = {}
    for kind, day in (("arrival", planning.days[0]), ("departure", planning.days[-1])):
        groups.setdefault(city_for_days(planning, (day.day_id,)), []).append(kind)
    return tuple((city, tuple(kinds)) for city, kinds in groups.items())


def _context(planning, run_id, kinds):
    return EndpointVerificationContext.from_planning_input(planning, run_id=run_id,
        context_revision=1, endpoint_kinds=kinds)


def _checked_execution(context, execution):
    """从地图诊断结果重建核实状态，拒绝把歧义候选冒充唯一地点。"""
    if not isinstance(execution, EndpointVerificationExecution):
        raise ValueError("到离地点结果类型无效")
    records = {}
    for kind in context.endpoint_kinds:
        query = getattr(context.planning_input.request, f"{kind}_place_query")
        if not query or not query.strip():
            continue
        endpoint = getattr(execution, kind)
        search = endpoint.search_result
        if search is None or search.source.tool_name != "maps_text_search" or search.tool_call_id != search.source.tool_call_id:
            raise ValueError("到离地点搜索来源无效")
        key = query.strip()
        record = _EndpointQueryRecord(key, search)
        exact, conflicts = _exact_candidates(key, search.data.places if search.data is not None else ())
        if len(endpoint.detail_results) > 1:
            raise ValueError("到离地点详情范围无效")
        if endpoint.detail_results:
            if (search.status != "ok" or len(exact) != 1 or conflicts
                    or (exact[0].location is not None and exact[0].city)
                    or _city_conflicts(exact[0].city, execution.city)):
                raise ValueError("到离地点不能查询范围外的详情")
            detail = endpoint.detail_results[0]
            if detail.source.tool_name != "maps_search_detail" or detail.tool_call_id != detail.source.tool_call_id:
                raise ValueError("到离地点详情来源无效")
            record.details[exact[0].poi_id] = detail
        if key in records and (records[key].search != search or records[key].details != record.details):
            raise ValueError("同一到离查询结果冲突")
        records[key] = record
    expected = _project_execution(context, records)
    if expected != execution:
        raise ValueError("到离地点核实状态与地图结果不一致")


def _combine(executions, scopes):
    endpoints = {kind: getattr(execution, kind)
                 for (_, kinds), execution in zip(scopes, executions) for kind in kinds}
    endpoints, facts = _reconcile_verified_endpoints(endpoints)
    return endpoints, _fact_store(facts, endpoints)


def validate_endpoint_evidence(planning, run_id, result):
    try:
        if not isinstance(result, EndpointEvidence) or result.run_id != run_id or result.planning_input != planning:
            raise ValueError("到离地点运行身份不一致")
        scopes = endpoint_scopes(planning)
        if len(result.executions) != len(scopes):
            raise ValueError("到离地点范围不完整")
        for (_, kinds), execution in zip(scopes, result.executions):
            _checked_execution(_context(planning, run_id, kinds), execution)
        endpoints, facts = _combine(result.executions, scopes)
        if result.arrival != endpoints["arrival"] or result.departure != endpoints["departure"] or result.fact_store != facts:
            raise ValueError("到离地点合并结果不一致")
    except (AttributeError, TypeError, ValueError, KeyError, IndexError):
        raise PlanningError("ENDPOINT_RESULT_INVALID", "到离地点事实结果无效", step="endpoint_projection") from None
