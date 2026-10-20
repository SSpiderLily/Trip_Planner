"""原生模型传输重试策略；不重跑工具、规划阶段或HTTP业务错误。"""
from random import uniform

import httpx
from openai import APIConnectionError
from hello_agents.core.exceptions import HelloAgentsException


MAX_MODEL_ATTEMPTS = 3  # 首次请求 + 最多两次连接重试。


def is_connection_failure(error: Exception) -> bool:
    # LLM适配器使用显式cause包装供应商异常。仅解开该包装，不沿任意
    # __context__查找旧异常，以免把当前参数/业务错误误判成可重试连接错误。
    seen = set()
    while isinstance(error, HelloAgentsException) and id(error) not in seen:
        seen.add(id(error))
        error = error.__cause__
    return isinstance(error, (APIConnectionError, httpx.NetworkError,
                              httpx.RemoteProtocolError, httpx.TimeoutException))


def retry_delay_seconds(attempt: int) -> float:
    """首次失败等待1—2秒，第二次等待2—4秒，避免并发请求同时重试。"""
    return uniform(1.0, 2.0) * 2 ** (attempt - 1)
