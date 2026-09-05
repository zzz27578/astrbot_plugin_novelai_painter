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


class FakeEvent:
    unified_msg_origin = "test:group:1"
    role = "admin"
    message_obj = SimpleNamespace(message_id="m-1")
    def __init__(self, message="1girl, silver hair, blue eyes, night city"):
        self.sent = []
        self.message = message
    async def send(self, message): self.sent.append(message)
    def is_private_chat(self): return False
    def is_admin(self): return self.role == "admin"
    def get_sender_id(self): return "u-1"
    def get_group_id(self): return "g-1"
    def get_platform_name(self): return "aiocqhttp"
    def get_message_str(self): return self.message
    def plain_result(self, message): return message


class CoreTests(unittest.TestCase):
    def make_plugin(self):
        p = object.__new__(NovelAIPainterPlugin)
        p.config = {"model":"nai-diffusion-5-full", "negative_prompt":DEFAULT_NEGATIVE, "quality_toggle":True, "width":832, "height":1216, "steps":28, "scale":5.0, "sampler":"k_euler_ancestral", "reference_strength":.6, "reference_fidelity":.6, "reference_information_extracted":1.0, "provider":"novelai_official", "invoke_mode":"both", "group_access":"admin_only", "admin_bypass":True, "private_access":"all", "dedupe_window_seconds":30, "queue_timeout":1, "retry_mode":"none", "retry_delay":1, "show_retry_notice":False, "auto_clean_delay":300, "llm_auto_reference":True, "reference_enabled":True, "img2img_enabled":True, "persona_preset_map":{}}
        p.presets = [{"id":"p1", "name":"P1", "positive_prompt":"watercolor, blue eyes, blonde hair, side ponytail, black bodysuit", "negative_prompt":"extra arms", "reference_id":"r1", "reference_type":"style", "lock_positive":True, "positive_strength":1.35, "quality_override":"off"}]
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
            )
            p._finish_event.assert_awaited_once()

        asyncio.run(run())

    def test_llm_automatically_uses_bound_reference(self):
        p = self.make_plugin()
        p.config["default_preset_id"] = "p1"
        self.assertEqual(p._resolve_llm_operation("generate", p.presets[0]), "reference")
        p.config["llm_auto_reference"] = False
        self.assertEqual(p._resolve_llm_operation("generate", p.presets[0]), "generate")

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
