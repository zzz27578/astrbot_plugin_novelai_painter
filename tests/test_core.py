import asyncio
import base64
import io
import json
import tempfile
import zipfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
from pathlib import Path
from types import SimpleNamespace

import drawing

from main import DEFAULT_NEGATIVE, GenerationResult, NovelAIPainterPlugin, ProviderError
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.core.agent.message import Message, TextPart, dump_messages_with_checkpoints
from astrbot.core.message.components import Image
from astrbot.core.message.message_event_result import MessageChain


async def drain_drawing_tasks(plugin):
    tasks = list(getattr(plugin, "_planner_tasks", {}))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def planning_payload(kwargs):
    """Read reference materials and native messages from a captured request."""
    import planner
    contexts = kwargs["contexts"]
    result = json.loads(contexts[0]["content"][len(planner.MATERIALS_HEADER):])
    count = result.pop("dialogue_message_count")
    has_reply = result.pop("current_reply_included")
    result["drawing_request"] = kwargs["prompt"]
    result["recent_dialogue"] = [{"role": m["role"], "text": m["content"]} for m in contexts[1:1+count]]
    result["current_reply"] = contexts[-1]["content"] if has_reply else ""
    return result


class FakeEvent:
    unified_msg_origin = "test:group:1"
    role = "admin"
    message_obj = SimpleNamespace(message_id="m-1")
    def __init__(self, message="1girl, silver hair, blue eyes, night city"):
        self.sent = []
        self.message = message
        self.result = None
        self.extras = {}
        self.call_llm = True
        self.stopped = False
    async def send(self, message): self.sent.append(message)
    def is_private_chat(self): return False
    def is_admin(self): return self.role == "admin"
    def get_sender_id(self): return "u-1"
    def get_group_id(self): return "g-1"
    def get_platform_name(self): return "aiocqhttp"
    def get_message_str(self): return self.message
    def get_result(self): return self.result
    def set_extra(self, key, value): self.extras[key] = value
    def get_extra(self, key, default=None): return self.extras.get(key, default)
    def plain_result(self, message): return message
    def should_call_llm(self, enabled): self.call_llm = enabled
    def stop_event(self): self.stopped = True


