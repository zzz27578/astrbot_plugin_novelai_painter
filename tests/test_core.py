import asyncio
import base64
import io
import json
import zipfile
import unittest
from unittest.mock import AsyncMock, Mock
from pathlib import Path
from types import SimpleNamespace

from main import DEFAULT_NEGATIVE, GenerationResult, NovelAIPainterPlugin, ProviderError


class FakeEvent:
    unified_msg_origin = "test:group:1"
    role = "admin"
    message_obj = SimpleNamespace(message_id="m-1")
    def is_private_chat(self): return False
    def is_admin(self): return True
    def get_sender_id(self): return "u-1"
    def get_group_id(self): return "g-1"


class CoreTests(unittest.TestCase):
    def make_plugin(self):
        p = object.__new__(NovelAIPainterPlugin)
        p.config = {"model":"nai-diffusion-5-full", "negative_prompt":DEFAULT_NEGATIVE, "quality_toggle":True, "width":832, "height":1216, "steps":28, "scale":5.0, "sampler":"k_euler_ancestral", "reference_strength":.6, "reference_fidelity":.6, "reference_information_extracted":1.0, "provider":"novelai_official", "invoke_mode":"both", "group_access":"admin_only", "admin_bypass":True, "private_access":"all", "dedupe_window_seconds":30, "queue_timeout":1, "auto_clean_delay":300}
        p.presets = [{"id":"p1", "name":"P1", "style_prompt":"watercolor", "character_prompt":"blue eyes", "negative_prompt":"extra arms", "reference_id":"r1", "reference_type":"style"}]
        p._recent_jobs = {}
        p._jobs = []
        p.lock = asyncio.Lock()
        p.temp_dir = Path(".")
        p._get_preset = lambda pid: next((x for x in p.presets if x.get("id") == pid), None)
        p._resolve_persona_id = lambda event=None: ""
        return p

    def test_prompt_and_negative_preset(self):
        p = self.make_plugin(); p.config["default_preset_id"] = "p1"
        prompt, pid, negative = p._compose_prompt("a city", FakeEvent())
        self.assertEqual(pid, "p1")
        self.assertIn("watercolor", prompt)
        self.assertIn("blue eyes", prompt)
        self.assertEqual(negative, "extra arms")

    def test_official_reference_payload(self):
        p = self.make_plugin(); p.config["default_preset_id"] = "p1"
        params = p._official_parameters("a city", "reference", None, {"image_b64":"abc", "reference_type":"style"})
        self.assertEqual(params["director_reference_descriptions"][0]["caption"]["base_caption"], "style")
        self.assertEqual(params["n_samples"], 1)

    def test_http_errors_never_retry(self):
        self.assertFalse(ProviderError("429", "x", retryable=False).retryable)
        self.assertEqual(NovelAIPainterPlugin._http_error(429, b"").code, "429")
        self.assertEqual(NovelAIPainterPlugin._http_error(401, b"").code, "auth")

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
            first, second = await asyncio.gather(p._run_job(e, "a scene"), p._run_job(e, "a scene"))
            self.assertEqual(p._call_official.await_count, 1)
            self.assertEqual(first.ok, second.ok)
        asyncio.run(run())

    def test_event_key_stable(self):
        p = self.make_plugin(); e = FakeEvent()
        self.assertEqual(p._event_key(e, "x", "generate"), p._event_key(e, "x", "generate"))
        self.assertNotEqual(p._event_key(e, "x", "generate"), p._event_key(e, "x", "img2img"))

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


if __name__ == "__main__":
    unittest.main()
