import asyncio
import inspect
import json
import gzip
import unittest
from unittest.mock import Mock, patch

import httpx
from openai import AsyncOpenAI
from astrbot.api.provider import LLMResponse
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial

import drawing
import planner
import planner_transport
import test_core as core
import test_planner as planning


class ByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks, stall=False):
        self.chunks, self.stall, self.closed = chunks, stall, False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.stall:
            await asyncio.Future()

    async def aclose(self):
        self.closed = True


class PlannerHTTPTests(unittest.TestCase):
    make_plugin = core.CoreTests.make_plugin
    make_context_plugin = core.CoreTests.make_context_plugin
    make_planner = planning.PlannerTests.make_planner

    def provider(self, client):
        provider = object.__new__(ProviderOpenAIOfficial)
        provider.provider_config = {"id": "drawing", "type": "openai_chat_completion", "custom_extra_body": {}}
        provider.model_name = "grok-4.7"
        provider.api_keys = ["mock-key"]
        provider.client = client
        provider.reasoning_key = "reasoning_content"
        provider.default_params = inspect.signature(client.chat.completions.create).parameters.keys()
        return provider

    def sse(self, delta, finish=None, usage=None):
        data = {"id": "observed-response", "created": 0, "model": "grok-4.7", "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        if usage:
            data["usage"] = usage
        return ("data: " + json.dumps(data) + "\n\n").encode()

    def test_native_dialogue_and_current_reply_order_preserve_original_request(self):
        payload = {"drawing_request": "换白裙坐窗前", "user_request": "换白裙坐窗前n",
                   "recent_dialogue": [{"role": "user", "text": "进房间"}, {"role": "assistant", "text": "我坐下。"}],
                   "current_reply": "我穿白裙坐在窗前。", "available_cards": [], "previous_drawing": None}
        result = planner.request_parts(payload)
        self.assertEqual([m["role"] for m in result["contexts"]], ["user", "user", "assistant", "user", "assistant"])
        self.assertEqual(result["contexts"][-2]["content"], payload["drawing_request"])
        self.assertEqual(result["contexts"][-1]["content"], payload["current_reply"])
        self.assertEqual(core.planning_payload(result), payload)
        self.assertEqual(result["prompt"], payload["drawing_request"])

    def test_tool_contract_reuses_public_fields_and_auto_follows_chat_transport(self):
        tool = planner.plan_tools().openai_schema()[0]["function"]
        self.assertEqual(tool["name"], "novelai_generate_image")
        for key in ("prompt", "subjects", "style"):
            self.assertEqual(tool["parameters"]["properties"][key], drawing.TOOL_PARAMETERS["properties"][key])
        provider = Mock(provider_config={"type": "openai_chat_completion"})
        self.assertEqual(planner_transport.select(provider, "auto", main_streaming=False), "nonstream")
        self.assertEqual(planner_transport.select(provider, "stream", main_streaming=False), "stream")

    def test_http_503_is_one_attempt_without_sdk_retry_or_format_retry(self):
        async def run():
            requests = []
            def handle(request):
                requests.append(request)
                return httpx.Response(503, json={"error": {"message": "busy"}})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle), headers={"X-Custom": "preserved"}) as http:
                client = AsyncOpenAI(api_key="original-key", base_url="https://mock.invalid/v1", http_client=http, max_retries=2)
                provider = self.provider(client)
                p = self.make_planner()
                p.config.update(drawing_llm_output_mode="json", drawing_llm_transport="nonstream")
                p.context.get_provider_by_id = Mock(return_value=provider)
                with patch("main.logger.info"):
                    with self.assertRaisesRegex(ValueError, "调用失败"):
                        await p._plan_drawing(core.FakeEvent(), {"prompt": "window"})
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0].headers["X-Custom"], "preserved")
                self.assertEqual(client.max_retries, 2)
                self.assertEqual(client.api_key, "original-key")
                self.assertEqual(provider.provider_config["custom_extra_body"], {})
                self.assertFalse(http.is_closed)
                p._call_official.assert_not_awaited()
                p.context.llm_generate.assert_not_awaited()
                rows = p._planner_diagnostics[core.FakeEvent.unified_msg_origin]["snapshots"]
                self.assertEqual(rows["request"]["provider_details"]["sdk_max_retries"], 0)
                self.assertEqual(rows["http_headers"]["http_metrics"]["http_status"], 503)
        asyncio.run(run())

    def test_partial_stream_timeout_retains_http_metrics_and_opt_in_preview(self):
        async def run(debug):
            stream = ByteStream([self.sse({"reasoning_content": "never-log-this-reasoning"}),
                                 self.sse({"content": '{"prompt":"window'})], stall=True)
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
                    200, headers={"Content-Type": "text/event-stream", "x-request-id": "upstream-id"}, stream=stream))) as http:
                client = AsyncOpenAI(api_key="mock-key", base_url="https://mock.invalid/v1", http_client=http)
                p = self.make_planner()
                p.config.update(drawing_llm_output_mode="json", drawing_llm_transport="stream", drawing_llm_debug=debug)
                p.context.get_provider_by_id = Mock(return_value=self.provider(client))
                wait_for = asyncio.wait_for
                async def short_wait(call, timeout):
                    return await wait_for(call, timeout=0.05)
                with patch("main.asyncio.wait_for", side_effect=short_wait), patch("main.logger.info"):
                    with self.assertRaisesRegex(ValueError, "超时"):
                        await p._plan_drawing(core.FakeEvent(), {"prompt": "window"})
                self.assertTrue(stream.closed)
                self.assertFalse(http.is_closed)
                snapshot = p._planner_diagnostics[core.FakeEvent.unified_msg_origin]
                metrics = snapshot["snapshots"]["response"]["http_metrics"]
                self.assertEqual(metrics["http_attempts"], 1)
                self.assertEqual(metrics["server_request_id"], "upstream-id")
                self.assertGreater(metrics["reasoning_chars"], 0)
                self.assertGreater(metrics["content_chars"], 0)
                self.assertIn("headers_ms", metrics)
                self.assertIn("first_body_ms", metrics)
                self.assertEqual("partial_response_preview" in metrics, debug)
                self.assertNotIn("never-log-this-reasoning", json.dumps(snapshot))
                p._call_official.assert_not_awaited()
        asyncio.run(run(False))
        asyncio.run(run(True))

    def test_headers_timeout_is_distinct_from_output_timeout(self):
        async def run():
            entered = asyncio.Event()
            async def handle(request):
                entered.set()
                await asyncio.Future()
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                client = AsyncOpenAI(api_key="mock-key", base_url="https://mock.invalid/v1", http_client=http)
                provider, _ = planner_transport.request_provider(self.provider(client), "inherit", 0, mode="json",
                    record=lambda phase, metrics: rows.append((phase, metrics)))
                rows = []
                task = asyncio.create_task(provider.text_chat(prompt="window", request_max_retries=1))
                await asyncio.wait_for(entered.wait(), 2)
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(task, 0.02)
                self.assertEqual(rows[-1][0], "http_error")
                self.assertFalse(rows[-1][1]["headers_received"])
                self.assertEqual(rows[-1][1]["http_attempts"], 1)
        asyncio.run(run())

    def test_real_sdk_tool_arguments_observable_before_astrbot_final_response(self):
        async def run():
            plan = {"prompt": "window", "subjects": [{"source": "self"}]}
            encoded = json.dumps(plan)
            chunks = [self.sse({"tool_calls": [{"index": 0, "id": "call1", "type": "function", "function": {
                "name": "novelai_generate_image", "arguments": encoded[:10]}}]}),
                self.sse({"tool_calls": [{"index": 0, "function": {"arguments": encoded[10:]}}]}),
                self.sse({}, "tool_calls", {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140,
                    "completion_tokens_details": {"reasoning_tokens": 10}}), b"data: [DONE]\n\n"]
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
                    200, headers={"Content-Type": "text/event-stream"}, stream=ByteStream(chunks)))) as http:
                client = AsyncOpenAI(api_key="mock-key", base_url="https://mock.invalid/v1", http_client=http)
                p = self.make_planner()
                p.config.update(drawing_llm_output_mode="tool", drawing_llm_transport="stream")
                p.context.get_provider_by_id = Mock(return_value=self.provider(client))
                with patch("main.logger.info"):
                    result = await p._plan_drawing(core.FakeEvent(), {"prompt": "window"})
                self.assertEqual(result["prompt"], "window")
                snapshot = p._planner_diagnostics[core.FakeEvent.unified_msg_origin]
                metrics = snapshot["snapshots"]["http_end"]["http_metrics"]
                self.assertEqual(metrics["tool_arguments_chars"], len(encoded))
                self.assertIn("first_tool_arguments_ms", metrics)
                self.assertEqual(metrics["usage"]["reasoning_tokens"], 10)
                self.assertEqual(metrics["wire"]["tools"], ["novelai_generate_image"])
                self.assertIsNone(metrics["wire"]["response_format_type"])
                self.assertEqual(snapshot["stream_metrics"]["chunks"], 0)
        asyncio.run(run())

    def test_json_wire_and_compressed_response_on_legacy_and_native_http_clients(self):
        import planner_http
        async def run(module, stream):
            requests = []
            plan = {"prompt": "window", "subjects": [{"source": "self"}]}
            encoded = json.dumps(plan)
            data = (self.sse({"content": encoded}) + self.sse({}, "stop") + b"data: [DONE]\n\n") if stream else json.dumps({
                "id": "compressed-response", "created": 0, "model": "grok-4.7", "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": encoded}, "finish_reason": "stop"}]}).encode()
            compressed = gzip.compress(data)
            class CompressedStream(module.AsyncByteStream):
                async def __aiter__(self):
                    yield compressed[:5]
                    yield compressed[5:]
                async def aclose(self):
                    pass
            def handle(request):
                requests.append(json.loads(request.content))
                return module.Response(200, headers={"Content-Type": "text/event-stream" if stream else "application/json",
                    "Content-Encoding": "gzip"}, stream=CompressedStream())
            async with module.AsyncClient(transport=module.MockTransport(handle)) as http:
                client = AsyncOpenAI(api_key="mock-key", base_url="https://mock.invalid/v1", http_client=http)
                p = self.make_planner()
                p.config.update(drawing_llm_output_mode="json", drawing_llm_transport="stream" if stream else "auto")
                p.context.get_config = Mock(return_value={"provider_settings": {"streaming_response": False}})
                p.context.get_provider_by_id = Mock(return_value=self.provider(client))
                with patch("main.logger.info"):
                    result = await p._plan_drawing(core.FakeEvent(), {"prompt": "window"})
                self.assertEqual(result["prompt"], "window")
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0]["response_format"], {"type": "json_object"})
                self.assertEqual(requests[0]["stream"], stream)
                self.assertEqual(requests[0]["messages"][-1]["content"], "window")
                metrics = p._planner_diagnostics[core.FakeEvent.unified_msg_origin]["snapshots"]["http_end"]["http_metrics"]
                self.assertEqual(metrics["content_chars"], len(encoded))
                self.assertEqual(metrics["body_bytes"], len(compressed))
                self.assertFalse(http.is_closed)
        modules = [httpx] + ([planner_http.httpx2] if planner_http.httpx2 else [])
        for module in modules:
            for stream in (False, True):
                asyncio.run(run(module, stream))

    def test_real_local_http_connection_and_header_trace_without_external_service(self):
        async def run():
            payload = json.dumps({"id": "local", "created": 0, "model": "grok-4.7", "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content":
                    '{"prompt":"window","subjects":[]}'}, "finish_reason": "stop"}]}).encode()
            async def serve(reader, writer):
                try:
                    headers = await reader.readuntil(b"\r\n\r\n")
                    length = next(int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                                  if line.lower().startswith(b"content-length:"))
                    await reader.readexactly(length)
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: " +
                                 str(len(payload)).encode() + b"\r\n\r\n" + payload)
                    await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()
            async with await asyncio.start_server(serve, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                async with httpx.AsyncClient(trust_env=False) as http:
                    client = AsyncOpenAI(api_key="mock-key", base_url=f"http://127.0.0.1:{port}/v1", http_client=http)
                    p = self.make_planner()
                    p.config.update(drawing_llm_output_mode="json", drawing_llm_transport="nonstream")
                    p.context.get_provider_by_id = Mock(return_value=self.provider(client))
                    with patch("main.logger.info"):
                        await p._plan_drawing(core.FakeEvent(), {"prompt": "window"})
                    metrics = p._planner_diagnostics[core.FakeEvent.unified_msg_origin]["snapshots"]["http_end"]["http_metrics"]
                    events = [e["event"] for e in metrics["network_events"]]
                    self.assertIn("connection.connect_tcp.complete", events)
                    self.assertIn("http11.send_request_headers.complete", events)
                    self.assertIn("http11.receive_response_headers.complete", events)
                    self.assertEqual(metrics["http_attempts"], 1)
        asyncio.run(run())

    def test_unsupported_json_constraint_reports_actionable_error_without_retry(self):
        async def run():
            requests = []
            def handle(request):
                requests.append(request)
                return httpx.Response(400, json={"error": {"message": "response_format json_object unsupported"}})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                client = AsyncOpenAI(api_key="mock-key", base_url="https://mock.invalid/v1", http_client=http)
                p = self.make_planner()
                p.config.update(drawing_llm_output_mode="json", drawing_llm_transport="nonstream")
                p.context.get_provider_by_id = Mock(return_value=self.provider(client))
                with patch("main.logger.info"):
                    with self.assertRaisesRegex(planner.PlanError, "JSON 格式约束"):
                        await p._plan_drawing(core.FakeEvent(), {"prompt": "window"})
                self.assertEqual(len(requests), 1)
                p._call_official.assert_not_awaited()
        asyncio.run(run())