class CoreTests(unittest.TestCase):
    def make_plugin(self):
        p = object.__new__(NovelAIPainterPlugin)
        p.config = {"model":"nai-diffusion-5-full", "negative_prompt":DEFAULT_NEGATIVE, "quality_toggle":True, "width":832, "height":1216, "steps":28, "scale":5.0, "sampler":"k_euler_ancestral", "reference_strength":.6, "reference_fidelity":.6, "reference_information_extracted":1.0, "provider":"novelai_official", "invoke_mode":"both", "group_access":"admin_only", "admin_bypass":True, "private_access":"all", "dedupe_window_seconds":30, "queue_timeout":1, "retry_mode":"none", "retry_delay":1, "show_retry_notice":False, "auto_clean_delay":300, "llm_auto_reference":True, "reference_enabled":True, "img2img_enabled":True, "persona_preset_map":{}}
        p.presets = [{"id":"p1", "name":"P1", "positive_prompt":"watercolor, blue eyes, blonde hair, side ponytail, black bodysuit", "negative_prompt":"extra arms", "reference_id":"r1", "reference_type":"style", "lock_positive":True, "positive_strength":1.35, "quality_override":"off"}]
        # Existing regression cases intentionally exercise the legacy strict mode.
        p.config["llm_card_mode"] = "strict"
        p._recent_jobs = {}
        p._delivered_jobs = {}
        p._llm_tool_claims = {}
        p._jobs = []
        p.lock = asyncio.Lock()
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        p.temp_dir = Path(temp_dir.name)
        p._resolve_persona_id = lambda event=None: ""
        return p

    def test_prompt_and_negative_preset(self):
        p = self.make_plugin(); p.config["default_preset_id"] = "p1"
        prompt, pid, negative = p._compose_prompt("a city", FakeEvent("画一张城市里的全身像"))
        self.assertEqual(pid, "p1")
        self.assertTrue(prompt.startswith("1.35::watercolor, blue eyes, blonde hair, side ponytail, black bodysuit::"))
        self.assertIn("consistent art style", prompt)
        self.assertNotIn("masterpiece", prompt)
        self.assertEqual(negative, "extra arms")

    def make_context_plugin(self):
        p = self.make_plugin()
        p.config.update({"llm_card_mode": "context", "default_preset_id": "p1"})
        p.context = SimpleNamespace(conversation_manager=SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value="conversation-1"),
            get_conversation=AsyncMock(return_value=SimpleNamespace(history="[]")),
        ))
        p._call_official = AsyncMock(return_value="image.png")
        p._load_reference = AsyncMock(return_value=(None, None))
        p._finish_event = AsyncMock(return_value="done")
        return p

    def drawing_event(self, message, message_id):
        event = FakeEvent(message)
        event.message_obj = SimpleNamespace(message_id=message_id)
        return event

    def persist_turn(self, p, event):
        conversation = p.context.conversation_manager.get_conversation.return_value
        history = json.loads(conversation.history)
        history.append({"role": "user", "content": event.message})
        history.append({"role": "assistant", "content": "ok"})
        conversation.history = json.dumps(history)

    def drawing_context(self, req):
        return next(part.text for part in req.extra_user_content_parts if part.text.startswith("[NovelAI drawing identities]"))

    def test_native_and_legacy_subject_arrays_produce_equivalent_prompts(self):
        async def run():
            prompts = []
            for subjects in ([{"source": "self"}], '[{"source":"self"}]'):
                p = self.make_context_plugin()
                await p.novelai_generate_image(FakeEvent(), "room", subjects=subjects)
                prompts.append(p._call_official.await_args.args[0])
            self.assertEqual(*prompts)
        asyncio.run(run())

    def test_complete_outfit_replaces_default_and_image_only_does_not_change_story(self):
        async def run():
            p = self.make_context_plugin()
            p.presets[0]["negative_prompt"] = "{white dress}, extra arms"
            before = json.dumps(p.presets)
            event = self.drawing_event("只在这张图穿白裙", "one-image")
            await p.novelai_generate_image(event, "room", subjects=[{"source":"self", "outfit":"white dress"}])
            args = p._call_official.await_args.args
            self.assertIn("white dress", args[0]); self.assertNotIn("black bodysuit", args[0])
            self.assertNotIn("white dress", args[5]); self.assertIn("extra arms", args[5])
            self.persist_turn(p, event)
            await p.novelai_generate_image(self.drawing_event("画现在的你", "next"), "room", subjects=[{"source":"self"}])
            self.assertIn("black bodysuit", p._call_official.await_args.args[0])
            self.assertFalse(p._ledger().states)
            self.assertEqual(json.dumps(p.presets), before)
        asyncio.run(run())

    def test_story_outfit_continues_and_can_return_to_default(self):
        async def run():
            p = self.make_context_plugin()
            e = self.drawing_event("我帮你换上白裙", "story")
            await p.novelai_generate_image(e, "room", subjects=[{"source":"self", "outfit":"white dress", "state_scope":"story"}])
            self.persist_turn(p, e)
            await p.novelai_generate_image(self.drawing_event("继续走到窗前", "next"), "window", subjects=[{"source":"self"}])
            prompt = p._call_official.await_args.args[0]
            self.assertIn("white dress", prompt); self.assertNotIn("black bodysuit", prompt)
            e = self.drawing_event("换回原装", "restore")
            await p.novelai_generate_image(e, "room", subjects=[{"source":"self", "outfit":"black bodysuit", "state_scope":"story"}])
            self.persist_turn(p, e)
            await p.novelai_generate_image(self.drawing_event("继续", "next2"), "room", subjects=[{"source":"self"}])
            self.assertIn("black bodysuit", p._call_official.await_args.args[0])
            self.assertNotIn("white dress", p._call_official.await_args.args[0])
        asyncio.run(run())

    def test_explicit_empty_outfit_replaces_all_default_clothes(self):
        async def run():
            p = self.make_context_plugin()
            await p.novelai_generate_image(FakeEvent(), "room", subjects=[{"source":"self", "outfit":""}])
            self.assertNotIn("black bodysuit", p._call_official.await_args.args[0])
            self.assertIn("blonde hair", p._call_official.await_args.args[0])
        asyncio.run(run())

    def test_failed_image_does_not_undo_actual_story_change(self):
        async def run():
            p = self.make_context_plugin()
            p._call_official.side_effect = ProviderError("network", "test failure")
            e = self.drawing_event("已经换好白裙了", "failed")
            await p.novelai_generate_image(e, "room", subjects=[{"source":"self", "outfit":"white dress", "state_scope":"story"}])
            self.persist_turn(p, e)
            p._call_official.side_effect = None
            await p.novelai_generate_image(self.drawing_event("再画", "retry-new-message"), "room", subjects=[{"source":"self"}])
            self.assertIn("white dress", p._call_official.await_args.args[0])
        asyncio.run(run())

    def test_reset_same_cid_new_cid_history_edit_and_card_edit_invalidate_story(self):
        async def run(change):
            p = self.make_context_plugin()
            e = self.drawing_event("换上白裙", "story")
            await p.novelai_generate_image(e, "room", subjects=[{"source":"self", "outfit":"white dress", "state_scope":"story"}])
            self.persist_turn(p, e)
            # Observe the persisted turn as it would be at the next LLM request.
            await p.drawing_identity_context(self.drawing_event("继续", "observe"), SimpleNamespace(system_prompt=""))
            e2 = self.drawing_event("新请求", "after-change")
            manager = p.context.conversation_manager
            if change == "reset":
                manager.get_conversation.return_value.history = "[]"
                reset = FakeEvent("/renamed-reset")
                reset.set_extra("_clean_group_context_session", True)
                await p.drawing_context_lifecycle(reset)
            elif change == "new":
                manager.get_curr_conversation_id.return_value = "conversation-2"
                manager.get_conversation.return_value.history = "[]"
            elif change == "history":
                manager.get_conversation.return_value.history = "[]"
            elif change == "card":
                p.presets[0]["positive_prompt"] = "green eyes, black suit"
            else:
                e2.unified_msg_origin = "test:group:other"
            await p.novelai_generate_image(e2, "room", subjects=[{"source":"self"}])
            self.assertNotIn("white dress", p._call_official.await_args.args[0])
        for change in ("reset", "new", "history", "card", "origin"):
            with self.subTest(change=change): asyncio.run(run(change))

    def test_plain_reset_text_does_not_clear_state_until_core_confirms_success(self):
        async def run():
            p = self.make_context_plugin()
            view = await p._story_view(FakeEvent())
            card = drawing.card_layers(p.presets[0])
            p._ledger().remember(view, card, "white dress")
            await p.drawing_context_lifecycle(FakeEvent("/reset"))
            self.assertTrue(p._ledger().states)
            e = FakeEvent(); e.set_extra("_clean_group_context_session", True)
            await p.drawing_context_lifecycle(e)
            self.assertFalse(p._ledger().states)
        asyncio.run(run())

    def test_missing_or_unreadable_history_never_uses_shared_outfit_cache(self):
        async def run():
            p = self.make_context_plugin()
            view = await p._story_view(FakeEvent())
            p._ledger().remember(view, drawing.card_layers(p.presets[0]), "white dress")
            p.context.conversation_manager.get_conversation.return_value.history = "unreadable"
            await p.novelai_generate_image(self.drawing_event("继续", "no-history"), "room", subjects=[{"source":"self"}])
            self.assertNotIn("white dress", p._call_official.await_args.args[0])
        asyncio.run(run())

    def test_stale_request_after_reset_is_rejected_before_generation(self):
        async def run():
            p = self.make_context_plugin()
            e = FakeEvent()
            await p.drawing_identity_context(e, SimpleNamespace(system_prompt=""))
            reset = FakeEvent(); reset.set_extra("_clean_group_context_session", True)
            await p.drawing_context_lifecycle(reset)
            result = await p.novelai_generate_image(e, "room", subjects=[{"source":"self"}])
            self.assertIn("No image request was sent", result)
            p._call_official.assert_not_awaited()
        asyncio.run(run())

    def test_request_built_before_context_switch_is_not_rebound_to_new_chat(self):
        async def run(cid, history):
            p = self.make_context_plugin(); e = FakeEvent()
            req = SimpleNamespace(system_prompt="", prompt=e.message,
                conversation=SimpleNamespace(cid=cid, history=history))
            await p.drawing_identity_context(e, req)
            result = await p.novelai_generate_image(e, "room", subjects=[{"source":"self", "outfit":"white dress", "state_scope":"story"}])
            self.assertIn("No image request was sent", result)
            self.assertFalse(p._ledger().states)
            p._call_official.assert_not_awaited()
        for cid, history in (("old-cid", "[]"), ("conversation-1", '[{"role":"user","content":"old story"}]')):
            with self.subTest(cid=cid): asyncio.run(run(cid, history))

    def test_reset_cancels_queued_request_and_does_not_recreate_story(self):
        async def run():
            p = self.make_context_plugin()
            p.config["show_queue_notice"] = False
            await p.lock.acquire()
            task = asyncio.create_task(p.novelai_generate_image(FakeEvent(), "room", subjects=[{"source":"self", "outfit":"white dress", "state_scope":"story"}]))
            for _ in range(10):
                await asyncio.sleep(0)
                if p._ledger().states: break
            self.assertTrue(p._ledger().states)
            reset = FakeEvent(); reset.set_extra("_clean_group_context_session", True)
            await p.drawing_context_lifecycle(reset)
            p.lock.release()
            await task
            p._call_official.assert_not_awaited()
            self.assertFalse(p._ledger().states)
            self.assertEqual(p._jobs[-1]["error_code"], "stale_context")
        asyncio.run(run())

    def test_reset_during_provider_call_does_not_send_old_image(self):
        async def run():
            p = self.make_context_plugin()
            async def reset_during_call(*args, **kwargs):
                reset = FakeEvent(); reset.set_extra("_clean_group_context_session", True)
                await p.drawing_context_lifecycle(reset)
                return "image.png"
            p._call_official.side_effect = reset_during_call
            await p.novelai_generate_image(FakeEvent(), "room", subjects=[{"source":"self", "outfit":"white dress", "state_scope":"story"}])
            result = p._finish_event.await_args.args[1]
            self.assertFalse(result.ok); self.assertIsNone(result.path)
            self.assertFalse(p._ledger().states)
        asyncio.run(run())

    def test_main_model_catalog_refreshes_in_reused_prompt(self):
        async def run():
            p = self.make_context_plugin()
            e = self.drawing_event("换白裙", "story")
            await p.novelai_generate_image(e, "room", subjects=[{"source":"self", "outfit":"white dress", "state_scope":"story"}])
            self.persist_turn(p, e)
            req = SimpleNamespace(system_prompt="Original persona")
            await p.drawing_identity_context(self.drawing_event("继续", "next"), req)
            self.assertIn('"current_story_outfit": "white dress"', self.drawing_context(req))
            p.context.conversation_manager.get_conversation.return_value.history = "[]"
            await p.drawing_identity_context(self.drawing_event("新对话", "new"), req)
            self.assertNotIn('"current_story_outfit": "white dress"', self.drawing_context(req))
            self.assertEqual(len(req.extra_user_content_parts), 1)
            self.assertEqual(req.system_prompt, "Original persona")
        asyncio.run(run())

    def test_contextual_reference_requires_explicit_opt_in(self):
        async def run():
            p = self.make_context_plugin()
            p._load_reference.return_value = (b"reference", {"image_b64":"abc"})
            await p.novelai_generate_image(FakeEvent(), "room", subjects=[{"source":"self", "outfit":"white dress"}])
            self.assertEqual(p._call_official.await_args.args[1], "generate")
            self.assertIsNone(p._call_official.await_args.args[4])
            await p.novelai_generate_image(self.drawing_event("照参考图画", "ref"), "room", subjects=[{"source":"self", "use_reference":True}])
            self.assertEqual(p._call_official.await_args.args[1], "reference")
        asyncio.run(run())

    def test_multicharacter_outfits_negatives_and_count_slots_are_separate(self):
        async def run():
            p = self.make_context_plugin()
            p.presets[0]["positive_prompt"] += ", 1girl, solo"
            p.presets.append({"id":"p2", "name":"Other", "positive_prompt":"1girl, solo, red hair, uniform", "negative_prompt":"kimono, blue eyes"})
            await p.novelai_generate_image(FakeEvent(), "2girls, park", subjects=[
                {"source":"self", "outfit":"white dress", "prompt":"smiling"},
                {"source":"card", "preset_id":"p2", "outfit":"kimono", "prompt":"waving"}])
            args = p._call_official.await_args
            chars = args.kwargs["character_prompts"]
            self.assertNotIn("white dress", args.args[0])
            self.assertIn("white dress", chars[0]["prompt"]); self.assertNotIn("kimono", chars[0]["prompt"])
            self.assertIn("kimono", chars[1]["prompt"]); self.assertNotIn("uniform", chars[1]["prompt"])
            self.assertEqual(chars[1]["negative_prompt"], "blue eyes")
            for char in chars:
                self.assertNotIn("1girl", char["prompt"]); self.assertNotIn("solo", char["prompt"])
                self.assertIn("girl", char["prompt"])
        asyncio.run(run())

    def test_openai_context_has_no_fixed_outfit_instruction(self):
        async def run():
            p = self.make_context_plugin(); p.config["provider"] = "openai_compatible"
            p._call_openai = AsyncMock(return_value="image.png")
            await p.novelai_generate_image(FakeEvent(), "room", subjects=[{"source":"self", "outfit":"white dress"}])
            prompt = p._call_openai.await_args.args[0]
            self.assertIn("white dress", prompt); self.assertNotIn("black bodysuit", prompt)
            self.assertNotIn("FIXED", prompt)
        asyncio.run(run())

    def test_custom_and_unbound_self_do_not_inherit_default(self):
        async def run():
            for source in ("self", "custom"):
                p = self.make_context_plugin(); p.config["default_preset_id"] = ""
                await p.novelai_generate_image(FakeEvent(), "room", subjects=[{"source":source, "appearance_prompt":"silver hair, green eyes, white dress", "prompt":"smiling"}])
                self.assertIn("silver hair", p._call_official.await_args.args[0])
                self.assertNotIn("black bodysuit", p._call_official.await_args.args[0])
                self.assertFalse(p._ledger().states)
        asyncio.run(run())

    def test_one_argument_correction_allowed_but_paid_task_cannot_repeat(self):
        async def run():
            p = self.make_context_plugin(); e = FakeEvent()
            first = await p.novelai_generate_image(e, "room", subjects={"source":"self"})
            self.assertIn("IMAGE_TOOL_ARGUMENT_ERROR", first)
            p._call_official.assert_not_awaited()
            second = await p.novelai_generate_image(e, "room", subjects=[{"source":"self"}])
            self.assertIn("FINAL_IMAGE_TOOL_RESULT", second)
            third = await p.novelai_generate_image(e, "room", subjects=[{"source":"self", "outfit":"white dress"}])
            self.assertIn("ENTRY_GATE_BLOCKED_DUPLICATE", third)
            p._call_official.assert_awaited_once()
        asyncio.run(run())

    def test_second_invalid_attempt_stops_correction_loop(self):
        async def run():
            p = self.make_context_plugin(); e = FakeEvent()
            await p.novelai_generate_image(e, "room", subjects={})
            result = await p.novelai_generate_image(e, "room", subjects={})
            self.assertIn("FINAL_IMAGE_TOOL_RESULT", result)
            await p.novelai_generate_image(e, "room", subjects=[])
            p._call_official.assert_not_awaited()
        asyncio.run(run())

    def test_legacy_patch_and_incomplete_outfit_weights_rejected_without_state_change(self):
        async def run(subject, scene):
            p = self.make_context_plugin()
            result = await p.novelai_generate_image(FakeEvent(), scene, subjects=[subject])
            self.assertIn("No image request was sent", result)
            p._call_official.assert_not_awaited(); self.assertFalse(p._ledger().states)
        for subject, scene in (({"source":"self", "appearance_patch":{"add":["white dress"]}}, "room"),
                               ({"source":"self", "outfit":"{{white dress"}, "room")):
            with self.subTest(subject=subject): asyncio.run(run(subject,scene))

    def test_complete_scene_card_selection_reaches_http_without_default_wardrobe(self):
        """The reported call shape: full prose at top, card selector only below."""
        async def run():
            p = self.make_context_plugin()
            p.presets[0]["positive_prompt"] = "{{elf (isekai ojisan)}}, pale blonde hair, {forehead jewel}, sleeveless black bodysuit, yellow split skirt, red boots, {atelier pontdarc}"
            p.config["api_token"] = "test-token"
            del p._call_official
            p._post_image_request = AsyncMock(return_value="image.png")
            scene = (
                "an adult elf and a white bear humanoid adventurer beneath an ancient tree, "
                "pale blonde hair, high side ponytail, emerald green eyes, "
                "black bodysuit partially unfastened, yellow split skirt folded on the grass, "
                "red boots still on, fluffy white fur, adventurer clothes, warm sunset"
            )
            event = self.drawing_event("画两人在树下的当前场景", "full-scene-card")
            result = await p.novelai_generate_image(event, scene,
                subjects=[{"source":"card", "preset_id":"p1", "state_scope":"image", "use_reference":False}],
                style="light novel cover style, clean lineart")
            self.assertNotIn("ARGUMENT_ERROR", result)
            p._post_image_request.assert_awaited_once()
            payload = p._post_image_request.await_args.args[2]
            self.assertIn(scene, payload["input"])
            self.assertIn("{{elf (isekai ojisan)}}", payload["input"])
            self.assertIn("{forehead jewel}", payload["input"])
            self.assertNotIn("sleeveless black bodysuit", payload["input"])
            self.assertEqual(payload["input"].count("yellow split skirt"), 1)
            self.assertFalse(p._ledger().states)
            self.assertEqual(p._request_snapshots[event.unified_msg_origin]["subjects"][0]["outfit_source"], "shared_prompt")
            duplicate = await p.novelai_generate_image(event, scene, subjects=[{"source":"card", "preset_id":"p1"}])
            self.assertIn("ENTRY_GATE_BLOCKED_DUPLICATE", duplicate)
            p._post_image_request.assert_awaited_once()
        asyncio.run(run())

    def test_inline_subject_clothes_override_saved_outfit_for_this_image_only(self):
        async def run():
            p = self.make_context_plugin()
            e = self.drawing_event("剧情中换上和服", "saved-outfit")
            await p.novelai_generate_image(e, "room", subjects=[{"source":"self", "outfit":"kimono", "state_scope":"story"}])
            self.persist_turn(p,e)
            result = await p.novelai_generate_image(self.drawing_event("这张画白裙", "inline-outfit"), "room",
                subjects=[{"source":"self", "prompt":"{{white dress}}, smiling"}])
            self.assertNotIn("ARGUMENT_ERROR", result)
            prompt = p._call_official.await_args.args[0]
            self.assertIn("{{white dress}}", prompt)
            self.assertNotIn("kimono", prompt)
            self.assertNotIn("black bodysuit", prompt)
            await p.novelai_generate_image(self.drawing_event("回到剧情继续画", "back-to-story"), "room", subjects=[{"source":"self"}])
            self.assertIn("kimono", p._call_official.await_args.args[0])
        asyncio.run(run())

    def test_custom_accepts_description_in_appearance_or_shared_prompt(self):
        async def run(subject, scene):
            p = self.make_context_plugin()
            result = await p.novelai_generate_image(FakeEvent(), scene, subjects=[subject])
            self.assertNotIn("ARGUMENT_ERROR", result)
            p._call_official.assert_awaited_once()
            args = p._call_official.await_args.args
            self.assertIn("hatsune miku", args[0])
            self.assertNotIn("blonde hair", args[0])
            self.assertNotIn("black bodysuit", args[0])
            self.assertEqual(args[1], "generate")
            self.assertIsNone(args[4])
        for subject,scene in (
            ({"source":"custom", "appearance_prompt":"hatsune miku, aqua hair, white dress"}, "park"),
            ({"source":"custom", "prompt":"", "appearance_prompt":"hatsune miku, aqua hair"}, "park"),
            ({"source":"custom"}, "hatsune miku, aqua hair, white dress, park"),
            ({"source":"custom", "prompt":"", "appearance_prompt":""}, "hatsune miku, aqua hair, park"),
            ({"source":"custom", "prompt":"hatsune miku", "appearance_prompt":""}, "park"),
        ):
            with self.subTest(subject=subject): asyncio.run(run(subject,scene))

    def test_empty_custom_entries_use_shared_description_without_empty_slots(self):
        async def run():
            p = self.make_context_plugin()
            scene = "2girls, hatsune miku, megurine luka, aqua hair, pink hair, park"
            await p.novelai_generate_image(FakeEvent(),scene,subjects=[{"source":"custom"},{"source":"custom", "prompt":""}])
            args = p._call_official.await_args
            self.assertIn(scene,args.args[0])
            self.assertNotIn("blonde hair",args.args[0])
            self.assertNotIn("character_prompts",args.kwargs)
        asyncio.run(run())

    def test_inline_subject_clothes_stay_local_in_multicharacter_payload(self):
        async def run():
            p = self.make_context_plugin()
            p.presets.append({"id":"p2", "name":"Other", "positive_prompt":"red hair, uniform"})
            await p.novelai_generate_image(FakeEvent(), "2girls, park", subjects=[
                {"source":"self", "prompt":"white dress, smiling"},
                {"source":"card", "preset_id":"p2", "prompt":"waving"}])
            args = p._call_official.await_args
            chars = args.kwargs["character_prompts"]
            self.assertNotIn("white dress",args.args[0])
            self.assertIn("white dress",chars[0]["prompt"])
            self.assertNotIn("black bodysuit",chars[0]["prompt"])
            self.assertIn("uniform",chars[1]["prompt"])
            self.assertNotIn("white dress",chars[1]["prompt"])
        asyncio.run(run())

    def test_clothing_props_and_actions_do_not_block_or_remove_fallback_outfit(self):
        async def run():
            p = self.make_context_plugin()
            await p.novelai_generate_image(FakeEvent(), "dress shop, sunlight", subjects=[{"source":"self", "prompt":"holding a dress, smiling"}])
            prompt = p._call_official.await_args.args[0]
            self.assertIn("holding a dress",prompt)
            self.assertIn("black bodysuit",prompt)
        asyncio.run(run())

    def test_worn_clothing_in_prose_is_not_mistaken_for_a_held_prop(self):
        async def run():
            p = self.make_context_plugin()
            scene = "holding hands while wearing a white dress beneath the tree"
            await p.novelai_generate_image(FakeEvent(), scene, subjects=[{"source":"self"}])
            prompt = p._call_official.await_args.args[0]
            self.assertIn(scene,prompt)
            self.assertNotIn("black bodysuit",prompt)
        asyncio.run(run())

    def test_shared_outfit_caption_is_not_copied_to_every_character_slot(self):
        async def run():
            p = self.make_context_plugin()
            p.presets.append({"id":"p2", "name":"Other", "positive_prompt":"red hair, uniform"})
            scene = "2girls, park, white dress"
            await p.novelai_generate_image(FakeEvent(),scene,subjects=[{"source":"self"},{"source":"card","preset_id":"p2"}])
            args = p._call_official.await_args
            self.assertIn(scene,args.args[0])
            for character in args.kwargs["character_prompts"]:
                self.assertNotIn("white dress", character["prompt"])
                self.assertNotIn("black bodysuit",character["prompt"])
                self.assertNotIn("uniform",character["prompt"])
            self.assertFalse(p._ledger().states)
        asyncio.run(run())

    def test_style_override_replaces_only_style_and_keeps_native_weights(self):
        async def run():
            p = self.make_context_plugin()
            p.presets[0]["positive_prompt"] = "{{elf (isekai ojisan)}}, {forehead jewel}, 1.2::green eyes::, black bodysuit, {atelier pontdarc}"
            await p.novelai_generate_image(FakeEvent("画你穿白裙，画风改成油画"), "room", subjects=[{"source":"self", "outfit":"white dress"}], style="oil painting")
            prompt = p._call_official.await_args.args[0]
            for tag in ("{{elf (isekai ojisan)}}", "{forehead jewel}", "1.2::green eyes::", "white dress", "oil painting"):
                self.assertIn(tag, prompt)
            self.assertNotIn("atelier pontdarc", prompt); self.assertNotIn("black bodysuit", prompt)
        asyncio.run(run())

    def test_clothing_actions_are_allowed_without_reintroducing_default_outfit(self):
        async def run():
            p = self.make_context_plugin()
            await p.novelai_generate_image(FakeEvent(), "room", subjects=[
                {"source":"self", "outfit":"white dress", "prompt":"adjusting dress, smiling"}])
            prompt = p._call_official.await_args.args[0]
            self.assertIn("adjusting dress", prompt)
            self.assertNotIn("black bodysuit", prompt)
        asyncio.run(run())

    def test_multicharacter_style_override_is_shared_once_including_custom(self):
        async def run():
            p = self.make_context_plugin()
            await p.novelai_generate_image(FakeEvent("画你和另一个人，使用油画风格"), "2girls, park", style="oil painting", subjects=[
                {"source":"self"}, {"source":"custom", "prompt":"1girl, solo, red hair", "negative_prompt":"blue eyes"}])
            args = p._call_official.await_args
            chars = args.kwargs["character_prompts"]
            self.assertEqual(args.args[0].count("oil painting"), 1)
            self.assertNotIn("watercolor", str(chars))
            self.assertNotIn("oil painting", str(chars))
            self.assertNotIn("1girl", chars[1]["prompt"])
            self.assertNotIn("solo", chars[1]["prompt"])
            self.assertEqual(chars[1]["negative_prompt"], "blue eyes")
        asyncio.run(run())

    def test_oversized_composition_is_rejected_before_anchor_truncation(self):
        async def run():
            p = self.make_context_plugin()
            p.presets[0]["positive_prompt"] = "green eyes, " * 600
            result = await p.novelai_generate_image(FakeEvent(), "scene " * 1000,
                subjects=[{"source":"self", "prompt":"smile " * 600}])
            self.assertIn("No image request was sent", result)
            p._call_official.assert_not_awaited()
            self.assertFalse(p._ledger().states)
        asyncio.run(run())

    def test_story_source_matches_decorated_multimodal_history(self):
        async def run():
            p = self.make_context_plugin()
            event = self.drawing_event("已经换好白裙", "wrapped")
            request = SimpleNamespace(system_prompt="", prompt="[prefix] 已经换好白裙")
            await p.drawing_identity_context(event, request)
            await p.novelai_generate_image(event, "room", subjects=[{"source":"self", "outfit":"white dress", "state_scope":"story"}])
            p.context.conversation_manager.get_conversation.return_value.history = json.dumps([
                {"role":"user", "content":[{"type":"text","text":request.prompt},
                    {"type":"image_url","image_url":{"url":"not-read"}},
                    {"type":"text","text":"<system_reminder>time</system_reminder>"}]},
                {"role":"assistant","content":"ok"}])
            await p.novelai_generate_image(self.drawing_event("继续", "continue-wrapped"), "room", subjects=[{"source":"self"}])
            self.assertIn("white dress", p._call_official.await_args.args[0])
        asyncio.run(run())

    def test_concurrent_validation_still_accepts_one_generation(self):
        async def run():
            p = self.make_context_plugin()
            resolver = p._resolve_active_preset
            async def slow_resolver(*args, **kwargs):
                await asyncio.sleep(0)
                return await resolver(*args, **kwargs)
            p._resolve_active_preset = slow_resolver
            event = FakeEvent()
            results = await asyncio.gather(*[p.novelai_generate_image(event,"room",subjects=[{"source":"self"}]) for _ in range(2)])
            self.assertTrue(any("ENTRY_GATE_BLOCKED_DUPLICATE" in value for value in results))
            p._call_official.assert_awaited_once()
        asyncio.run(run())

    def test_reference_snapshot_omits_image_bytes_and_keeps_reference_settings(self):
        async def run():
            p = self.make_context_plugin()
            p._load_reference.return_value = (b"secret image bytes", {"image_b64":"secret-base64"})
            await p.novelai_generate_image(FakeEvent(), "room", subjects=[{"source":"self", "use_reference":True}])
            snapshot = p._request_snapshots[FakeEvent.unified_msg_origin]
            self.assertTrue(snapshot["uses_reference"])
            self.assertIn("director_reference_strength_values", snapshot["parameters"])
            self.assertNotIn("secret-base64", json.dumps(snapshot))
            reset = FakeEvent(); reset.set_extra("_clean_group_context_session", True)
            await p.drawing_context_lifecycle(reset)
            self.assertFalse(p._request_snapshots)
        asyncio.run(run())

    def test_elf_user_card_and_legacy_rewrite_serialize_exact_anchors(self):
        anchor = """{{elf (isekai ojisan)}}, 1girl, solo, slender body, cute proportions,
pale blonde hair, orange hair tips, high side ponytail, red hair tie,
horizontal pointed ears, emerald green eyes, light-colored eyelashes, {forehead jewel},
sleeveless black bodysuit, yellow split skirt, red boots,
[在此填入你的自定义背景/动作，或直接留空],
light novel cover style, visual novel cg, anime screencap, {atelier pontdarc},
vibrant colors, vivid lighting, soft lineart, subtle shadows, delicate highlights, glossy highlights,
flat cell shading, clean lineart, masterpiece, best quality"""
        negative = "{text}, {subtitles}, {watermark}, {logo}, {signature}, {username}, artist name, scanlines, banding, chromatic aberration, artifacts, pixelated, moire, JPEG artifacts, barcode, monochrome, greyscale, blurry, {bad anatomy}, {bad hands}, error, worst quality, low quality"
        async def run():
            p = self.make_context_plugin(); p.presets[0].update(positive_prompt=anchor,negative_prompt=negative)
            p.config["api_token"] = "test-secret-token"
            del p._call_official
            p._post_image_request = AsyncMock(return_value="image.png")
            await p.novelai_generate_image(FakeEvent(), "full body, room", subjects=[{"source":"self", "appearance_prompt":"light yellow short skirt, atelier style", "negative_prompt":"", "prompt":"smiling"}])
            payload = p._post_image_request.await_args.args[2]
            for tag in ("{{elf (isekai ojisan)}}", "{atelier pontdarc}", "yellow split skirt", "{forehead jewel}"):
                self.assertIn(tag, payload["input"])
            self.assertNotIn("light yellow short skirt", payload["input"])
            self.assertNotIn("在此填入", payload["input"])
            for tag in drawing.tags(negative): self.assertIn(tag, payload["parameters"]["negative_prompt"])
            snapshot = p._request_snapshots[FakeEvent.unified_msg_origin]
            self.assertEqual(snapshot["prompt"], payload["input"])
            self.assertEqual(snapshot["parameters"], payload["parameters"])
            self.assertNotIn("test-secret-token", json.dumps(snapshot))
            self.assertNotIn("image_b64", json.dumps(snapshot))
        asyncio.run(run())

    def test_explicit_builtin_card_request_repairs_empty_subjects_and_preserves_card_style(self):
        """An empty selection plus a paraphrased style must not discard a card."""
        async def run(subjects, scene):
            p = self.make_context_plugin()
            original = "{{elf (isekai ojisan)}}, 1girl, solo, slender body, cute proportions, pale blonde hair, orange hair tips, high side ponytail, red hair tie, horizontal pointed ears, emerald green eyes, light-colored eyelashes, {forehead jewel}, sleeveless black bodysuit, yellow split skirt, red boots, light novel cover style, visual novel cg, anime screencap, {atelier pontdarc}, vibrant colors, vivid lighting, soft lineart, subtle shadows, delicate highlights, glossy highlights, flat cell shading, clean lineart, masterpiece, best quality"
            negative = "{text}, {watermark}, {bad anatomy}, {bad hands}, worst quality, low quality"
            p.presets[0].update(positive_prompt=original, negative_prompt=negative)
            p.config["api_token"] = "test-token"
            del p._call_official
            p._post_image_request = AsyncMock(return_value="image.png")
            event = FakeEvent("你用我们内置的角色卡去画一张图")
            result = await p.novelai_generate_image(event, scene, subjects=subjects,
                style="light novel cover style, visual novel cg, anime screencap, clean lineart, best quality")
            self.assertNotIn("ARGUMENT_ERROR", result)
            p._post_image_request.assert_awaited_once()
            payload = p._post_image_request.await_args.args[2]
            for tag in drawing.tags(original):
                self.assertIn(tag, payload["input"])
            for tag in drawing.tags(negative):
                self.assertIn(tag, payload["parameters"]["negative_prompt"])
            self.assertEqual(payload["input"].count("light novel cover style"), 1 + scene.count("light novel cover style"))
            self.assertEqual(payload["input"].count("yellow split skirt"), 1)
            self.assertEqual(p._request_snapshots[event.unified_msg_origin]["subjects"][0]["preset_id"], "p1")
        for subjects in ([], "[]", None):
            for scene in (
                "elf adventurer, pale blonde hair, standing gracefully, shy smile, soft indoor lighting",
                "slender elf adventurer, 1girl solo, pale blonde hair in high side ponytail with orange hair tips, horizontal pointed ears, emerald green eyes with light-colored eyelashes, red gemstone on forehead, wearing red hair tie, sleeveless black bodysuit, yellow split skirt with high slits on thigh midline, red boots, standing gracefully, shy smile, soft indoor lighting, light novel cover style, visual novel cg, anime screencap, clean lineart, best quality",
            ):
                with self.subTest(subjects=subjects, scene=scene): asyncio.run(run(subjects, scene))

    def test_empty_subjects_honor_explicit_self_and_named_card_but_not_other_characters(self):
        async def run():
            for message in ("画你现在的样子", "画一张你的全身像"):
                p = self.make_context_plugin()
                await p.novelai_generate_image(FakeEvent(message), "portrait", subjects=[])
                self.assertIn("blonde hair", p._call_official.await_args.args[0])
            for message in ("用内置角色卡画一张，不要自行发挥", "用内置角色卡画图，画风不要变"):
                p = self.make_context_plugin()
                await p.novelai_generate_image(FakeEvent(message), "portrait", subjects=[])
                self.assertIn("blonde hair", p._call_official.await_args.args[0])
            p = self.make_context_plugin()
            p.presets.append({"id":"p2", "name":"Other", "positive_prompt":"red hair", "negative_prompt":"blue hair"})
            await p.novelai_generate_image(FakeEvent("用 Other 角色卡画你"), "portrait", subjects=[])
            self.assertIn("red hair", p._call_official.await_args.args[0])
            self.assertNotIn("blonde hair", p._call_official.await_args.args[0])
            for message in ("画初音未来", "画一片星空", "不要用角色卡，画初音", "画你现在的样子，不用角色卡", "用角色卡的画风画星空", "用内置角色卡画你和初音合影"):
                p = self.make_context_plugin()
                await p.novelai_generate_image(FakeEvent(message), "park", subjects=[])
                self.assertNotIn("blonde hair", p._call_official.await_args.args[0])
        asyncio.run(run())

    def test_empty_builtin_selection_uses_only_available_card_and_never_guesses_among_many(self):
        async def run():
            p = self.make_context_plugin()
            p.config["default_preset_id"] = ""
            await p.novelai_generate_image(FakeEvent("用内置角色卡画一张"), "room", subjects=[])
            self.assertIn("blonde hair", p._call_official.await_args.args[0])
            p = self.make_context_plugin()
            p.config["default_preset_id"] = ""
            p.presets.append({"id":"p2", "name":"Other", "positive_prompt":"red hair"})
            result = await p.novelai_generate_image(FakeEvent("用内置角色卡画一张"), "room", subjects=[])
            self.assertIn("IMAGE_TOOL_ARGUMENT_ERROR", result)
            p._call_official.assert_not_awaited()
            p.presets[1]["enabled"] = False
            await p.novelai_generate_image(FakeEvent("用内置角色卡画一张"), "room", subjects=[])
            self.assertIn("blonde hair", p._call_official.await_args.args[0])
        asyncio.run(run())

    def test_unsolicited_style_cannot_erase_card_artist_in_single_or_multiple_subjects(self):
        async def run():
            for message in ("画你现在的样子", "按角色卡的画风画一张", "保持画风不变", "不要改画风", "keep the original art style", "do not change the art style"):
                for count in (1, 2):
                    p = self.make_context_plugin()
                    p.presets[0]["positive_prompt"] = "{{elf (isekai ojisan)}}, green eyes, black bodysuit, {atelier pontdarc}"
                    subjects = [{"source":"self"}]
                    if count == 2: subjects.append({"source":"custom", "prompt":"red hair"})
                    await p.novelai_generate_image(FakeEvent(message), "room", subjects=subjects, style="generic anime style")
                    args = p._call_official.await_args
                    composed = args.args[0] if count == 1 else args.kwargs["character_prompts"][0]["prompt"]
                    self.assertIn("{atelier pontdarc}", composed)
                    self.assertIn("{{elf (isekai ojisan)}}", composed)
                    self.assertNotIn("generic anime style", args.args[0])
        asyncio.run(run())

    def test_unknown_or_disabled_named_card_does_not_fall_back_to_bound_card(self):
        async def run():
            for message, cards in (("用 Missing 角色卡画一张", []), ("用角色卡 Missing 画一张", []),
                ("用 Hidden 角色卡画一张", [{"id":"p2", "name":"Hidden", "positive_prompt":"red hair", "enabled":False}])):
                p = self.make_context_plugin()
                p.presets.extend(cards)
                result = await p.novelai_generate_image(FakeEvent(message), "room", subjects=[])
                self.assertIn("IMAGE_TOOL_ARGUMENT_ERROR", result)
                p._call_official.assert_not_awaited()
        asyncio.run(run())

    def test_custom_only_style_does_not_require_a_card_style_change(self):
        async def run():
            p = self.make_context_plugin()
            await p.novelai_generate_image(FakeEvent("画一个路人"), "room", subjects=[{"source":"custom", "prompt":"red hair"}], style="oil painting")
            self.assertIn("oil painting", p._call_official.await_args.args[0])
            self.assertNotIn("blonde hair", p._call_official.await_args.args[0])
        asyncio.run(run())

    def test_layer_migration_is_lossless_backed_up_and_idempotent(self):
        p = self.make_plugin(); p.presets_path = p.temp_dir / "presets.json"
        original = [{"id":"p1", "name":"Elf", "positive_prompt":"{{elf (isekai ojisan)}}, {yellow split skirt, red boots}, 1.2::green eyes, white dress::, {atelier pontdarc}", "negative_prompt":"{bad hands}"}]
        p.presets_path.write_text(json.dumps(original),encoding="utf-8")
        p.presets = p._load_presets(); p._save_presets()
        migrated = p.presets[0]
        self.assertEqual(migrated["migration_original_prompt"], original[0]["positive_prompt"])
        self.assertEqual(migrated["default_outfit_prompt"], "{yellow split skirt, red boots}")
        self.assertIn("1.2::green eyes, white dress::", migrated["identity_prompt"])
        self.assertTrue(migrated["layers_need_review"]); self.assertTrue(migrated["migration_notes"])
        self.assertEqual(json.loads((p.temp_dir/"presets.before-layers.json").read_text()), original)
        p._preset_schema_migrated = False
        self.assertEqual(p._load_presets(), p.presets)
        self.assertFalse(p._preset_schema_migrated)
        edited = p._normalize_preset_fields({"layers_version":1,"identity_prompt":"{{elf (isekai ojisan)}}, green eyes", "default_outfit_prompt":"white dress", "style_prompt":"{atelier pontdarc}"}, migrated)
        self.assertFalse(edited["layers_need_review"])
        self.assertEqual(edited["migration_original_prompt"], migrated["migration_original_prompt"])
        self.assertEqual(edited["positive_prompt"], "{{elf (isekai ojisan)}}, green eyes, white dress, {atelier pontdarc}")

    def test_official_reference_payload(self):
        p = self.make_plugin(); p.config["default_preset_id"] = "p1"
        params = p._official_parameters("a city", "reference", None, {"image_b64":"abc", "reference_type":"style"}, preset=p.presets[0])
        self.assertEqual(params["director_reference_descriptions"][0]["caption"]["base_caption"], "style")
        self.assertEqual(params["n_samples"], 1)
        self.assertFalse(params["qualityToggle"])

    def test_official_request_uses_anchored_prompt_and_one_sample(self):
        async def run():
            p = self.make_plugin()
            p.config["default_preset_id"] = "p1"
            p.config["api_token"] = "token"
            prompt, preset_id, negative = p._compose_prompt("running in rain", FakeEvent("画她在雨中奔跑"))
            p._post_image_request = AsyncMock(return_value="image.png")
            await p._call_official(prompt, "generate", "job-1", None, None, negative, p._get_preset(preset_id))
            payload = p._post_image_request.await_args.args[2]
            self.assertTrue(payload["input"].startswith("1.35::watercolor, blue eyes"))
            self.assertEqual(payload["parameters"]["n_samples"], 1)
            self.assertFalse(payload["parameters"]["qualityToggle"])
            self.assertIn("extra arms", payload["parameters"]["negative_prompt"])
        asyncio.run(run())

    def test_openai_preset_uses_explicit_fixed_instructions(self):
        p = self.make_plugin()
        p.config["provider"] = "openai_compatible"
        p.config["default_preset_id"] = "p1"
        prompt, preset_id, _ = p._compose_prompt("running in rain", FakeEvent("画她在雨中奔跑"))
        self.assertEqual(preset_id, "p1")
        self.assertIn("Role-card positive prompt (FIXED; preserve every unspecified identity and style detail): watercolor, blue eyes", prompt)
        self.assertIn("Requested change: running in rain", prompt)
        compatible_prompt = p._openai_prompt_with_negative(prompt, "extra arms")
        self.assertIn(DEFAULT_NEGATIVE, compatible_prompt)
        self.assertIn("extra arms", compatible_prompt)

    def test_only_explicit_429_and_503_are_retry_candidates(self):
        limited = NovelAIPainterPlugin._http_error(429, b"", {"Retry-After": "7"})
        self.assertTrue(limited.retryable)
        self.assertEqual(limited.retry_after, 7)
        self.assertFalse(NovelAIPainterPlugin._http_error(401, b"").retryable)
        self.assertTrue(NovelAIPainterPlugin._http_error(503, b"").retryable)
        self.assertFalse(NovelAIPainterPlugin._http_error(502, b"").retryable)

    def test_zip_decode(self):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as zf: zf.writestr("image.png", b"png-data")
        self.assertEqual(NovelAIPainterPlugin._decode_zip(out.getvalue()), b"png-data")

    def test_sticker_json_parser_accepts_fenced_json(self):
        parsed = NovelAIPainterPlugin._sticker_json(
            '```json\n{"send": true, "emotion": "无奈", "prompt": "deadpan face"}\n```'
        )
        self.assertEqual(parsed["emotion"], "无奈")
        self.assertTrue(parsed["send"])

    def test_sticker_same_bubble_sends_text_and_image_as_one_chain(self):
        async def run():
            p = self.make_plugin()
            p.config.update({
                "sticker_enabled": True,
                "sticker_probability": 100,
                "sticker_cooldown_seconds": 0,
                "sticker_prompt": "reaction sticker",
                "sticker_role_card": {"positive_prompt": "silver hair", "negative_prompt": "text"},
                "auto_clean_delay": 0,
            })
            event = FakeEvent("hello")
            event.result = MessageChain().message("hello")
            event.set_extra("sticker_final_text", "hello")
            image_path = p.temp_dir / "sticker.png"
            image_path.write_bytes(b"png")
            p._decide_sticker = AsyncMock(return_value={
                "send": True, "emotion": "无奈", "prompt": "deadpan face, shrugging"
            })
            p._run_job = AsyncMock(return_value=GenerationResult(
                True, "sticker-job", "novelai_official", path=str(image_path), message="sticker"
            ))
            with patch("main.random.random", return_value=0.0):
                await p._try_sticker(event, same_bubble=True)
            self.assertEqual(len(event.sent), 1)
            self.assertEqual(event.sent[0].get_plain_text(), "hello")
            self.assertEqual(len(event.sent[0].chain), 2)
            self.assertEqual(event.result.chain, [])
            self.assertEqual(p._run_job.await_args.kwargs["job_kind"], "sticker")
        asyncio.run(run())

    def test_sticker_emotion_tool_keeps_metadata_out_of_reply(self):
        async def run():
            p = self.make_plugin()
            p.config["sticker_enabled"] = True
            event = FakeEvent()
            result = await p.novelai_set_emotion(event, "嘲笑", "side-eye")
            self.assertIn("recorded", result)
            self.assertEqual(event.get_extra("sticker_emotion"), "嘲笑")
            self.assertEqual(event.get_extra("sticker_visual_hint"), "side-eye")
        asyncio.run(run())

    def test_sticker_emotion_tool_is_hidden_when_sticker_mode_is_off(self):
        p = self.make_plugin()
        tool = SimpleNamespace(active=True)
        manager = SimpleNamespace(get_func=lambda name: tool if name == "novelai_set_emotion" else None)
        p.context = SimpleNamespace(get_llm_tool_manager=lambda: manager)

        p.config["sticker_enabled"] = False
        p._sync_sticker_emotion_tool()
        self.assertFalse(tool.active)

        p.config["sticker_enabled"] = True
        p.config["sticker_emotion_tool"] = True
        p._sync_sticker_emotion_tool()
        self.assertTrue(tool.active)

        p.config["sticker_emotion_tool"] = False
        p._sync_sticker_emotion_tool()
        self.assertFalse(tool.active)

    def test_sticker_final_reply_is_captured_from_llm_response(self):
        async def run():
            p = self.make_plugin()
            p.config["sticker_enabled"] = True
            event = FakeEvent()
            await p.sticker_capture_reply(event, LLMResponse("assistant", completion_text="这就很无奈。"))
            self.assertEqual(event.get_extra("sticker_final_text"), "这就很无奈。")
            self.assertTrue(event.get_extra("sticker_final_ready"))
        asyncio.run(run())


    def test_permission_denies_non_admin_group(self):
        p = self.make_plugin()
        p.config["admin_bypass"] = False
        p.config["group_access"] = "admin_only"
        event = FakeEvent()
        event.role = "member"
        ok, reason = p._can_use(event)
        self.assertFalse(ok)
        self.assertIn("管理员", reason)

    def test_admin_policy_and_disabled_policy_are_not_controlled_by_allowlist_bypass(self):
        p = self.make_plugin()
        event = FakeEvent()
        p.config["admin_bypass"] = False
        p.config["group_access"] = "admin_only"
        self.assertTrue(p._can_use(event)[0])
        p.config["admin_bypass"] = True
        p.config["group_access"] = "disabled"
        self.assertFalse(p._can_use(event)[0])

    def test_stale_embedded_persona_binding_does_not_bypass_canonical_mapping(self):
        p = self.make_plugin()
        p.presets[0]["persona_id"] = "persona-a"
        p._resolve_persona_id = lambda event=None: "persona-a"
        self.assertIsNone(p._resolve_preset(FakeEvent()))
        p.config["persona_preset_map"] = {"persona-a": "p1"}
        self.assertEqual(p._resolve_preset(FakeEvent())["id"], "p1")
        p.presets[0]["enabled"] = False
        self.assertIsNone(p._resolve_preset(FakeEvent()))

    def test_editor_persona_binding_synchronizes_config_mapping(self):
        p = self.make_plugin()
        p.presets[0]["persona_id"] = "persona-a"
        self.assertTrue(p._sync_preset_persona_binding(p.presets[0]))
        self.assertEqual(p.config["persona_preset_map"], {"persona-a": "p1"})

    def test_unchanged_embedded_binding_does_not_override_canonical_mapping(self):
        p = self.make_plugin()
        p.presets[0]["persona_id"] = "persona-a"
        p.config["persona_preset_map"] = {"persona-a": "other"}
        self.assertFalse(p._sync_preset_persona_binding(p.presets[0], "persona-a"))
        self.assertEqual(p.config["persona_preset_map"], {"persona-a": "other"})
        self.assertEqual(p._public_presets()[0]["persona_id"], "")

    def test_preset_id_is_server_owned_and_immutable(self):
        p = self.make_plugin()
        created = p._normalize_preset_fields({"id": "client-id", "name": "New"})
        self.assertNotEqual(created["id"], "client-id")
        updated = p._normalize_preset_fields({"id": "replacement", "name": "Updated"}, p.presets[0])
        self.assertEqual(updated["id"], "p1")

    def test_legacy_style_and_character_fields_migrate_to_one_positive_prompt(self):
        p = object.__new__(NovelAIPainterPlugin)
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        p.presets_path = Path(temp_dir.name) / "presets.json"
        p.presets_path.write_text(json.dumps([{
            "id": "legacy",
            "name": "Legacy",
            "style_prompt": "watercolor",
            "character_prompt": "blonde hair, elf ears",
            "lock_style": True,
            "lock_character": True,
            "style_strength": 1.35,
            "character_strength": 1.25,
            "negative_prompt": "pink hair",
        }]), encoding="utf-8")
        p._preset_schema_migrated = False
        migrated = p._load_presets()[0]
        self.assertEqual(migrated["positive_prompt"], "watercolor, blonde hair, elf ears")
        self.assertEqual(migrated["negative_prompt"], "pink hair")
        self.assertTrue(migrated["lock_positive"])
        self.assertEqual(migrated["positive_strength"], 1.35)
        self.assertEqual(migrated["style_prompt"], "watercolor")
        self.assertNotIn("character_prompt", migrated)
        self.assertTrue(p._preset_schema_migrated)

    def test_old_embedded_persona_binding_is_migrated_without_overwriting_explicit_map(self):
        p = self.make_plugin()
        p.presets[0]["persona_id"] = "persona-a"
        p.config["persona_preset_map"] = {"persona-a": "explicit", "persona-b": "other"}
        p._save_config = Mock()
        p._migrate_embedded_persona_bindings()
        self.assertEqual(p.config["persona_preset_map"]["persona-a"], "explicit")
        p._save_config.assert_not_called()

        p.config["persona_preset_map"] = {"persona-b": "other"}
        p._migrate_embedded_persona_bindings()
        self.assertEqual(p.config["persona_preset_map"]["persona-a"], "p1")
        p._save_config.assert_called_once()

    def test_active_preset_uses_astrbot_4273_persona_manager(self):
        async def run():
            p = self.make_plugin()
            p.config["persona_preset_map"] = {"persona-a": "p1"}
            conversation_manager = SimpleNamespace(
                get_curr_conversation_id=AsyncMock(return_value="conv-1"),
                get_conversation=AsyncMock(return_value=SimpleNamespace(persona_id="conversation-persona")),
            )
            persona_manager = SimpleNamespace(
                resolve_selected_persona=AsyncMock(return_value=("persona-a", {}, "persona-a", False)),
            )
            p.context = SimpleNamespace(
                conversation_manager=conversation_manager,
                persona_manager=persona_manager,
                get_config=Mock(return_value={"provider_settings": {"default_personality": "default-persona"}}),
            )
            event = FakeEvent()
            preset = await p._resolve_active_preset(event)
            self.assertEqual(preset["id"], "p1")
            persona_manager.resolve_selected_persona.assert_awaited_once_with(
                umo=event.unified_msg_origin,
                conversation_persona_id="conversation-persona",
                platform_name="aiocqhttp",
                provider_settings={"default_personality": "default-persona"},
            )
        asyncio.run(run())

    def test_active_preset_passes_none_when_no_current_conversation(self):
        async def run():
            p = self.make_plugin()
            p.config["persona_preset_map"] = {"persona-a": "p1"}
            conversation_manager = SimpleNamespace(
                get_curr_conversation_id=AsyncMock(return_value=None),
                get_conversation=AsyncMock(return_value=None),
            )
            persona_manager = SimpleNamespace(
                resolve_selected_persona=AsyncMock(return_value=("persona-a", {}, None, False)),
            )
            p.context = SimpleNamespace(
                conversation_manager=conversation_manager,
                persona_manager=persona_manager,
                get_config=Mock(return_value={"provider_settings": {}}),
            )
            preset = await p._resolve_active_preset(FakeEvent())
            self.assertEqual(preset["id"], "p1")
            conversation_manager.get_conversation.assert_not_awaited()
            self.assertIsNone(
                persona_manager.resolve_selected_persona.await_args.kwargs["conversation_persona_id"]
            )
        asyncio.run(run())

    def test_explicit_no_persona_does_not_fall_back_to_session_default_persona(self):
        async def run():
            p = self.make_plugin()
            p.config["default_preset_id"] = "p1"
            p.config["persona_preset_map"] = {"default-persona": "other"}
            p._resolve_persona_id = Mock(return_value="default-persona")
            p.context = SimpleNamespace(
                conversation_manager=SimpleNamespace(
                    get_curr_conversation_id=AsyncMock(return_value="conv-1"),
                    get_conversation=AsyncMock(return_value=SimpleNamespace(persona_id="[%None]")),
                ),
                persona_manager=SimpleNamespace(
                    resolve_selected_persona=AsyncMock(return_value=("[%None]", None, None, False)),
                ),
                get_config=Mock(return_value={"provider_settings": {"default_personality": "default-persona"}}),
            )
            preset = await p._resolve_active_preset(FakeEvent())
            self.assertEqual(preset["id"], "p1")
            p._resolve_persona_id.assert_not_called()
        asyncio.run(run())

    def test_locked_preset_filters_hallucinated_character_and_style_tags(self):
        p = self.make_plugin()
        event = FakeEvent("宝宝，画给我看看你现在的样子")
        filtered = p._filter_llm_prompt_conflicts(
            "full body, pink hair, wedding dress, anime screencap, looking at viewer",
            event,
            p.presets[0],
        )
        self.assertIn("full body", filtered)
        self.assertIn("looking at viewer", filtered)
        self.assertNotIn("pink hair", filtered)
        self.assertNotIn("wedding dress", filtered)
        self.assertNotIn("anime screencap", filtered)

    def test_explicit_character_change_is_preserved(self):
        p = self.make_plugin()
        p.presets[0]["negative_prompt"] = "pink hair, wedding dress, extra arms"
        event = FakeEvent("把衣服换成婚纱")
        filtered = p._filter_llm_prompt_conflicts(
            "pink hair, wedding dress, standing",
            event,
            p.presets[0],
        )
        self.assertIn("wedding dress", filtered)
        self.assertNotIn("pink hair", filtered)
        composed, _, negative = p._compose_prompt("wedding dress, standing", event, "p1")
        self.assertNotIn("black bodysuit", composed)
        self.assertIn("blonde hair", composed)
        self.assertIn("wedding dress", composed)
        self.assertNotIn("wedding dress", negative)
        self.assertIn("pink hair", negative)
        self.assertIn("extra arms", negative)

    def test_openai_job_receives_role_card_negative_prompt(self):
        async def run():
            p = self.make_plugin()
            p.config["provider"] = "openai_compatible"
            p.config["default_preset_id"] = "p1"
            p._call_openai = AsyncMock(return_value="image.png")
            p._load_reference = AsyncMock(return_value=(None, None))
            result = await p._run_job(FakeEvent("画她在雨中奔跑"), "running in rain")
            self.assertTrue(result.ok)
            self.assertEqual(p._call_openai.await_args.args[4], "extra arms")

        asyncio.run(run())

    def test_duplicate_event_has_one_provider_call(self):
        async def run():
            p = self.make_plugin()
            p._call_official = AsyncMock(return_value="image.png")
            p._load_reference = AsyncMock(return_value=(None, None))
            e = FakeEvent()
            first, second = await asyncio.gather(p._run_job(e, "a scene"), p._run_job(e, "a rewritten scene"))
            self.assertEqual(p._call_official.await_count, 1)
            self.assertEqual(first.ok, second.ok)
            self.assertNotEqual(first.send_image, second.send_image)
        asyncio.run(run())

    def test_event_key_is_one_per_incoming_message(self):
        p = self.make_plugin(); e = FakeEvent()
        first = p._event_key(e, "x", "generate")
        self.assertEqual(first, p._event_key(e, "rewritten prompt", "img2img"))

    def test_429_retry_options_are_bounded(self):
        async def run():
            p = self.make_plugin()
            p.config["retry_mode"] = "rate_limit_once"
            p._call_official = AsyncMock(side_effect=[
                ProviderError("429", "limited", retryable=True),
                "image.png",
            ])
            p._load_reference = AsyncMock(return_value=(None, None))
            with patch("main.asyncio.sleep", new=AsyncMock()) as sleeper:
                result = await p._run_job(FakeEvent(), "a scene")
            self.assertTrue(result.ok)
            self.assertEqual(result.attempts, 2)
            self.assertEqual(p._call_official.await_count, 2)
            sleeper.assert_awaited_once()

            p = self.make_plugin()
            p.config["retry_mode"] = "rate_limit_twice"
            p._call_official = AsyncMock(side_effect=ProviderError("429", "limited", retryable=True))
            p._load_reference = AsyncMock(return_value=(None, None))
            with patch("main.asyncio.sleep", new=AsyncMock()):
                result = await p._run_job(FakeEvent(), "a scene")
            self.assertFalse(result.ok)
            self.assertEqual(result.attempts, 3)
            self.assertEqual(p._call_official.await_count, 3)
        asyncio.run(run())

    def test_retry_mode_obeys_hard_request_limit(self):
        p = self.make_plugin()
        p.config["retry_mode"] = "rate_limit_twice"
        p.config["max_api_requests_per_job"] = 1
        self.assertEqual(p._retry_limit(), 1)

    def test_retry_wait_releases_global_request_lock(self):
        async def run():
            p = self.make_plugin()
            p.config["retry_mode"] = "rate_limit_once"
            p.config["max_api_requests_per_job"] = 2
            p._call_official = AsyncMock(side_effect=[
                ProviderError("429", "limited", retryable=True),
                "image.png",
            ])
            p._load_reference = AsyncMock(return_value=(None, None))
            lock_states = []

            async def observe_sleep(_):
                lock_states.append(p.lock.locked())

            with patch("main.asyncio.sleep", new=observe_sleep):
                result = await p._run_job(FakeEvent(), "a scene")
            self.assertTrue(result.ok)
            self.assertEqual(lock_states, [False])
        asyncio.run(run())

    def test_non_429_error_never_retries(self):
        async def run():
            p = self.make_plugin()
            p.config["retry_mode"] = "rate_limit_twice"
            p._call_official = AsyncMock(side_effect=ProviderError("network", "uncertain", retryable=True))
            p._load_reference = AsyncMock(return_value=(None, None))
            result = await p._run_job(FakeEvent(), "a scene")
            self.assertFalse(result.ok)
            self.assertEqual(result.attempts, 1)
            self.assertEqual(p._call_official.await_count, 1)
        asyncio.run(run())

    def test_same_job_image_is_sent_only_once(self):
        async def run():
            p = self.make_plugin()
            p.config["auto_clean_delay"] = 0
            image_path = p.temp_dir / "one.png"
            image_path.write_bytes(b"png")
            event = FakeEvent()
            result = GenerationResult(True, "job-once", "novelai_official", path=str(image_path), message="ok")
            first = await p._finish_event(event, result)
            second = await p._finish_event(event, result)
            await asyncio.sleep(0)
            self.assertEqual(first, "ok")
            self.assertIn("重复发送", second)
            self.assertEqual(len(event.sent), 1)
        asyncio.run(run())

    def test_delete_preset_cleans_all_config_references(self):
        p = self.make_plugin()
        p.config["default_preset_id"] = "p1"
        p.config["persona_preset_map"] = {"persona-a": "p1", "persona-b": "other"}
        p._save_presets = Mock()
        p._save_config = Mock()

        self.assertTrue(p._delete_preset_record("p1"))
        self.assertEqual(p.presets, [])
        self.assertEqual(p.config["default_preset_id"], "")
        self.assertEqual(p.config["persona_preset_map"], {"persona-b": "other"})
        p._save_presets.assert_called_once_with()
        p._save_config.assert_called_once_with()

    def test_delete_unknown_preset_does_not_write(self):
        p = self.make_plugin()
        p._save_presets = Mock()
        p._save_config = Mock()

        self.assertFalse(p._delete_preset_record("missing"))
        p._save_presets.assert_not_called()
        p._save_config.assert_not_called()

    def test_webui_uses_multi_selector_for_foreach(self):
        script = Path("pages/settings/app.js").read_text(encoding="utf-8")
        import re
        self.assertIsNone(re.search(r"(?<!\$)\$\('\[data-config\]'\)\.forEach", script))
        self.assertIsNone(re.search(r"(?<!\$)\$\('\[data-delete\]'\)\.forEach", script))
        self.assertIn("$$('[data-config]').forEach", script)
        self.assertIn("$$('[data-delete]'", script)

    def test_llm_tool_declares_prompt_and_handles_missing_argument(self):
        doc = NovelAIPainterPlugin.novelai_generate_image.__doc__ or ""
        self.assertIn("Args:", doc)
        self.assertIn("prompt(string):", doc)

        from astrbot.core.provider.register import llm_tools
        tool = next(item for item in llm_tools.func_list if item.name == "novelai_generate_image")
        prompt_schema = tool.parameters.get("properties", {}).get("prompt", {})
        self.assertEqual(prompt_schema.get("type"), "string")
        self.assertEqual(tool.parameters["properties"]["subjects"]["type"], "array")
        self.assertEqual(tool.parameters["properties"]["subjects"]["items"]["properties"]["state_scope"]["enum"], ["image", "story"])

        async def run():
            p = self.make_plugin()
            p._mode_allows = lambda mode: mode == "llm_tool"
            p._run_job = AsyncMock(return_value=GenerationResult(True, "job-1", "novelai_official"))
            p._finish_event = AsyncMock(return_value="ok")
            event = FakeEvent()
            result = await p.novelai_generate_image(event)
            self.assertIn("FINAL_IMAGE_TOOL_RESULT", result)
            self.assertEqual(json.loads(result.partition(": ")[2])["status"], "completed")
            p._run_job.assert_awaited_once_with(
                event,
                "1girl, silver hair, blue eyes, night city",
                "generate",
                preset_id=None,
                reference_type=None,
                preset_override={},
            )
            p._finish_event.assert_awaited_once()

        asyncio.run(run())

    def test_llm_automatically_uses_bound_reference(self):
        p = self.make_plugin()
        p.config["default_preset_id"] = "p1"
        self.assertEqual(p._resolve_llm_operation("generate", p.presets[0]), "reference")
        p.config["llm_auto_reference"] = False
        self.assertEqual(p._resolve_llm_operation("generate", p.presets[0]), "generate")

    def test_other_characters_and_scenery_never_inherit_self_card_or_reference(self):
        async def run(message, subjects=""):
            p = self.make_plugin()
            p.config["default_preset_id"] = "p1"
            p._call_official = AsyncMock(return_value="image.png")
            p._load_reference = AsyncMock(return_value=(None, None))
            p._finish_event = AsyncMock(return_value="done")
            await p.novelai_generate_image(FakeEvent(message), "hatsune miku, aqua hair, twintails", subjects=subjects)
            args = p._call_official.await_args.args
            self.assertEqual(args[1], "generate")
            self.assertIn("hatsune miku, aqua hair, twintails", args[0])
            self.assertNotIn("blonde hair", args[0])
            self.assertNotIn("extra arms", args[5])
            self.assertEqual(args[6], {})
            p._load_reference.assert_awaited_once_with(None)
            self.assertEqual(p.config["default_preset_id"], "p1")
        for message in ("帮我画初音未来", "你帮我画初音未来", "你自己画初音未来", "不要画你自己，画初音", "画一片星空", "画你和初音合影"):
            with self.subTest(message=message):
                asyncio.run(run(message))
        asyncio.run(run("画你现在的样子，不要使用角色卡", subjects="[]"))

    def test_explicit_card_selection_is_local_and_does_not_require_persona_binding(self):
        async def run():
            p = self.make_plugin()
            p.config["default_preset_id"] = "p1"
            other = {"id": "p2", "name": "Other", "positive_prompt": "red hair, green eyes", "negative_prompt": "blue eyes", "enabled": True}
            p.presets.append(other)
            p._call_official = AsyncMock(return_value="image.png")
            p._load_reference = AsyncMock(return_value=(None, None))
            p._finish_event = AsyncMock(return_value="done")
            await p.novelai_generate_image(
                FakeEvent("画 Other 在公园散步"), "park",
                subjects=json.dumps([{"source": "card", "preset_id": "Other", "prompt": "walking"}]),
            )
            args = p._call_official.await_args.args
            self.assertIn("red hair, green eyes", args[0])
            self.assertIn("walking", args[0])
            self.assertNotIn("blonde hair", args[0])
            self.assertEqual(args[5], "blue eyes")
            self.assertEqual(args[6], other)
            self.assertEqual(p.config["default_preset_id"], "p1")
            self.assertEqual(p.config["persona_preset_map"], {})
        asyncio.run(run())

    def test_custom_subject_needs_no_card_and_keeps_character_identity(self):
        async def run():
            p = self.make_plugin()
            p.config["default_preset_id"] = "p1"
            p._call_official = AsyncMock(return_value="image.png")
            p._load_reference = AsyncMock(return_value=(None, None))
            p._finish_event = AsyncMock(return_value="done")
            await p.novelai_generate_image(
                FakeEvent("画初音未来"), "stage, full body",
                subjects='[{"source":"custom","prompt":"hatsune miku, aqua hair, twintails, singing"}]',
            )
            args = p._call_official.await_args.args
            self.assertIn("hatsune miku, aqua hair, twintails, singing", args[0])
            self.assertNotIn("blonde hair", args[0])
            self.assertEqual(args[1], "generate")
        asyncio.run(run())

    def test_multi_subject_payload_keeps_cards_negatives_and_changes_separate(self):
        async def run():
            p = self.make_plugin()
            p.config.update({"default_preset_id": "p1", "api_token": "test-token"})
            p.presets[0]["negative_prompt"] = "green eyes, wedding dress"
            p.presets.append({
                "id": "p2", "name": "Other", "positive_prompt": "red hair, green eyes, black suit",
                "negative_prompt": "blue eyes, wedding dress", "reference_id": "r2",
            })
            before = json.dumps(p.presets)
            p._post_image_request = AsyncMock(return_value="image.png")
            p._load_reference = AsyncMock(return_value=(None, None))
            p._finish_event = AsyncMock(return_value="done")
            subjects = [
                {"source": "self", "prompt": "wedding dress, smiling"},
                {"source": "card", "preset_id": "p2", "prompt": "standing"},
                {"source": "custom", "prompt": "hatsune miku, aqua hair, twintails, singing"},
            ]
            await p.novelai_generate_image(
                FakeEvent("你穿婚纱，和 Other、初音一起合影"), "3girls, park",
                subjects=json.dumps(subjects),
            )
            payload = p._post_image_request.await_args.args[2]
            params = payload["parameters"]
            self.assertEqual(payload["input"], "3girls, park")
            positive = params["v4_prompt"]["caption"]["char_captions"]
            negative = params["v4_negative_prompt"]["caption"]["char_captions"]
            self.assertEqual(len(positive), 3)
            self.assertEqual(len(negative), 3)
            self.assertIn("blonde hair", positive[0]["char_caption"])
            self.assertIn("wedding dress", positive[0]["char_caption"])
            self.assertNotIn("black bodysuit", positive[0]["char_caption"])
            self.assertNotIn("wedding dress", negative[0]["char_caption"])
            self.assertIn("red hair, green eyes, black suit", positive[1]["char_caption"])
            self.assertNotIn("blonde hair", positive[1]["char_caption"])
            self.assertIn("wedding dress", negative[1]["char_caption"])
            self.assertEqual(positive[2]["char_caption"], "hatsune miku, aqua hair, twintails, singing")
            self.assertEqual(negative[2]["char_caption"], "")
            self.assertNotIn("green eyes", params["negative_prompt"])
            self.assertNotIn("director_reference_images", params)
            self.assertEqual(json.dumps(p.presets), before)
            p._load_reference.assert_awaited_once_with(None)
            p._post_image_request.assert_awaited_once()
        asyncio.run(run())

    def test_multi_subject_openai_and_v3_prompts_scope_negative_traits(self):
        async def run(provider):
            p = self.make_plugin()
            p.config.update({"default_preset_id": "p1", "provider": provider, "model": "nai-diffusion-3"})
            p._call_openai = AsyncMock(return_value="image.png")
            p._call_official = AsyncMock(return_value="image.png")
            p._load_reference = AsyncMock(return_value=(None, None))
            p._finish_event = AsyncMock(return_value="done")
            await p.novelai_generate_image(
                FakeEvent("画你和初音合影"), "park",
                subjects='[{"source":"self","prompt":"smiling"},{"source":"custom","prompt":"hatsune miku, aqua hair"}]',
            )
            call = p._call_openai if provider == "openai_compatible" else p._call_official
            args = call.await_args.args
            self.assertIn("Subject 1:", args[0])
            self.assertIn("Subject 2:", args[0])
            self.assertIn("Avoid for subject 1 only: extra arms", args[0])
            self.assertEqual(args[1], "generate")
            self.assertEqual(args[4 if provider == "openai_compatible" else 5], "")
        for provider in ("openai_compatible", "novelai_official"):
            asyncio.run(run(provider))

    def test_invalid_subjects_do_not_generate_or_fall_back_to_default(self):
        async def run(subjects, operation="generate"):
            p = self.make_plugin()
            p.config["default_preset_id"] = "p1"
            p._run_job = AsyncMock()
            p._notify = AsyncMock()
            result = await p.novelai_generate_image(FakeEvent(), "portrait", operation=operation, subjects=subjects)
            self.assertIn("No image request was sent", result)
            p._run_job.assert_not_awaited()
        for subjects in (
            "invalid", "{}", "[1]", '[{"source":"unknown"}]', '[{"source":[]}]', '[{"source":{}}]',
            '[{"source":"card","preset_id":"missing"}]', '[{"source":"self","preset_id":"other-card"}]',
            '[{"source":"self","appearance_prompt":null}]',
            '[{"source":"self","negative_prompt":false}]', '[{"source":"self","use_reference":"false"}]',
            '[{"source":"self","state_scope":[]}]', '[{"source":"self","reset":true}]',
            '[{"source":"self","outfit_prompt":"white dress"}]',
            json.dumps([{"source": "custom", "prompt": "person"}] * 7),
        ):
            with self.subTest(subjects=subjects):
                asyncio.run(run(subjects))
        asyncio.run(run('[{"source":"self"},{"source":"custom","prompt":"hatsune miku"}]', "reference"))

    def test_drawing_identity_context_exposes_self_and_enabled_card_catalog(self):
        async def run():
            p = self.make_plugin()
            p.config["default_preset_id"] = "p1"
            p.presets.append({"id": "disabled", "name": "Hidden", "enabled": False})
            req = SimpleNamespace(system_prompt="Original persona")
            await p.drawing_identity_context(FakeEvent(), req)
            self.assertEqual(req.system_prompt, "Original persona")
            self.assertIn('"self_card": {"id": "p1", "name": "P1"}', self.drawing_context(req))
            self.assertIn("source=custom", self.drawing_context(req))
            self.assertNotIn("Hidden", self.drawing_context(req))
            self.assertNotIn("blonde hair", self.drawing_context(req))
            previous = self.drawing_context(req)
            await p.drawing_identity_context(FakeEvent(), req)
            self.assertEqual(self.drawing_context(req), previous)
            self.assertEqual(len(req.extra_user_content_parts), 1)
            p.config["invoke_mode"] = "command_only"
            req.system_prompt = "Original persona"
            await p.drawing_identity_context(FakeEvent(), req)
            self.assertEqual(req.system_prompt, "Original persona")
            self.assertEqual(req.extra_user_content_parts, [])
        asyncio.run(run())

    def test_explicit_self_selection_uses_resolved_card_without_keyword_guessing(self):
        async def run():
            p = self.make_plugin()
            p._resolve_active_preset = AsyncMock(return_value=p.presets[0])
            p._call_official = AsyncMock(return_value="image.png")
            p._load_reference = AsyncMock(return_value=(b"reference", {"image_b64": "abc"}))
            p._finish_event = AsyncMock(return_value="done")
            await p.novelai_generate_image(
                FakeEvent("来张自拍"), "portrait", subjects='[{"source":"self","prompt":"smiling"}]',
            )
            args = p._call_official.await_args.args
            self.assertIn("blonde hair", args[0])
            self.assertIn("smiling", args[0])
            self.assertEqual(args[1], "reference")
            p._resolve_active_preset.assert_awaited_once()
        asyncio.run(run())

    def test_drawing_context_is_temporary_in_real_provider_assembly_and_history(self):
        async def run():
            p = self.make_context_plugin()
            original_system = "Original persona\nKeep the original system whitespace.\n"
            history = [{"role":"user", "content":"Earlier request"}, {"role":"assistant", "content":"Earlier response"}]
            req = ProviderRequest(prompt="画你在窗前微笑", system_prompt=original_system,
                contexts=history.copy(), extra_user_content_parts=[TextPart(text="Other plugin context")])
            await p.drawing_identity_context(FakeEvent(req.prompt), req)
            self.assertEqual(req.system_prompt, original_system)
            self.assertEqual(req.contexts, history)
            self.assertEqual(req.prompt, "画你在窗前微笑")
            assembled = await req.assemble_context()
            self.assertIn("[NovelAI drawing identities]", drawing.text_content(assembled["content"]))
            saved = dump_messages_with_checkpoints([Message.model_validate(assembled)])
            saved_text = drawing.text_content(saved[0]["content"])
            self.assertEqual(saved_text, "画你在窗前微笑\nOther plugin context")
            self.assertNotIn("NovelAI drawing identities", json.dumps(saved))
            self.assertNotIn("identity_prompt", self.drawing_context(req))
            self.assertNotIn("style_prompt", self.drawing_context(req))
        asyncio.run(run())

    def test_legacy_system_injection_is_removed_without_overwriting_other_plugins(self):
        async def run():
            p = self.make_context_plugin()
            req = ProviderRequest(prompt="继续", system_prompt="Persona\n[NovelAI drawing identities]\nold card rules\n[End NovelAI drawing identities]\nOther plugin rules",
                extra_user_content_parts=[TextPart(text="Other temporary content").mark_as_temp()])
            await p.drawing_identity_context(FakeEvent(), req)
            self.assertEqual(req.system_prompt, "Persona\n\nOther plugin rules")
            self.assertEqual(req.extra_user_content_parts[0].text, "Other temporary content")
            self.assertEqual(len(req.extra_user_content_parts), 2)
            p.presets[0]["name"] = "Updated card"
            await p.drawing_identity_context(FakeEvent(), req)
            self.assertEqual(len(req.extra_user_content_parts), 2)
            self.assertIn("Updated card", self.drawing_context(req))
            self.assertNotIn("old card rules", req.system_prompt)
            self.assertEqual(req.system_prompt, "Persona\n\nOther plugin rules")
        asyncio.run(run())

    def test_completed_tool_result_reports_this_scene_without_persona_reply_template(self):
        async def run():
            p = self.make_context_plugin()
            summaries = []
            for index, scene in enumerate(("window, smiling, standing", "garden, running, laughing", "appearance, " * 100 + "dancing beside the lake")):
                result = await p.novelai_generate_image(self.drawing_event("画当前画面", str(index)), scene, subjects=[{"source":"self"}])
                summary = json.loads(result.partition(": ")[2])
                self.assertEqual(summary["status"], "completed")
                if len(scene) <= 800:
                    self.assertEqual(summary["requested_scene"], scene)
                else:
                    self.assertTrue(summary["requested_scene"].endswith("dancing beside the lake"))
                self.assertEqual(summary["request"], "画当前画面")
                self.assertNotIn("persona", result)
                self.assertNotIn("reply to the user once", result)
                summaries.append(summary)
            self.assertNotEqual(summaries[0]["requested_scene"], summaries[1]["requested_scene"])
        asyncio.run(run())

    def test_multi_subject_shared_style_change_preserves_other_subject_features(self):
        p = self.make_plugin()
        characters = p._compose_character_prompts(
            [{"preset": p.presets[0], "prompt": "wedding dress"}, {"preset": p.presets[0], "prompt": "standing"}],
            FakeEvent("你换婚纱，另一人不换衣服，画风改为写实"), "realistic",
        )
        for character in characters:
            self.assertNotIn("watercolor", character["prompt"])
            self.assertIn("blonde hair", character["prompt"])
        self.assertNotIn("black bodysuit", characters[0]["prompt"])
        self.assertIn("black bodysuit", characters[1]["prompt"])

    def test_disabled_card_is_rejected_instead_of_using_default_card(self):
        async def run():
            p = self.make_plugin()
            p.presets[0]["enabled"] = False
            p._run_job = AsyncMock()
            p._notify = AsyncMock()
            result = await p.novelai_generate_image(FakeEvent(), "portrait", subjects='[{"source":"card","preset_id":"p1"}]')
            self.assertIn("已禁用", result)
            p._run_job.assert_not_awaited()
        asyncio.run(run())

    def test_llm_request_reaches_provider_with_anchors_and_bound_reference(self):
        async def run():
            p = self.make_plugin()
            p.config["default_preset_id"] = "p1"
            p._load_reference = AsyncMock(return_value=(b"reference", {"id": "r1", "reference_type": "style"}))
            p._call_official = AsyncMock(return_value="image.png")
            p._finish_event = AsyncMock(return_value="done")
            event = FakeEvent("宝宝，画给我看看你现在的样子")
            result = await p.novelai_generate_image(
                event,
                "full body, pink hair, wedding dress, looking at viewer",
            )
            self.assertIn("FINAL_IMAGE_TOOL_RESULT", result)
            args = p._call_official.await_args.args
            self.assertTrue(args[0].startswith("1.35::watercolor, blue eyes"))
            self.assertNotIn("pink hair", args[0])
            self.assertNotIn("wedding dress", args[0])
            self.assertEqual(args[1], "reference")
            self.assertIsNotNone(args[4])
            self.assertEqual(args[4]["reference_type"], "style")
            p._finish_event.assert_awaited_once()
        asyncio.run(run())

    def test_repeated_llm_tool_call_keeps_followup_result_but_uses_provider_once(self):
        async def run():
            p = self.make_plugin()
            p.config["default_preset_id"] = "p1"
            p.config["llm_auto_reference"] = False
            p._call_official = AsyncMock(return_value="image.png")
            p._load_reference = AsyncMock(return_value=(None, None))
            p._finish_event = AsyncMock(return_value="图片已发送")
            event = FakeEvent("给我画一张你现在的样子")
            first = await p.novelai_generate_image(event, "full body, standing")
            second = await p.novelai_generate_image(event, "full body, standing")
            self.assertEqual(json.loads(first.partition(": ")[2])["status"], "completed")
            self.assertIn("ENTRY_GATE_BLOCKED_DUPLICATE", second)
            self.assertEqual(p._call_official.await_count, 1)
        asyncio.run(run())

    def test_llm_entry_gate_blocks_before_role_card_resolution_and_job_creation(self):
        async def run():
            p = self.make_plugin()
            p._resolve_active_preset = AsyncMock(return_value=None)
            p._run_job = AsyncMock(
                return_value=GenerationResult(True, "job-1", "novelai_official", send_image=False)
            )
            p._finish_event = AsyncMock(return_value="图片已发送")
            event = FakeEvent("给我画一张你现在的样子")
            first, second = await asyncio.gather(
                p.novelai_generate_image(event, "full body, standing"),
                p.novelai_generate_image(event, "different prompt"),
            )
            self.assertIn("FINAL_IMAGE_TOOL_RESULT", first)
            self.assertIn("ENTRY_GATE_BLOCKED_DUPLICATE", second)
            self.assertEqual(p._resolve_active_preset.await_count, 1)
            self.assertEqual(p._run_job.await_count, 1)

        asyncio.run(run())

    def test_role_card_command_can_list_show_switch_and_clear_persona_binding(self):
        async def run():
            p = self.make_plugin()
            p._save_config = Mock()
            p._resolve_active_persona_id = AsyncMock(return_value="persona-a")
            listing = await p._handle_role_card_command(FakeEvent(), "list")
            self.assertIn("P1", listing)
            switched = await p._handle_role_card_command(FakeEvent(), "use P1")
            self.assertIn("已将", switched)
            self.assertEqual(p.config["persona_preset_map"]["persona-a"], "p1")
            current = await p._handle_role_card_command(FakeEvent(), "current")
            self.assertIn("正向 Tag", current)
            cleared = await p._handle_role_card_command(FakeEvent(), "clear")
            self.assertIn("已解除", cleared)
            self.assertNotIn("persona-a", p.config["persona_preset_map"])
            p.config["default_preset_id"] = "p1"
            fallback = await p._handle_role_card_command(FakeEvent(), "current")
            self.assertIn("未单独绑定", fallback)
            no_binding = await p._handle_role_card_command(FakeEvent(), "clear")
            self.assertIn("默认角色卡未更改", no_binding)

        asyncio.run(run())

    def test_model_command_lists_and_persists_supported_nai_model(self):
        p = self.make_plugin()
        p._save_config = Mock()
        listing = p._handle_model_command("list")
        self.assertIn("nai-diffusion-5-full", listing)
        result = p._handle_model_command("use v4.5-curated")
        self.assertIn("V4.5 Curated", result)
        self.assertEqual(p.config["model"], "nai-diffusion-4-5-curated")
        p._save_config.assert_called_once()

    def test_nai_without_arguments_shows_all_commands_and_obeys_command_permission(self):
        async def collect(plugin, event):
            return [item async for item in plugin.cmd_draw(event)]

        async def run():
            p = self.make_plugin()
            help_results = await collect(p, FakeEvent("/nai"))
            self.assertEqual(len(help_results), 1)
            self.assertIn("card current", help_results[0])
            self.assertIn("model use", help_results[0])

            denied_event = FakeEvent("/nai card list")
            denied_event.role = "member"
            denied_results = await collect(p, denied_event)
            self.assertIn("仅允许管理员", denied_results[0])

            p.config["invoke_mode"] = "llm_tool_only"
            unrelated = await collect(p, FakeEvent("/help"))
            self.assertEqual(unrelated, [])

        asyncio.run(run())

    def test_nai_raw_command_sends_only_an_image_with_bound_card_and_no_chat_model(self):
        async def run():
            p = self.make_context_plugin()
            del p._finish_event
            p.config.update({"auto_clean_delay":0, "command_prefix":"paint", "show_queue_notice":True})
            p.context.llm_generate = AsyncMock()
            p.context.get_current_chat_provider_id = AsyncMock()
            event = FakeEvent("/nai@Painter white dress, smiling, window\nsoft lighting")
            replies = [value async for value in p.cmd_draw(event)]
            self.assertEqual(replies, [])
            self.assertFalse(event.call_llm)
            self.assertTrue(event.stopped)
            self.assertEqual(len(event.sent), 1)
            self.assertTrue(all(isinstance(component, Image) for component in event.sent[0].chain))
            args = p._call_official.await_args.args
            self.assertIn("blonde hair", args[0])
            self.assertIn("white dress, smiling, window\nsoft lighting", args[0])
            self.assertNotIn("black bodysuit", args[0])
            self.assertIn("extra arms", args[5])
            self.assertEqual(args[1], "generate")
            p.context.llm_generate.assert_not_awaited()
            p.context.get_current_chat_provider_id.assert_not_awaited()
            self.assertFalse(p._ledger().states)
        asyncio.run(run())

    def test_nai_raw_command_respects_story_outfit_without_saving_image_only_changes(self):
        async def run():
            p = self.make_context_plugin()
            event = self.drawing_event("剧情中已换上和服", "story")
            await p.novelai_generate_image(event, "room", subjects=[{"source":"self", "outfit":"kimono", "state_scope":"story"}])
            self.persist_turn(p, event)
            async for _ in p.cmd_draw(self.drawing_event("/nai white dress, garden", "n-1")): pass
            self.assertIn("white dress", p._call_official.await_args.args[0])
            self.assertNotIn("kimono", p._call_official.await_args.args[0])
            async for _ in p.cmd_draw(self.drawing_event("/nai window, smiling", "n-2")): pass
            self.assertIn("kimono", p._call_official.await_args.args[0])
            self.assertNotIn("white dress", p._call_official.await_args.args[0])
        asyncio.run(run())

    def test_nai_raw_command_without_card_uses_raw_prompt_and_failure_does_not_start_chat(self):
        async def run():
            p = self.make_context_plugin()
            p.config["default_preset_id"] = ""
            del p._finish_event
            p._call_official.side_effect = ProviderError("network", "test service unavailable")
            event = FakeEvent("/nai starry sky, mountains")
            async for _ in p.cmd_draw(event): pass
            self.assertFalse(event.call_llm)
            self.assertTrue(event.stopped)
            self.assertEqual(p._call_official.await_args.args[0], "starry sky, mountains")
            self.assertEqual(len(event.sent), 1)
            self.assertFalse(any(isinstance(component, Image) for component in event.sent[0].chain))
        asyncio.run(run())

    def test_nai_raw_command_explicit_style_replaces_card_style_for_this_image(self):
        async def run():
            p = self.make_context_plugin()
            async for _ in p.cmd_draw(self.drawing_event("/nai oil painting, smiling", "oil")): pass
            self.assertIn("oil painting", p._call_official.await_args.args[0])
            self.assertIn("blonde hair", p._call_official.await_args.args[0])
            self.assertNotIn("watercolor", p._call_official.await_args.args[0])
            async for _ in p.cmd_draw(self.drawing_event("/nai window, smiling", "default")): pass
            self.assertIn("watercolor", p._call_official.await_args.args[0])
        asyncio.run(run())

    def test_nai_raw_command_blocks_llm_for_help_permission_and_disabled_entry(self):
        async def run():
            for command, mode, role in (("/n", "both", "admin"), ("/nai window", "both", "member"),
                ("/nai window", "llm_tool_only", "admin"), ("/nai " + "x" * 8001, "both", "admin")):
                p = self.make_context_plugin()
                p.config["invoke_mode"] = mode
                p._run_job = AsyncMock()
                p._notify = AsyncMock()
                event = FakeEvent(command); event.role = role
                async for _ in p.cmd_draw(event): pass
                self.assertFalse(event.call_llm)
                self.assertTrue(event.stopped)
                p._run_job.assert_not_awaited()
                p._notify.assert_awaited_once()
        asyncio.run(run())

    def test_nai_raw_command_is_fixed_and_does_not_consume_other_command_names(self):
        async def run():
            p = self.make_context_plugin()
            p.config["command_prefix"] = "n"
            p._run_job = AsyncMock(return_value=GenerationResult(True, "job", "novelai_official", send_image=False))
            async for _ in p.cmd_draw(FakeEvent("/nai window")): pass
            self.assertEqual(p._run_job.await_args.args[1], "window")
            help_replies = [value async for value in p.cmd_draw(FakeEvent("/nai"))]
            self.assertIn("/n [自然语言要求]", help_replies[0])
            self.assertIn("/nai card list", help_replies[0])
            event = FakeEvent("/novel other command")
            async for _ in p.cmd_draw(event): pass
            self.assertTrue(event.call_llm)
            self.assertFalse(event.stopped)
        asyncio.run(run())

    def test_job_records_can_be_deleted_individually_or_cleared(self):
        async def run():
            p = self.make_plugin()
            p._jobs = [{"job_id": "one"}, {"job_id": "two"}]
            with patch("main.request", SimpleNamespace(json=AsyncMock(return_value={"action": "delete", "job_id": "one"}))):
                await p.page_manage_jobs()
            self.assertEqual([job["job_id"] for job in p._jobs], ["two"])

            with patch("main.request", SimpleNamespace(json=AsyncMock(return_value={"action": "clear"}))):
                await p.page_manage_jobs()
            self.assertEqual(p._jobs, [])

        asyncio.run(run())

    def test_command_keeps_complete_prompt_and_does_not_mutate_reference_type(self):
        async def run():
            p = self.make_plugin()
            p.config["default_preset_id"] = "p1"
            p._run_job = AsyncMock(return_value=GenerationResult(True, "job-1", "novelai_official", send_image=False))
            p._finish_event = AsyncMock()
            event = FakeEvent("/nai draw 1girl, blonde hair, blue eyes")
            command_results = [item async for item in p.cmd_draw(event)]
            self.assertEqual(command_results, [])
            self.assertEqual(p._run_job.await_args.args[1], "1girl, blonde hair, blue eyes")

            original_type = p.presets[0]["reference_type"]
            event = FakeEvent("/nai reference both standing in rain")
            async for _ in p.cmd_draw(event):
                pass
            self.assertEqual(p._run_job.await_args.args[5], "both")
            self.assertEqual(p.presets[0]["reference_type"], original_type)
        asyncio.run(run())

    def test_draw_self_and_card_draw_commands_select_only_requested_card(self):
        async def run(command, expects_card):
            p = self.make_plugin()
            p.config["default_preset_id"] = "p1"
            p._call_official = AsyncMock(return_value="image.png")
            p._load_reference = AsyncMock(return_value=(None, None))
            p._finish_event = AsyncMock(return_value="done")
            async for _ in p.cmd_draw(FakeEvent(command)):
                pass
            args = p._call_official.await_args.args
            self.assertEqual("extra arms" in args[5], expects_card)
            self.assertEqual(p.config["default_preset_id"], "p1")
        for command, expects_card in (
            ("/nai draw hatsune miku, aqua hair", False),
            ("/nai hatsune miku, aqua hair", True),
            ("/nai self standing", True),
            ("/nai card-draw p1 standing", True),
        ):
            with self.subTest(command=command):
                asyncio.run(run(command, expects_card))

    def test_webui_uses_embedded_confirmation_and_wraps_long_text(self):
        script = Path("pages/settings/app.js").read_text(encoding="utf-8")
        html = Path("pages/settings/index.html").read_text(encoding="utf-8")
        css = Path("pages/settings/style.css").read_text(encoding="utf-8")
        self.assertNotIn("window.confirm(", script)
        self.assertIn("askConfirmation", script)
        self.assertIn('role="alertdialog"', html)
        self.assertIn("overflow-wrap: anywhere", css)
        self.assertIn("width: min(420px, calc(100vw - 32px))", css)
        self.assertIn(".field > span", css)
        self.assertIn("word-break: break-word", css)
        self.assertIn('value="rate_limit_once"', html)
        self.assertIn('id="preset-lock-positive"', html)
        self.assertIn('id="preset-positive"', html)
        self.assertIn('id="preset-negative"', html)
        self.assertIn('id="clear-jobs"', html)
        self.assertIn("data-job-delete", script)
        self.assertIn('bridge.apiPost("jobs/manage"', script)
        self.assertNotIn('id="preset-lock-style"', html)
        self.assertNotIn('id="preset-character"', html)
        self.assertIn("preset-badges", script)
        self.assertIn('id="preset-enabled"', html)
        self.assertIn('data-config="llm_auto_reference"', html)
        self.assertIn('list="persona-options"', html)
        self.assertIn('enabled: $("#preset-enabled").checked', script)


if __name__ == "__main__":
    unittest.main()
