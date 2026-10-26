"""原生调用与角色配置：全部使用 SDK 模拟响应，不访问模型端点。"""
from copy import deepcopy
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch, sentinel

from openai.types.chat import ChatCompletionMessage

from app.config import Settings
from app.services import llm_service
from app.services.llm_service import UsageAwareLLM, resolve_agent_model_config
from app.services.planning_timeout_policy import timeout_policy


def settings_fixture(**overrides):
    with patch.dict(os.environ, {}, clear=True):
        return Settings(_env_file=None, **overrides)


class AgentModelConfigTests(unittest.TestCase):
    def tearDown(self):
        llm_service.reset_llm()

    def test_defaults_and_shared_gateway_use_distinct_role_models(self):
        config = settings_fixture(llm_api_key="fixture-shared", llm_base_url="https://shared.invalid/v1")
        master = resolve_agent_model_config("master", config)
        self.assertEqual(master.model, "kimi-k3")
        for role in ("attractions", "dining", "lodging"):
            profile = resolve_agent_model_config(role, config)
            self.assertEqual(profile.model, "sub_agent")
            self.assertEqual(profile.api_key, "fixture-shared")
            self.assertEqual(profile.base_url, "https://shared.invalid/v1")
        self.assertEqual(master.api_key, "fixture-shared")
        self.assertNotIn("fixture-shared", repr(master))
        self.assertNotIn("fixture-shared", repr(config))

    def test_only_three_child_profiles_have_low_reasoning_default(self):
        config = settings_fixture()
        self.assertIsNone(resolve_agent_model_config("master", config).reasoning_effort)
        for role in ("attractions", "dining", "lodging"):
            with self.subTest(role=role):
                self.assertEqual(resolve_agent_model_config(role, config).reasoning_effort, "low")

    def test_role_models_override_defaults_and_share_gateway_before_openai_fallback(self):
        config = settings_fixture(
            llm_api_key="fixture-shared", llm_base_url="https://shared.invalid/v1",
            openai_api_key="fixture-openai", openai_base_url="https://fallback.invalid/v1",
            master_llm_model_id="master-override", subagent_llm_model_id="sub-override",
        )
        master, child = (resolve_agent_model_config(role, config) for role in ("master", "dining"))
        self.assertEqual((master.model, master.api_key, master.base_url),
                         ("master-override", "fixture-shared", "https://shared.invalid/v1"))
        self.assertEqual((child.model, child.api_key, child.base_url),
                         ("sub-override", "fixture-shared", "https://shared.invalid/v1"))

    def test_openai_fallback_does_not_override_role_model(self):
        config = settings_fixture(openai_api_key="fixture-openai", openai_base_url="https://fallback.invalid/v1",
                                  openai_model="legacy-model")
        profile = resolve_agent_model_config("lodging", config)
        self.assertEqual((profile.model, profile.api_key, profile.base_url),
                         ("sub_agent", "fixture-openai", "https://fallback.invalid/v1"))

    def test_environment_role_settings_are_loaded_without_touching_real_env(self):
        env = {"MASTER_LLM_MODEL_ID": "master-env", "SUBAGENT_LLM_MODEL_ID": "sub-env",
               "LLM_API_KEY": "fixture-env-key", "LLM_BASE_URL": "https://shared.invalid/v1"}
        with patch.dict(os.environ, env, clear=True):
            config = Settings(_env_file=None)
        for role, model in (("master", "master-env"), ("attractions", "sub-env"),
                            ("dining", "sub-env"), ("lodging", "sub-env")):
            with self.subTest(role=role):
                profile = resolve_agent_model_config(role, config)
                self.assertEqual((profile.model, profile.api_key, profile.base_url),
                                 (model, "fixture-env-key", "https://shared.invalid/v1"))
        self.assertNotIn("fixture-env-key", repr(config))

    def test_factory_is_lazy_role_cached_and_never_changes_environment(self):
        llm_service.reset_llm()
        config = settings_fixture(llm_api_key="fixture-key", llm_base_url="https://gateway.invalid/v1")
        before = dict(os.environ)
        with patch.object(llm_service, "get_settings", return_value=config), \
                patch.object(llm_service, "UsageAwareLLM", side_effect=[sentinel.master, sentinel.child]) as factory:
            self.assertIs(llm_service.get_agent_llm("master"), sentinel.master)
            self.assertIs(llm_service.get_agent_llm("master"), sentinel.master)
            self.assertIs(llm_service.get_agent_llm("attractions"), sentinel.child)
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(factory.call_args_list[0].kwargs, {
            "model": "kimi-k3", "api_key": "fixture-key",
            "base_url": "https://gateway.invalid/v1", "provider": "custom",
            "temperature": 0.01, "output_token_limit": 1_000_000, "output_token_parameter": "max_tokens",
        })
        self.assertEqual(factory.call_args_list[1].kwargs["model"], "sub_agent")
        self.assertEqual(factory.call_args_list[1].kwargs, {
            **factory.call_args_list[0].kwargs, "model": "sub_agent", "reasoning_effort": "low",
        })
        self.assertEqual(dict(os.environ), before)

    def test_central_role_parameters_reach_actual_sdk_requests(self):
        config = settings_fixture(master_llm_max_output_tokens=40000,
            subagent_llm_max_output_tokens=20000, master_llm_reasoning_effort="high",
            subagent_llm_reasoning_effort="low", llm_output_token_parameter="max_completion_tokens",
            llm_api_key="fixture", llm_base_url="https://fixture.invalid/v1")
        def initialize(instance, *, model, temperature, **kwargs):
            instance.model, instance.temperature = model, temperature
            instance._client = Mock()
            instance._client.with_options.return_value = instance._client
            instance._client.chat.completions.create.return_value = SimpleNamespace(
                choices=[SimpleNamespace(message={"role":"assistant", "content":"done"}, finish_reason="stop")],
                usage=None)
        with patch.object(llm_service, "get_settings", return_value=config), \
                patch.object(llm_service.HelloAgentsLLM, "__init__", initialize):
            for role in ("master", "attractions", "dining", "lodging"):
                with self.subTest(role=role):
                    model = llm_service.get_agent_llm(role)
                    model.invoke_native([], tools=[])
                    args = model._client.chat.completions.create.call_args.kwargs
                    self.assertEqual(args["temperature"], 0.01)
                    self.assertEqual(args["max_completion_tokens"], 40000 if role == "master" else 20000)
                    self.assertNotIn("max_tokens", args)
                    self.assertEqual(args["reasoning_effort"], "high" if role == "master" else "low")
                    # 单次显式参数不同时发送两种互斥的输出上限字段。
                    model.invoke_native([], tools=[], max_tokens=10000)
                    args = model._client.chat.completions.create.call_args.kwargs
                    self.assertEqual(args["max_tokens"], 10000)
                    self.assertNotIn("max_completion_tokens", args)

    def test_output_limit_requires_positive_integer_not_max_string(self):
        for value in ("max", 0, -1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                settings_fixture(master_llm_max_output_tokens=value)

    def test_missing_credentials_and_unknown_role_never_create_client(self):
        with patch.object(llm_service, "get_settings", return_value=settings_fixture()), \
                patch.object(llm_service, "UsageAwareLLM") as factory:
            with self.assertRaises(ValueError):
                llm_service.get_agent_llm("master")
            with self.assertRaises(ValueError):
                llm_service.get_agent_llm("unknown")
            factory.assert_not_called()

    def test_role_factory_does_not_replace_legacy_singleton_and_reset_clears_both(self):
        llm_service.reset_llm()
        llm_service._llm_instance = sentinel.legacy
        config = settings_fixture(llm_api_key="fixture", llm_base_url="https://gateway.invalid/v1")
        with patch.object(llm_service, "get_settings", return_value=config), \
                patch.object(llm_service, "UsageAwareLLM", return_value=sentinel.master):
            self.assertIs(llm_service.get_agent_llm("master"), sentinel.master)
            self.assertIs(llm_service.get_llm(), sentinel.legacy)
        llm_service.reset_llm()
        self.assertIsNone(llm_service._llm_instance)
        self.assertEqual(llm_service._agent_llm_instances, {})


class NativeCompletionTests(unittest.TestCase):
    def make_llm(self, message, finish_reason="tool_calls"):
        llm = object.__new__(UsageAwareLLM)
        llm.model, llm.temperature = "fixture-model", 0.7
        client = Mock()
        client.with_options.return_value = client
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason=finish_reason)], usage=usage)
        llm._client = client
        return llm, client, usage

    def test_native_preserves_sdk_tool_calls_reasoning_and_usage(self):
        message = ChatCompletionMessage(role="assistant", content=None,
            tool_calls=[{"id": "call_1", "type": "function", "function": {
                "name": "find_attractions", "arguments": '{"city":"杭州"}'}}],
            reasoning_content="fixture-private-reasoning", provider_state="fixture-state")
        llm, client, usage = self.make_llm(message)
        tools = [{"type": "function", "function": {"name": "find_attractions"}}]
        completion = llm.invoke_native([{"role": "user", "content": "fixture"}], tools=tools)
        self.assertEqual(completion.message["tool_calls"][0]["id"], "call_1")
        self.assertEqual(completion.message["reasoning_content"], "fixture-private-reasoning")
        self.assertEqual(completion.message["provider_state"], "fixture-state")
        self.assertIs(completion.usage, usage)
        self.assertEqual(completion.finish_reason, "tool_calls")
        self.assertNotIn("fixture-private-reasoning", repr(completion))
        self.assertEqual(client.chat.completions.create.call_args.kwargs["tools"], tools)
        self.assertEqual(client.with_options.call_args.kwargs["max_retries"], 0)
        self.assertNotIn("max_tokens", client.chat.completions.create.call_args.kwargs)

    def test_real_role_factory_sends_low_to_sdk_only_for_children(self):
        config = settings_fixture(llm_api_key="fixture", llm_base_url="https://fixture.invalid/v1")
        def initialize(instance, *, model="legacy", **kwargs):
            prepared, _, _ = self.make_llm(ChatCompletionMessage(role="assistant", content="done"), "stop")
            instance.model, instance.temperature, instance._client = model, prepared.temperature, prepared._client
        llm_service.reset_llm()
        self.addCleanup(llm_service.reset_llm)
        with patch.object(llm_service, "get_settings", return_value=config), \
                patch.object(llm_service.HelloAgentsLLM, "__init__", initialize):
            for role in ("attractions", "dining", "lodging", "master"):
                with self.subTest(role=role):
                    llm = llm_service.get_agent_llm(role)
                    for _ in range(2):
                        llm.invoke_native([], tools=[])
                    requests = llm._client.chat.completions.create.call_args_list
                    self.assertTrue(all(call.kwargs.get("max_tokens") == 1_000_000 for call in requests))
                    if role == "master":
                        self.assertTrue(all("reasoning_effort" not in call.kwargs for call in requests))
                    else:
                        self.assertEqual([call.kwargs.get("reasoning_effort") for call in requests], ["low", "low"])
            legacy = UsageAwareLLM()
            legacy.invoke_native([])
            self.assertNotIn("reasoning_effort", legacy._client.chat.completions.create.call_args.kwargs)

    def test_low_is_preserved_across_tool_round_and_connection_retry(self):
        from test_native_model_retry import connection_error, response, tool
        from app.services.native_tool_loop import NativeToolLoop
        llm, client, _ = self.make_llm(ChatCompletionMessage(role="assistant", content="done"), "stop")
        llm.reasoning_effort = "low"
        client.chat.completions.create.side_effect = [response("query"), connection_error(), response()]
        with patch.object(NativeToolLoop, "_wait_before_retry"), patch("app.services.native_tool_loop.span") as observed:
            result = NativeToolLoop(llm, [tool("query", lambda _: {"status": "ok"})]).run([])
        self.assertEqual(result.model_calls, 3)
        self.assertEqual([call.kwargs.get("reasoning_effort") for call in client.chat.completions.create.call_args_list], ["low"] * 3)
        model_spans = [call for call in observed.call_args_list if call.args[0] == "llm.native_tool_loop"]
        self.assertEqual(len(model_spans), 3)
        self.assertTrue(all(call.args[2].get("reasoning_effort") == "low" for call in model_spans))

    def test_dictionary_message_is_copied_before_return(self):
        message = {"role": "assistant", "content": "done", "provider_field": {"state": "fixture"}}
        original = deepcopy(message)
        llm, _, _ = self.make_llm(message, "stop")
        completion = llm.invoke_native([])
        completion.message["provider_field"]["state"] = "changed"
        self.assertEqual(message, original)

    def test_legacy_text_and_usage_remain_unchanged(self):
        llm, client, usage = self.make_llm(ChatCompletionMessage(role="assistant", content="{}"), "stop")
        self.assertEqual(llm.invoke([]), "{}")
        self.assertEqual(llm.invoke_with_usage([]), ("{}", usage))
        self.assertEqual(llm.invoke_with_metadata([]).finish_reason, "stop")
        self.assertNotIn("tools", client.chat.completions.create.call_args.kwargs)

    def test_native_and_legacy_share_timeout_policy_and_safe_error_handling(self):
        llm, client, _ = self.make_llm(ChatCompletionMessage(role="assistant", content="done"), "stop")
        with timeout_policy(False):
            llm.invoke_native([], request_timeout=0.01)
        self.assertIsNone(client.with_options.call_args.kwargs["timeout"])
        with timeout_policy(True):
            llm.invoke_native([], request_timeout=3)
        self.assertEqual(client.with_options.call_args.kwargs["timeout"], 3)
        client.chat.completions.create.side_effect = RuntimeError("fixture-private-secret")
        with self.assertRaisesRegex(Exception, "LLM调用失败") as raised:
            llm.invoke_native([])
        self.assertNotIn("fixture-private-secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
