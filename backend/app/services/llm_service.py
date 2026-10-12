"""LLM服务模块"""

from app.services.planning_timeout_policy import deadline_after, wait_timeout, timeouts_enabled
from dataclasses import dataclass

from hello_agents import HelloAgentsLLM
from hello_agents.core.exceptions import HelloAgentsException
from ..config import get_settings
from .observation_service import record_model_diagnostics
from .model_diagnostics import completion_diagnostics, provider_error_diagnostics


@dataclass(frozen=True)
class LLMCompletion:
    """带结束原因的非流式响应；文本和 token 用量与旧接口保持兼容。"""

    text: str | None
    usage: object | None
    finish_reason: str | None

# 全局LLM实例
_llm_instance = None


class UsageAwareLLM(HelloAgentsLLM):
    """保留 HelloAgents 非流式调用行为，同时向观测层返回原响应的用量。"""

    def _invoke_completion(self, messages, *, request_timeout=None, max_retries=None, **kwargs):
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
            choice = response.choices[0]
            return LLMCompletion(
                text=choice.message.content,
                usage=response.usage,
                finish_reason=getattr(choice, 'finish_reason', None),
            )
        except Exception as exc:
            record_model_diagnostics(provider_error_diagnostics(exc))
            raise HelloAgentsException("LLM调用失败") from exc

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
