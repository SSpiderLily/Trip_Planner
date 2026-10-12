"""模型响应诊断只保留结构化白名单，不保存异常原文、提示词或响应正文。"""
import re

_FINISH = {"stop", "length", "content_filter", "tool_calls", "function_call"}
_TYPES = {
    "APIConnectionError", "APITimeoutError", "APIStatusError", "AuthenticationError",
    "PermissionDeniedError", "RateLimitError", "BadRequestError", "NotFoundError",
    "InternalServerError", "UnprocessableEntityError", "ConnectError", "ReadError",
    "RemoteProtocolError", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
    "TimeoutError", "ConnectionError", "SSLError", "SSLEOFError", "HelloAgentsException",
    "ValueError", "TypeError", "IndexError", "AttributeError",
}
_CODES = {
    "insufficient_quota", "invalid_api_key", "invalid_request_error", "invalid_request",
    "rate_limit_exceeded", "model_not_found", "context_length_exceeded",
    "server_error", "internal_error", "overloaded_error", "permission_denied",
    "authentication_error", "quota_exceeded", "content_filter", "invalid_parameter",
}


def _field(value, name):
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _count(value):
    return value if type(value) is int and value >= 0 else None


def completion_diagnostics(response):
    choice = response.choices[0]
    reason = getattr(choice, "finish_reason", None)
    usage = getattr(response, "usage", None)
    detail = _field(usage, "completion_tokens_details")
    return {
        "outcome": "returned",
        "finish_reason": safe_finish_reason(reason),
        "usage_counts": {
            "input": _count(_field(usage, "prompt_tokens")),
            "output": _count(_field(usage, "completion_tokens")),
            "total": _count(_field(usage, "total_tokens")),
            "reasoning": _count(_field(detail, "reasoning_tokens")),
        },
    }


def provider_error_diagnostics(exc):
    chain, pending, seen = [], [exc], set()
    status, code, request_id = None, None, None
    while pending and len(seen) < 8:
        current = pending.pop(0)
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        name = type(current).__name__
        chain.append(name if name in _TYPES else "OtherException")
        value = getattr(current, "status_code", None)
        if status is None and type(value) is int and 100 <= value <= 599:
            status = value
        body = getattr(current, "body", None)
        if isinstance(body, dict):
            error = body.get("error", body)
            if isinstance(error, dict):
                value = error.get("code") or error.get("type")
                if code is None and isinstance(value, str):
                    code = value if value in _CODES else "other"
        value = getattr(current, "request_id", None)
        # 接受标准UUID或常见req标识；不读取任意响应头、URL、message或body文本。
        if request_id is None and isinstance(value, str) and not re.search(r"sk-|key|token|secret", value, re.I):
            if re.fullmatch(r"(?:[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}|req[-_][a-zA-Z0-9_-]{8,80})", value):
                request_id = value
        pending.extend((getattr(current, "__cause__", None), getattr(current, "__context__", None)))
    return {"outcome": "error", "exception_chain": chain, "http_status": status,
            "provider_code": code, "request_id": request_id}


def safe_finish_reason(reason):
    return reason if isinstance(reason, str) and reason in _FINISH else ("other" if reason is not None else None)
