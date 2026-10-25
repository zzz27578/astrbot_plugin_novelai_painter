import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import planner
import test_core as core
import test_planner as planning_tests
from main import DrawingMarkerFilter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.core.message.message_event_result import MessageEventResult, ResultContentType


class DrawingRoutesTests(unittest.TestCase):
    make_plugin = core.CoreTests.make_plugin
    make_context_plugin = core.CoreTests.make_context_plugin
    make_planner = planning_tests.PlannerTests.make_planner
    drawing_event = core.CoreTests.drawing_event
    persist_turn = core.CoreTests.persist_turn
    drawing_context = core.CoreTests.drawing_context
    complete_reply = planning_tests.PlannerTests.complete_reply

    def test_marker_boundaries_customization_and_english_words(self):
        for text, expected in (("n", ""), ("N画窗前的你", "画窗前的你"), ("画窗前的你n", "画窗前的你"),
                ("n\n换白裙", "换白裙"), ("换白裙 n", "换白裙")):
            self.assertEqual(planner.marker_request(text, "n"), expected)
        for text in ("night", "sun", "python", "https://example.org/n", "/n window", "plain text", "n_model"):
            self.assertIsNone(planner.marker_request(text, "n"))
        self.assertEqual(planner.marker_request("画窗前的你出图", "出图"), "画窗前的你")
        self.assertIsNone(planner.marker_request("n", ""))

    def test_all_entry_switches_allow_their_documented_routes(self):
        p = self.make_plugin()
        for mode, command, tool in (("command_only", True, False), ("llm_tool_only", False, True),
                ("both", True, True), ("disabled", False, False)):
            p.config["invoke_mode"] = mode
            self.assertEqual((p._mode_allows("command"), p._mode_allows("llm_tool")), (command, tool))

    def test_marker_queues_independently_and_main_reply_still_goes_through_real_stage(self):
        from astrbot.core.pipeline.process_stage.stage import ProcessStage
        async def run():
            p = self.make_planner(); p.config["invoke_mode"] = "command_only"
            event = self.drawing_event("换白裙坐到窗前n", "dual")
            event.call_llm = False  # AstrBot default means no plugin has handled LLM.
            event._has_send_oper = False
            await p.drawing_marker_event(event)
            self.assertFalse(event.stopped)
            self.assertFalse(event.call_llm)
            p.context.llm_generate.assert_not_awaited()
            called = []
            async def no_handlers(e):
                if False: yield
            async def main_chat(e):
                called.append(e)
                if False: yield
            event.set_extra("activated_handlers", ["marker"])
            stage = ProcessStage()
            stage.ctx = SimpleNamespace(astrbot_config={"provider_settings":{"enable":True}})
            stage.star_request_sub_stage = SimpleNamespace(process=no_handlers)
            stage.agent_sub_stage = SimpleNamespace(process=main_chat)
            async for _ in stage.process(event): pass
            self.assertEqual(called, [event])
            req = ProviderRequest(prompt="prefix: " + event.message, system_prompt="Persona", contexts=[
                {"role":"user","content":"我们正在房间里。"}])
            await p.drawing_identity_context(event, req)
            self.assertEqual(req.prompt, "prefix: 换白裙坐到窗前")
            self.assertEqual(req.system_prompt, "Persona")
            result = await p.novelai_generate_image(event,"not needed")
            self.assertIn("DUPLICATE", result)
            await self.complete_reply(p,event,"我换好白裙，在窗前坐下。")
            p.context.llm_generate.assert_awaited_once()
            p._call_official.assert_awaited_once()
            payload = json.loads(p.context.llm_generate.await_args.kwargs["prompt"])
            self.assertEqual(payload["user_request"], event.message)
            self.assertEqual(payload["drawing_request"], "换白裙坐到窗前")
            self.assertIn("白裙",payload["current_reply"])
            self.assertEqual(payload["recent_dialogue"][0]["text"], "我们正在房间里。")
        asyncio.run(run())

    def test_bare_marker_uses_current_scene_and_stream_finish_only_generates_once(self):
        async def run():
            p = self.make_planner(); event = core.FakeEvent("n")
            await p.drawing_marker_event(event)
            await p.drawing_identity_context(event,ProviderRequest(prompt="n"))
            await p.drawing_capture_reply(event,LLMResponse("assistant",completion_text="我在窗边向你微笑。"))
            event.result = MessageEventResult().message("reply").set_result_content_type(ResultContentType.STREAMING_FINISH)
            await p.drawing_stream_finished(event)
            await p.drawing_after_reply(event)
            p.context.llm_generate.assert_awaited_once()
            self.assertIn("当前场景", json.loads(p.context.llm_generate.await_args.kwargs["prompt"])["drawing_request"])
        asyncio.run(run())

    def test_dynamic_filter_only_wakes_configured_markers(self):
        p = self.make_planner(); p.config["drawing_trigger"] = "出图"
        matcher = DrawingMarkerFilter(); matcher.plugin = p
        self.assertTrue(matcher.filter(core.FakeEvent("画窗边的你出图"), {}))
        self.assertFalse(matcher.filter(core.FakeEvent("n"), {}))
        p.config["drawing_trigger"] = ""
        self.assertFalse(matcher.filter(core.FakeEvent("出图"), {}))

    def test_n_without_description_uses_current_history_and_does_not_chat(self):
        async def run():
            p = self.make_planner()
            p.context.conversation_manager.get_conversation.return_value.history = json.dumps([
                {"role":"user","content":"我们在窗前。"},{"role":"assistant","content":"我穿白裙坐下。"}])
            event = core.FakeEvent("/n")
            async for _ in p.cmd_draw(event): pass
            self.assertFalse(event.call_llm); self.assertTrue(event.stopped)
            payload = json.loads(p.context.llm_generate.await_args.kwargs["prompt"])
            self.assertEqual(payload["current_reply"], "")
            self.assertIn("白裙", str(payload["recent_dialogue"]))
            p._call_official.assert_awaited_once()
        asyncio.run(run())

    def test_n_requires_drawing_llm_but_nai_never_calls_it(self):
        async def run():
            p = self.make_planner()
            async for _ in p.cmd_draw(core.FakeEvent("/nai white dress, garden")): pass
            p.context.llm_generate.assert_not_awaited()
            p._call_official.assert_awaited_once()
            p.config["drawing_llm_provider_id"] = ""; p._notify = AsyncMock()
            event = self.drawing_event("/n 画窗前的你", "missing")
            async for _ in p.cmd_draw(event): pass
            p._notify.assert_awaited_once()
            self.assertEqual(p._call_official.await_count,1)
            self.assertFalse(event.call_llm)
        asyncio.run(run())

    def test_primary_tool_draws_directly_even_with_independent_provider_configured(self):
        async def run():
            p = self.make_planner()
            event = core.FakeEvent("画一张窗前的你，换白裙")
            req = ProviderRequest(prompt=event.message)
            await p.drawing_identity_context(event,req)
            context = self.drawing_context(req)
            self.assertIn("default_outfit",context)
            self.assertNotIn("简短绘图意图",context)
            result = await p.novelai_generate_image(event,"window, sunset",subjects=[{"source":"self","outfit":"white dress"}])
            self.assertIn("FINAL_IMAGE_TOOL_RESULT",result)
            self.assertIsNone(event.get_extra("nai_planner_pending"))
            self.assertIn("white dress",p._call_official.await_args.args[0])
            self.assertIn("window, sunset",p._call_official.await_args.args[0])
            p._call_official.assert_awaited_once()
            await self.complete_reply(p,event)
            p.context.llm_generate.assert_not_awaited()
        asyncio.run(run())

    def test_primary_tool_still_corrects_its_own_parameters_without_drawing_llm(self):
        async def run():
            p = self.make_planner(); event = core.FakeEvent("画窗边的你")
            result = await p.novelai_generate_image(event,"window",subjects=[{"source":"card","preset_id":"missing"}])
            self.assertIn("IMAGE_TOOL_ARGUMENT_ERROR",result)
            p._call_official.assert_not_awaited()
            result = await p.novelai_generate_image(event,"window",subjects=[{"source":"self"}])
            self.assertIn("FINAL_IMAGE_TOOL_RESULT",result)
            p._call_official.assert_awaited_once()
            p.context.llm_generate.assert_not_awaited()
        asyncio.run(run())

    def test_primary_drawing_can_be_used_by_later_independent_edit_without_delegation(self):
        async def run():
            p = self.make_planner(); del p._finish_event
            image=p.temp_dir/"image.png"; image.write_bytes(b"image")
            p._call_official.return_value=str(image)
            event=self.drawing_event("画你穿白裙坐窗前","primary")
            await p.novelai_generate_image(event,"window",subjects=[{"source":"self","outfit":"white dress","prompt":"sitting"}])
            p.context.llm_generate.assert_not_awaited()
            self.persist_turn(p,event)
            await p._quick_draw(self.drawing_event("/n 改上一张的视角，其他不变","edit"),"改上一张的视角，其他不变")
            payload=json.loads(p.context.llm_generate.await_args.kwargs["prompt"])
            self.assertEqual(payload["previous_drawing"]["subjects"][0]["outfit"],"white dress")
            self.assertEqual(payload["previous_user_request"],event.message)
        asyncio.run(run())

    def test_text_tool_wrappers_and_redundant_defaults_are_normalized_without_retry(self):
        async def run(value):
            p = self.make_planner(); p.context.llm_generate.return_value = LLMResponse("assistant",completion_text=json.dumps(value))
            await p._quick_draw(core.FakeEvent("/n 换白裙"),"换白裙")
            p.context.llm_generate.assert_awaited_once(); p._call_official.assert_awaited_once()
        plan={"prompt":"window", "subjects":'[{"source":"self","outfit":"white dress"}]',
            "operation":"generate","reference_type":""}
        for value in (plan,{"name":"submit_drawing_plan","arguments":plan},
                {"type":"function","function":{"name":"submit_drawing_plan","arguments":json.dumps(plan)}}):
            asyncio.run(run(value))
        for bad in ({**plan,"operation":"img2img"},{**plan,"extra":"unknown"},
                {"name":"other_tool","arguments":plan}, {"name":[],"arguments":plan}):
            with self.assertRaises(ValueError): planner.parse_plan(json.dumps(bad))

    def test_format_retry_uses_remaining_budget_and_logs_request_start_before_provider_call(self):
        async def run(first_seconds, expected):
            p = self.make_planner(); p._notify = AsyncMock()
            good = p.context.llm_generate.return_value
            clock = [0]
            timeouts=[]
            real_wait_for=asyncio.wait_for
            async def wait_for(awaitable, timeout):
                timeouts.append(timeout)
                return await real_wait_for(awaitable,timeout)
            async def generate(**kwargs):
                self.assertEqual(p._planner_diagnostics[core.FakeEvent.unified_msg_origin]["phase"],"request")
                if len(timeouts)==1:
                    clock[0]=first_seconds
                    return LLMResponse("assistant",completion_text='{"prompt":"window","subjects":{}}')
                clock[0]=59
                return good
            p.context.llm_generate.side_effect=generate
            with patch("main.time.perf_counter",side_effect=lambda:clock[0]),patch("main.asyncio.wait_for",side_effect=wait_for):
                await p._quick_draw(core.FakeEvent("/n 换白裙"),"换白裙")
            self.assertEqual(p.context.llm_generate.await_count,expected)
            self.assertEqual(timeouts[0],60)
            if expected==2:
                self.assertEqual(timeouts[1],60-first_seconds)
                p._call_official.assert_awaited_once()
            else: p._call_official.assert_not_awaited()
        asyncio.run(run(53,2)); asyncio.run(run(59,1))

    def test_direct_drawing_receipt_and_request_are_visible_to_next_main_and_drawing_calls(self):
        async def run():
            p = self.make_planner(); del p._finish_event
            image = p.temp_dir / "image.png"; image.write_bytes(b"image")
            p._call_official.return_value = str(image)
            event = self.drawing_event("/n 只在这张图换白裙", "direct")
            async for _ in p.cmd_draw(event): pass
            next_event = self.drawing_event("画上一张的侧面，其他不变n", "next")
            req = ProviderRequest(prompt=next_event.message)
            await p.drawing_marker_event(next_event)
            await p.drawing_identity_context(next_event,req)
            catalog = self.drawing_context(req)
            self.assertIn("只在这张图换白裙",catalog)
            self.assertNotIn("window, sunlight",catalog) # raw generation Tags stay out of main context
            await self.complete_reply(p,next_event)
            payload = json.loads(p.context.llm_generate.await_args.kwargs["prompt"])
            self.assertEqual(payload["previous_user_request"],event.message)
            self.assertIn("white dress", str(payload["previous_drawing"]))
            reset = core.FakeEvent(); reset.set_extra("_clean_group_context_session",True)
            await p.drawing_context_lifecycle(reset)
            req2 = ProviderRequest(prompt="继续")
            await p.drawing_identity_context(self.drawing_event("继续","after-reset"),req2)
            self.assertNotIn("只在这张图换白裙",self.drawing_context(req2))
        asyncio.run(run())

    def test_openai_protocol_submits_internal_parameters_without_global_tool_registration(self):
        from astrbot.core.provider.register import llm_tools
        async def run():
            p = self.make_planner()
            p.context.get_provider_by_id = Mock(return_value=SimpleNamespace(provider_config={"type":"openai_chat_completion"}))
            p.context.llm_generate.return_value = LLMResponse("tool",tools_call_name=["submit_drawing_plan"],
                tools_call_args=[{"prompt":"window, sunlight","subjects":[{"source":"self","outfit":"white dress"}]}])
            p.context.llm_generate.side_effect = [LLMResponse("tool",completion_text="提交方案。",
                tools_call_name=["submit_drawing_plan"],tools_call_args=[{"prompt":"window", "subjects":[{"source":"card","preset_id":"missing"}]}]),
                p.context.llm_generate.return_value]
            before = [tool.name for tool in llm_tools.func_list]
            await p._quick_draw(core.FakeEvent("/n 换白裙"),"换白裙")
            kwargs = p.context.llm_generate.await_args.kwargs
            self.assertEqual(kwargs["tools"].names(), ["submit_drawing_plan"])
            self.assertEqual(kwargs["tool_choice"],"required")
            self.assertEqual(kwargs["request_max_retries"],1)
            self.assertEqual([tool.name for tool in llm_tools.func_list],before)
            self.assertIn("missing", json.loads(kwargs["prompt"])["format_correction"]["previous_output"])
            self.assertIn("提交方案", json.loads(kwargs["prompt"])["format_correction"]["previous_output"])
            p._call_official.assert_awaited_once()
        asyncio.run(run())

    def test_wrapped_json_is_cleaned_without_an_extra_llm_call(self):
        async def run():
            p = self.make_planner()
            raw = p.context.llm_generate.return_value.completion_text
            p.context.llm_generate.return_value = LLMResponse("assistant",completion_text="<think>Consider composition.</think>\n方案如下：\n```json\n"+raw+"\n```\n完成。")
            await p._quick_draw(core.FakeEvent("/n 画窗边"),"画窗边")
            p.context.llm_generate.assert_awaited_once()
            p._call_official.assert_awaited_once()
        asyncio.run(run())

    def test_format_and_subject_errors_are_sent_back_once_before_any_image_request(self):
        async def run(bad):
            p = self.make_planner()
            good = p.context.llm_generate.return_value
            calls = []
            async def generate(**kwargs):
                p._call_official.assert_not_awaited()
                calls.append(json.loads(kwargs["prompt"]))
                return LLMResponse("assistant",completion_text=bad) if len(calls)==1 else good
            p.context.llm_generate.side_effect = generate
            await p._quick_draw(core.FakeEvent("/n 换白裙"),"换白裙")
            self.assertEqual(len(calls),2)
            self.assertIn("format_correction",calls[1])
            self.assertEqual(calls[1]["format_correction"]["previous_output"],bad)
            self.assertEqual(calls[0]["user_request"],calls[1]["user_request"])
            p._call_official.assert_awaited_once()
        for bad in ('{"prompt":"room","subjects":[', '{"prompt":"room","subjects":[{"source":"card","preset_id":"missing"}]}',
                '{"prompt":"room","subjects":[{"source":"self","prompt":"{smiling"}]}'):
            asyncio.run(run(bad))

    def test_refusal_is_not_retried_and_repeated_format_errors_never_reach_image_service(self):
        async def run(text, expected):
            p = self.make_planner(); p._notify = AsyncMock()
            p.context.llm_generate.return_value = LLMResponse("assistant",completion_text=text)
            await p._quick_draw(core.FakeEvent("/n 画窗边"),"画窗边")
            self.assertEqual(p.context.llm_generate.await_count,expected)
            p._call_official.assert_not_awaited(); p._notify.assert_awaited_once()
        asyncio.run(run('我无法处理该请求。',1))
        asyncio.run(run('window, sunlight',2))

    def test_truncation_and_empty_output_have_distinct_diagnostics(self):
        for response,code in ((LLMResponse("assistant",completion_text="",raw_completion={"choices":[{"finish_reason":"length"}]}),"truncated"),
                (LLMResponse("assistant",completion_text=""),"empty")):
            with self.assertRaises(planner.PlanError) as caught: planner.response_plan(response)
            self.assertEqual(caught.exception.code,code)
        with self.assertRaises(planner.PlanError): planner.parse_plan('[{"prompt":"room","subjects":[]}]')

    def test_debug_preview_is_opt_in_bounded_and_redacts_keys(self):
        async def run(enabled):
            p = self.make_planner(); p.config.update(drawing_llm_debug=enabled,api_key="secret-key-value")
            event = core.FakeEvent()
            with patch("main.logger.info"):
                p._planner_log(event,"drawing-provider","json",1,LLMResponse("assistant",completion_text="secret-key-value"+"x"*3000),
                    planner.PlanError("HTTP 400: secret-key-value Bearer test-token", "provider", False))
            detail = p._planner_diagnostics[event.unified_msg_origin]
            self.assertEqual("response_preview" in detail,enabled)
            self.assertNotIn("secret-key-value",json.dumps(detail))
            if enabled: self.assertLessEqual(len(detail["response_preview"]),2000)
        asyncio.run(run(False)); asyncio.run(run(True))

    def test_real_openai_provider_serializes_internal_tool_and_reads_native_tool_arguments(self):
        from openai.types.chat import ChatCompletion
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
        async def run():
            provider = object.__new__(ProviderOpenAIOfficial)
            provider.provider_config = {}
            provider.reasoning_key = "reasoning_content"
            provider.default_params = {"model","messages","tools","tool_choice"}
            provider._apply_provider_specific_request_overrides = Mock()
            plan = {"prompt":"window, sunlight","subjects":[{"source":"self"}]}
            completion = ChatCompletion.model_validate({"id":"mock","created":0,"model":"grok-model",
                "object":"chat.completion","choices":[{"index":0,"finish_reason":"tool_calls","message":{
                    "role":"assistant","content":None,"tool_calls":[{"id":"call1","type":"function",
                        "function":{"name":"submit_drawing_plan","arguments":json.dumps(plan)}}]}}]})
            create = AsyncMock(return_value=completion)
            provider.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
            provider.api_keys=["mock-test-key"]
            provider._prepare_chat_payload=AsyncMock(return_value=({"model":"grok-model","messages":[{"role":"user","content":"draw"}]},[]))
            response = await provider.text_chat(prompt="draw",func_tool=planner.plan_tools(),tool_choice="required",request_max_retries=1)
            schema = create.await_args.kwargs["tools"][0]["function"]
            self.assertEqual(schema["name"],"submit_drawing_plan")
            self.assertIn("subjects",schema["parameters"]["properties"])
            self.assertEqual(create.await_args.kwargs["tool_choice"],"required")
            self.assertEqual(planner.response_plan(response)["prompt"],plan["prompt"])
        asyncio.run(run())

    def test_marker_filter_binding_and_unloading_do_not_wake_unrelated_messages(self):
        from astrbot.core.star.star_handler import star_handlers_registry
        p = self.make_planner(); p._sync_marker_filter()
        handler = star_handlers_registry.get_handler_by_full_name("main_drawing_marker_event")
        matcher = next(item for item in handler.event_filters if isinstance(item, DrawingMarkerFilter))
        self.assertTrue(matcher.filter(core.FakeEvent("n"),{}))
        self.assertFalse(matcher.filter(core.FakeEvent("普通聊天"),{}))
        p._sync_marker_filter(unbind=True)
        self.assertFalse(matcher.filter(core.FakeEvent("n"),{}))

    def test_reset_between_bad_output_and_format_retry_cancels_second_call(self):
        async def run():
            p = self.make_planner(); p._notify = AsyncMock()
            async def bad_after_reset(**kwargs):
                event = core.FakeEvent(); event.set_extra("_clean_group_context_session",True)
                await p.drawing_context_lifecycle(event)
                return LLMResponse("assistant",completion_text="window, sunlight")
            p.context.llm_generate.side_effect = bad_after_reset
            await p._quick_draw(core.FakeEvent("/n 画窗边"),"画窗边")
            p.context.llm_generate.assert_awaited_once()
            p._call_official.assert_not_awaited()
            p._notify.assert_awaited_once()
        asyncio.run(run())
