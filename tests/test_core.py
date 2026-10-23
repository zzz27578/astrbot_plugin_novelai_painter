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

from main import DEFAULT_NEGATIVE, GenerationResult, NovelAIPainterPlugin, ProviderError
from astrbot.api.provider import LLMResponse
from astrbot.core.message.message_event_result import MessageChain


class FakeEvent:
    unified_msg_origin = "test:group:1"
    role = "admin"
    message_obj = SimpleNamespace(message_id="m-1")
    def __init__(self, message="1girl, silver hair, blue eyes, night city"):
        self.sent = []
        self.message = message
        self.result = None
        self.extras = {}
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
        ))
        p._call_official = AsyncMock(return_value="image.png")
        p._load_reference = AsyncMock(return_value=(None, None))
        p._finish_event = AsyncMock(return_value="done")
        return p

    def drawing_event(self, message, message_id):
        event = FakeEvent(message)
        event.message_obj = SimpleNamespace(message_id=message_id)
        return event

    def test_contextual_outfit_continues_across_turns_and_can_restore_anchor(self):
        async def run():
            p = self.make_context_plugin()
            p.presets[0]["negative_prompt"] = "white dress, extra arms"
            before = json.dumps(p.presets)
            appearance = "watercolor, blonde hair, blue eyes, white dress"
            await p.novelai_generate_image(
                self.drawing_event("换上白裙画一张", "change-outfit"), "bedroom",
                subjects=json.dumps([{"source": "self", "appearance_prompt": appearance, "negative_prompt": "extra arms", "prompt": "smiling"}]),
            )
            first = p._call_official.await_args.args
            self.assertIn(appearance, first[0])
            self.assertNotIn("black bodysuit", first[0])
            self.assertNotIn("same outfit", first[0])
            self.assertNotIn("white dress", first[5])
            self.assertEqual(first[1], "generate")
            await p.novelai_generate_image(
                self.drawing_event("继续，走到窗边再画一张", "continue-scene"), "window, sunlight",
                subjects='[{"source":"self","prompt":"standing"}]',
            )
            continued = p._call_official.await_args.args
            self.assertIn(appearance, continued[0])
            self.assertNotIn("black bodysuit", continued[0])
            self.assertNotIn("white dress", continued[5])
            self.assertNotIn("smiling", continued[0])
            await p.novelai_generate_image(
                self.drawing_event("换回默认衣服", "restore-outfit"), "bedroom",
                subjects=json.dumps([{"source": "self", "appearance_prompt": p.presets[0]["positive_prompt"], "negative_prompt": p.presets[0]["negative_prompt"], "prompt": "standing"}]),
            )
            restored = p._call_official.await_args.args
            self.assertIn("black bodysuit", restored[0])
            self.assertNotIn("white dress", restored[0])
            self.assertEqual(json.dumps(p.presets), before)
        asyncio.run(run())

    def test_contextual_states_are_isolated_by_session_dialogue_and_card(self):
        async def run():
            p = self.make_context_plugin()
            await p.novelai_generate_image(
                self.drawing_event("换装", "first"), "room",
                subjects='[{"source":"self","appearance_prompt":"blonde hair, white dress"}]',
            )
            for scope_change in ("session", "dialogue", "edited-card"):
                event = self.drawing_event("继续画", scope_change)
                p.context.conversation_manager.get_curr_conversation_id.return_value = "conversation-1"
                if scope_change == "session":
                    event.unified_msg_origin = "test:group:2"
                elif scope_change == "dialogue":
                    p.context.conversation_manager.get_curr_conversation_id.return_value = "conversation-2"
                else:
                    p.presets[0]["positive_prompt"] = "green eyes, black suit"
                await p.novelai_generate_image(event, "room", subjects='[{"source":"self","prompt":"standing"}]')
                self.assertNotIn("white dress", p._call_official.await_args.args[0])
            p.presets.append({"id": "p2", "name": "Second", "positive_prompt": "red hair, green eyes, kimono"})
            await p.novelai_generate_image(
                self.drawing_event("画另一个角色", "other-card"), "room",
                subjects='[{"source":"card","preset_id":"p2","prompt":"sitting"}]',
            )
            self.assertIn("kimono", p._call_official.await_args.args[0])
            self.assertNotIn("white dress", p._call_official.await_args.args[0])
        asyncio.run(run())

    def test_failed_or_duplicate_generation_does_not_commit_new_appearance(self):
        async def run():
            p = self.make_context_plugin()
            p._call_official.side_effect = ProviderError("network", "test failure")
            subjects = '[{"source":"self","appearance_prompt":"blonde hair, white dress"}]'
            await p.novelai_generate_image(self.drawing_event("换装", "failed"), "room", subjects=subjects)
            self.assertFalse(getattr(p, "_appearance_states", {}))
            p._call_official.side_effect = None
            event = self.drawing_event("画默认衣服", "successful")
            await p.novelai_generate_image(event, "room", subjects='[{"source":"self"}]')
            state = dict(p._current_appearance((event.unified_msg_origin, "conversation-1"), p.presets[0]))
            await p.novelai_generate_image(event, "room", subjects=subjects)
            self.assertEqual(p._current_appearance((event.unified_msg_origin, "conversation-1"), p.presets[0]), state)
            self.assertIn("black bodysuit", state["positive_prompt"])
        asyncio.run(run())

    def test_dialogue_change_during_generation_does_not_store_appearance(self):
        async def run():
            p = self.make_context_plugin()
            async def switch_dialogue(*args, **kwargs):
                p.context.conversation_manager.get_curr_conversation_id.return_value = "conversation-2"
                return "image.png"
            p._call_official.side_effect = switch_dialogue
            await p.novelai_generate_image(
                self.drawing_event("换装", "switch-during-generation"), "room",
                subjects='[{"source":"self","appearance_prompt":"blonde hair, white dress"}]',
            )
            self.assertFalse(getattr(p, "_appearance_states", {}))
        asyncio.run(run())

    def test_contextual_reference_requires_explicit_model_opt_in(self):
        async def run():
            p = self.make_context_plugin()
            p._load_reference.return_value = (b"reference", {"image_b64": "abc"})
            await p.novelai_generate_image(
                self.drawing_event("画你穿白裙", "no-reference"), "room",
                subjects='[{"source":"self","appearance_prompt":"blonde hair, white dress"}]',
            )
            self.assertEqual(p._call_official.await_args.args[1], "generate")
            self.assertIsNone(p._call_official.await_args.args[4])
            await p.novelai_generate_image(
                self.drawing_event("以参考图外观画你", "with-reference"), "room",
                subjects='[{"source":"self","use_reference":true}]',
            )
            self.assertEqual(p._call_official.await_args.args[1], "reference")
            self.assertIsNotNone(p._call_official.await_args.args[4])
        asyncio.run(run())

    def test_contextual_multicharacter_prompt_preserves_each_resolved_outfit(self):
        async def run():
            p = self.make_context_plugin()
            p.presets.append({"id": "p2", "name": "Other", "positive_prompt": "red hair, uniform", "negative_prompt": "kimono"})
            subjects = [
                {"source": "self", "appearance_prompt": "blonde hair, white dress", "negative_prompt": "extra arms", "prompt": "smiling"},
                {"source": "card", "preset_id": "p2", "appearance_prompt": "red hair, kimono", "negative_prompt": "", "prompt": "waving"},
            ]
            await p.novelai_generate_image(self.drawing_event("画两人一起散步", "multi"), "2girls, park", subjects=json.dumps(subjects))
            characters = p._call_official.await_args.kwargs["character_prompts"]
            self.assertEqual(characters[0]["prompt"], "blonde hair, white dress, smiling")
            self.assertEqual(characters[1]["prompt"], "red hair, kimono, waving")
            self.assertEqual(characters[1]["negative_prompt"], "")
            await p.novelai_generate_image(
                self.drawing_event("继续走", "multi-next"), "2girls, street",
                subjects='[{"source":"self","prompt":"walking"},{"source":"card","preset_id":"p2","prompt":"walking"}]',
            )
            characters = p._call_official.await_args.kwargs["character_prompts"]
            self.assertIn("white dress", characters[0]["prompt"])
            self.assertIn("kimono", characters[1]["prompt"])
            self.assertNotIn("uniform", characters[1]["prompt"])
        asyncio.run(run())

    def test_main_model_receives_initial_anchors_and_last_successful_appearance(self):
        async def run():
            p = self.make_context_plugin()
            await p.novelai_generate_image(
                self.drawing_event("换装", "context-first"), "room",
                subjects='[{"source":"self","appearance_prompt":"blonde hair, white dress","negative_prompt":"extra arms"}]',
            )
            request = SimpleNamespace(system_prompt="Original persona")
            await p.drawing_identity_context(FakeEvent(), request)
            self.assertTrue(request.system_prompt.startswith("Original persona"))
            self.assertIn("black bodysuit", request.system_prompt)
            self.assertIn('"current_appearance": {"positive_prompt": "blonde hair, white dress"', request.system_prompt)
            self.assertIn("不是必须还原的每轮制服", request.system_prompt)
            self.assertNotIn("anchor_signature", request.system_prompt)
        asyncio.run(run())

    def test_contextual_openai_does_not_apply_fixed_or_unchanged_outfit_instructions(self):
        async def run():
            p = self.make_context_plugin()
            p.config["provider"] = "openai_compatible"
            p._call_openai = AsyncMock(return_value="image.png")
            await p.novelai_generate_image(
                self.drawing_event("继续剧情画你现在的样子", "openai-context"), "room",
                subjects='[{"source":"self","appearance_prompt":"blonde hair, white dress","negative_prompt":""}]',
            )
            prompt = p._call_openai.await_args.args[0]
            self.assertIn("Current character appearance: blonde hair, white dress", prompt)
            self.assertNotIn("FIXED", prompt)
            self.assertNotIn("black bodysuit", prompt)
        asyncio.run(run())

    def test_contextual_self_without_bound_card_uses_model_resolved_appearance(self):
        async def run():
            p = self.make_context_plugin()
            p.config["default_preset_id"] = ""
            await p.novelai_generate_image(
                self.drawing_event("按现在的剧情画你", "self-without-card"), "room",
                subjects='[{"source":"self","appearance_prompt":"silver hair, green eyes, white dress","prompt":"smiling"}]',
            )
            prompt = p._call_official.await_args.args[0]
            self.assertIn("silver hair, green eyes, white dress", prompt)
            self.assertNotIn("black bodysuit", prompt)
            self.assertFalse(getattr(p, "_appearance_states", {}))
        asyncio.run(run())

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

    def test_only_429_is_retryable(self):
        limited = NovelAIPainterPlugin._http_error(429, b"", {"Retry-After": "7"})
        self.assertTrue(limited.retryable)
        self.assertEqual(limited.retry_after, 7)
        self.assertFalse(NovelAIPainterPlugin._http_error(401, b"").retryable)
        self.assertFalse(NovelAIPainterPlugin._http_error(503, b"").retryable)

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
        self.assertNotIn("style_prompt", migrated)
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
        self.assertEqual(tool.parameters["properties"]["subjects"]["type"], "string")

        async def run():
            p = self.make_plugin()
            p._mode_allows = lambda mode: mode == "llm_tool"
            p._run_job = AsyncMock(return_value=GenerationResult(True, "job-1", "novelai_official"))
            p._finish_event = AsyncMock(return_value="ok")
            event = FakeEvent()
            result = await p.novelai_generate_image(event)
            self.assertIn("FINAL_IMAGE_TOOL_RESULT", result)
            self.assertIn("reply to the user once", result)
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
        asyncio.run(run("画你现在的样子", subjects="[]"))

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
            "invalid", "{}", "[1]", '[{"source":"unknown"}]', '[{"source":"custom"}]',
            '[{"source":"card","preset_id":"missing"}]', '[{"source":"self","preset_id":"p1"}]',
            '[{"source":"self","appearance_prompt":""}]', '[{"source":"self","appearance_prompt":null}]',
            '[{"source":"self","negative_prompt":false}]', '[{"source":"self","use_reference":"false"}]',
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
            self.assertTrue(req.system_prompt.startswith("Original persona"))
            self.assertIn('"self_card": {"id": "p1", "name": "P1"}', req.system_prompt)
            self.assertIn("source=custom", req.system_prompt)
            self.assertNotIn("Hidden", req.system_prompt)
            self.assertNotIn("blonde hair", req.system_prompt)
            previous = req.system_prompt
            await p.drawing_identity_context(FakeEvent(), req)
            self.assertEqual(req.system_prompt, previous)
            p.config["invoke_mode"] = "command_only"
            req.system_prompt = "Original persona"
            await p.drawing_identity_context(FakeEvent(), req)
            self.assertEqual(req.system_prompt, "Original persona")
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
            self.assertIn("reply to the user once", first)
            self.assertIn("reply to the user once", second)
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
            self.assertEqual("blonde hair" in args[0], expects_card)
            self.assertEqual(p.config["default_preset_id"], "p1")
        for command, expects_card in (
            ("/nai draw hatsune miku, aqua hair", False),
            ("/nai hatsune miku, aqua hair", False),
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
