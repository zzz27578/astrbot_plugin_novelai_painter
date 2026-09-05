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
    def __init__(self): self.sent = []
    async def send(self, message): self.sent.append(message)
    def is_private_chat(self): return False
    def is_admin(self): return True
    def get_sender_id(self): return "u-1"
    def get_group_id(self): return "g-1"
    def get_message_str(self): return "1girl, silver hair, blue eyes, night city"


class CoreTests(unittest.TestCase):
    def make_plugin(self):
        p = object.__new__(NovelAIPainterPlugin)
        p.config = {"model":"nai-diffusion-5-full", "negative_prompt":DEFAULT_NEGATIVE, "quality_toggle":True, "width":832, "height":1216, "steps":28, "scale":5.0, "sampler":"k_euler_ancestral", "reference_strength":.6, "reference_fidelity":.6, "reference_information_extracted":1.0, "provider":"novelai_official", "invoke_mode":"both", "group_access":"admin_only", "admin_bypass":True, "private_access":"all", "dedupe_window_seconds":30, "queue_timeout":1, "retry_mode":"none", "retry_delay":1, "show_retry_notice":False, "auto_clean_delay":300}
        p.presets = [{"id":"p1", "name":"P1", "style_prompt":"watercolor", "character_prompt":"blue eyes", "negative_prompt":"extra arms", "reference_id":"r1", "reference_type":"style", "lock_style":True, "lock_character":True, "style_strength":1.35, "character_strength":1.25, "quality_override":"off"}]
        p._recent_jobs = {}
        p._delivered_jobs = {}
        p._jobs = []
        p.lock = asyncio.Lock()
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        p.temp_dir = Path(temp_dir.name)
        p._get_preset = lambda pid: next((x for x in p.presets if x.get("id") == pid), None)
        p._resolve_persona_id = lambda event=None: ""
        return p

    def test_prompt_and_negative_preset(self):
        p = self.make_plugin(); p.config["default_preset_id"] = "p1"
        prompt, pid, negative = p._compose_prompt("a city", FakeEvent())
        self.assertEqual(pid, "p1")
        self.assertTrue(prompt.startswith("1.35::watercolor::"))
        self.assertIn("1.25::blue eyes::", prompt)
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
            prompt, preset_id, negative = p._compose_prompt("running in rain", FakeEvent())
            p._post_image_request = AsyncMock(return_value="image.png")
            await p._call_official(prompt, "generate", "job-1", None, None, negative, p._get_preset(preset_id))
            payload = p._post_image_request.await_args.args[2]
            self.assertTrue(payload["input"].startswith("1.35::watercolor::"))
            self.assertEqual(payload["parameters"]["n_samples"], 1)
            self.assertFalse(payload["parameters"]["qualityToggle"])
        asyncio.run(run())

    def test_openai_preset_uses_explicit_fixed_instructions(self):
        p = self.make_plugin()
        p.config["provider"] = "openai_compatible"
        p.config["default_preset_id"] = "p1"
        prompt, preset_id, _ = p._compose_prompt("running in rain", FakeEvent())
        self.assertEqual(preset_id, "p1")
        self.assertIn("Art style (FIXED; do not reinterpret or replace): watercolor", prompt)
        self.assertIn("Character design (FIXED; preserve identity", prompt)
        self.assertIn("Requested change: running in rain", prompt)

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
            self.assertEqual(result, "ok")
            p._run_job.assert_awaited_once_with(
                event,
                "1girl, silver hair, blue eyes, night city",
                "generate",
            )

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
        self.assertIn('id="preset-lock-style"', html)
        self.assertIn("preset-badges", script)


if __name__ == "__main__":
    unittest.main()
