import asyncio
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import jsonschema
import diagnostics
import drawing
import drawing_runtime
import planner
import test_core as core
import test_planner as planning
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.tools.message_tools import SendMessageToUserTool


class DrawingContractFixes(unittest.TestCase):
    make_plugin = core.CoreTests.make_plugin
    make_context_plugin = core.CoreTests.make_context_plugin
    make_planner = planning.PlannerTests.make_planner

    def test_both_schemas_require_nonempty_card_id_without_requiring_it_for_other_sources(self):
        schemas = [drawing.SUBJECT_SCHEMA, planner.plan_tools().openai_schema()[0]["function"]["parameters"]["properties"]["subjects"]["items"]]
        for schema in schemas:
            for item in ({"source": "card"}, {"source": "card", "preset_id": ""}, {"source": "card", "preset_id": " "}):
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate(item, schema)
            for item in ({"source": "card", "preset_id": "p1"}, {"source": "self"}, {"source": "custom", "prompt": "hatsune miku"}):
                jsonschema.validate(item, schema)

    def test_first_tool_request_is_required_and_generic_full_body_repairs_only_missing_selector(self):
        async def run():
            plan = {"prompt": "standing, full body", "subjects": [{"source": "card", "outfit": "white dress, shoes", "prompt": "smiling"}]}
            p = self.make_planner(plan)
            p.config["drawing_llm_output_mode"] = "tool"
            p.context.llm_generate.return_value = LLMResponse("tool",
                completion_text='tool call novelai_generate_image with subjects is [{"preset_id":"do-not-use-preamble"}]',
                tools_call_name=["novelai_generate_image"], tools_call_args=[plan])
            event = core.FakeEvent("/n 全身照")
            await p._quick_draw(event, "全身照")
            await core.drain_drawing_tasks(p)
            p.context.llm_generate.assert_awaited_once()
            self.assertEqual(p.context.llm_generate.await_args.kwargs["tool_choice"], "required")
            p._call_official.assert_awaited_once()
            canonical = p._finish_event.await_args.args[0].get_extra("nai_planner_plan")["subjects"][0]
            self.assertEqual(canonical["preset_id"], "p1")
            self.assertEqual(canonical["outfit"], "white dress, shoes")
            self.assertNotIn("preset_id", plan["subjects"][0])
            self.assertIn("white dress", p._call_official.await_args.args[0])
        asyncio.run(run())

    def test_repair_preserves_previous_selected_card_and_never_applies_self_to_previous_custom(self):
        async def run():
            p = self.make_context_plugin()
            p.presets.append({"id": "p2", "name": "second", "positive_prompt": "black hair", "enabled": True})
            p._previous_drawing = Mock(return_value={"plan": {"prompt": "old", "subjects": [{"source": "card", "preset_id": "p2"}]}})
            raw = [{"source": "card", "prompt": "standing"}]
            result = await p._complete_card_selector(core.FakeEvent("画个全身的照片"), raw)
            self.assertEqual(result[0]["preset_id"], "p2")
            self.assertNotIn("preset_id", raw[0])
            result = await p._complete_card_selector(core.FakeEvent("画你自己的全身照"), raw)
            self.assertEqual(result[0]["preset_id"], "p1")
            p._previous_drawing.return_value = {"plan": {"subjects": [{"source": "custom", "prompt": "hatsune miku"}]}}
            self.assertEqual(await p._complete_card_selector(core.FakeEvent("全身照"), raw), raw)
        asyncio.run(run())

    def test_ambiguous_named_other_and_multi_subjects_are_not_repaired(self):
        async def run():
            p = self.make_context_plugin()
            raw = [{"source": "card"}]
            for text in ("画初音全身照", "画一个陌生女孩", "画你和别人合影", "画一张森林风景", "不用角色卡，画全身照"):
                self.assertEqual(await p._complete_card_selector(core.FakeEvent(text), raw), raw)
            self.assertEqual(await p._complete_card_selector(core.FakeEvent("全身照"), raw * 2), raw * 2)
            p.config["default_preset_id"] = ""
            p.presets.append({"id": "p2", "name": "second", "positive_prompt": "black hair"})
            self.assertEqual(await p._complete_card_selector(core.FakeEvent("全身照"), raw), raw)
        asyncio.run(run())

    def test_all_logged_pseudo_tool_variants_are_classified_and_native_args_override_preamble(self):
        for prefix in ("tool call", "tool request", "call tool", "tool"):
            with self.assertRaises(planner.PlanError) as raised:
                planner.parse_plan(prefix + ' novelai_generate_image with prompt is window subjects is [{"source":"card"}]')
            self.assertEqual(raised.exception.code, "pseudo_tool")
        response = LLMResponse("tool", completion_text='tool call novelai_generate_image with subjects is [{"preset_id":"wrong"}]',
            tools_call_name=["novelai_generate_image"], tools_call_args=[{"prompt": "window", "subjects": [{"source": "self"}]}])
        self.assertEqual(planner.response_plan(response)["subjects"][0]["source"], "self")

    def test_unsupported_required_is_actionable_without_silent_protocol_retry(self):
        async def run():
            p = self.make_planner()
            p.config["drawing_llm_output_mode"] = "tool"
            error = RuntimeError("tool_choice required is not supported")
            error.status_code = 400
            p.context.llm_generate.side_effect = error
            with self.assertRaises(planner.PlanError) as raised:
                await p._plan_drawing(core.FakeEvent(), {"prompt": "window"})
            self.assertEqual(raised.exception.code, "tool_choice_unsupported")
            self.assertIn("JSON", str(raised.exception))
            p.context.llm_generate.assert_awaited_once()
            p._call_official.assert_not_awaited()
        asyncio.run(run())

    def test_real_runner_waits_for_delivery_retires_drawing_and_guards_same_batch_placeholder(self):
        from astrbot.core.agent.hooks import BaseAgentRunHooks
        from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
        from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor

        async def run(mode):
            p = self.make_context_plugin()
            event = core.FakeEvent("画一张你的全身照")
            send = SendMessageToUserTool()
            send.call = AsyncMock(return_value="sent")
            image = FunctionTool(name="novelai_generate_image", description="draw", parameters=drawing.TOOL_PARAMETERS, handler=p.novelai_generate_image)
            shared = ToolSet([image, send])
            req = ProviderRequest(prompt=event.message, func_tool=shared)
            await p.drawing_identity_context(event, req)
            original_timeout = 0.12
            ctx = SimpleNamespace(messages=[], context=SimpleNamespace(event=event), tool_call_timeout=original_timeout)
            class Hooks(BaseAgentRunHooks):
                async def on_tool_start(self, context, tool, args):
                    await p.drawing_tool_start(event, tool, args)
            runner = ToolLoopAgentRunner()
            await runner.reset(provider=SimpleNamespace(provider_config={}), request=req, run_context=ctx,
                tool_executor=FunctionToolExecutor(), agent_hooks=Hooks(), tool_schema_mode=mode)
            await p.drawing_agent_begin(event, ctx)
            async def delivery(*args):
                # Cross the old 45/60-second boundary with a scaled timeout.
                await asyncio.sleep(0.16)
                self.assertGreater(ctx.tool_call_timeout, original_timeout)
                return "done"
            p._finish_event.side_effect = delivery
            response = LLMResponse("tool", tools_call_name=["novelai_generate_image", "send_message_to_user"],
                tools_call_args=[{"prompt": "full body", "subjects": [{"source": "self"}]},
                    {"messages": [{"type": "image", "url": "generated_image_url_here"}, {"type": "plain", "text": "caption"}]}],
                tools_call_ids=["draw", "send"])
            with patch.object(p, "_image_tool_budget", return_value=0.5):
                results = [r async for r in runner._handle_function_tools(req, response)]
            blocks = [r.tool_call_result_blocks for r in results if r.kind == "tool_call_result_blocks"][-1]
            text = "\n".join(str(b.content) for b in blocks)
            self.assertIn('"status": "completed"', text)
            self.assertIn("IMAGE_DELIVERY_HANDLED", text)
            self.assertNotIn("accepted_background", text)
            p._call_official.assert_awaited_once()
            p._finish_event.assert_awaited_once()
            send.call.assert_not_awaited()
            self.assertEqual(ctx.tool_call_timeout, original_timeout)
            self.assertNotIn("novelai_generate_image", req.func_tool.names())
            self.assertIn("novelai_generate_image", shared.names())
            self.assertIs(shared.get_tool("send_message_to_user"), send)
        for mode in ("full", "skills_like"):
            with self.subTest(mode=mode):
                asyncio.run(run(mode))

    def test_timeout_restore_on_invalid_args_and_cancellation(self):
        async def run(cancel):
            p = self.make_context_plugin()
            e = core.FakeEvent()
            ctx = SimpleNamespace(tool_call_timeout=60)
            e.set_extra("nai_agent_run_context", ctx)
            tool = SimpleNamespace(name="novelai_generate_image")
            await p.drawing_tool_start(e, tool, {})
            self.assertGreater(ctx.tool_call_timeout, 60)
            if cancel:
                started = asyncio.Event()
                async def image(*args, **kwargs):
                    started.set()
                    await asyncio.Event().wait()
                p._call_official.side_effect = image
                task = asyncio.create_task(p.novelai_generate_image(e, "window"))
                await started.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertFalse(p.lock.locked())
            else:
                receipt = await p.novelai_generate_image(e, "window", subjects=[{"source": "wrong"}])
                self.assertIn("IMAGE_TOOL_ARGUMENT_ERROR", receipt)
            self.assertEqual(ctx.tool_call_timeout, 60)
        asyncio.run(run(False))
        asyncio.run(run(True))

    def test_delivery_guard_preserves_other_media_other_sessions_and_other_turns(self):
        async def run():
            e = core.FakeEvent()
            original = SendMessageToUserTool()
            original.call = AsyncMock(return_value="sent")
            shared = ToolSet([original])
            request = ProviderRequest(func_tool=shared)
            drawing_runtime.isolate_tools(request)
            drawing_runtime.guard_delivery(e, request.func_tool)
            guarded = request.func_tool.get_tool(original.name)
            ctx = SimpleNamespace(context=SimpleNamespace(event=e))
            picture = {"type": "image", "url": "https://example.com/p.png"}
            await guarded.call(ctx, messages=[picture])  # no drawing accepted
            e.set_extra("nai_image_status", "completed")
            await guarded.call(ctx, messages=[picture], session="other:session")
            await guarded.call(ctx, messages=[{"type": "file", "path": "file.txt"}])
            await guarded.call(ctx, messages=[picture, {"type": "file", "path": "file.txt"}])
            self.assertEqual(original.call.await_count, 4)
            self.assertEqual(original.call.await_args.kwargs["messages"], [{"type": "file", "path": "file.txt"}])
            self.assertIs(shared.get_tool(original.name), original)
        asyncio.run(run())

    def test_wait_console_does_not_repeat_http_payload_or_count_as_request(self):
        detail = {"phase": "provider_wait", "request_id": "r1", "attempt": 1, "elapsed_ms": 30000,
            "http_metrics": {"http_attempts": 1, "body_bytes": 0, "wire": {"request_bytes": 7000}, "network_events": ["details"]},
            "snapshots": {"request": {}}, "timeline": ["history"]}
        original = copy.deepcopy(detail)
        title, level, row = diagnostics.console_entry(detail)
        self.assertEqual(title, "绘图等待进度")
        self.assertEqual(level, "info")
        self.assertEqual(row["http_attempts"], 1)
        self.assertFalse(row["sends_request"])
        for key in ("wire", "network_events", "snapshots", "timeline", "http_metrics"):
            self.assertNotIn(key, row)
        self.assertEqual(detail, original)
        self.assertEqual(diagnostics.console_entry({"phase": "http_first_body"})[1], "debug")
