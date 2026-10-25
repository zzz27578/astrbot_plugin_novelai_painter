import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import planner
import test_core as core
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.core.message.components import Image
from astrbot.core.message.message_event_result import MessageEventResult, ResultContentType
from main import GenerationResult


class PlannerTests(unittest.TestCase):
    make_plugin = core.CoreTests.make_plugin
    make_context_plugin = core.CoreTests.make_context_plugin
    drawing_event = core.CoreTests.drawing_event
    persist_turn = core.CoreTests.persist_turn
    drawing_context = core.CoreTests.drawing_context

    def make_planner(self, plan=None):
        plugin = self.make_context_plugin()
        plugin.config.update(planner.DEFAULTS)
        plugin.config["drawing_llm_provider_id"] = "drawing-provider"
        plan = plan or {"prompt":"window, sunlight", "subjects":[{"source":"self", "outfit":"white dress", "prompt":"smiling"}]}
        plugin.context.llm_generate = AsyncMock(return_value=LLMResponse("assistant", completion_text=json.dumps(plan)))
        return plugin

    async def complete_reply(self, plugin, event, text="我坐到窗前，向你微笑。"):
        await plugin.drawing_capture_reply(event, LLMResponse("assistant", completion_text=text))
        await plugin.drawing_after_reply(event)

    def test_queue_does_not_generate_until_main_reply_then_receives_user_original(self):
        async def run():
            p = self.make_planner()
            p.config["drawing_llm_context_messages"] = 2
            p.presets[0]["positive_prompt"] = "{{elf (isekai ojisan)}}, green eyes, black bodysuit, {atelier pontdarc}"
            event = self.drawing_event("把裙子换成白色，坐到窗前，保持耳朵不变", "queued")
            request = ProviderRequest(prompt=event.message, system_prompt="Persona", contexts=[
                {"role":"system", "content":"Do not share this persona"},
                {"role":"user", "content":"Earlier request"}, {"role":"tool", "content":"Old tool payload"},
                {"role":"assistant", "content":"我刚走进房间。"}])
            await p.drawing_identity_context(event, request)
            queued = await p.novelai_generate_image(event, "画现在的你")
            self.assertIn("DRAWING_REQUEST_QUEUED", queued)
            p.context.llm_generate.assert_not_awaited()
            p._call_official.assert_not_awaited()
            await p.drawing_after_reply(event)
            p.context.llm_generate.assert_not_awaited()
            await self.complete_reply(p, event)
            payload = json.loads(p.context.llm_generate.await_args.kwargs["prompt"])
            self.assertEqual(payload["user_request"], event.message)
            self.assertEqual(payload["drawing_intent"]["prompt"], "画现在的你")
            self.assertIn("坐到窗前", payload["current_reply"])
            self.assertEqual(len(payload["recent_dialogue"]), 2)
            self.assertNotIn("Old tool payload", str(payload))
            self.assertNotIn("Do not share this persona", str(payload))
            self.assertEqual(p.context.llm_generate.await_args.kwargs["chat_provider_id"], "drawing-provider")
            self.assertNotIn("tools", p.context.llm_generate.await_args.kwargs)
            self.assertIn("{{elf (isekai ojisan)}}", p._call_official.await_args.args[0])
            self.assertIn("{atelier pontdarc}", p._call_official.await_args.args[0])
            self.assertIn("white dress", p._call_official.await_args.args[0])
            self.assertNotIn("black bodysuit", p._call_official.await_args.args[0])
            self.assertFalse(p._ledger().states)
            self.assertEqual(request.contexts[0]["content"], "Do not share this persona")
        asyncio.run(run())

    def test_duplicate_queue_and_repeated_after_send_only_call_one_planner_and_renderer(self):
        async def run():
            p = self.make_planner()
            event = core.FakeEvent("画一张你")
            results = await asyncio.gather(p.novelai_generate_image(event,"画你"), p.novelai_generate_image(event,"再画你"))
            self.assertEqual(sum("DRAWING_REQUEST_QUEUED" in result for result in results), 1)
            self.assertEqual(sum("DUPLICATE" in result for result in results), 1)
            await p.drawing_capture_reply(event, LLMResponse("assistant", completion_text="好。"))
            await asyncio.gather(p.drawing_after_reply(event), p.drawing_after_reply(event))
            await p.drawing_after_reply(event)
            p.context.llm_generate.assert_awaited_once()
            p._call_official.assert_awaited_once()
        asyncio.run(run())

    def test_no_tool_request_does_not_automatically_draw(self):
        async def run():
            p = self.make_planner()
            event = core.FakeEvent("今天天气很好")
            await self.complete_reply(p, event)
            p.context.llm_generate.assert_not_awaited()
            p._call_official.assert_not_awaited()
        asyncio.run(run())

    def test_permissions_and_entry_modes_are_checked_before_planning(self):
        async def run():
            for mode, role in (("disabled", "admin"), ("both", "member")):
                p = self.make_planner()
                p.config["invoke_mode"] = mode
                event = core.FakeEvent(); event.role = role
                result = await p.novelai_generate_image(event,"draw")
                self.assertIn("FINAL_IMAGE_TOOL_RESULT", result)
                await self.complete_reply(p,event)
                p.context.llm_generate.assert_not_awaited()
        asyncio.run(run())

    def test_invalid_plan_timeout_or_provider_error_never_falls_back_to_primary_tags(self):
        async def run(response, error=None):
            p = self.make_planner()
            p.context.llm_generate.return_value = LLMResponse("assistant", completion_text=response)
            p.context.llm_generate.side_effect = error
            p._notify = AsyncMock()
            event = core.FakeEvent()
            await p.novelai_generate_image(event,"primary tags, portrait")
            await self.complete_reply(p,event)
            p.context.llm_generate.assert_awaited_once()
            p._call_official.assert_not_awaited()
            p._notify.assert_awaited_once()
            await p.drawing_after_reply(event)
            p.context.llm_generate.assert_awaited_once()
        for response,error in (("I cannot process this request",None), ('{"prompt":"room","subjects":"[]"}',None),
            ('{"prompt":"room","subjects":[{"source":"card","preset_id":"missing"}]}',None),
            ("",asyncio.TimeoutError()), ("",RuntimeError("service failure"))):
            with self.subTest(response=response,error=error): asyncio.run(run(response,error))

    def test_reset_while_queued_or_during_planning_cancels_render(self):
        async def run(during):
            p = self.make_planner(); p._notify = AsyncMock()
            event = core.FakeEvent()
            await p.novelai_generate_image(event,"draw")
            async def reset_then_return(**kwargs):
                reset = core.FakeEvent(); reset.set_extra("_clean_group_context_session", True)
                await p.drawing_context_lifecycle(reset)
                return LLMResponse("assistant", completion_text='{"prompt":"room","subjects":[{"source":"self"}]}')
            if during:
                p.context.llm_generate.side_effect = reset_then_return
            else:
                await reset_then_return()
            await self.complete_reply(p,event)
            p._call_official.assert_not_awaited()
            p._notify.assert_awaited_once()
            self.assertEqual(p.context.llm_generate.await_count, int(during))
        asyncio.run(run(False)); asyncio.run(run(True))

    def test_n_uses_planner_for_original_chinese_request_without_main_model_reply(self):
        async def run():
            p = self.make_planner()
            event = core.FakeEvent("/n 换白裙坐到窗前")
            replies = [reply async for reply in p.cmd_draw(event)]
            self.assertEqual(replies, [])
            self.assertFalse(event.call_llm)
            self.assertTrue(event.stopped)
            payload = json.loads(p.context.llm_generate.await_args.kwargs["prompt"])
            self.assertEqual(payload["user_request"], "/n 换白裙坐到窗前")
            self.assertEqual(payload["drawing_intent"]["prompt"], "换白裙坐到窗前")
            self.assertEqual(payload["current_reply"], "")
            self.assertIn("white dress", p._call_official.await_args.args[0])
        asyncio.run(run())

    def test_current_images_require_opt_in_and_zero_context_does_not_remove_original(self):
        async def run(enabled):
            p = self.make_planner()
            p.config.update(drawing_llm_use_images=enabled, drawing_llm_context_messages=0)
            event = core.FakeEvent("照我发的图调整站姿")
            req = ProviderRequest(prompt=event.message, contexts=[{"role":"user","content":"old secret"}],
                image_urls=["https://example.org/user.png"])
            await p.drawing_identity_context(event,req)
            await p.novelai_generate_image(event,"pose")
            await self.complete_reply(p,event)
            kwargs = p.context.llm_generate.await_args.kwargs
            self.assertEqual(kwargs["image_urls"], ["https://example.org/user.png"] if enabled else None)
            payload = json.loads(kwargs["prompt"])
            self.assertEqual(payload["recent_dialogue"], [])
            self.assertEqual(payload["user_request"], event.message)
        asyncio.run(run(False)); asyncio.run(run(True))

    def test_successful_drawing_is_previous_reference_only_in_same_valid_history(self):
        async def run():
            p = self.make_planner()
            p.config["drawing_llm_use_images"] = True
            del p._finish_event
            source = p.temp_dir / "generated.png"; source.write_bytes(b"test-image")
            p._call_official.return_value = str(source)
            event = self.drawing_event("画你坐在窗前", "one")
            await p.novelai_generate_image(event,"window")
            await self.complete_reply(p,event)
            self.assertEqual(len(p._drawing_memory), 1)
            copied = Path(next(iter(p._drawing_memory.values()))["image"])
            self.assertTrue(copied.exists())
            self.persist_turn(p,event)
            event2 = self.drawing_event("站起来，衣服不变", "two")
            await p.novelai_generate_image(event2,"stand")
            await self.complete_reply(p,event2)
            kwargs = p.context.llm_generate.await_args.kwargs
            payload = json.loads(kwargs["prompt"])
            self.assertEqual(payload["previous_drawing"]["subjects"][0]["outfit"], "white dress")
            self.assertIn(str(copied), kwargs["image_urls"])
            self.assertTrue(payload["images"]["previous_drawing_attached"])
            self.assertFalse(copied.exists())  # replaced by the new successful image
            reset = core.FakeEvent(); reset.set_extra("_clean_group_context_session", True)
            await p.drawing_context_lifecycle(reset)
            self.assertFalse(p._drawing_memory)
            await p.terminate()
        asyncio.run(run())

    def test_missing_history_or_changed_card_prevents_previous_image_reuse(self):
        async def run():
            for change in ("card", "history", "scope"):
                p = self.make_planner()
                event = self.drawing_event("画你", "one")
                plan = await p._plan_drawing(event,{"prompt":"portrait"})
                subjects = await p._resolve_drawing_subjects(event,plan["subjects"])
                view = await p._story_view(event)
                event.set_extra("nai_generation_view",view)
                event.set_extra("nai_drawing_details",[{"preset_id":"p1"}])
                p._remember_planned_drawing(event,GenerationResult(True,"one","novelai_official"))
                self.persist_turn(p,event)
                if change == "card": p.presets[0]["positive_prompt"] = "red hair"
                elif change == "history": p.context.conversation_manager.get_conversation.return_value.history = '[]'
                else: p.context.conversation_manager.get_curr_conversation_id.return_value = "new-conversation"
                current = await p._story_view(self.drawing_event("再画", "two"))
                self.assertEqual(p._previous_drawing(current), {})
        asyncio.run(run())

    def test_planner_cannot_persist_guessed_story_outfit(self):
        async def run():
            p = self.make_planner({"prompt":"room","subjects":[{"source":"self","outfit":"kimono","state_scope":"story"}]})
            event = core.FakeEvent("设想穿和服")
            await p.novelai_generate_image(event,"kimono")
            await self.complete_reply(p,event)
            self.assertIn("kimono", p._call_official.await_args.args[0])
            self.assertFalse(p._ledger().states)
        asyncio.run(run())

    def test_strict_mode_can_use_planner_without_story_fields(self):
        async def run():
            p = self.make_planner({"prompt":"room","subjects":[{"source":"self","prompt":"smiling"}]})
            p.config["llm_card_mode"] = "strict"
            p.config["llm_auto_reference"] = False
            event = core.FakeEvent("画你微笑")
            await p.novelai_generate_image(event,"smiling")
            await self.complete_reply(p,event)
            p._call_official.assert_awaited_once()
        asyncio.run(run())

    def test_real_astrbot_reply_hook_and_send_stage_draw_after_text_delivery(self):
        from astrbot.core.astr_agent_hooks import MainAgentHooks
        from astrbot.core.pipeline.respond.stage import RespondStage

        async def run():
            p = self.make_planner()
            del p._finish_event
            image = p.temp_dir / "rendered.png"; image.write_bytes(b"image")
            p._call_official.return_value = str(image)
            event = core.FakeEvent("画你站在窗前")
            event.get_sender_name = lambda: "user"
            event._outline_chain = lambda chain: "reply"
            event.clear_result = lambda: setattr(event, "result", None)
            await p.novelai_generate_image(event,"window")
            text = "我站在窗前，穿着白裙。"
            async def capture_hook(current, kind, response, *args):
                await p.drawing_capture_reply(current, response)
            with patch("astrbot.core.astr_agent_hooks.call_event_hook", side_effect=capture_hook):
                await MainAgentHooks().on_agent_done(SimpleNamespace(context=SimpleNamespace(event=event)),
                    LLMResponse("assistant", completion_text=text))
            p.context.llm_generate.assert_not_awaited()
            async def generate_after_text(**kwargs):
                self.assertEqual(event.sent[0].get_plain_text(), text)
                return p.context.llm_generate.return_value
            p.context.llm_generate.side_effect = generate_after_text
            event.result = MessageEventResult().message(text)
            stage = RespondStage(); stage.platform_settings = {}; stage.enable_seg = False
            async def send_hook(current, kind):
                await p.drawing_after_reply(current)
                return False
            with patch("astrbot.core.pipeline.respond.stage.call_event_hook", side_effect=send_hook):
                await stage.process(event)
            self.assertEqual(len(event.sent), 2)
            self.assertIsInstance(event.sent[1].chain[0], Image)
            p.context.llm_generate.assert_awaited_once()
        asyncio.run(run())

    def test_stream_finish_triggers_planner_once_and_preserves_complete_reply(self):
        async def run():
            p = self.make_planner(); event = core.FakeEvent("把裙子换白色")
            await p.novelai_generate_image(event, "change dress")
            text = "开始的动作。" + "剧情" * 5000 + "结尾的动作。"
            await p.drawing_capture_reply(event, LLMResponse("assistant", completion_text=text))
            event.result = MessageEventResult().message(text)
            await p.drawing_stream_finished(event)
            p.context.llm_generate.assert_not_awaited()
            event.result.set_result_content_type(ResultContentType.STREAMING_FINISH)
            await p.drawing_stream_finished(event)
            await p.drawing_after_reply(event)
            p.context.llm_generate.assert_awaited_once()
            self.assertEqual(json.loads(p.context.llm_generate.await_args.kwargs["prompt"])["current_reply"], text)
        asyncio.run(run())

    def test_changing_provider_cancels_queued_or_inflight_plan(self):
        async def run(during):
            p = self.make_planner(); p._notify = AsyncMock()
            event = core.FakeEvent("draw")
            await p.novelai_generate_image(event,"draw")
            if during:
                async def generate(**kwargs):
                    p.config["drawing_llm_provider_id"] = "another-provider"
                    return LLMResponse("assistant",completion_text='{"prompt":"room","subjects":[]}')
                p.context.llm_generate.side_effect = generate
            else: p.config["drawing_llm_provider_id"] = "another-provider"
            await self.complete_reply(p,event)
            self.assertEqual(p.context.llm_generate.await_count, int(during))
            p._call_official.assert_not_awaited()
            p._notify.assert_awaited_once()
        asyncio.run(run(False)); asyncio.run(run(True))

    def test_optional_continuity_cache_failure_does_not_report_successful_send_as_failed(self):
        async def run():
            p = self.make_planner(); del p._finish_event
            p._send_generated_image = AsyncMock(return_value=True)
            p._remember_planned_drawing = Mock(side_effect=OSError("read-only cache"))
            p._notify = AsyncMock()
            event = core.FakeEvent()
            result = await p._finish_event(event,GenerationResult(True,"job","novelai_official",path="image.png",message="sent"))
            self.assertEqual(result,"sent")
            p._send_generated_image.assert_awaited_once()
            p._notify.assert_not_awaited()
        asyncio.run(run())

    def test_current_image_components_are_read_for_n_only_when_opted_in(self):
        async def run(enabled):
            p = self.make_planner(); p.config["drawing_llm_use_images"] = enabled
            event = core.FakeEvent("/n 按附图的姿势重新画")
            image = Image.fromFileSystem(str(p.temp_dir / "user.png"))
            event.get_messages = lambda:[image]
            with patch.object(Image,"convert_to_file_path", new_callable=AsyncMock, return_value="user.png") as convert:
                await p._quick_draw(event,"按附图的姿势重新画")
            self.assertEqual(convert.await_count, int(enabled))
            self.assertEqual(p.context.llm_generate.await_args.kwargs["image_urls"], ["user.png"] if enabled else None)
        asyncio.run(run(False)); asyncio.run(run(True))

    def test_planner_configuration_saves_bounds_and_never_exposes_provider_keys(self):
        async def run():
            p = self.make_planner()
            p._save_config = Mock(); p._sync_sticker_emotion_tool = Mock()
            fields = {"drawing_llm_provider_id":"visual-provider", "drawing_llm_timeout":999,
                "drawing_llm_context_messages":999, "drawing_llm_use_images":True, "drawing_llm_prompt":"保留构图"}
            with patch("main.request", SimpleNamespace(json=AsyncMock(return_value=fields))), patch("main.json_response", side_effect=lambda value:value):
                result = await p.page_save_config()
            self.assertTrue(result["saved"])
            self.assertEqual(p.config["drawing_llm_timeout"],180)
            self.assertEqual(p.config["drawing_llm_context_messages"],30)
            self.assertTrue(p.config["drawing_llm_use_images"])
            p.context.get_all_providers = Mock(return_value=[SimpleNamespace(meta=lambda:SimpleNamespace(id="visual-provider"),
                get_model=lambda:"vision-model", provider_config={"api_key":"secret"})])
            p._reference_list = Mock(return_value=[])
            with patch("main.json_response", side_effect=lambda value:value):
                settings = await p.page_settings()
            self.assertEqual(settings["drawing_providers"],[{"id":"visual-provider","model":"vision-model"}])
            self.assertNotIn("secret", json.dumps(settings))
        asyncio.run(run())

    def test_plan_parser_accepts_fences_and_rejects_invalid_contracts(self):
        result = planner.parse_plan('```json\n{"prompt":"room","subjects":[]}\n```')
        self.assertEqual(result["prompt"],"room")
        for invalid in ('[]','{}','{"prompt":"","subjects":[]}', '{"prompt":"room","subjects":[],"image_urls":["file"]}'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError): planner.parse_plan(invalid)
