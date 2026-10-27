import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import planner
import test_core as core
import test_planner as planning
from astrbot.api.provider import LLMResponse


class ImageLifecycleTests(unittest.TestCase):
    make_plugin = core.CoreTests.make_plugin
    make_context_plugin = core.CoreTests.make_context_plugin
    make_planner = planning.PlannerTests.make_planner

    def test_self_with_matching_id_is_lossless_and_does_not_replan(self):
        async def run():
            p = self.make_planner({"prompt": "window", "subjects": [
                {"source": "self", "preset_id": "p1", "outfit": "bunny suit, heels"}]})
            await p._quick_draw(core.FakeEvent("/n 换兔女郎服装"), "换兔女郎服装")
            p.context.llm_generate.assert_awaited_once()
            p._call_official.assert_awaited_once()
            prompt = p._call_official.await_args.args[0]
            self.assertIn("bunny suit", prompt)
            self.assertNotIn("black bodysuit", prompt)
            self.assertIn("blue eyes", prompt)
            self.assertEqual(p.presets[0]["positive_prompt"],
                "watercolor, blue eyes, blonde hair, side ponytail, black bodysuit")
        asyncio.run(run())

    def test_self_cannot_select_unbound_or_other_card(self):
        async def run():
            p = self.make_context_plugin()
            for identifier in ("other", "missing"):
                with self.assertRaisesRegex(ValueError, "与当前绑定卡不符"):
                    await p._resolve_drawing_subjects(core.FakeEvent(), [{"source": "self", "preset_id": identifier}])
            p.config["default_preset_id"] = ""
            with self.assertRaises(ValueError):
                await p._resolve_drawing_subjects(core.FakeEvent(), [{"source": "self", "preset_id": "p1"}])
        asyncio.run(run())

    def test_missing_subject_selection_for_clear_self_request_applies_bound_card(self):
        async def run():
            p = self.make_context_plugin()
            e = core.FakeEvent("宝宝，来一张你在床上看书的图片，画图")
            result = await p.novelai_generate_image(e, "reading in bed", subjects=[])
            self.assertIn('"status": "completed"', result)
            self.assertIn("blonde hair", p._call_official.await_args.args[0])
            self.assertEqual(e.get_extra("nai_drawing_details")[0]["preset_id"], "p1")
        asyncio.run(run())

    def test_ambiguous_other_people_and_negations_do_not_inherit_self(self):
        async def run():
            p = self.make_context_plugin()
            for request in ("画一张你的房间", "画你和初音一起看书", "不要画你，画兔子", "来一张你在画的风景", "画初音在床上看书"):
                self.assertEqual(await p._empty_subject_fallback(core.FakeEvent(request)), [], request)
        asyncio.run(run())

    def test_long_image_survives_core_timeout_without_extra_request(self):
        async def run():
            p = self.make_context_plugin()
            gate = asyncio.Event()
            async def image(*args, **kwargs):
                await gate.wait()
                return "image.png"
            p._call_official.side_effect = image
            e = core.FakeEvent()
            with patch("main.IMAGE_TOOL_INLINE_SECONDS", 0.01):
                receipt = await asyncio.wait_for(p.novelai_generate_image(e, "window"), timeout=0.1)
            self.assertIn("DRAWING_IN_PROGRESS", receipt)
            self.assertEqual(e.get_extra("nai_image_status"), "running")
            p._finish_event.assert_not_awaited()
            duplicate = await p.novelai_generate_image(e, "window again")
            self.assertIn("status=running", duplicate)
            tasks = list(p._image_tasks)
            gate.set()
            await asyncio.gather(*tasks)
            p._call_official.assert_awaited_once()
            p._finish_event.assert_awaited_once()
            self.assertEqual(e.get_extra("nai_image_status"), "completed")
            self.assertFalse(p._image_tasks)
        asyncio.run(run())

    def test_503_retry_can_complete_after_tool_receipt(self):
        async def run():
            p = self.make_context_plugin()
            p.config["retry_503_count"] = 1
            gate = asyncio.Event()
            async def retry_wait(delay):
                self.assertFalse(p.lock.locked())
                await gate.wait()
            p._call_official.side_effect = [p._http_error(503, b""), "image.png"]
            e = core.FakeEvent()
            with patch("main.IMAGE_TOOL_INLINE_SECONDS", 0.01), patch("main.asyncio.sleep", side_effect=retry_wait):
                receipt = await asyncio.wait_for(p.novelai_generate_image(e, "window"), 0.1)
                self.assertIn("DRAWING_IN_PROGRESS", receipt)
                tasks = list(p._image_tasks)
                gate.set()
                await asyncio.gather(*tasks)
            self.assertEqual(p._call_official.await_count, 2)
            p._finish_event.assert_awaited_once()
            self.assertEqual(p._jobs[-1]["attempts"], 2)
            self.assertEqual(e.get_extra("nai_image_status"), "completed")
        asyncio.run(run())

    def test_caller_cancellation_cleans_state_lock_and_does_not_claim_sending(self):
        async def run():
            p = self.make_context_plugin()
            started = asyncio.Event()
            async def image(*args, **kwargs):
                started.set()
                await asyncio.Event().wait()
            p._call_official.side_effect = image
            e = core.FakeEvent()
            task = asyncio.create_task(p.novelai_generate_image(e, "window"))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(e.get_extra("nai_image_status"), "cancelled")
            self.assertFalse(p.lock.locked())
            self.assertFalse(p._inflight_jobs)
            self.assertFalse(p._image_tasks)
            self.assertEqual(p._jobs[-1]["error_code"], "cancelled")
            p._finish_event.assert_not_awaited()
            duplicate = await p.novelai_generate_image(e, "room")
            self.assertIn("status=cancelled", duplicate)
            self.assertIn("不会继续发送", duplicate)
            p._call_official.assert_awaited_once()
        asyncio.run(run())

    def test_unload_cancels_deferred_image_and_finishes_its_state(self):
        async def run():
            p = self.make_context_plugin()
            async def image(*args, **kwargs):
                await asyncio.Event().wait()
            p._call_official.side_effect = image
            e = core.FakeEvent()
            with patch("main.IMAGE_TOOL_INLINE_SECONDS", 0.01):
                await p.novelai_generate_image(e, "window")
            await p.terminate()
            self.assertEqual(e.get_extra("nai_image_status"), "cancelled")
            self.assertFalse(p.lock.locked())
            self.assertFalse(p._image_tasks)
        asyncio.run(run())

    def test_actual_core_executor_receives_queued_receipt_before_its_deadline(self):
        from astrbot.core.agent.tool import FunctionTool
        from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
        async def run():
            p = self.make_context_plugin()
            gate = asyncio.Event()
            async def image(*args, **kwargs):
                await gate.wait()
                return "image.png"
            p._call_official.side_effect = image
            e = core.FakeEvent()
            wrapper = SimpleNamespace(context=SimpleNamespace(event=e), tool_call_timeout=0.15)
            p.context.get_config = Mock(return_value={"provider_settings": {"tool_call_timeout": 0.15}})
            tool = FunctionTool(name="novelai_generate_image", description="draw", parameters={}, handler=p.novelai_generate_image)
            # Exercise AstrBot's real wait_for(anext(wrapper)) boundary, using
            # a short configured timeout rather than a minute-long test.
            results = [r async for r in FunctionToolExecutor._execute_local(tool, wrapper, prompt="window")]
            self.assertEqual(len(results), 1)
            self.assertIn("DRAWING_IN_PROGRESS", results[0].content[0].text)
            self.assertEqual(e.get_extra("nai_image_status"), "running")
            tasks = list(p._image_tasks)
            gate.set()
            await asyncio.gather(*tasks)
            self.assertEqual(e.get_extra("nai_image_status"), "completed")
            p._call_official.assert_awaited_once()
            p._finish_event.assert_awaited_once()
        asyncio.run(run())

    def test_send_failure_and_backend_exception_have_terminal_status(self):
        async def run():
            p = self.make_context_plugin()
            del p._finish_event
            p._send_generated_image = AsyncMock(side_effect=OSError("send failed"))
            p._notify = AsyncMock()
            e = core.FakeEvent()
            receipt = await p.novelai_generate_image(e, "window")
            self.assertIn('"status": "failed"', receipt)
            self.assertEqual(e.get_extra("nai_image_delivery"), "failed")
            p = self.make_context_plugin()
            p._call_official.side_effect = RuntimeError("broken")
            e = core.FakeEvent()
            with patch("main.logger.exception"):
                receipt = await p.novelai_generate_image(e, "window")
            self.assertIn("status=failed", receipt)
            self.assertEqual(e.get_extra("nai_image_status"), "failed")
            self.assertEqual(p._jobs[-1]["error_code"], "internal_error")
            self.assertFalse(p.lock.locked())
            self.assertFalse(p._inflight_jobs)
        asyncio.run(run())

    def test_previous_image_only_sent_for_edit_but_current_image_always_respected(self):
        async def run(request, expected):
            p = self.make_planner()
            p.config["drawing_llm_use_images"] = True
            image = p.temp_dir / "previous.png"
            image.write_bytes(b"previous-image")
            p._previous_drawing = Mock(return_value={"image": str(image), "plan": {"prompt": "old", "subjects": []}})
            e = core.FakeEvent(request)
            e.set_extra("nai_planner_images", ["current.png"])
            await p._plan_drawing(e, {"prompt": request})
            kwargs = p.context.llm_generate.await_args.kwargs
            self.assertEqual(kwargs["image_urls"], ["current.png", str(image)] if expected else ["current.png"])
            self.assertEqual(core.planning_payload(kwargs)["previous_drawing"]["prompt"], "old")
        asyncio.run(run("画一张森林风景", False))
        asyncio.run(run("上一张改成夜景", True))

    def test_fake_tool_text_gets_one_native_tool_correction_preserving_user_request(self):
        async def run():
            p = self.make_planner()
            p.config["drawing_llm_output_mode"] = "tool"
            good = p.context.llm_generate.return_value
            p.context.llm_generate.side_effect = [LLMResponse("assistant", completion_text=
                '```xml\ncall tool novelai_generate_image with prompt is window subjects is [{"source":"self"}]\n```'), good]
            e = core.FakeEvent("/n 给你换白裙坐窗前")
            await p._quick_draw(e, "给你换白裙坐窗前")
            calls = p.context.llm_generate.await_args_list
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0].kwargs["tool_choice"], "auto")
            self.assertEqual(calls[1].kwargs["tool_choice"], "required")
            payload = core.planning_payload(calls[1].kwargs)
            self.assertEqual(payload["user_request"], e.message)
            self.assertEqual(payload["drawing_request"], "给你换白裙坐窗前")
            self.assertIn("文本模拟工具", payload["format_correction"]["error"])
            p._call_official.assert_awaited_once()
        asyncio.run(run())

    def test_first_tool_latency_is_recorded_once_and_is_not_final_response_usage(self):
        async def run():
            p = self.make_context_plugin()
            p._planner_log = Mock()
            e = core.FakeEvent()
            e.set_extra("nai_main_hook_started", time.perf_counter() - 2)
            await p.novelai_generate_image(e, "window")
            await p.novelai_generate_image(e, "room")
            rows = [c.kwargs for c in p._planner_log.call_args_list if c.kwargs.get("phase") == "main_first_drawing_tool"]
            self.assertEqual(len(rows), 1)
            self.assertGreaterEqual(rows[0]["elapsed_ms"], 2000)
            self.assertIn("not_http", rows[0]["timing_scope"])
            self.assertNotIn("usage", rows[0])
        asyncio.run(run())
