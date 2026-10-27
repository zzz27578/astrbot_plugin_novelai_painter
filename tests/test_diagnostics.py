import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import diagnostics
import test_core as core
import test_planner as planning
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.core.provider.entities import TokenUsage


class DiagnosticsTests(unittest.TestCase):
    make_planner = planning.PlannerTests.make_planner
    make_context_plugin = core.CoreTests.make_context_plugin
    make_plugin = core.CoreTests.make_plugin

    def logs(self, logger):
        return [json.loads(call.args[0].split("绘图规划: ", 1)[1]) for call in logger.call_args_list
                if "绘图规划: " in call.args[0]]

    def test_registered_schema_requires_complete_direct_drawing_parameters(self):
        from astrbot.core.provider.register import llm_tools
        tool = next(t for t in llm_tools.func_list if t.name == "novelai_generate_image")
        prompt = tool.parameters["properties"]["prompt"]["description"]
        self.assertIn("本工具直接请求图片服务", prompt)
        self.assertIn("必须提供完整参数", prompt)
        self.assertNotIn("可只写简短绘图意图", prompt)

    def test_provider_descriptor_does_not_dump_credentials_or_arbitrary_config(self):
        provider = SimpleNamespace(provider_config={"id": "grok", "type": "openai_chat_completion",
            "key": ["provider-secret"], "custom_headers": {"Authorization": "header-secret"},
            "custom_extra_body": {"reasoning_effort": "high", "max_tokens": 4096,
                "thinking": {"type": "enabled", "budget_tokens": 1000, "secret": "thinking-secret"},
                "metadata": {"secret": "extra-secret"}}}, model_name="grok-4.7", timeout=120,
            client=SimpleNamespace(max_retries=2, base_url="https://user:password@api.example.com/private-key/v1?api_key=query-secret"))
        result = diagnostics.provider_details(provider)
        text = json.dumps(result)
        for secret in ("user", "password", "private-key", "query-secret", "header-secret", "extra-secret", "thinking-secret", "provider-secret"):
            self.assertNotIn(secret, text)
        self.assertEqual(result["model"], "grok-4.7")
        self.assertEqual(result["endpoint_host"], "api.example.com")
        self.assertEqual(result["sdk_max_retries"], 2)
        self.assertEqual(result["configured_extra_body"]["reasoning_effort"], "high")
        provider.client.base_url = "https://api.example.com/other/v1"
        self.assertNotEqual(result["endpoint_fingerprint"], diagnostics.provider_details(provider)["endpoint_fingerprint"])

    def test_response_usage_and_finish_reason_support_dict_real_sdk_and_stream_usage(self):
        from openai.types.chat import ChatCompletion
        raw = {"id": "completion-123", "object": "chat.completion", "created": 0, "model": "grok-4.7",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant", "content": None}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 80, "total_tokens": 180,
                "prompt_tokens_details": {"cached_tokens": 90}, "completion_tokens_details": {"reasoning_tokens": 60}}}
        for completion in (raw, ChatCompletion.model_validate(raw)):
            response = LLMResponse("assistant", raw_completion=completion, reasoning_content="private thoughts")
            result = diagnostics.response_details(response)
            self.assertEqual(result["usage"]["reasoning_tokens"], 60)
            self.assertEqual(result["usage"]["cached_tokens"], 90)
            self.assertEqual(result["finish_reason"], "tool_calls")
            self.assertNotIn("private thoughts", json.dumps(result))
        response = LLMResponse("assistant", usage=TokenUsage(input_other=10, input_cached=20, output=30))
        self.assertEqual(diagnostics.response_details(response)["usage"],
            {"input_tokens": 30, "cached_tokens": 20, "output_tokens": 30, "total_tokens": 60})
        self.assertEqual(diagnostics.response_details(LLMResponse("assistant"))["usage"], {})

    def test_request_sizes_sources_timing_and_image_job_share_request_id(self):
        async def run():
            p = self.make_planner()
            p.config.update(drawing_llm_context_messages=0, drawing_llm_use_images=True)
            event = core.FakeEvent("画窗边的你n")
            await p.drawing_marker_event(event)
            event.set_extra("nai_planner_images", ["https://private.example.com/image?token=secret"])
            event.set_extra("nai_planner_context", [])
            with patch("main.logger.info") as logger:
                plan = await p._plan_drawing(event, {"prompt": "画窗边的你"})
                await p._image_tool_entry(event, **plan, accepted=True, entry="command")
            logs = self.logs(logger)
            self.assertEqual(len({row["request_id"] for row in logs}), 1)
            request = next(row for row in logs if row["phase"] == "request")
            self.assertTrue(request["context_disabled"])
            self.assertEqual(request["image_sources"], ["main_request_image"])
            self.assertEqual(request["context_source"], "main_request_snapshot")
            self.assertGreater(request["total_text_chars"], request["request_chars"])
            self.assertGreater(request["payload_size"]["utf8_bytes"], request["request_chars"])
            phases = [row["phase"] for row in logs]
            self.assertLess(phases.index("received"), phases.index("response"))
            result = next(row for row in logs if row["phase"] == "response")
            self.assertIn("provider_ms", result)
            self.assertIn("validation_ms", result)
            api_request = next(row for row in logs if row["phase"] == "image_api_request")
            self.assertIn("queue_ms", api_request)
            self.assertEqual(api_request["job_id"], next(row for row in logs if row["phase"] == "image_end")["job_id"])
            self.assertNotIn("private.example.com", json.dumps(logs))
            snapshot = p._planner_diagnostics[event.unified_msg_origin]
            self.assertIn("response_details", snapshot["snapshots"]["response"])
            self.assertIn("provider_details", snapshot["snapshots"]["request"])
            self.assertLessEqual(len(snapshot["timeline"]), 24)
        asyncio.run(run())

    def test_main_baseline_resolves_selected_provider_and_keeps_reply_private(self):
        async def run():
            p = self.make_planner()
            provider = SimpleNamespace(provider_config={"id": "selected-grok", "type": "openai_chat_completion"}, model_name="grok-4.7")
            p.context.get_provider_by_id = Mock(return_value=provider)
            event = core.FakeEvent("画图")
            event.set_extra("selected_provider", "selected-grok")
            p.config["drawing_llm_debug"] = True
            with patch("main.logger.info") as logger:
                await p.drawing_identity_context(event, ProviderRequest(prompt="private prompt", model="override-model"))
                await p.drawing_capture_reply(event, LLMResponse("assistant", completion_text="private reply"))
            logs = self.logs(logger)
            request = next(row for row in logs if row["phase"] == "main_request_hook")
            response = next(row for row in logs if row["phase"] == "main_response_hook")
            self.assertEqual(request["provider"], "selected-grok")
            self.assertEqual(request["provider_details"]["request_model_override"], "override-model")
            self.assertEqual(response["timing_scope"], "agent_turn_including_tools")
            self.assertNotIn("private reply", json.dumps(logs))
            self.assertNotIn("private prompt", json.dumps(logs))
        asyncio.run(run())

    def test_provider_error_logs_status_and_redacts_provider_key_url(self):
        async def run():
            p = self.make_planner()
            p.context.get_provider_by_id = Mock(return_value=SimpleNamespace(provider_config={"key": ["provider-secret"]}))
            error = RuntimeError("provider-secret failed at https://user:pass@api.example.com/path?key=hidden")
            error.status_code = 503
            error.request_id = "server-request-123"
            p.context.llm_generate.side_effect = error
            with patch("main.logger.info") as logger:
                with self.assertRaises(ValueError):
                    await p._plan_drawing(core.FakeEvent(), {"prompt": "window"})
            logs = self.logs(logger)
            result = logs[-1]
            self.assertEqual(result["http_status"], 503)
            self.assertEqual(result["exception_type"], "RuntimeError")
            self.assertEqual(result["server_request_id"], "server-request-123")
            self.assertFalse(result["retry"])
            self.assertNotIn("provider-secret", json.dumps(logs))
            self.assertNotIn("user:pass", json.dumps(logs))
            self.assertNotIn("hidden", json.dumps(logs))
            p.context.llm_generate.assert_awaited_once()
        asyncio.run(run())

    def test_wait_heartbeat_stops_and_does_not_survive_timeout_or_cancellation(self):
        async def run(cancel):
            p = self.make_planner()
            event = core.FakeEvent()
            started = asyncio.Event()
            stopped = asyncio.Event()
            async def watcher(*args):
                started.set()
                try:
                    await asyncio.Future()
                finally:
                    stopped.set()
            async def generate(**kwargs):
                await started.wait()
                if cancel:
                    raise asyncio.CancelledError()
                raise asyncio.TimeoutError()
            p._planner_wait_log = watcher
            p.context.llm_generate.side_effect = generate
            with patch("main.logger.info") as logger:
                with self.assertRaises(asyncio.CancelledError if cancel else ValueError):
                    await p._plan_drawing(event, {"prompt": "window"})
            self.assertTrue(stopped.is_set())
            self.assertEqual(self.logs(logger)[-1]["status"], "cancelled" if cancel else "timeout")
            p.context.llm_generate.assert_awaited_once()
            p._call_official.assert_not_awaited()
        asyncio.run(run(False))
        asyncio.run(run(True))

    def test_duplicate_main_tool_log_distinguishes_completed_drawing(self):
        async def run():
            p = self.make_planner()
            event = core.FakeEvent()
            with patch("main.logger.info") as logger:
                await p.novelai_generate_image(event, "window", subjects=[{"source": "self"}])
                duplicate = await p.novelai_generate_image(event, "window", subjects=[{"source": "self"}])
            self.assertIn("DUPLICATE", duplicate)
            self.assertEqual(self.logs(logger)[-1]["drawing_state"], "completed")
            p._call_official.assert_awaited_once()
        asyncio.run(run())

    def test_heartbeat_reports_provider_wait_without_claiming_first_token(self):
        async def run():
            p = self.make_planner()
            with patch("main.asyncio.sleep", new=AsyncMock(side_effect=[None, asyncio.CancelledError()])), \
                    patch("main.time.perf_counter", return_value=45), patch("main.logger.info") as logger:
                with self.assertRaises(asyncio.CancelledError):
                    await p._planner_wait_log(core.FakeEvent(), "drawing-provider", "json", 1, 15, 120)
            result = self.logs(logger)[-1]
            self.assertEqual(result["phase"], "provider_wait")
            self.assertEqual(result["elapsed_ms"], 30000)
            self.assertEqual(result["remaining_seconds"], 90)
            self.assertFalse(result["first_token_observable"])
        asyncio.run(run())

    def test_current_message_and_previous_image_have_distinct_sources(self):
        from astrbot.core.message.components import Image
        async def run():
            p = self.make_planner()
            p.config["drawing_llm_use_images"] = True
            image = p.temp_dir / "previous.png"
            image.write_bytes(b"image")
            p._previous_drawing = Mock(return_value={"image": str(image), "plan": {"prompt": "previous", "subjects": []}})
            component = Mock(spec=Image)
            component.convert_to_file_path = AsyncMock(return_value="current.png")
            event = core.FakeEvent()
            event.get_messages = Mock(return_value=[component])
            with patch("main.logger.info") as logger:
                await p._plan_drawing(event, {"prompt": "window"})
            request = next(row for row in self.logs(logger) if row["phase"] == "request")
            self.assertEqual(request["image_sources"], ["current_message_image", "previous_drawing"])
            self.assertTrue(request["previous_plan_included"])
            payload = core.planning_payload(p.context.llm_generate.await_args.kwargs)
            self.assertEqual(payload["images"], {"current_user_images": 1, "previous_drawing_attached": True})
            self.assertNotIn(str(image), json.dumps(self.logs(logger)))
        asyncio.run(run())

    def test_preparation_exception_gets_logged_and_notified_without_model_or_image_call(self):
        async def run():
            p = self.make_planner()
            p._notify = AsyncMock()
            p._plan_drawing = AsyncMock(side_effect=OSError("private file path"))
            with patch("main.logger.info") as logger:
                await p._quick_draw(core.FakeEvent("/n window"), "window")
            result = self.logs(logger)[-1]
            self.assertEqual(result["phase"], "failed")
            self.assertEqual(result["exception_type"], "OSError")
            self.assertNotIn("private file path", p._notify.await_args.args[1])
            p.context.llm_generate.assert_not_awaited()
            p._call_official.assert_not_awaited()
            p._notify.assert_awaited_once()
        asyncio.run(run())
