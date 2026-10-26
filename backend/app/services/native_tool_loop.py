"""同步原生工具循环；只处理协议，业务派发和 MCP 由注册的工具执行。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import inspect
import json
import re
from threading import Event
from typing import Any, Callable, Protocol, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, ValidationError

from .llm_service import NativeLLMCompletion
from .observation_service import span, token_usage
from .native_observation import native_metadata, native_event_emitter
from .native_model_retry import MAX_MODEL_ATTEMPTS, is_connection_failure, retry_delay_seconds
from .model_diagnostics import provider_error_diagnostics


class NativeToolArguments(BaseModel):
    """工具参数须显式建模；默认拒绝未知字段，始终拒绝隐式类型转换。"""

    model_config = ConfigDict(extra="forbid", strict=True)


@dataclass(frozen=True)
class NativeTool:
    name: str
    description: str
    arguments_model: type[NativeToolArguments]
    handler: Callable[[Any], Any] = field(repr=False)

    def __post_init__(self):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", self.name):
            raise ValueError("工具名称不符合函数工具格式")
        if not self.description.strip():
            raise ValueError("工具必须有明确说明")
        if not issubclass(self.arguments_model, NativeToolArguments):
            raise ValueError("工具参数模型必须继承 NativeToolArguments")
        if (self.arguments_model.model_config.get("extra") not in {"forbid", "ignore"}
                or not self.arguments_model.model_config.get("strict")):
            raise ValueError("工具参数模型必须拒绝类型转换，未知字段只能拒绝或忽略")
        if (not callable(self.handler) or inspect.iscoroutinefunction(self.handler)
                or inspect.iscoroutinefunction(getattr(self.handler, "__call__", None))):
            raise ValueError("当前工具循环只接受同步执行函数")

    def declaration(self) -> dict[str, Any]:
        # 不默认发送 strict/tool_choice 等供应商扩展，端点兼容性待实测。
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": self.arguments_model.model_json_schema(),
        }}


class NativeModel(Protocol):
    def invoke_native(self, messages, *, tools, **kwargs) -> NativeLLMCompletion: ...


class NativeToolLoopError(RuntimeError):
    """只有稳定错误码，不携带模型、参数或工具异常原文。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(f"原生工具循环未完成：{code}")


@dataclass(frozen=True)
class NativeToolLoopResult:
    text: str = field(repr=False)
    messages: tuple[dict[str, Any], ...] = field(repr=False)
    completions: tuple[NativeLLMCompletion, ...] = field(repr=False)
    tool_calls: int
    model_attempts: int | None = None
    termination_reason: str = "model_stop"

    @property
    def model_calls(self) -> int:
        return self.model_attempts if self.model_attempts is not None else len(self.completions)


_TOOL_ERRORS = {
    "UNKNOWN_TOOL": "工具未注册或未向当前智能体开放",
    "INVALID_TOOL_ARGUMENTS": "工具参数不符合声明，请根据参数定义修正",
    "TOOL_EXECUTION_FAILED": "工具执行失败，可调整任务或保留缺项",
    "INVALID_TOOL_RESULT": "工具返回结果无法编码为 JSON",
}


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("重复 JSON 字段")
        result[key] = value
    return result


def _reject_constant(_):
    raise ValueError("非标准 JSON 数值")


