"""为旧的采集替身补齐公开证据契约，不调用外部服务。"""
from dataclasses import replace
from app.services.native_weather_service import NativeWeatherService
from app.services.native_endpoint_service import NativeEndpointService
from app.services.amap_contract_adapter import AmapContractAdapter
from app.services.day_repair_service import _merge_facts

def normalized_collection(state, run_id=None):
    run_id=run_id or state.run_id
    adapter=AmapContractAdapter(lambda *_: {'result_status':'empty','city':'上海','forecasts':[],'pois':[]})
    weather=state.weather or NativeWeatherService(adapter=adapter).run(state.planning_input,run_id=run_id)
    endpoints=state.endpoints or NativeEndpointService(adapter=adapter).run(state.planning_input,run_id=run_id)
    weather=replace(weather,run_id=run_id,executions=tuple(e.model_copy(update={'run_id':run_id}) for e in weather.executions))
    endpoints=replace(endpoints,run_id=run_id,executions=tuple(e.model_copy(update={'run_id':run_id}) for e in endpoints.executions))
    children=tuple(replace(c,task_result=c.task_result.model_copy(update={'run_id':run_id})) for c in state.children)
    facts=_merge_facts(_merge_facts(state.fact_store,weather.fact_store),endpoints.fact_store)
    return replace(state,run_id=run_id,weather=weather,endpoints=endpoints,children=children,fact_store=facts)
