"""LLM服务模块"""

from app.services.planning_timeout_policy import timeouts_enabled
from copy import deepcopy
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Literal

from hello_agents import HelloAgentsLLM
from hello_agents.core.exceptions import HelloAgentsException
from ..config import Settings, get_settings
from .observation_service import record_model_diagnostics
from .model_diagnostics import completion_diagnostics, provider_error_diagnostics


@dataclass(frozen=True)
class LLMCompletion:
    """带结束原因的非流式响应；文本和 token 用量与旧接口保持兼容。"""

    text: str | None
    usage: object | None
    finish_reason: str | None


@dataclass(frozen=True)
class NativeLLMCompletion:
    """完整 assistant 消息仅供协议回传，不写入日志或默认 repr。"""

    message: dict[str, Any] = field(repr=False)
    usage: object | None = field(repr=False)
    finish_reason: str | None


AgentRole = Literal["master", "attractions", "dining", "lodging"]
_AGENT_ROLES = frozenset({"master", "attractions", "dining", "lodging"})


@dataclass(frozen=True)
class AgentModelConfig:
    model: str
    api_key: str = field(repr=False)
    base_url: str = field(repr=False)


def resolve_agent_model_config(role: AgentRole, settings: Settings | None = None) -> AgentModelConfig:
    """所有角色共用网关配置，模型ID按主/子角色区分；OPENAI 配置兜底。"""
    if role not in _AGENT_ROLES:
        raise ValueError("未知智能体角色")
    config = settings if settings is not None else get_settings()
    prefix = "master" if role == "master" else "subagent"
    return AgentModelConfig(
        model=getattr(config, f"{prefix}_llm_model_id"),
        api_key=config.llm_api_key or config.openai_api_key,
        base_url=config.llm_base_url or config.openai_base_url,
    )

# 全局LLM实例
_llm_instance = None
_agent_llm_instances: dict[str, "UsageAwareLLM"] = {}
_agent_llm_lock = RLock()


class UsageAwareLLM(HelloAgentsLLM):
    """保留 HelloAgents 非流式调用行为，同时向观测层返回原响应的用量。"""

    def _request_completion(self, messages, *, request_timeout=None, max_retries=None, **kwargs):
        try:
            client = self._client
            client_options = {}
            if not timeouts_enabled():
                client_options['timeout'] = None  # 显式关闭SDK默认HTTP超时
            elif request_timeout is not None:
                client_options['timeout'] = request_timeout
            if max_retries is not None:
                client_options['max_retries'] = max_retries
            if client_options:
                client = client.with_options(**client_options)

            # 不注入项目或 SDK 默认输出上限；未指定的参数直接省略。
            response = client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=kwargs.get('temperature', self.temperature),
                **{key: value for key, value in kwargs.items() if key != 'temperature' and value is not None}
            )
            record_model_diagnostics(completion_diagnostics(response))
            return response
        except Exception as exc:
            record_model_diagnostics(provider_error_diagnostics(exc))
            raise HelloAgentsException("LLM调用失败") from exc

    def _invoke_completion(self, messages, **kwargs):
        response = self._request_completion(messages, **kwargs)
        choice = response.choices[0]
        return LLMCompletion(
            text=choice.message.content,
            usage=response.usage,
            finish_reason=getattr(choice, 'finish_reason', None),
        )

    def invoke_native(self, messages, *, tools=None, request_timeout=None, max_retries=0, **kwargs):
        """非流式 Chat Completions；保留 tool_calls 和供应商的额外消息字段。"""
        response = self._request_completion(
            messages, tools=tools, request_timeout=request_timeout,
            max_retries=max_retries, **kwargs,
        )
        choice = response.choices[0]
        message = choice.message
        if isinstance(message, dict):
            payload = deepcopy(message)
        else:
            payload = message.model_dump(mode="json", exclude_none=True)
        return NativeLLMCompletion(
            message=payload, usage=response.usage,
            finish_reason=getattr(choice, 'finish_reason', None),
        )

    def invoke_with_metadata(self, messages, *, request_timeout=None, max_retries=None, **kwargs):
        """返回文本、token用量和供应商结束原因，不暴露完整 SDK 响应对象。"""
        return self._invoke_completion(
            messages,
            request_timeout=request_timeout,
            max_retries=max_retries,
            **kwargs,
        )

    def invoke_with_usage(self, messages, *, request_timeout=None, max_retries=None, **kwargs):
        """兼容旧调用方：仍返回 (text, usage) 两值。"""
        completion = self._invoke_completion(
            messages,
            request_timeout=request_timeout,
            max_retries=max_retries,
            **kwargs,
        )
        return completion.text, completion.usage

    def invoke(self, messages, **kwargs):
        text, _ = self.invoke_with_usage(messages, **kwargs)
        return text


def get_llm() -> HelloAgentsLLM:
    """
    获取LLM实例(单例模式)
    
    Returns:
        HelloAgentsLLM实例
    """
    global _llm_instance
    
    if _llm_instance is None:
        settings = get_settings()
        
        # HelloAgentsLLM会自动从环境变量读取配置
        # 包括OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL等
        _llm_instance = UsageAwareLLM()
        
        print(f"✅ LLM服务初始化成功")
        print(f"   提供商: {_llm_instance.provider}")
        print(f"   模型: {_llm_instance.model}")
    
    return _llm_instance


def reset_llm():
    """重置LLM实例(用于测试或重新配置)"""
    global _llm_instance
    _llm_instance = None
    with _agent_llm_lock:
        _agent_llm_instances.clear()


def get_agent_llm(role: AgentRole) -> UsageAwareLLM:
    """新增角色入口，惰性创建客户端，不修改环境变量或旧规划的模型。"""
    profile = resolve_agent_model_config(role)
    if not profile.api_key or not profile.base_url:
        raise ValueError("智能体模型缺少 API 密钥或服务地址")
    with _agent_llm_lock:
        if role not in _agent_llm_instances:
            # 显式 custom，防止 HelloAgents 自动探测其他供应商覆盖角色配置。
            _agent_llm_instances[role] = UsageAwareLLM(
                model=profile.model, api_key=profile.api_key,
                base_url=profile.base_url, provider="custom",
            )
        return _agent_llm_instances[role]
