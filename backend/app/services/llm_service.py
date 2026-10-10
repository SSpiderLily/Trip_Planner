"""LLM服务模块"""

from hello_agents import HelloAgentsLLM
from hello_agents.core.exceptions import HelloAgentsException
from ..config import get_settings

# 全局LLM实例
_llm_instance = None


class UsageAwareLLM(HelloAgentsLLM):
    """保留 HelloAgents 非流式调用行为，同时向观测层返回原响应的用量。"""

    def invoke_with_usage(self, messages, *, request_timeout=None, max_retries=None, **kwargs):
        try:
            client = self._client
            client_options = {}
            if request_timeout is not None:
                client_options['timeout'] = request_timeout
            if max_retries is not None:
                client_options['max_retries'] = max_retries
            if client_options:
                client = client.with_options(**client_options)

            response = client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=kwargs.get('temperature', self.temperature),
                max_tokens=kwargs.get('max_tokens', self.max_tokens),
                **{key: value for key, value in kwargs.items() if key not in ('temperature', 'max_tokens')}
            )
            return response.choices[0].message.content, response.usage
        except Exception as exc:
            raise HelloAgentsException(f"LLM调用失败: {str(exc)}")

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
