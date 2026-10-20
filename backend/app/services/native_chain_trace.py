"""独立链路的脱敏阶段日志；不记录模型推理、供应商额外字段或异常原文。"""
from __future__ import annotations

from copy import deepcopy
import json
from time import monotonic
from threading import RLock, Lock

from app.services.model_diagnostics import provider_error_diagnostics
from app.services.observation_service import sanitize, token_usage


class NativeChainTrace:
    def __init__(self, sink=None, *, secrets=()):
        self.events = []
        self.sink = sink
        self.secrets = tuple(secret for secret in secrets if secret)
        self.started = monotonic()
        self._lock = RLock()

    def emit(self, event, data):
        try:
            safe = sanitize(deepcopy(data))
            text = json.dumps(safe, ensure_ascii=False, allow_nan=False)
            for secret in self.secrets:
                text = text.replace(secret, "[REDACTED]")
            safe = json.loads(text)
        except (ValueError, TypeError, OverflowError):
            safe = {"trace_status": "unavailable"}
        with self._lock:
            self._append(event, safe)

    def _append(self, event, safe):
        row = {"sequence": len(self.events) + 1, "elapsed_seconds": round(monotonic() - self.started, 3),
               "event": event, "data": safe}
        self.events.append(row)
        if self.sink is not None:
            try:
                self.sink(row)
            except Exception:
                pass  # 日志输出失败不能改变业务结果。


class TracedNativeModel:
    def __init__(self, llm, role, trace):
        self.llm, self.role, self.trace = llm, role, trace
        self.requests = 0
        self._lock = Lock()

    def invoke_native(self, messages, *, tools, **kwargs):
        with self._lock:
            self.requests += 1
            request_number = self.requests
        model = getattr(self.llm, "model", None)
        self.trace.emit("model.request", {"role": self.role, "model": model if isinstance(model, str) else None,
                                        "request_number": request_number,
                                        "message_count": len(messages),
                                        "tools": [item["function"]["name"] for item in tools or []]})
        try:
            completion = self.llm.invoke_native(messages, tools=tools, **kwargs)
        except Exception as exc:
            self.trace.emit("model.error", {"role": self.role, "request_number": request_number, "diagnostics": provider_error_diagnostics(exc)})
            raise
        calls = []
        for item in completion.message.get("tool_calls") or []:
            function = item.get("function", {})
            try:
                arguments = json.loads(function.get("arguments", ""))
            except (ValueError, TypeError):
                arguments = {"parse_status": "invalid_json"}
            calls.append({"id": item.get("id"), "name": function.get("name"), "arguments": arguments})
        usage = token_usage(completion.usage)
        self.trace.emit("model.response", {"role": self.role, "request_number": request_number,
            "finish_reason": completion.finish_reason, "tool_calls": calls,
            "content": completion.message.get("content"),
            "usage_counts": {"input": usage["input_tokens"], "output": usage["output_tokens"],
                             "total": usage["total_tokens"]} if usage else None})
        return completion
