"""项目内的执行适配：业务成功证据独立于观测存储。"""
import ast
import json
from datetime import date

from hello_agents import SimpleAgent
from ..services.observation_service import span


class PlanningError(RuntimeError):
    def __init__(self, code, message, step=None):
        super().__init__(message)
        self.code, self.step = code, step


def decode_result(value):
    """兼容当前 MCPTool 的文本外壳与 JSON 返回，不执行返回文本。"""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text.startswith("工具 '") and '\n' in text:
        text = text.split('\n', 1)[1].strip()
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        try:
            return ast.literal_eval(text)
        except (ValueError, SyntaxError):
            if text.startswith(('错误', '❌', 'Error', 'error')):
                raise PlanningError('TOOL_FAILED', '工具返回错误')
            raise PlanningError('TOOL_RESPONSE_INVALID', '工具返回内容无法验证')


def check_tool_result(data):
    if isinstance(data, dict):
        if (data.get('isError') is True or data.get('success') is False
                or str(data.get('status', '1')) == '0' or data.get('error')):
            raise PlanningError('TOOL_FAILED', '工具返回业务错误')
        for value in data.values():
            if isinstance(value, (dict, list)):
                check_tool_result(value)
    elif isinstance(data, list):
        for value in data:
            check_tool_result(value)


def find_items(data, key):
    if isinstance(data, dict):
        if key in data and isinstance(data[key], list):
            return data[key]
        for value in data.values():
            found = find_items(value, key)
            if found is not None:
                return found
    elif isinstance(data, list):
        for value in data:
            found = find_items(value, key)
            if found is not None:
                return found
    return None


class PlanningAgent(SimpleAgent):
    """覆盖吞异常的工具执行入口，保留框架消息循环及参数解析。"""
    def run(self, input_text, **kwargs):
        self.clear_history()
        self.tool_evidence = {}
        try:
            return super().run(input_text, **kwargs)
        finally:
            self.clear_history()

    def _execute_tool_call(self, tool_name, parameters):
        tool = self.tool_registry.get_tool(tool_name) if self.tool_registry else None
        if tool is None:
            raise PlanningError('TOOL_NOT_FOUND', '模型请求了不存在的工具')
        args = self._parse_tool_parameters(tool_name, parameters)
        with span('tool.' + tool_name, 'tool', args) as record:
            try:
                result = tool.run(args)
            except Exception as exc:
                raise PlanningError('TOOL_FAILED', '工具执行失败') from exc
            record.output = result
            data = decode_result(result)
            check_tool_result(data)
            self.tool_evidence.setdefault(tool_name, []).append(data)
        # 工具正常空返回仍是成功 Span，但业务必须立即终止，不能再让模型改词重查。
        if tool_name == 'amap_maps_text_search':
            self.require(tool_name, 'pois')
        elif tool_name == 'amap_maps_weather':
            forecast_by_date(self.require(tool_name, 'casts'))
        return f'🔧 工具 {tool_name} 执行结果：\n{result}'

    def require(self, name, key):
        evidence = self.tool_evidence.get(name, [])
        if not evidence:
            raise PlanningError('REQUIRED_TOOL_NOT_CALLED', '必要工具未实际执行')
        items = []
        for payload in evidence:
            values = find_items(payload, key)
            # 当前 amap MCP 将 casts 展平为 forecasts；同时兼容原始高德结构。
            if values is None and key == 'casts':
                values = find_items(payload, 'forecasts')
            items.extend(values or [])
        if not items:
            raise PlanningError('INSUFFICIENT_INFORMATION', '必要查询没有有效结果')
        return items


def forecast_by_date(casts):
    result = {}
    for cast in casts:
        if not isinstance(cast, dict):
            continue
        try:
            day = date.fromisoformat(cast['date']).isoformat()
        except (ValueError, TypeError, KeyError):
            continue
        if not cast.get('dayweather') or not cast.get('nightweather'):
            continue
        result[day] = cast
    if not result:
        raise PlanningError('INSUFFICIENT_INFORMATION', '天气查询没有有效预报')
    return result
