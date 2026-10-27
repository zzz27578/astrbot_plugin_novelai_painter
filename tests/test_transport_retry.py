import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import planner
import planner_transport
import test_core as core
import test_planner as planning
from astrbot.api.provider import LLMResponse
from main import NovelAIPainterPlugin, ProviderError


class TransportRetryTests(unittest.TestCase):
    make_plugin = core.CoreTests.make_plugin
    make_context_plugin = core.CoreTests.make_context_plugin
    make_planner = planning.PlannerTests.make_planner

    def test_503_default_disabled_and_configured_retries_are_bounded(self):
        async def run(count, expected):
            p = self.make_context_plugin()
            p.config["retry_503_count"] = count
            p._call_official.side_effect = NovelAIPainterPlugin._http_error(503, b"", {"Retry-After": "7"})
            with patch("main.asyncio.sleep", new=AsyncMock()) as sleep:
                result = await p._run_job(core.FakeEvent(), "window")
            self.assertEqual(p._call_official.await_count, expected)
            self.assertEqual(result.attempts, expected)
            self.assertFalse(result.ok)
            for args in sleep.await_args_list:
                self.assertEqual(args.args[0], 7)
            self.assertFalse(p.lock.locked())
        for count, expected in ((0, 1), (1, 2), (3, 4), (99, 4)):
            asyncio.run(run(count, expected))

    def test_503_retries_only_image_api_and_delivers_once(self):
        async def run():
            p = self.make_planner()
            p.config["retry_503_count"] = 1
            p._call_official.side_effect = [p._http_error(503, b""), "image.png"]
            async def wait(seconds):
                self.assertFalse(p.lock.locked())
                self.assertTrue(await p.lock.acquire())
                p.lock.release()
            with patch("main.asyncio.sleep", side_effect=wait):
                await p._quick_draw(core.FakeEvent("/n window"), "window")
            p.context.llm_generate.assert_awaited_once()
            self.assertEqual(p._call_official.await_count, 2)
            p._finish_event.assert_awaited_once()
        asyncio.run(run())

    def test_mixed_429_503_budgets_are_independent_and_other_errors_do_not_retry(self):
        async def run():
            p = self.make_context_plugin()
            p.config.update(retry_mode="rate_limit_twice", retry_503_count=3)
            p._call_official.side_effect = [p._http_error(code, b"") for code in (503, 429, 503, 429, 503)] + ["image.png"]
            with patch("main.asyncio.sleep", new=AsyncMock()):
                result = await p._run_job(core.FakeEvent(), "window")
            self.assertTrue(result.ok)
            self.assertEqual(result.attempts, 6)
            for code in ("network", "http_500", "http_502", "auth"):
                self.assertFalse(p._should_retry(ProviderError(code, "failed", retryable=True), 1))
        asyncio.run(run())

    def test_image_diagnostics_do_not_lookup_backend_type_as_chat_provider(self):
        p = self.make_planner()
        p.context.get_provider_by_id = Mock()
        p._planner_log(core.FakeEvent(), "openai_compatible", "image", 0, None, phase="image_start")
        p.context.get_provider_by_id.assert_not_called()

    def test_local_overrides_never_mutate_shared_provider_or_inherited_extras(self):
        config = {"type": "openai_chat_completion", "custom_extra_body": {
            "temperature": 0.8, "reasoning_effort": "high", "max_tokens": 8000}}
        original = SimpleNamespace(provider_config=config)
        local, overrides = planner_transport.request_provider(original, "low", 2048)
        self.assertIsNot(local, original)
        self.assertEqual(config["custom_extra_body"]["reasoning_effort"], "high")
        self.assertEqual(config["custom_extra_body"]["max_tokens"], 8000)
        self.assertEqual(local.provider_config["custom_extra_body"], {
            "temperature": 0.8, "reasoning_effort": "low", "max_completion_tokens": 2048})
        self.assertEqual(overrides["reasoning_effort"], "low")
        self.assertIs(planner_transport.request_provider(original, "inherit", 0)[0], original)

    def test_stream_waits_for_final_and_reads_mutable_chunks_without_double_counting(self):
        async def run():
            response = LLMResponse("assistant", is_chunk=True)
            final = LLMResponse("assistant", completion_text='{"prompt":"window","subjects":[]}')
            closed = []
            async def stream(**kwargs):
                try:
                    response.completion_text = "first"
                    yield response
                    response.completion_text = "second"
                    response.reasoning_content = "reason"
                    yield response
                    yield final
                finally:
                    closed.append(True)
            records = []
            result = await planner_transport.collect_stream(SimpleNamespace(text_chat_stream=stream), {},
                lambda phase, metrics: records.append((phase, metrics)), clock=lambda: 0)
            self.assertIs(result, final)
            self.assertEqual(records[-1][1]["streamed_text_chars"], 11)
            self.assertEqual(records[-1][1]["streamed_reasoning_chars"], 6)
            self.assertEqual(records[-1][1]["chunks"], 2)
            self.assertTrue(closed)
        asyncio.run(run())

    def test_stream_without_final_never_uses_partial_json_or_falls_back(self):
        async def run():
            p = self.make_planner()
            async def stream(**kwargs):
                yield LLMResponse("assistant", completion_text='{"prompt":"window","subjects":[]}', is_chunk=True)
            provider = SimpleNamespace(provider_config={"type": "openai_chat_completion"}, text_chat_stream=stream)
            p.context.get_provider_by_id = Mock(return_value=provider)
            p._notify = AsyncMock()
            await p._quick_draw(core.FakeEvent("/n window"), "window")
            p.context.llm_generate.assert_not_awaited()
            p._call_official.assert_not_awaited()
            self.assertIn("完整最终结果", p._notify.await_args.args[1])
        asyncio.run(run())

    def test_timeout_cancels_stream_and_closes_iterator(self):
        async def run():
            closed = asyncio.Event()
            async def stream(**kwargs):
                try:
                    yield LLMResponse("assistant", completion_text="partial", is_chunk=True)
                    await asyncio.Future()
                finally:
                    closed.set()
            task = planner_transport.collect_stream(SimpleNamespace(text_chat_stream=stream), {}, lambda *args: None)
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(task, timeout=0.01)
            self.assertTrue(closed.is_set())
        asyncio.run(run())

    def test_real_openai_wire_streams_and_applies_request_only_overrides_for_json_and_tool(self):
        from openai.types.chat import ChatCompletionChunk
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
        async def run(mode):
            plan = {"prompt": "window, sunlight", "subjects": [{"source": "self"}]}
            encoded = json.dumps(plan)
            p = self.make_planner()
            p.config.update(drawing_llm_output_mode=mode, drawing_llm_reasoning_effort="low", drawing_llm_max_tokens=2048)
            provider = object.__new__(ProviderOpenAIOfficial)
            provider.provider_config = {"type": "openai_chat_completion", "custom_extra_body": {"reasoning_effort": "high"}}
            provider.model_name = "grok-4.7"
            provider.reasoning_key = "reasoning_content"
            provider.default_params = {"model", "messages", "tools", "tool_choice"}
            provider.api_keys = ["mock-test-key"]
            provider._apply_provider_specific_request_overrides = Mock()
            provider._prepare_chat_payload = AsyncMock(return_value=({"model": "grok-4.7", "messages": []}, []))
            def chunk(delta, finish=None, usage=None):
                return ChatCompletionChunk.model_validate({"id": "mock", "created": 0, "model": "grok-4.7", "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], "usage": usage})
            async def network_stream():
                yield chunk({"role": "assistant"})
                if mode == "json":
                    yield chunk({"content": encoded[:20]})
                    yield chunk({"content": encoded[20:]})
                else:
                    yield chunk({"tool_calls": [{"index": 0, "id": "call1", "type": "function", "function": {
                        "name": "submit_drawing_plan", "arguments": encoded[:20]}}]})
                    yield chunk({"tool_calls": [{"index": 0, "function": {"arguments": encoded[20:]}}]})
                yield chunk({}, "stop" if mode == "json" else "tool_calls", {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150})
            create = AsyncMock(side_effect=lambda **kwargs: network_stream())
            provider.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
            p.context.get_provider_by_id = Mock(return_value=provider)
            with patch("main.logger.info"):
                result = await p._plan_drawing(core.FakeEvent(), {"prompt": "draw"})
            self.assertEqual(result["prompt"], plan["prompt"])
            self.assertEqual(result["subjects"][0]["source"], "self")
            p.context.llm_generate.assert_not_awaited()
            create.assert_awaited_once()
            wire = create.await_args.kwargs
            self.assertTrue(wire["stream"])
            expected = {"reasoning_effort": "low", "max_completion_tokens": 2048}
            if mode == "json":
                expected["response_format"] = {"type": "json_object"}
            self.assertEqual(wire["extra_body"], expected)
            self.assertEqual(provider.provider_config["custom_extra_body"], {"reasoning_effort": "high"})
            if mode == "tool":
                self.assertEqual(wire["tool_choice"], "required")
                self.assertEqual(wire["tools"][0]["function"]["name"], "novelai_generate_image")
            response = p._planner_diagnostics[core.FakeEvent.unified_msg_origin]
            self.assertEqual(response["response_details"]["usage"]["total_tokens"], 150)
            if mode == "json":
                self.assertGreater(response["stream_metrics"]["chunks"], 0)
            else:
                # AstrBot only yields accumulated tool arguments in the final
                # response, not raw tool deltas. Do not invent a first token.
                self.assertEqual(response["stream_metrics"]["chunks"], 0)
                self.assertNotIn("first_chunk_ms", response["stream_metrics"])
        asyncio.run(run("json"))
        asyncio.run(run("tool"))

    def test_nonstream_request_overrides_reach_real_openai_extra_body(self):
        from openai.types.chat import ChatCompletion
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
        async def run():
            provider = object.__new__(ProviderOpenAIOfficial)
            provider.provider_config = {"type": "openai_chat_completion", "custom_extra_body": {"reasoning_effort": "high"}}
            provider.reasoning_key = "reasoning_content"
            provider.default_params = {"model", "messages"}
            provider.api_keys = ["mock-test-key"]
            provider._apply_provider_specific_request_overrides = Mock()
            provider._prepare_chat_payload = AsyncMock(return_value=({"model": "grok-4.7", "messages": []}, []))
            raw = {"id": "mock", "created": 0, "model": "grok-4.7", "object": "chat.completion", "choices": [{"index": 0,
                "finish_reason": "stop", "message": {"role": "assistant", "content": '{"prompt":"window","subjects":[]}'}}]}
            create = AsyncMock(return_value=ChatCompletion.model_validate(raw))
            provider.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
            p = self.make_planner()
            p.config.update(drawing_llm_transport="nonstream", drawing_llm_reasoning_effort="low")
            p.context.get_provider_by_id = Mock(return_value=provider)
            await p._plan_drawing(core.FakeEvent(), {"prompt": "draw"})
            self.assertFalse(create.await_args.kwargs["stream"])
            self.assertEqual(create.await_args.kwargs["extra_body"]["reasoning_effort"], "low")
            self.assertEqual(provider.provider_config["custom_extra_body"]["reasoning_effort"], "high")
        asyncio.run(run())

    def test_new_settings_save_validate_and_preserve_legacy_429_limit(self):
        async def run():
            p = self.make_planner()
            p._save_config = Mock()
            p._sync_sticker_emotion_tool = Mock()
            p._page_error = Mock(side_effect=lambda value: {"error": value})
            fields = {"drawing_llm_transport": "stream", "drawing_llm_reasoning_effort": "low", "drawing_llm_max_tokens": 2048,
                "retry_503_count": 2, "retry_mode": "none"}
            with patch("main.request", SimpleNamespace(json=AsyncMock(return_value=fields))), patch("main.json_response", side_effect=lambda value: value):
                self.assertTrue((await p.page_save_config())["saved"])
            self.assertEqual(p._retry_limit(), 1)
            self.assertEqual(p._503_retries(), 2)
            for invalid in ({"retry_503_count": -1}, {"retry_503_count": 4}, {"drawing_llm_max_tokens": 16385},
                            {"drawing_llm_transport": "bad"}, {"drawing_llm_reasoning_effort": "bad"}):
                p._save_config.reset_mock()
                with patch("main.request", SimpleNamespace(json=AsyncMock(return_value=invalid))):
                    self.assertIn("error", await p.page_save_config())
                p._save_config.assert_not_called()
        asyncio.run(run())

    def test_marker_and_slash_n_share_stream_transport_without_losing_original_request(self):
        async def run(marker):
            p = self.make_planner()
            p.config["drawing_llm_output_mode"] = "json"
            calls = []
            async def stream(**kwargs):
                calls.append(kwargs)
                yield LLMResponse("assistant", completion_text='{"prompt":"window","subjects":[{"source":"self"}]}')
            p.context.get_provider_by_id = Mock(return_value=SimpleNamespace(
                provider_config={"type": "openai_chat_completion"}, text_chat_stream=stream))
            event = core.FakeEvent("画窗边的你n" if marker else "/n 画窗边的你")
            if marker:
                await p.drawing_marker_event(event)
                await p.drawing_after_reply(event)
                self.assertEqual(calls, [])
                await p.drawing_capture_reply(event, LLMResponse("assistant", completion_text="完整的本轮回复"))
                await p.drawing_after_reply(event)
                await p.drawing_after_reply(event)
            else:
                await p._quick_draw(event, "画窗边的你")
            self.assertEqual(len(calls), 1)
            payload = core.planning_payload(calls[0])
            self.assertEqual(payload["user_request"], event.message)
            self.assertEqual(payload["current_reply"], "完整的本轮回复" if marker else "")
            p.context.llm_generate.assert_not_awaited()
            p._call_official.assert_awaited_once()
        asyncio.run(run(False))
        asyncio.run(run(True))

    def test_truncated_final_stream_does_not_trigger_another_planning_call(self):
        async def run():
            p = self.make_planner()
            calls = []
            async def stream(**kwargs):
                calls.append(kwargs)
                yield LLMResponse("assistant", completion_text='{"prompt":"window","subjects":[]}',
                    raw_completion={"choices": [{"finish_reason": "length"}]})
            p.context.get_provider_by_id = Mock(return_value=SimpleNamespace(
                provider_config={"type": "openai_chat_completion"}, text_chat_stream=stream))
            p._notify = AsyncMock()
            await p._quick_draw(core.FakeEvent("/n window"), "window")
            self.assertEqual(len(calls), 1)
            p._call_official.assert_not_awaited()
            self.assertIn("截断", p._notify.await_args.args[1])
        asyncio.run(run())

    def test_stale_conversation_during_503_wait_cancels_retry(self):
        async def run():
            p = self.make_context_plugin()
            p.config["retry_503_count"] = 1
            p._call_official.side_effect = p._http_error(503, b"")
            p._generation_context_current = AsyncMock(side_effect=[True, False])
            with patch("main.asyncio.sleep", new=AsyncMock()):
                result = await p._run_job(core.FakeEvent(), "window")
            p._call_official.assert_awaited_once()
            self.assertEqual(result.error_code, "stale_context")
            self.assertFalse(p.lock.locked())
        asyncio.run(run())

    def test_real_sdk_http_body_and_sse_with_actual_astrbot_payload_builder(self):
        import httpx
        from openai import AsyncOpenAI
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
        async def run():
            seen = []
            plan = {"prompt": "window", "subjects": [{"source": "self"}]}
            def handle(request):
                seen.append(json.loads(request.content))
                events = []
                for delta, finish in (({"role": "assistant", "content": json.dumps(plan)}, None), ({}, "stop")):
                    chunk = {"id": "http-mock", "created": 0, "model": "grok-4.7", "object": "chat.completion.chunk",
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                    if finish:
                        chunk["usage"] = {"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80}
                    events.append("data: " + json.dumps(chunk) + "\n\n")
                return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                    content=("".join(events) + "data: [DONE]\n\n").encode())
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client:
                client = AsyncOpenAI(api_key="mock-key", base_url="https://mock.invalid/v1", http_client=http_client, max_retries=0)
                provider = object.__new__(ProviderOpenAIOfficial)
                provider.provider_config = {"type": "openai_chat_completion"}
                provider.model_name = "grok-4.7"
                provider.reasoning_key = "reasoning_content"
                provider.default_params = {"model", "messages", "tools", "tool_choice"}
                provider.api_keys = ["mock-key"]
                provider.client = client
                p = self.make_planner()
                p.config.update(drawing_llm_output_mode="json", drawing_llm_reasoning_effort="low", drawing_llm_max_tokens=2048)
                p.context.get_provider_by_id = Mock(return_value=provider)
                await p._plan_drawing(core.FakeEvent("画窗边的你"), {"prompt": "画窗边的你"})
            self.assertEqual(len(seen), 1)
            self.assertTrue(seen[0]["stream"])
            self.assertEqual(seen[0]["reasoning_effort"], "low")
            self.assertEqual(seen[0]["max_completion_tokens"], 2048)
            self.assertEqual(seen[0]["messages"][0]["role"], "system")
            user = seen[0]["messages"][-1]["content"]
            if isinstance(user, list):
                user = "".join(part.get("text", "") for part in user)
            self.assertEqual(user, "画窗边的你")
            self.assertEqual(seen[0]["response_format"], {"type": "json_object"})
        asyncio.run(run())