def _json_default(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    raise TypeError("工具结果不支持的类型")


def _error_content(code, **details):
    return json.dumps({"ok": False, "error": {
        "code": code, "message": _TOOL_ERRORS[code], **details,
    }}, ensure_ascii=False)


class NativeToolLoop:
    """每次 run 使用本地消息列表；默认顺序执行，可注入业务批次执行器。

    上限是本次运行的技术边界，和每日期最多三轮业务修订无关。
    业务去重、依赖、并发与事实校验由工具执行层承担。
    """

    def __init__(self, llm: NativeModel, tools: Sequence[NativeTool], *,
                 max_model_calls: int = 8, max_tool_calls: int = 24,
                 batch_executor=None, observation_role=None, observation_stage=None,
                 accepted_submission_tools: frozenset[str] = frozenset()):
        if (type(max_model_calls) is not int or max_model_calls < 1
                or type(max_tool_calls) is not int or max_tool_calls < 1):
            raise ValueError("模型及工具调用上限必须是正整数")
        self.llm = llm
        self.tools = {tool.name: tool for tool in tools}
        if len(self.tools) != len(tools):
            raise ValueError("工具名称必须唯一")
        self.accepted_submission_tools = frozenset(accepted_submission_tools)
        if not self.accepted_submission_tools.issubset(self.tools):
            raise ValueError("提交结束工具必须来自本循环的注册表")
        self.max_model_calls = max_model_calls
        self.max_tool_calls = max_tool_calls
        self.batch_executor = batch_executor
        self.observation_metadata = {key: value for key, value in
            (("role", observation_role), ("stage", observation_stage)) if value is not None}

    @staticmethod
    def _check_cancelled(cancel_event):
        if cancel_event is not None and cancel_event.is_set():
            raise NativeToolLoopError("CANCELLED")

    @staticmethod
    def _wait_before_retry(delay, cancel_event):
        # Event.wait允许运行协调器的取消信号立即结束退避等待。
        (cancel_event if cancel_event is not None else Event()).wait(delay)

    def _request_model(self, history, request_number, cancel_event, model_options):
        """只重发当前模型请求；history中的工具结果和供应商额外字段保持原样。"""
        metadata = {**native_metadata(), **self.observation_metadata,
                    "request_number": request_number, "model_request_id": uuid4().hex,
                    "model": getattr(self.llm, "model", None), "tool_names": list(self.tools),
                    "max_attempts": MAX_MODEL_ATTEMPTS}
        effort = model_options.get("reasoning_effort", getattr(self.llm, "reasoning_effort", None))
        if isinstance(effort, str) and effort in {"low", "medium", "high", "max"}:
            metadata["reasoning_effort"] = effort
        declarations = [tool.declaration() for tool in self.tools.values()] or None
        # SDK重试保持关闭，由本层记录所有物理请求，避免叠加隐式重试。
        options = {**model_options, "max_retries": 0}
        for attempt in range(1, MAX_MODEL_ATTEMPTS + 1):
            self._check_cancelled(cancel_event)
            will_retry = False
            delay = 0.0
            try:
                with span("llm.native_tool_loop", "llm", {**metadata, "attempt": attempt}) as record:
                    try:
                        completion = self.llm.invoke_native(
                            deepcopy(history), tools=deepcopy(declarations), **deepcopy(options))
                    except Exception as exc:
                        retryable = is_connection_failure(exc)
                        will_retry = retryable and attempt < MAX_MODEL_ATTEMPTS
                        delay = retry_delay_seconds(attempt) if will_retry else 0.0
                        record.output = {"status": "failed", "retry": {
                            "retryable": retryable, "will_retry": will_retry,
                            "exhausted": retryable and not will_retry, "delay_seconds": delay}}
                        if record.model_diagnostics is None:
                            record.model_diagnostics = provider_error_diagnostics(exc)
                        raise NativeToolLoopError("MODEL_CALL_FAILED") from None
                    if not isinstance(completion, NativeLLMCompletion):
                        raise NativeToolLoopError("INVALID_COMPLETION")
                    record.usage = token_usage(completion.usage)
                    record.output = {"status": "returned", "finish_reason": completion.finish_reason}
                    return completion, attempt
            except NativeToolLoopError:
                self._check_cancelled(cancel_event)
                if not will_retry:
                    raise
            # 等待单独记录，模型调用耗时不包含退避，失败尝试仍保留failed状态。
            with span("model.retry_wait", "planning", {**metadata, "after_attempt": attempt,
                      "delay_seconds": delay}) as record:
                self._check_cancelled(cancel_event)
                self._wait_before_retry(delay, cancel_event)
                self._check_cancelled(cancel_event)
                record.output = {"status": "completed"}
        raise NativeToolLoopError("MODEL_CALL_FAILED")

    @staticmethod
    def _calls(message, seen_ids):
        calls = message.get("tool_calls")
        if calls is None:
            return []
        if not isinstance(calls, list):
            raise NativeToolLoopError("INVALID_TOOL_CALL")
        batch_ids = set()
        for call in calls:
            if not isinstance(call, dict) or call.get("type") != "function":
                raise NativeToolLoopError("INVALID_TOOL_CALL")
            call_id = call.get("id")
            function = call.get("function")
            if (not isinstance(call_id, str) or not call_id.strip()
                    or not isinstance(function, dict)
                    or not isinstance(function.get("name"), str)
                    or not function["name"].strip()
                    or not isinstance(function.get("arguments"), str)):
                raise NativeToolLoopError("INVALID_TOOL_CALL")
            if call_id in seen_ids or call_id in batch_ids:
                raise NativeToolLoopError("DUPLICATE_TOOL_CALL_ID")
            batch_ids.add(call_id)
        return calls

    def _record_argument_rejection(self, call, tool, issues, issue_count):
        native_event_emitter()("tool.arguments_rejected", {
            **self.observation_metadata, "tool_name": tool.name, "tool_call_id": call["id"],
            "code": "INVALID_TOOL_ARGUMENTS", "issues": issues,
            "issue_count": issue_count, "issues_truncated": issue_count > len(issues),
        })

    def _execute(self, call):
        function = call["function"]
        tool = self.tools.get(function["name"])
        if tool is None:
            return _error_content("UNKNOWN_TOOL")
        try:
            raw = function["arguments"]
            decoded = json.loads(raw, object_pairs_hook=_reject_duplicate_keys,
                                 parse_constant=_reject_constant)
            if not isinstance(decoded, dict):
                raise ValueError("工具参数必须是对象")
            arguments = tool.arguments_model.model_validate_json(raw, strict=True)
        except ValidationError as exc:
            schema = tool.arguments_model.model_json_schema()
            fields = set()
            def collect(value):
                if isinstance(value, dict):
                    fields.update(value.get("properties", {}))
                    for child in value.values():
                        collect(child)
                elif isinstance(value, list):
                    for child in value:
                        collect(child)
            collect(schema)
            # 只回传声明中的字段名与稳定错误类型，不回传输入、未知字段名或异常原文。
            errors = exc.errors(include_input=False, include_context=False, include_url=False)
            issues = [{"path": [part if isinstance(part, int) or part in fields else "unknown_field"
                                 for part in issue["loc"]], "type": issue["type"]}
                      for issue in errors[:8]]
            self._record_argument_rejection(call, tool, issues, len(errors))
            return _error_content("INVALID_TOOL_ARGUMENTS", issues=issues, expected_parameters=schema)
        except (ValueError, TypeError):
            self._record_argument_rejection(call, tool, [{"path": [], "type": "invalid_json_or_object"}], 1)
            return _error_content("INVALID_TOOL_ARGUMENTS")

        # 只记录工具名和结果分类，不保存参数、供应商推理或异常原文。
        with span("tool.native_call", "tool", {**native_metadata(), **self.observation_metadata, "tool_name": tool.name, "tool_call_id": call["id"]}) as record:
            try:
                result = tool.handler(arguments)
            except NativeToolLoopError as exc:
                if exc.code == "CANCELLED":
                    raise
                record.output = {"status": "failed", "code": "TOOL_EXECUTION_FAILED"}
                return _error_content("TOOL_EXECUTION_FAILED")
            except Exception:
                record.output = {"status": "failed", "code": "TOOL_EXECUTION_FAILED"}
                return _error_content("TOOL_EXECUTION_FAILED")
            try:
                content = json.dumps({"ok": True, "data": result},
                                     ensure_ascii=False, allow_nan=False, default=_json_default)
            except (TypeError, ValueError, OverflowError):
                record.output = {"status": "failed", "code": "INVALID_TOOL_RESULT"}
                return _error_content("INVALID_TOOL_RESULT")
            record.output = {"status": "completed"}
            return content

    def _accepted_submission(self, calls, contents):
        """只检查本批已编码回执；业务 handler 须在保存结构化结果后返回 accepted。"""
        if not self.accepted_submission_tools:
            return None
        accepted = None
        for call, content in zip(calls, contents):
            try:
                receipt = json.loads(content, object_pairs_hook=_reject_duplicate_keys,
                                     parse_constant=_reject_constant)
            except (ValueError, TypeError):
                return None
            if not isinstance(receipt, dict) or receipt.get("ok") is not True or "data" not in receipt:
                return None
            data = receipt["data"]
            status = data.get("status") if isinstance(data, dict) else None
            # 地图 handler 用 result_status/query_status 和 error 表示失败，
            # 外层 ok=true 只证明 handler 正常返回，不能掩盖这些执行错误。
            if isinstance(data, dict) and (
                    any(data.get(key) in ("rejected", "error", "failed")
                        for key in ("status", "result_status", "query_status"))
                    or data.get("error") is not None):
                return None
            if (accepted is None and call["function"]["name"] in self.accepted_submission_tools
                    and status == "accepted"):
                accepted = call
        return accepted

    def run(self, messages: Sequence[dict[str, Any]], *,
            cancel_event: Event | None = None, **model_options) -> NativeToolLoopResult:
        if "tools" in model_options:
            raise ValueError("工具列表只能来自本循环的注册表")
        history = deepcopy(list(messages))
        completions = []
        # 阶段切换仍保留原生历史；新调用ID不能碰撞此前阶段的调用。
        seen_ids = {call["id"] for message in history if isinstance(message, dict)
                    for call in (message.get("tool_calls") or []) if isinstance(call, dict)
                    and isinstance(call.get("id"), str)}
        tool_count = 0
        model_attempts = 0
        for request_number in range(1, self.max_model_calls + 1):
            self._check_cancelled(cancel_event)
            completion, attempts = self._request_model(history, request_number, cancel_event, model_options)
            model_attempts += attempts
            self._check_cancelled(cancel_event)
            completions.append(completion)
            message = deepcopy(completion.message)
            if not isinstance(message, dict) or message.get("role") != "assistant":
                raise NativeToolLoopError("INVALID_COMPLETION")
            if completion.finish_reason == "length":
                raise NativeToolLoopError("INCOMPLETE_COMPLETION")
            if message.get("refusal") or completion.finish_reason == "content_filter":
                raise NativeToolLoopError("MODEL_REFUSED")
            if (not isinstance(completion.finish_reason, str)
                    or completion.finish_reason not in {"stop", "tool_calls"}):
                raise NativeToolLoopError("INVALID_COMPLETION")
            calls = self._calls(message, seen_ids)
            if not calls:
                text = message.get("content")
                if (completion.finish_reason != "stop"
                        or not isinstance(text, str) or not text.strip()):
                    raise NativeToolLoopError("INVALID_COMPLETION")
                history.append(message)
                return NativeToolLoopResult(text, tuple(history), tuple(completions), tool_count, model_attempts)

            # 整批预检：无法匹配结果或没有后续模型轮次时，不先执行部分工具。
            if tool_count + len(calls) > self.max_tool_calls:
                raise NativeToolLoopError("TOOL_CALL_LIMIT")
            if request_number == self.max_model_calls:
                raise NativeToolLoopError("MODEL_CALL_LIMIT")
            history.append(message)
            if self.batch_executor is None:
                contents = []
                for call in calls:
                    self._check_cancelled(cancel_event)
                    contents.append(self._execute(call))
            else:
                contents = self.batch_executor(calls, self._execute)
                if len(contents) != len(calls) or any(not isinstance(item, str) for item in contents):
                    raise NativeToolLoopError("INVALID_BATCH_RESULT")
            self._check_cancelled(cancel_event)
            for call, content in zip(calls, contents):
                self._check_cancelled(cancel_event)
                history.append({"role": "tool", "tool_call_id": call["id"], "content": content})
                seen_ids.add(call["id"])
                tool_count += 1
            accepted = self._accepted_submission(calls, contents)
            self._check_cancelled(cancel_event)
            if accepted is not None:
                native_event_emitter()("loop.submission_accepted", {
                    **self.observation_metadata, "tool_name": accepted["function"]["name"],
                    "tool_call_id": accepted["id"], "termination_reason": "submission_accepted"})
                self._check_cancelled(cancel_event)
                return NativeToolLoopResult("", tuple(history), tuple(completions), tool_count,
                                            model_attempts, "submission_accepted")
        raise NativeToolLoopError("MODEL_CALL_LIMIT")
