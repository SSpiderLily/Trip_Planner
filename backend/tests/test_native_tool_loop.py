"""模拟主→子→地图调用；不构造真实模型客户端或高德 MCP。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date
import json
from threading import Barrier, Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pydantic import Field

from openai.types.chat import ChatCompletionMessage

from app.services.llm_service import NativeLLMCompletion, UsageAwareLLM
from app.services.native_tool_loop import (
    NativeTool, NativeToolArguments, NativeToolLoop, NativeToolLoopError, NativeToolLoopResult,
)


class SearchArgs(NativeToolArguments):
    city: str = Field(min_length=1)
    dates: list[date] = Field(min_length=1)
    preferences: list[str]


class FakeNativeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def invoke_native(self, messages, **kwargs):
        self.requests.append((deepcopy(messages), deepcopy(kwargs)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def call(call_id="call_1", name="find_attractions", arguments=None):
    if arguments is None:
        arguments = json.dumps({"city": "杭州", "dates": ["2026-10-20"],
                                "preferences": ["历史文化", "轻松步行"]}, ensure_ascii=False)
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def completion(calls=None, content=None, reason=None, **extra):
    return NativeLLMCompletion(
        message={"role": "assistant", "content": content, **({"tool_calls": calls} if calls else {}), **extra},
        usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        finish_reason=reason or ("tool_calls" if calls else "stop"),
    )


def tool(handler, name="find_attractions"):
    return NativeTool(name, "按城市、日期和偏好查询候选", SearchArgs, handler)


class NativeToolLoopTests(unittest.TestCase):
    def assert_loop_error(self, loop, code, **kwargs):
        with self.assertRaises(NativeToolLoopError) as raised:
            loop.run([{"role": "user", "content": "fixture"}], **kwargs)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_arrays_dates_full_message_and_paired_result_survive_next_request(self):
        handler = Mock(return_value={"candidates": [{"poi_id": "fixture-poi", "name": "fixture-place"}]})
        model = FakeNativeModel([
            completion([call()], reasoning_content="fixture-reasoning", provider_field={"state": "fixture"}),
            completion(content="最终结果"),
        ])
        inputs = [{"role": "system", "content": "fixture"}, {"role": "user", "content": "fixture"}]
        before = deepcopy(inputs)
        result = NativeToolLoop(model, [tool(handler)]).run(inputs, temperature=0)
        arguments = handler.call_args.args[0]
        self.assertEqual(arguments.city, "杭州")
        self.assertEqual(arguments.dates, [date(2026, 10, 20)])
        self.assertEqual(arguments.preferences, ["历史文化", "轻松步行"])
        history, options = model.requests[1]
        self.assertEqual(history[2]["tool_calls"][0], call())
        self.assertEqual(history[2]["reasoning_content"], "fixture-reasoning")
        self.assertEqual(history[2]["provider_field"], {"state": "fixture"})
        self.assertEqual((history[3]["role"], history[3]["tool_call_id"]), ("tool", "call_1"))
        self.assertEqual(json.loads(history[3]["content"])["data"]["candidates"][0]["poi_id"], "fixture-poi")
        self.assertEqual(options["temperature"], 0)
        self.assertEqual(options["tools"][0]["function"]["parameters"]["additionalProperties"], False)
        self.assertEqual((result.text, result.model_calls, result.tool_calls), ("最终结果", 2, 1))
        self.assertEqual(inputs, before)
        self.assertNotIn("fixture-reasoning", repr(result))
        self.assertEqual(len(result.completions), 2)

    def test_multiple_calls_all_return_before_next_model_request(self):
        attractions, dining = Mock(return_value=[]), Mock(return_value=[])
        model = FakeNativeModel([
            completion([call("sights"), call("food", "find_restaurants")]), completion(content="done"),
        ])
        result = NativeToolLoop(model, [tool(attractions), tool(dining, "find_restaurants")]).run([])
        self.assertEqual([item["tool_call_id"] for item in model.requests[1][0] if item["role"] == "tool"],
                         ["sights", "food"])
        self.assertEqual(result.tool_calls, 2)
        attractions.assert_called_once()
        dining.assert_called_once()

    def test_model_can_delegate_again_after_tool_result(self):
        handler = Mock(return_value={"status": "partial"})
        model = FakeNativeModel([completion([call("one")]), completion([call("two")]), completion(content="done")])
        result = NativeToolLoop(model, [tool(handler)]).run([])
        self.assertEqual((result.model_calls, result.tool_calls, handler.call_count), (3, 2, 2))

    def test_nested_master_child_and_mock_map_loop(self):
        map_handler = Mock(return_value={"poi_id": "fixture-poi", "source": "fixture-map"})
        child_model = FakeNativeModel([completion([call("map_1", "maps_search")]),
                                       completion(content='{"candidate_poi_ids":["fixture-poi"]}')])
        child = NativeToolLoop(child_model, [tool(map_handler, "maps_search")])
        def run_child(args):
            return json.loads(child.run([{"role": "user", "content": args.model_dump_json()}]).text)
        master_model = FakeNativeModel([completion([call("child_1")]), completion(content="fixture-plan")])
        result = NativeToolLoop(master_model, [tool(run_child)]).run([])
        data = json.loads(master_model.requests[1][0][-1]["content"])["data"]
        self.assertEqual(data, {"candidate_poi_ids": ["fixture-poi"]})
        self.assertEqual(result.text, "fixture-plan")
        map_handler.assert_called_once()

    def test_sdk_mock_completion_runs_through_real_native_adapter_and_loop(self):
        client = Mock()
        client.with_options.return_value = client
        messages = [ChatCompletionMessage(role="assistant", content=None, tool_calls=[call()],
                                          reasoning_content="fixture-private-reasoning"),
                    ChatCompletionMessage(role="assistant", content="fixture-result")]
        client.chat.completions.create.side_effect = [
            SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=reason)], usage=None)
            for message, reason in zip(messages, ("tool_calls", "stop"))
        ]
        llm = object.__new__(UsageAwareLLM)
        llm.model, llm.temperature, llm._client = "fixture-model", 0, client
        handler = Mock(return_value={"poi_id": "fixture-poi"})
        result = NativeToolLoop(llm, [tool(handler)]).run([{"role": "user", "content": "fixture"}])
        followup = client.chat.completions.create.call_args_list[1].kwargs["messages"]
        self.assertEqual(followup[1]["reasoning_content"], "fixture-private-reasoning")
        self.assertEqual(followup[2]["tool_call_id"], "call_1")
        self.assertEqual(result.text, "fixture-result")
        self.assertEqual(client.chat.completions.create.call_count, 2)
        handler.assert_called_once()

    def test_bad_arguments_return_error_and_model_can_correct_them(self):
        bad_inputs = ["not-json", "[]", "null", '{"city":"杭州"}',
                      '{"city":123,"dates":["2026-10-20"],"preferences":[]}',
                      '{"city":"杭州","dates":["bad-date"],"preferences":[]}',
                      '{"city":"杭州","dates":["2026-10-20"],"preferences":[],"extra":1}',
                      '{"city":"杭州","city":"上海","dates":[],"preferences":[]}',
                      '{"city":NaN,"dates":[],"preferences":[]}']
        for raw in bad_inputs:
            with self.subTest(raw=raw):
                handler = Mock(return_value=[])
                model = FakeNativeModel([completion([call("bad", arguments=raw)]),
                                         completion([call("fixed")]), completion(content="done")])
                result = NativeToolLoop(model, [tool(handler)]).run([])
                error = json.loads(model.requests[1][0][-1]["content"])["error"]
                self.assertEqual(error["code"], "INVALID_TOOL_ARGUMENTS")
                handler.assert_called_once()
                self.assertEqual(result.model_calls, 3)

    def test_unknown_tool_is_never_executed_and_returns_matching_id(self):
        handler = Mock()
        model = FakeNativeModel([completion([call("unknown", "not_registered")]), completion(content="done")])
        NativeToolLoop(model, [tool(handler)]).run([])
        message = model.requests[1][0][-1]
        self.assertEqual(message["tool_call_id"], "unknown")
        self.assertEqual(json.loads(message["content"])["error"]["code"], "UNKNOWN_TOOL")
        handler.assert_not_called()

    def test_argument_repair_reports_schema_paths_without_private_input_or_unknown_names(self):
        handler = Mock(return_value=[])
        bad = json.dumps({"city": "杭州", "dates": ["2026-10-20"], "fixture-secret-field": "private-input"})
        model = FakeNativeModel([completion([call(arguments=bad)]), completion([call("corrected")]), completion(content="done")])
        NativeToolLoop(model, [tool(handler)]).run([])
        content = model.requests[1][0][-1]["content"]
        error = json.loads(content)["error"]
        self.assertEqual(error["code"], "INVALID_TOOL_ARGUMENTS")
        self.assertIn({"path": ["preferences"], "type": "missing"}, error["issues"])
        self.assertIn({"path": ["unknown_field"], "type": "extra_forbidden"}, error["issues"])
        self.assertIn("preferences", error["expected_parameters"]["properties"])
        self.assertNotIn("private-input", content)
        self.assertNotIn("fixture-secret-field", content)
        handler.assert_called_once()

    def test_tool_exception_is_safe_and_other_calls_still_receive_results(self):
        fail, succeed = Mock(side_effect=RuntimeError("fixture-private-secret")), Mock(return_value=[])
        model = FakeNativeModel([completion([call("fail"), call("ok", "find_restaurants")]), completion(content="done")])
        result = NativeToolLoop(model, [tool(fail), tool(succeed, "find_restaurants")]).run([])
        messages = model.requests[1][0]
        self.assertEqual(json.loads(messages[-2]["content"])["error"]["code"], "TOOL_EXECUTION_FAILED")
        self.assertTrue(json.loads(messages[-1]["content"])["ok"])
        self.assertNotIn("fixture-private-secret", repr(messages))
        self.assertEqual(result.tool_calls, 2)

    def test_invalid_result_returns_safe_error(self):
        for value in (object(), {"cost": float("nan")}):
            with self.subTest(value_type=type(value).__name__):
                model = FakeNativeModel([completion([call()]), completion(content="done")])
                NativeToolLoop(model, [tool(lambda _: value)]).run([])
                error = json.loads(model.requests[1][0][-1]["content"])["error"]
                self.assertEqual(error["code"], "INVALID_TOOL_RESULT")

    def test_duplicate_id_preflight_does_not_execute_partial_batch(self):
        handler = Mock()
        model = FakeNativeModel([completion([call("same"), call("same")])])
        self.assert_loop_error(NativeToolLoop(model, [tool(handler)]), "DUPLICATE_TOOL_CALL_ID")
        handler.assert_not_called()

    def test_id_reuse_from_prior_phase_does_not_execute_again(self):
        handler = Mock(return_value=[])
        model = FakeNativeModel([completion([call("prior")])])
        history = [completion([call("prior")]).message,
                   {"role": "tool", "tool_call_id": "prior", "content": '{"ok":true,"data":[]}'}]
        with self.assertRaises(NativeToolLoopError) as raised:
            NativeToolLoop(model, [tool(handler)]).run(history)
        self.assertEqual(raised.exception.code, "DUPLICATE_TOOL_CALL_ID")
        handler.assert_not_called()

    def test_id_reuse_across_rounds_does_not_execute_again(self):
        handler = Mock(return_value=[])
        model = FakeNativeModel([completion([call("same")]), completion([call("same")])])
        self.assert_loop_error(NativeToolLoop(model, [tool(handler)]), "DUPLICATE_TOOL_CALL_ID")
        handler.assert_called_once()

    def test_malformed_protocol_preflight_executes_no_tools(self):
        malformed = [dict(call(), id=""), dict(call(), type="custom"),
                     {"id": "bad", "type": "function", "function": {}},
                     {"id": "bad", "type": "function", "function": {"name": "find_attractions", "arguments": {}}}]
        for bad in malformed:
            with self.subTest(bad=bad):
                handler = Mock()
                model = FakeNativeModel([completion([call("valid"), bad])])
                self.assert_loop_error(NativeToolLoop(model, [tool(handler)]), "INVALID_TOOL_CALL")
                handler.assert_not_called()

    def test_truncated_or_refused_calls_are_never_executed(self):
        for response, code in [(completion([call()], reason="length"), "INCOMPLETE_COMPLETION"),
                               (completion([call()], refusal="fixture"), "MODEL_REFUSED")]:
            handler = Mock()
            self.assert_loop_error(NativeToolLoop(FakeNativeModel([response]), [tool(handler)]), code)
            handler.assert_not_called()

    def test_model_failure_empty_reply_and_missing_tool_payload_are_not_success(self):
        for response, code in [(RuntimeError("fixture-private-secret"), "MODEL_CALL_FAILED"),
                               (completion(content=""), "INVALID_COMPLETION"),
                               (completion(content="done", reason="tool_calls"), "INVALID_COMPLETION"),
                               (completion(content="done", reason="other"), "INVALID_COMPLETION")]:
            error = self.assert_loop_error(NativeToolLoop(FakeNativeModel([response]), []), code)
            self.assertNotIn("fixture-private-secret", str(error))

    def test_invalid_message_or_finish_reason_shape_is_safe(self):
        responses = [NativeLLMCompletion([], None, "stop"),
                     NativeLLMCompletion({"role": "user", "content": "fixture"}, None, "stop"),
                     NativeLLMCompletion({"role": "assistant", "content": "fixture"}, None, [])]
        for response in responses:
            self.assert_loop_error(NativeToolLoop(FakeNativeModel([response]), []), "INVALID_COMPLETION")

    def test_one_run_does_not_carry_history_or_ids_into_next_run(self):
        handler = Mock(return_value=[])
        model = FakeNativeModel([completion([call()]), completion(content="one"),
                                 completion([call()]), completion(content="two")])
        loop = NativeToolLoop(model, [tool(handler)])
        first = loop.run([{"role": "user", "content": "first"}])
        second = loop.run([{"role": "user", "content": "second"}])
        self.assertEqual((first.text, second.text), ("one", "two"))
        self.assertEqual(model.requests[2][0], [{"role": "user", "content": "second"}])
        self.assertEqual(handler.call_count, 2)

    def test_model_limit_does_not_execute_tools_without_a_followup_round(self):
        handler = Mock()
        model = FakeNativeModel([completion([call()])])
        self.assert_loop_error(NativeToolLoop(model, [tool(handler)], max_model_calls=1), "MODEL_CALL_LIMIT")
        handler.assert_not_called()
        self.assertEqual(len(model.requests), 1)

    def test_tool_limit_preflight_does_not_execute_partial_batch(self):
        handler = Mock()
        model = FakeNativeModel([completion([call("one"), call("two")])])
        self.assert_loop_error(NativeToolLoop(model, [tool(handler)], max_tool_calls=1), "TOOL_CALL_LIMIT")
        handler.assert_not_called()

    def test_cancel_before_model_after_model_and_between_tools(self):
        cancelled = Event()
        cancelled.set()
        model = FakeNativeModel([])
        self.assert_loop_error(NativeToolLoop(model, []), "CANCELLED", cancel_event=cancelled)
        self.assertEqual(model.requests, [])
        cancelled.clear()
        handler = Mock(return_value=[])
        model = Mock()
        def cancel_on_response(*_, **__):
            cancelled.set()
            return completion([call()])
        model.invoke_native.side_effect = cancel_on_response
        self.assert_loop_error(NativeToolLoop(model, [tool(handler)]), "CANCELLED", cancel_event=cancelled)
        handler.assert_not_called()
        cancelled.clear()
        def cancel_on_tool(_):
            cancelled.set()
            return []
        second = Mock()
        model = FakeNativeModel([completion([call("one"), call("two", "find_restaurants")])])
        self.assert_loop_error(NativeToolLoop(model, [tool(cancel_on_tool), tool(second, "find_restaurants")]),
                               "CANCELLED", cancel_event=cancelled)
        second.assert_not_called()
        self.assertEqual(len(model.requests), 1)

    def test_registry_budget_and_no_tool_final_answer(self):
        with self.assertRaises(ValueError):
            NativeToolLoop(FakeNativeModel([]), [tool(Mock()), tool(Mock())])
        for invalid in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                NativeToolLoop(FakeNativeModel([]), [], max_model_calls=invalid)
        model = FakeNativeModel([completion(content="done")])
        result = NativeToolLoop(model, []).run([])
        self.assertEqual((result.model_calls, result.tool_calls), (1, 0))
        self.assertIsNone(model.requests[0][1]["tools"])
        with self.assertRaises(ValueError):
            NativeToolLoop(FakeNativeModel([]), []).run([], tools=[])


class AcceptedSubmissionTests(unittest.TestCase):
    """实际公共循环；接受之后的额外模型请求必须使测试失败。"""

    def loop(self, responses, handler=None, **options):
        model = FakeNativeModel(responses)
        loop = NativeToolLoop(model, [tool(handler or (lambda _: {"status": "accepted"}), "submit")],
                              accepted_submission_tools=frozenset({"submit"}), **options)
        return loop, model

    def test_accepted_submission_returns_saved_result_without_confirmation(self):
        saved = []
        def save(args):
            saved.append(args.city)
            return {"status": "accepted", "task_status": "partial", "missing": ["dinner"]}
        response = completion([call(name="submit")], reasoning_content="private-reasoning",
                              provider_field={"state": "original"})
        before = deepcopy(response.message)
        loop, model = self.loop([response, AssertionError("confirmation forbidden")], save)
        result = loop.run([])
        self.assertEqual(saved, ["杭州"])
        self.assertEqual((result.text, result.termination_reason, result.model_calls, result.tool_calls),
                         ("", "submission_accepted", 1, 1))
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(result.messages[0], before)
        self.assertEqual(result.completions, (response,))
        self.assertEqual(result.completions[0].finish_reason, "tool_calls")
        self.assertEqual(result.completions[0].usage["total_tokens"], 5)
        self.assertEqual(json.loads(result.messages[-1]["content"])["data"]["missing"], ["dinner"])
        self.assertEqual(response.message, before)

    def test_default_and_unregistered_accepted_results_still_need_model_decision(self):
        for names in (frozenset(), frozenset({"other"})):
            with self.subTest(names=names):
                model = FakeNativeModel([completion([call(name="submit")]), completion(content="done")])
                result = NativeToolLoop(model, [tool(lambda _: {"status": "accepted"}, "submit"),
                    tool(lambda _: {}, "other")], accepted_submission_tools=names).run([])
                self.assertEqual((result.text, result.termination_reason, result.model_calls),
                                 ("done", "model_stop", 2))

    def test_rejected_or_invalid_arguments_continue_until_corrected(self):
        for bad in (call("bad", "submit", "{}"), call("bad", "submit")):
            with self.subTest(arguments=bad["function"]["arguments"]):
                returns = iter([{"status": "rejected"}, {"status": "accepted"}]) if bad["function"]["arguments"] != "{}" else iter([{"status": "accepted"}])
                loop, model = self.loop([completion([bad]), completion([call("fixed", "submit")]),
                                        AssertionError("confirmation forbidden")], lambda _: next(returns))
                result = loop.run([])
                self.assertEqual((result.model_calls, result.tool_calls), (2, 2))
                self.assertEqual(model.requests[1][0][-1]["tool_call_id"], "bad")
                self.assertEqual(result.termination_reason, "submission_accepted")

    def test_completed_partial_or_nested_accepted_is_not_an_acceptance_receipt(self):
        for data in ({"status": "completed"}, {"status": "partial"}, {"status": True},
                     {"nested": {"status": "accepted"}}, ["accepted"], None):
            with self.subTest(data=data):
                loop, _ = self.loop([completion([call(name="submit")]), completion(content="done")], lambda _: data)
                result = loop.run([])
                self.assertEqual((result.model_calls, result.termination_reason), (2, "model_stop"))

    def test_entire_batch_executes_and_pairs_in_order_for_sequential_and_batch_executor(self):
        for batch in (False, True):
            with self.subTest(batch=batch):
                saved = []
                def submit(_):
                    if saved:
                        return {"status": "already_submitted"}
                    saved.append("first")
                    return {"status": "accepted"}
                def query(_):
                    saved.append("query-closed")
                    return {"status": "completed", "code": "CANDIDATES_ALREADY_SUBMITTED"}
                calls = [call("first", "submit"), call("query", "query"), call("second", "submit")]
                model = FakeNativeModel([completion(calls), AssertionError("confirmation forbidden")])
                executor = (lambda calls, execute: [execute(c) for c in calls]) if batch else None
                result = NativeToolLoop(model, [tool(submit, "submit"), tool(query, "query")],
                    accepted_submission_tools=frozenset({"submit"}), batch_executor=executor).run([])
                self.assertEqual(saved, ["first", "query-closed"])
                self.assertEqual([m["tool_call_id"] for m in result.messages if m["role"] == "tool"],
                                 ["first", "query", "second"])
                self.assertEqual((result.tool_calls, result.model_calls), (3, 1))

    def test_accepted_cannot_hide_any_error_in_same_batch_or_reuse_it_next_round(self):
        for mode in ("unknown", "arguments", "exception", "encoding", "rejected", "error", "failed"):
            with self.subTest(mode=mode):
                def other(_):
                    if mode == "exception":
                        raise RuntimeError("private-failure")
                    if mode == "encoding":
                        return object()
                    return {"status": mode}
                bad = call("bad", "unknown" if mode == "unknown" else "other", "{}" if mode == "arguments" else None)
                model = FakeNativeModel([completion([call("accept", "submit"), bad]),
                    completion([call("next", "other")]), completion(content="done")])
                result = NativeToolLoop(model, [tool(lambda _: {"status": "accepted"}, "submit"), tool(other, "other")],
                    accepted_submission_tools=frozenset({"submit"})).run([])
                self.assertEqual((result.model_calls, result.tool_calls, result.termination_reason), (3, 3, "model_stop"))
                self.assertEqual([m["tool_call_id"] for m in model.requests[1][0] if m["role"] == "tool"], ["accept", "bad"])

    def test_map_error_receipts_in_same_batch_block_accepted_submission(self):
        for data in ({"query_status": "error", "result_status": "error", "error": {"category": "timeout"}},
                     {"query_status": "duplicate_skipped", "result_status": "error"},
                     {"query_status": "failed"}, {"error": {"code": "FAILED"}}):
            with self.subTest(data=data):
                model = FakeNativeModel([completion([call("map", "map_query"), call("accept", "submit")]),
                                         completion(content="done")])
                result = NativeToolLoop(model, [tool(lambda _: data, "map_query"),
                    tool(lambda _: {"status": "accepted"}, "submit")],
                    accepted_submission_tools=frozenset({"submit"})).run([])
                self.assertEqual((result.model_calls, result.termination_reason), (2, "model_stop"))
                self.assertEqual(len([m for m in result.messages if m["role"] == "tool"]), 2)

    def test_malformed_encoded_batch_receipt_never_triggers_success(self):
        for malformed in ('not-json', '{"ok":true,"data":{"status":"accepted"},"ok":true}',
                          '{"ok":1,"data":{"status":"accepted"}}', '{"ok":true}',
                          '{"ok":true,"data":{"status":"accepted"},"n":NaN}', '[]'):
            with self.subTest(malformed=malformed):
                loop, _ = self.loop([completion([call(name="submit")]), completion(content="done")],
                                    batch_executor=lambda calls, execute: [malformed])
                result = loop.run([])
                self.assertEqual((result.model_calls, result.termination_reason), (2, "model_stop"))

    def test_cancel_during_saved_submission_or_success_event_is_not_success(self):
        for boundary in ("handler", "event"):
            with self.subTest(boundary=boundary):
                event, saved = Event(), []
                def handler(_):
                    saved.append(True)
                    if boundary == "handler":
                        event.set()
                    return {"status": "accepted"}
                def emit(*_):
                    event.set()
                loop, model = self.loop([completion([call(name="submit")])], handler)
                with patch('app.services.native_tool_loop.native_event_emitter', return_value=emit):
                    with self.assertRaises(NativeToolLoopError) as raised:
                        loop.run([], cancel_event=event)
                self.assertEqual(raised.exception.code, "CANCELLED")
                self.assertEqual((saved, len(model.requests)), ([True], 1))

    def test_cancel_after_model_and_between_tools_skips_side_effects(self):
        for boundary in ("model", "between_tools"):
            with self.subTest(boundary=boundary):
                event, effects = Event(), []
                def handler(_):
                    effects.append("first")
                    event.set()
                    return {"status": "accepted"}
                model = FakeNativeModel([completion([call("first", "submit"), call("second", "other")])])
                if boundary == "model":
                    invoke = model.invoke_native
                    def cancel(*args, **kwargs):
                        response = invoke(*args, **kwargs)
                        event.set()
                        return response
                    model.invoke_native = cancel
                loop = NativeToolLoop(model, [tool(handler, "submit"), tool(lambda _: effects.append("second"), "other")],
                    accepted_submission_tools=frozenset({"submit"}))
                with self.assertRaises(NativeToolLoopError) as raised:
                    loop.run([], cancel_event=event)
                self.assertEqual(raised.exception.code, "CANCELLED")
                self.assertEqual(effects, [] if boundary == "model" else ["first"])

    def test_no_submission_and_limits_keep_original_behavior(self):
        loop, _ = self.loop([completion(content="no submission")])
        result = loop.run([])
        self.assertEqual((result.text, result.termination_reason, result.tool_calls), ("no submission", "model_stop", 0))
        for options, calls, code in (({"max_model_calls": 1}, [call(name="submit")], "MODEL_CALL_LIMIT"),
                                    ({"max_tool_calls": 1}, [call("a", "submit"), call("b", "submit")], "TOOL_CALL_LIMIT"),
                                    ({}, [call("same", "submit"), call("same", "submit")], "DUPLICATE_TOOL_CALL_ID")):
            effects = []
            loop, _ = self.loop([completion(calls)], lambda _: effects.append(True), **options)
            with self.assertRaises(NativeToolLoopError) as raised:
                loop.run([])
            self.assertEqual(raised.exception.code, code)
            self.assertEqual(effects, [])

    def test_old_stage_acceptance_does_not_end_new_stage_and_ids_stay_protected(self):
        first, _ = self.loop([completion([call("old", "submit")])])
        history = first.run([]).messages
        loop, _ = self.loop([completion([call("new", "submit")]), completion(content="done")], lambda _: {"status": "completed"})
        result = loop.run(history)
        self.assertEqual(result.messages[:2], history)
        self.assertEqual((result.model_calls, result.termination_reason), (2, "model_stop"))
        duplicate, _ = self.loop([completion([call("old", "submit")])])
        with self.assertRaises(NativeToolLoopError) as raised:
            duplicate.run(history)
        self.assertEqual(raised.exception.code, "DUPLICATE_TOOL_CALL_ID")

    def test_same_loop_repeated_and_concurrent_runs_do_not_share_acceptance_or_ids(self):
        barrier = Barrier(2)
        class PerRequestModel:
            def invoke_native(self, messages, **kwargs):
                if len(messages) != 1:
                    raise AssertionError("confirmation forbidden")
                if messages[0]["content"].startswith("parallel"):
                    barrier.wait(timeout=5)
                return completion([call("same", "submit")])
        loop = NativeToolLoop(PerRequestModel(), [tool(lambda _: {"status": "accepted"}, "submit")],
                             accepted_submission_tools=frozenset({"submit"}))
        def run(label):
            return loop.run([{"role": "user", "content": label}])
        results = [run("one"), run("two")]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results.extend(pool.map(run, ["parallel-one", "parallel-two"]))
        self.assertEqual([r.model_calls for r in results], [1, 1, 1, 1])
        self.assertEqual([r.messages[0]["content"] for r in results], ["one", "two", "parallel-one", "parallel-two"])

    def test_stage_aware_model_for_six_names_preserves_paired_history(self):
        saved, requests = [], []
        class StageModel:
            def invoke_native(self, messages, *, tools, **kwargs):
                name = tools[0]["function"]["name"] if tools else None
                requests.append(name)
                if name is None:
                    return completion(content="master decision")
                if name in saved:
                    raise AssertionError("confirmation forbidden in accepted phase")
                return completion([call(name, name)])
        model, history = StageModel(), ()
        names = ("submit_requirements", "submit_attractions", "submit_restaurants",
                 "submit_lodging", "submit_daily_plan", "submit_day_revision")
        for name in names:
            def save(_, phase=name):
                saved.append(phase)
                return {"status": "accepted"}
            result = NativeToolLoop(model, [tool(save, name)],
                accepted_submission_tools=frozenset({name})).run(history)
            self.assertEqual(result.model_calls, 1)
            self.assertEqual(result.completions[0].finish_reason, "tool_calls")
            self.assertEqual(result.messages[:len(history)], history)
            history = result.messages
        result = NativeToolLoop(model, []).run(history)
        self.assertEqual((saved, requests), (list(names), [*names, None]))
        self.assertEqual((result.text, result.termination_reason), ("master decision", "model_stop"))
        self.assertEqual([m["tool_call_id"] for m in history if m["role"] == "tool"], list(names))

    def test_cancel_at_end_decision_after_batch_does_not_publish_success(self):
        event = Event()
        loop, _ = self.loop([completion([call(name="submit")])])
        original = loop._accepted_submission
        def cancel_at_decision(calls, contents):
            accepted = original(calls, contents)
            self.assertEqual(json.loads(contents[0])["data"]["status"], "accepted")
            event.set()
            return accepted
        with patch.object(loop, '_accepted_submission', side_effect=cancel_at_decision), \
                patch('app.services.native_tool_loop.native_event_emitter') as emitter:
            with self.assertRaises(NativeToolLoopError) as raised:
                loop.run([], cancel_event=event)
        self.assertEqual(raised.exception.code, "CANCELLED")
        emitter.assert_not_called()

    def test_unknown_completion_tool_rejected_and_positional_result_stays_compatible(self):
        with self.assertRaises(ValueError):
            NativeToolLoop(FakeNativeModel([]), [], accepted_submission_tools=frozenset({"missing"}))
        result = NativeToolLoopResult("done", (), (), 0, 1)
        self.assertEqual(result.termination_reason, "model_stop")


if __name__ == "__main__":
    unittest.main()
