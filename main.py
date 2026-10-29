from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import io
import json
import mimetypes
import os
import random
import re
import shutil
import time
import uuid
import zipfile
from contextlib import suppress
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Optional

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
try:
    from astrbot.api.web import error_response, json_response, request
    WEB_API_AVAILABLE = True
except ImportError:
    WEB_API_AVAILABLE = False
    request = None
    def json_response(payload, status_code=200):
        return payload
    def error_response(message, status_code=400):
        return {"error": message, "status_code": status_code}

from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.core.agent.message import TextPart
from astrbot.core.message.components import Image
from astrbot.core.message.message_event_result import MessageChain, ResultContentType
if __package__:
    from . import drawing, planner, diagnostics, planner_transport, drawing_runtime, drawing_session
else:  # Standalone test discovery.
    import drawing
    import planner
    import diagnostics
    import planner_transport
    import drawing_runtime
    import drawing_session

PLUGIN_NAME = "astrbot_plugin_novelai_painter"
VERSION = "2.11.0"
# Leave room before the 60-second tool deadline seen in the supplied logs.
IMAGE_TOOL_INLINE_SECONDS = 45
STICKER_EMOTION_TOOL_NAME = "novelai_set_emotion"
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_RESPONSE_BYTES = 48 * 1024 * 1024
DEFAULT_MODEL = "nai-diffusion-5-full"
NAI_MODELS = {
    "nai-diffusion-5-full": "V5 Full",
    "nai-diffusion-5-curated": "V5 Curated",
    "nai-diffusion-4-5-full": "V4.5 Full",
    "nai-diffusion-4-5-curated": "V4.5 Curated",
    "nai-diffusion-4-full": "V4 Full",
    "nai-diffusion-4-curated-preview": "V4 Curated Preview",
    "nai-diffusion-3": "V3 Anime",
    "nai-diffusion-furry-3": "V3 Furry",
}
NAI_MODEL_ALIASES = {
    "v5": "nai-diffusion-5-full",
    "v5-full": "nai-diffusion-5-full",
    "v5-curated": "nai-diffusion-5-curated",
    "v4.5": "nai-diffusion-4-5-full",
    "v4.5-full": "nai-diffusion-4-5-full",
    "v4.5-curated": "nai-diffusion-4-5-curated",
    "v4": "nai-diffusion-4-full",
    "v4-full": "nai-diffusion-4-full",
    "v4-curated": "nai-diffusion-4-curated-preview",
    "v3": "nai-diffusion-3",
    "v3-anime": "nai-diffusion-3",
    "v3-furry": "nai-diffusion-furry-3",
}
DEFAULT_NEGATIVE = (
    "lowres, blurry, bad anatomy, bad hands, text, watermark, error, missing fingers, "
    "extra digit, fewer digits, cropped, worst quality, low quality, jpeg artifacts, "
    "signature, username"
)
STICKER_DEFAULTS = {
    "sticker_enabled": False,
    "sticker_probability": 20,
    "sticker_send_mode": "after_reply",
    "sticker_cooldown_seconds": 60,
    "sticker_context_messages": 8,
    "sticker_llm_provider_id": "",
    "sticker_llm_timeout": 45,
    "sticker_width": 512,
    "sticker_height": 512,
    "sticker_auto_reference": True,
    "sticker_emotion_tool": True,
    "sticker_prompt": "chibi, reaction sticker, expressive face, upper body, simple background, white background, no text",
    "sticker_decision_prompt": "根据对话语境和回复情绪选择合适的表情，也可使用嘲笑、无奈、得意、委屈、震惊、吐槽等特殊表情。严肃或不适合插入表情包的场合不发送。",
    "sticker_role_card": {
        "name": "表情包角色卡", "positive_prompt": "", "negative_prompt": "",
        "lock_positive": True, "positive_strength": 1.35, "quality_override": "off",
        "reference_id": "", "reference_type": "character",
    },
}
CONFIG_KEYS = {
    "config_version", "provider", "api_token", "api_key", "base_url", "openai_base_url",
    "openai_image_endpoint", "openai_edit_endpoint", "openai_auth_header", "openai_auth_prefix",
    "model", "custom_model", "command_prefix", "invoke_mode", "private_access", "group_access",
    "allowed_users", "allowed_groups", "admin_bypass", "legacy_command_enabled", "width", "height",
    "steps", "scale", "sampler", "negative_prompt", "quality_toggle", "images_per_request",
    "max_api_requests_per_job", "dedupe_window_seconds", "queue_timeout", "retry_mode", "retry_delay", "retry_503_count",
    "error_notify_mode", "show_queue_notice", "notify_429", "show_retry_notice", "auto_clean_delay",
    "default_preset_id", "persona_preset_map", "llm_auto_reference", "llm_card_mode", "img2img_enabled",
    "reference_enabled", "img2img_strength", "img2img_noise", "img2img_color_correct",
    "reference_strength", "reference_fidelity", "reference_information_extracted",
} | STICKER_DEFAULTS.keys() | planner.DEFAULTS.keys()


@dataclass
class GenerationResult:
    ok: bool
    job_id: str
    provider: str
    path: Optional[str] = None
    error_code: Optional[str] = None
    message: str = ""
    attempts: int = 0
    send_image: bool = True


class ProviderError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after


class DrawingMarkerFilter(filter.CustomFilter):
    plugin = None

    def filter(self, event, cfg):
        plugin = self.plugin
        return bool(plugin and plugin._drawing_llm_id() and plugin._mode_allows("command")
            and planner.marker_request(event.get_message_str(), str(plugin._cfg("drawing_trigger", "n"))) is not None)


@register(PLUGIN_NAME, "AstrBot 社区", "NovelAI 生图与参考图工具", VERSION)
class NovelAIPainterPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        # Keep AstrBot's dict-like config object even when it is currently empty;
        # replacing it with a plain dict would make subsequent saves ineffective.
        self.config = config if config is not None else {}
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.temp_dir = self.data_dir / "temp"
        self.reference_dir = self.data_dir / "references"
        self.presets_path = self.data_dir / "presets.json"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.reference_dir.mkdir(parents=True, exist_ok=True)
        self.lock = asyncio.Lock()
        self._recent_jobs: dict[str, tuple[float, GenerationResult]] = {}
        self._inflight_jobs: dict[str, str] = {}
        self._llm_tool_claims: dict[str, float] = {}
        self._delivered_jobs: dict[str, float] = {}
        self._jobs: list[dict[str, Any]] = []
        self._image_tasks: set[asyncio.Task] = set()
        self._planner_tasks: dict[asyncio.Task, str] = {}
        self._story_ledger = drawing.StoryLedger()
        self._preset_schema_migrated = False
        self.presets = self._load_presets()
        self._ensure_defaults()
        self._cleanup_expired()
        self._sync_sticker_emotion_tool()
        self._sync_drawing_tool_schema()
        self._sync_marker_filter()
        if self._preset_schema_migrated:
            try:
                self._save_presets()
            except Exception as exc:
                logger.warning(f"[{PLUGIN_NAME}] 保存角色卡迁移结果失败: {exc}")
        self._migrate_embedded_persona_bindings()
        self._register_web_api()

    async def terminate(self):
        tasks = list(getattr(self, "_image_tasks", ())) + list(getattr(self, "_planner_tasks", ()))
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for key in list(getattr(self, "_drawing_memory", {})):
            self._drop_drawing_memory(key)
        self._sync_marker_filter(unbind=True)

    # --------------------------- configuration ---------------------------
    def _ensure_defaults(self) -> None:
        defaults = {
            "config_version": 7,
            "provider": "novelai_official",
            "api_token": "",
            "api_key": "",
            "base_url": "https://image.novelai.net",
            "openai_base_url": "",
            "openai_image_endpoint": "/v1/images/generations",
            "openai_edit_endpoint": "/v1/images/edits",
            "openai_auth_header": "Authorization",
            "openai_auth_prefix": "Bearer",
            "model": DEFAULT_MODEL,
            "custom_model": "",
            "command_prefix": "nai",
            "invoke_mode": "command_only",
            "private_access": "all",
            "group_access": "admin_only",
            "allowed_users": [],
            "allowed_groups": [],
            "admin_bypass": True,
            "legacy_command_enabled": True,
            "width": 832,
            "height": 1216,
            "steps": 28,
            "scale": 5.0,
            "sampler": "k_euler_ancestral",
            "negative_prompt": DEFAULT_NEGATIVE,
            "quality_toggle": True,
            "images_per_request": 1,
            "max_api_requests_per_job": 3,
            "dedupe_window_seconds": 30,
            "queue_timeout": 120,
            "retry_mode": "none",
            "retry_delay": 5.0,
            "retry_503_count": 0,
            "error_notify_mode": "final_only",
            "show_queue_notice": True,
            "notify_429": True,
            "show_retry_notice": False,
            "auto_clean_delay": 300,
            "default_preset_id": "",
            "persona_preset_map": {},
            "llm_auto_reference": True,
            "llm_card_mode": "context",
            "img2img_enabled": True,
            "reference_enabled": True,
            "img2img_strength": 0.7,
            "img2img_noise": 0.1,
            "img2img_color_correct": True,
            "reference_strength": 0.6,
            "reference_fidelity": 0.6,
            "reference_information_extracted": 1.0,
            **STICKER_DEFAULTS,
            **planner.DEFAULTS,
        }
        changed = False
        for key, value in defaults.items():
            if key not in self.config:
                self.config[key] = dict(value) if isinstance(value, dict) else value
                changed = True
        try:
            config_version = int(self.config.get("config_version", 0) or 0)
        except (TypeError, ValueError):
            config_version = 0
        if config_version < 5:
            self.config["config_version"] = 5
            self.config["images_per_request"] = 1
            self.config["max_api_requests_per_job"] = 3
            changed = True
        if config_version < 6:
            self.config["config_version"] = 6
            changed = True
        if config_version < 7:
            self.config["config_version"] = 7
            changed = True
        if changed:
            saver = getattr(self.config, "save_config", None)
            if callable(saver):
                try:
                    saver()
                except Exception as exc:
                    logger.warning(f"[{PLUGIN_NAME}] 保存默认配置失败: {exc}")

    def _cfg(self, key: str, default: Any = None) -> Any:
        value = self.config.get(key, default)
        return default if value is None else value

    @staticmethod
    def _as_bool(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "是", "开启"}
        return bool(value) if value is not None else default

    @staticmethod
    def _as_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            return [item.strip() for item in re.split(r"[,\\n]+", value) if item.strip()]
        return []

    def _active_model(self) -> str:
        model = str(self._cfg("model", DEFAULT_MODEL) or DEFAULT_MODEL).strip()
        custom = str(self._cfg("custom_model", "") or "").strip()
        if model == "custom":
            return custom or DEFAULT_MODEL
        return model or custom or DEFAULT_MODEL

    def _openai_headers(self, key: str) -> dict[str, str]:
        header = str(self._cfg("openai_auth_header", "Authorization") or "Authorization").strip() or "Authorization"
        prefix = str(self._cfg("openai_auth_prefix", "Bearer") or "").strip()
        if prefix and key.lower().startswith(prefix.lower() + " "):
            return {header: key}
        return {header: f"{prefix} {key}".strip()}

    def _provider_name(self) -> str:
        provider = str(self._cfg("provider", "novelai_official") or "novelai_official").strip()
        return provider if provider in {"novelai_official", "openai_compatible"} else "novelai_official"

    def _mode_allows(self, entry: str) -> bool:
        mode = str(self._cfg("invoke_mode", "command_only") or "command_only")
        return mode in {entry, entry + "_only", "both"}

    def _sync_marker_filter(self, unbind=False):
        from astrbot.core.star.star_handler import star_handlers_registry
        handler = star_handlers_registry.get_handler_by_full_name(type(self).__module__ + "_drawing_marker_event")
        for item in handler.event_filters if handler else ():
            if isinstance(item, DrawingMarkerFilter) and (not unbind or item.plugin is self):
                item.plugin = None if unbind else self

    # --------------------------- storage and presets ---------------------------
    def _load_presets(self) -> list[dict[str, Any]]:
        if not self.presets_path.exists():
            return []
        try:
            data = json.loads(self.presets_path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return []
            normalized: list[dict[str, Any]] = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                preset = dict(item)
                is_layered = preset.get("layers_version") == 1
                legacy_style = "" if is_layered else str(preset.get("style_prompt", "") or "").strip()
                legacy_character = str(preset.get("character_prompt", "") or "").strip()
                positive_prompt = str(preset.get("positive_prompt", "") or "").strip()
                if not positive_prompt:
                    positive_prompt = ", ".join(
                        part for part in (legacy_style, legacy_character) if part
                    )
                if any(key not in preset for key in ("positive_prompt", "lock_positive", "positive_strength")) or any(
                    key in preset
                    for key in (
                        "character_prompt", "lock_style",
                        "lock_character", "style_strength", "character_strength",
                    )
                ):
                    self._preset_schema_migrated = True
                preset["positive_prompt"] = positive_prompt
                preset["lock_positive"] = self._as_bool(
                    preset.get(
                        "lock_positive",
                        self._as_bool(preset.get("lock_style", True), True)
                        or self._as_bool(preset.get("lock_character", True), True),
                    ),
                    True,
                )
                preset["enabled"] = self._as_bool(preset.get("enabled", True), True)
                preset["positive_strength"] = self._preset_strength(
                    preset.get(
                        "positive_strength",
                        max(
                            self._preset_strength(preset.get("style_strength", 1.35), 1.35),
                            self._preset_strength(preset.get("character_strength", 1.25), 1.25),
                        ),
                    ),
                    1.35,
                )
                if str(preset.get("quality_override", "")) not in {"inherit", "on", "off"}:
                    preset["quality_override"] = "off" if positive_prompt else "inherit"
                for legacy_key in (
                    "character_prompt", "lock_style",
                    "lock_character", "style_strength", "character_strength",
                ):
                    preset.pop(legacy_key, None)
                if not is_layered:
                    preset.pop("style_prompt", None)
                layered = drawing.card_layers(preset)
                if layered != preset:
                    self._preset_schema_migrated = True
                normalized.append(layered)
            return normalized
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 读取角色卡失败: {exc}")
            return []

    def _save_presets(self) -> None:
        # Keep a one-time recovery copy before writing migrated layer data.
        backup = self.presets_path.with_name("presets.before-layers.json")
        if getattr(self, "_preset_schema_migrated", False) and self.presets_path.exists() and not backup.exists():
            backup.write_bytes(self.presets_path.read_bytes())
        tmp = self.presets_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.presets, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.presets_path)

    def _save_config(self) -> None:
        saver = getattr(self.config, "save_config", None)
        if callable(saver):
            saver()

    def _migrate_embedded_persona_bindings(self) -> None:
        """Make the preset editor's legacy persona_id field operational.

        persona_preset_map remains the canonical many-persona mapping. Older
        presets only stored persona_id, so copy non-conflicting bindings into
        the map during startup instead of silently ignoring them.
        """
        raw_mapping = self._cfg("persona_preset_map", {})
        mapping = dict(raw_mapping) if isinstance(raw_mapping, dict) else {}
        changed = not isinstance(raw_mapping, dict)
        for preset in self.presets:
            persona_id = str(preset.get("persona_id", "") or "").strip()
            preset_id = str(preset.get("id", "") or "").strip()
            if persona_id and preset_id and persona_id not in mapping:
                mapping[persona_id] = preset_id
                changed = True
        if changed:
            self.config["persona_preset_map"] = mapping
            try:
                self._save_config()
            except Exception as exc:
                logger.warning(f"[{PLUGIN_NAME}] 迁移人设角色卡映射失败: {exc}")

    def _sync_preset_persona_binding(self, preset: dict[str, Any], previous_persona_id: str = "") -> bool:
        """Synchronize the preset editor binding with persona_preset_map."""
        preset_id = str(preset.get("id", "") or "").strip()
        persona_id = str(preset.get("persona_id", "") or "").strip()
        previous_persona_id = str(previous_persona_id or "").strip()
        raw_mapping = self._cfg("persona_preset_map", {})
        mapping = dict(raw_mapping) if isinstance(raw_mapping, dict) else {}
        changed = not isinstance(raw_mapping, dict)

        if persona_id != previous_persona_id:
            if previous_persona_id and mapping.get(previous_persona_id) == preset_id:
                mapping.pop(previous_persona_id, None)
                changed = True
            if persona_id and mapping.get(persona_id) != preset_id:
                mapping[persona_id] = preset_id
                changed = True
        if changed:
            self.config["persona_preset_map"] = mapping
        return changed

    def _normalize_preset_fields(
        self,
        payload: dict[str, Any],
        current: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        merged = dict(current or {})
        merged.update(payload)
        name = str(merged.get("name", "") or "").strip()
        if not name:
            raise ValueError("角色卡名称不能为空")
        reference_type = str(merged.get("reference_type", "character") or "character").strip().lower()
        if reference_type == "character&style":
            reference_type = "both"
        if reference_type not in {"character", "style", "both"}:
            raise ValueError("参考类型只能是 character、style 或 both")
        reference_id = str(merged.get("reference_id", "") or "").strip()
        if reference_id and (Path(reference_id).name != reference_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", reference_id)):
            raise ValueError("参考图 ID 格式不正确")
        preset_id = str((current or {}).get("id", "") or "").strip()
        if not preset_id:
            preset_id = uuid.uuid4().hex[:10]
        positive_prompt = str(merged.get("positive_prompt", "") or "").strip()
        if not positive_prompt:
            positive_prompt = ", ".join(
                part
                for part in (
                    str(merged.get("style_prompt", "") or "").strip(),
                    str(merged.get("character_prompt", "") or "").strip(),
                )
                if part
            )
        lock_positive_default = (
            self._as_bool(merged.get("lock_style", True), True)
            or self._as_bool(merged.get("lock_character", True), True)
        )
        positive_strength_default = max(
            self._preset_strength(merged.get("style_strength", 1.35), 1.35),
            self._preset_strength(merged.get("character_strength", 1.25), 1.25),
        )
        layers = drawing.card_layers({**merged, "positive_prompt": positive_prompt})
        if any(key in payload for key in drawing.LAYERS) or payload.get("layers_version") == 1:
            for key in drawing.LAYERS:
                value = payload.get(key, layers.get(key, ""))
                if not isinstance(value, str) or len(value) > 8000:
                    raise ValueError(f"{key} 必须是不超过 8000 字符的字符串")
                drawing.tags(value, validate=True)
                layers[key] = value.strip()
            positive_prompt = drawing.join(*(layers[key] for key in drawing.LAYERS))
            if len(positive_prompt) > 8000:
                raise ValueError("角色卡各层合计不能超过 8000 字符")
            layers["layers_need_review"] = False
        elif "positive_prompt" in payload:
            # Old clients can still save a full card; reclassify once, then
            # expose the proposed layers for review instead of stale fields.
            layers = drawing.card_layers({"positive_prompt": positive_prompt})
        if len(positive_prompt) > 8000:
            raise ValueError("角色卡正向词不能超过 8000 字符")
        return {
            # Preset IDs are server-owned and immutable. Accepting an ID from
            # a create/update payload can create duplicates or orphan mappings.
            "id": preset_id,
            "name": name[:80],
            "description": str(merged.get("description", "") or "").strip()[:300],
            "positive_prompt": positive_prompt,
            **{key: layers.get(key, "") for key in (*drawing.LAYERS, "layers_version", "migration_original_prompt", "layers_need_review", "migration_notes")},
            "negative_prompt": str(merged.get("negative_prompt", "") or "").strip()[:4000],
            "reference_id": reference_id,
            "reference_type": reference_type,
            "lock_positive": self._as_bool(
                merged.get("lock_positive", lock_positive_default),
                True,
            ),
            "positive_strength": self._preset_strength(
                merged.get("positive_strength", positive_strength_default),
                1.35,
            ),
            "quality_override": str(merged.get("quality_override", "off"))
            if str(merged.get("quality_override", "off")) in {"inherit", "on", "off"}
            else "off",
            "persona_id": str(merged.get("persona_id", "") or "").strip()[:200],
            "enabled": self._as_bool(merged.get("enabled", True), True),
        }

    def _create_preset_record(self, payload: dict[str, Any]) -> dict[str, Any]:
        preset = self._normalize_preset_fields(payload)
        raw_mapping = self._cfg("persona_preset_map", {})
        previous_mapping = dict(raw_mapping) if isinstance(raw_mapping, dict) else {}
        self.presets.append(preset)
        try:
            mapping_changed = self._sync_preset_persona_binding(preset)
            self._save_presets()
            if mapping_changed:
                self._save_config()
        except Exception:
            self.presets = [item for item in self.presets if item is not preset]
            self.config["persona_preset_map"] = previous_mapping
            try:
                self._save_presets()
                self._save_config()
            except Exception:
                pass
            raise
        return preset

    def _update_preset_record(self, preset: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize_preset_fields(payload, preset)
        previous = dict(preset)
        raw_mapping = self._cfg("persona_preset_map", {})
        previous_mapping = dict(raw_mapping) if isinstance(raw_mapping, dict) else {}
        previous_persona_id = str(previous.get("persona_id", "") or "")
        try:
            preset.clear()
            preset.update(normalized)
            mapping_changed = self._sync_preset_persona_binding(preset, previous_persona_id)
            self._save_presets()
            if mapping_changed:
                self._save_config()
        except Exception:
            preset.clear()
            preset.update(previous)
            self.config["persona_preset_map"] = previous_mapping
            try:
                self._save_presets()
                self._save_config()
            except Exception:
                pass
            raise
        return preset

    def _delete_preset_record(self, preset_id: str) -> bool:
        """Delete a preset and remove every configuration reference to it."""
        preset_id = str(preset_id or "").strip()
        if not preset_id or not self._get_preset(preset_id, include_disabled=True):
            return False

        self.presets = [p for p in self.presets if str(p.get("id", "")) != preset_id]

        config_changed = False
        if str(self._cfg("default_preset_id", "")) == preset_id:
            self.config["default_preset_id"] = ""
            config_changed = True

        mapping = self._cfg("persona_preset_map", {})
        if isinstance(mapping, dict):
            cleaned_mapping = {
                str(persona_id): str(mapped_id)
                for persona_id, mapped_id in mapping.items()
                if str(mapped_id) != preset_id
            }
            if cleaned_mapping != mapping:
                self.config["persona_preset_map"] = cleaned_mapping
                config_changed = True

        self._save_presets()
        if config_changed:
            self._save_config()
        return True

    def _get_preset(self, preset_id: str | None, *, include_disabled: bool = False) -> dict[str, Any] | None:
        if not preset_id:
            return None
        preset = next((p for p in self.presets if p.get("id") == preset_id), None)
        if preset and not include_disabled and not self._as_bool(preset.get("enabled", True), True):
            return None
        return preset

    def _resolve_persona_id(self, event: AstrMessageEvent | None = None) -> str:
        candidates: list[Any] = []
        try:
            if event is not None:
                session_cfg = self.context.get_config(umo=event.unified_msg_origin)
                candidates.extend([session_cfg.get("persona_id"), session_cfg.get("persona")])
                provider_settings = session_cfg.get("provider_settings", {})
                if isinstance(provider_settings, dict):
                    candidates.append(provider_settings.get("default_personality"))
                candidates.append(session_cfg.get("default_personality"))
        except Exception:
            pass
        candidates.extend([self._cfg("persona_id", ""), self._cfg("persona", "")])
        for candidate in candidates:
            if isinstance(candidate, dict):
                candidate = candidate.get("id") or candidate.get("persona_id")
            if candidate:
                return str(candidate)
        return ""

    def _resolve_preset_for_persona(self, persona_id: str) -> dict[str, Any] | None:
        mapping = self._cfg("persona_preset_map", {})
        if isinstance(mapping, dict) and persona_id:
            mapped = self._get_preset(str(mapping.get(persona_id, "")))
            if mapped:
                return mapped
        return self._get_preset(str(self._cfg("default_preset_id", "")))

    async def _resolve_active_persona_id(
        self,
        event: AstrMessageEvent | None = None,
    ) -> str:
        persona_id = ""
        resolved_by_persona_manager = False
        if event is not None:
            origin = str(getattr(event, "unified_msg_origin", "") or "")
            try:
                conversation_persona_id = None
                conversation_manager = self.context.conversation_manager
                conversation_id = await conversation_manager.get_curr_conversation_id(origin)
                if conversation_id:
                    conversation = await conversation_manager.get_conversation(origin, conversation_id)
                    if conversation:
                        conversation_persona_id = getattr(conversation, "persona_id", None)

                session_config = self.context.get_config(umo=origin) or {}
                provider_settings = session_config.get("provider_settings", {})
                if not isinstance(provider_settings, dict):
                    provider_settings = {}
                platform_name = event.get_platform_name()
                resolved = await self.context.persona_manager.resolve_selected_persona(
                    umo=event.unified_msg_origin,
                    conversation_persona_id=conversation_persona_id,
                    platform_name=platform_name,
                    provider_settings=provider_settings,
                )
                if isinstance(resolved, tuple) and resolved:
                    resolved_by_persona_manager = True
                    selected_persona_id = resolved[0]
                    if selected_persona_id and selected_persona_id != "[%None]":
                        persona_id = str(selected_persona_id)
            except Exception as exc:
                # Keep a conservative fallback for partially initialized contexts,
                # while AstrBot >= 4.27.3 normally uses PersonaManager above.
                logger.debug(f"[{PLUGIN_NAME}] 通过 PersonaManager 解析当前人设失败: {exc}")
        if not persona_id and not resolved_by_persona_manager:
            persona_id = self._resolve_persona_id(event)
        return persona_id

    async def _resolve_active_preset(
        self,
        event: AstrMessageEvent | None = None,
        preset_id: str | None = None,
    ) -> dict[str, Any] | None:
        if preset_id:
            return self._get_preset(preset_id)
        persona_id = await self._resolve_active_persona_id(event)
        return self._resolve_preset_for_persona(persona_id)

    def _resolve_preset(self, event: AstrMessageEvent | None = None, preset_id: str | None = None):
        if preset_id and self._get_preset(preset_id):
            return self._get_preset(preset_id)
        persona_id = self._resolve_persona_id(event)
        return self._resolve_preset_for_persona(persona_id)

    @staticmethod
    def _preset_strength(value: Any, default: float) -> float:
        try:
            return max(0.1, min(2.0, float(value)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _nai_anchor(text: str, strength: float, modern_model: bool) -> str:
        text = str(text or "").strip()
        if not text:
            return ""
        strength = max(0.1, min(2.0, strength))
        if modern_model:
            return f"{strength:.2f}::{text}::"
        if strength >= 1.0:
            levels = min(8, max(0, round((strength - 1.0) / 0.05)))
            return "{" * levels + text + "}" * levels
        levels = min(8, max(1, round((1.0 - strength) / 0.05)))
        return "[" * levels + text + "]" * levels

    def _preset_quality_toggle(self, preset: dict[str, Any] | None) -> bool:
        raw_override = (preset or {}).get("quality_override")
        if raw_override is None and preset:
            has_locked_card = bool(str(preset.get("positive_prompt", "") or "").strip()) and self._as_bool(preset.get("lock_positive", True), True)
            if has_locked_card:
                return False
        override = str(raw_override or "inherit")
        if override == "on":
            return True
        if override == "off":
            return False
        return self._as_bool(self._cfg("quality_toggle", True))

    @staticmethod
    def _requested_change_categories(
        event: AstrMessageEvent | None,
        candidate_prompt: str = "",
        *,
        request_text: str | None = None,
    ) -> set[str]:
        try:
            raw_request = str(event.get_message_str() or "").lower().replace("_", " ") if event else ""
        except Exception:
            raw_request = ""
        if request_text is not None:
            raw_request = request_text.lower().replace("_", " ")
        patterns = {
            "identity": r"(性别|男生|女生|男孩|女孩|男人|女人|少年|少女|\b(?:1|2|multiple)?\s*(?:girls?|boys?)\b|\bmale\b|\bfemale\b|\bman\b|\bwoman\b)",
            "hair": r"(头发|发型|发色|马尾|辫子?|刘海|呆毛|[粉金银白黑红蓝绿紫棕]毛|\bhair\b|\bponytails?\b|\btwintails?\b|\bbraids?\b|\bbangs?\b)",
            "eyes": r"(眼睛|眼色|瞳色?|异色瞳|\beyes?\b|\biris\b|\bheterochromia\b)",
            "ears_species": r"(耳朵|精灵耳|兽耳|种族|猫娘|狐娘|狼娘|兔娘|龙娘|恶魔|天使|\bears?\b|\belf\b|\bkemonomimi\b|\bfox\b|\bcat\b|\bwolf\b|\bdog\b|\brabbit\b|\bbunny\b|\bdragon\b|\bdemon\b|\bangel\b|\bhuman\b)",
            "clothing": r"(衣服|服装|换装|穿(?:着|上|了|一|件|身)|婚纱|礼服|裙|裤|鞋|帽|配饰|饰品|\boutfit\b|\bclothes?\b|\bclothing\b|\bwear(?:ing)?\b|\bdress\b|\bgown\b|\bskirt\b|\buniform\b|\baccessor(?:y|ies)\b|\bbikini\b|\bswimsuit\b|\blingerie\b|\barmor\b|\bkimono\b|\bhoodie\b|\bsuit\b)",
            "body_features": r"(肤色|皮肤|身材|体型|胸部?|年龄|\bskin\b|\bbody shape\b|\bbreasts?\b|\bmuscular\b|\bpetite\b|\byoung\b|\badult\b|\bchild\b|\bteen(?:age|ager)?\b)",
            "style": r"(画风|风格|二次元|水彩|油画|写实|线稿|素描|赛璐璐|像素|\bstyle\b|\bwatercolor\b|\bphotorealistic\b|\brealistic\b|\bscreencap\b|\bsketch\b|\bcel shading\b|\banime\b|\bmanga\b|\bcartoon\b|\bcomic\b|\bpainting\b|\bdigital art\b|\billustration\b|\bpixel art\b|\b3d\b)",
        }
        categories = {
            category
            for category, pattern in patterns.items()
            if re.search(pattern, raw_request, re.I)
        }
        explicit_change = re.search(
            r"(换|改|变成|改成|改为|换成|换上|脱掉|摘掉|去掉|remove|replace|change|switch|"
            r"turn into|instead of)",
            raw_request,
            re.I,
        )
        if explicit_change and candidate_prompt and not categories:
            normalized_candidate = str(candidate_prompt).lower().replace("_", " ")
            for category, pattern in NovelAIPainterPlugin._card_tag_patterns().items():
                if pattern.search(normalized_candidate):
                    categories.add(category)
        if explicit_change and re.search(r"(造型|形象|look|appearance)", raw_request, re.I):
            categories.update({"hair", "clothing", "body_features"})
        return categories

    @staticmethod
    def _card_tag_patterns() -> dict[str, re.Pattern[str]]:
        return {
            "identity": re.compile(
                r"\b((?:1|2|multiple)[ _]?(?:girls?|boys?)|girls?|boys?|male|female|man|woman)\b",
                re.I,
            ),
            "hair": re.compile(
                r"\b(hair|ponytails?|twintails?|braids?|bangs?|ahoge|blonde|brunette)\b",
                re.I,
            ),
            "eyes": re.compile(r"\b(eyes?|iris|heterochromia)\b", re.I),
            "ears_species": re.compile(
                r"\b(ears?|elf|kemonomimi|fox|cat|wolf|dog|rabbit|bunny|dragon|demon|angel|human)\b",
                re.I,
            ),
            "clothing": re.compile(
                r"\b(dress|gown|shirt|skirt|pants|stockings?|bodysuit|outfit|clothes?|clothing|"
                r"uniform|jacket|coat|shoes?|boots?|gloves?|hat|ribbon|necklace|earrings?|"
                r"accessor(?:y|ies)|bikini|swimsuit|lingerie|armor|robe|kimono|hoodie|suit)\b",
                re.I,
            ),
            "body_features": re.compile(
                r"\b(skin|pale|tan|dark-skinned|body shape|breasts?|muscular|petite|young|"
                r"adult|child|teen(?:age|ager)?)\b",
                re.I,
            ),
            "style": re.compile(
                r"\b(artist|art style|screencap|watercolor|oil painting|photorealistic|realistic|"
                r"3d|cel shading|lineart|line art|sketch|anime|manga|cartoon|comic|painting|"
                r"digital art|illustration|pixel art|style)\b",
                re.I,
            ),
        }

    def _adapt_positive_prompt(
        self,
        positive_prompt: str,
        event: AstrMessageEvent | None,
        locked: bool,
        candidate_prompt: str = "",
        *,
        request_text: str | None = None,
    ) -> str:
        """Apply request-local role-card overrides without mutating the card."""
        if not locked:
            return positive_prompt
        requested_changes = self._requested_change_categories(event, candidate_prompt, request_text=request_text)
        if not requested_changes:
            return positive_prompt
        patterns = self._card_tag_patterns()
        kept: list[str] = []
        for part in re.split(r"[,\n]+", positive_prompt):
            tag = part.strip()
            if not tag:
                continue
            if any(
                category in requested_changes and pattern.search(tag.replace("_", " "))
                for category, pattern in patterns.items()
            ):
                continue
            kept.append(tag)
        return ", ".join(kept)

    def _adapt_negative_prompt(
        self,
        negative_prompt: str,
        event: AstrMessageEvent | None,
        candidate_prompt: str = "",
        *,
        request_text: str | None = None,
    ) -> str:
        """Let an explicit request override role-card negatives for that category."""
        requested_changes = self._requested_change_categories(event, candidate_prompt, request_text=request_text)
        if not requested_changes:
            return negative_prompt
        patterns = self._card_tag_patterns()
        kept: list[str] = []
        for part in re.split(r"[,\n]+", negative_prompt):
            tag = part.strip()
            if not tag:
                continue
            if any(
                category in requested_changes and pattern.search(tag.replace("_", " "))
                for category, pattern in patterns.items()
            ):
                continue
            kept.append(tag)
        return ", ".join(kept)

    def _compose_prompt(self, prompt: str, event: AstrMessageEvent | None = None, preset_id: str | None = None, *, preset_override: dict[str, Any] | None = None, request_text: str | None = None) -> tuple[str, str, str]:
        preset = preset_override if preset_override is not None else self._resolve_preset(event, preset_id)
        user_prompt = str(prompt or "").strip()
        if not preset:
            if self._provider_name() == "openai_compatible" and self._as_bool(self._cfg("quality_toggle", True)):
                user_prompt = f"high quality, highly detailed, {user_prompt}"
            return user_prompt[:12000], "", ""

        positive_prompt = str(preset.get("positive_prompt", "") or "").strip()
        if preset.get("_contextual_appearance"):
            # Exact identity, one resolved outfit and style, plus scene/action. Do not
            # reinsert default clothes or paraphrase the retained tags.
            if self._provider_name() == "novelai_official":
                composed = ", ".join(part for part in (positive_prompt, user_prompt) if part)
            else:
                composed = f"Current character appearance: {positive_prompt}. Scene/action: {user_prompt}"
                if self._preset_quality_toggle(preset):
                    composed += ". Output quality: high quality, highly detailed"
            return composed[:12000], str(preset.get("id", "")), str(preset.get("negative_prompt", "") or "").strip()
        lock_positive = self._as_bool(preset.get("lock_positive", True), True)
        positive_strength = self._preset_strength(preset.get("positive_strength", 1.35), 1.35)
        effective_positive = self._adapt_positive_prompt(
            positive_prompt,
            event,
            lock_positive,
            user_prompt,
            request_text=request_text,
        )
        requested_changes = self._requested_change_categories(event, user_prompt, request_text=request_text)

        if self._provider_name() == "novelai_official":
            model = self._active_model()
            modern_model = model.startswith("nai-diffusion-4") or model.startswith("nai-diffusion-5")
            parts: list[str] = []
            if effective_positive:
                parts.append(self._nai_anchor(effective_positive, positive_strength if lock_positive else 1.0, modern_model))
            if positive_prompt and lock_positive:
                consistency = ["same character", "consistent character design"]
                if "style" not in requested_changes:
                    consistency.extend(["consistent art style", "stable visual style"])
                if "eyes" not in requested_changes and "body_features" not in requested_changes:
                    consistency.append("same facial features")
                if "hair" not in requested_changes:
                    consistency.append("same hairstyle")
                if "ears_species" not in requested_changes:
                    consistency.append("same species and ear shape")
                if "clothing" not in requested_changes:
                    consistency.extend(["same outfit", "same accessories"])
                parts.append(self._nai_anchor(", ".join(consistency), 1.15, modern_model))
            parts.append(user_prompt)
            composed = ", ".join(part for part in parts if part)
        else:
            instructions: list[str] = []
            if effective_positive:
                qualifier = "FIXED; preserve every unspecified identity and style detail" if lock_positive else "preferred"
                instructions.append(f"Role-card positive prompt ({qualifier}): {effective_positive}")
            instructions.append(f"Requested change: {user_prompt}")
            if lock_positive:
                instructions.append("Apply only changes explicitly requested by the user; preserve every other role-card detail")
            if self._preset_quality_toggle(preset):
                instructions.append("Output quality: high quality, highly detailed")
            composed = ". ".join(part for part in instructions if part)

        preset_negative = self._adapt_negative_prompt(
            str(preset.get("negative_prompt", "") or "").strip(),
            event,
            user_prompt,
            request_text=request_text,
        )
        return composed[:12000], str(preset.get("id", "")), preset_negative

    def _filter_llm_prompt_conflicts(
        self,
        prompt: str,
        event: AstrMessageEvent,
        preset: dict[str, Any] | None,
    ) -> str:
        """Drop appearance/style tags hallucinated by the LLM for locked presets.

        Explicit user requests to change the corresponding category remain
        allowed. This is intentionally applied only to LLM-generated tags;
        fixed command prompts are treated as direct user input.
        """
        if not preset or preset.get("_contextual_appearance"):
            return prompt
        requested_changes = self._requested_change_categories(event, prompt)
        lock_positive = self._as_bool(preset.get("lock_positive", True), True)
        tag_patterns = self._card_tag_patterns()
        kept: list[str] = []
        removed = 0
        for part in re.split(r"[,\n]+", str(prompt or "")):
            tag = part.strip()
            if not tag:
                continue
            if lock_positive and any(
                pattern.search(tag.replace("_", " ")) and category not in requested_changes
                for category, pattern in tag_patterns.items()
            ):
                removed += 1
                continue
            kept.append(tag)
        if removed:
            logger.info(f"[{PLUGIN_NAME}] 已移除 {removed} 个与锁定角色卡冲突的 LLM 标签")
        return ", ".join(kept) if kept else "portrait, looking at viewer"

    @staticmethod
    def _explicit_self_portrait(event: AstrMessageEvent) -> bool:
        """Compatibility for older tool calls; ambiguous subjects never inherit a card."""
        text = str(event.get_message_str() or "").lower()
        if re.search(r"合影|同框|一起|和|与|以及|还有|加上|不要|别|不是|\band\b|\bwith\b|\bdon't\b|\bdo not\b", text):
            return False
        return bool(re.search(
            r"(?:画|绘制)(?:一张|一下|一个|给我看看)?(?:你自己|你现在的样子|你的(?:样子|照片|画像|自拍|全身像))|"
            r"(?:画|绘制)(?:一张|一下|一个)?你(?:[，。！？!?,\s]|$)|"
            r"(?:画|绘制|生成|来)(?:一张|张|一下|一个)?你(?:自己)?(?:穿着|穿上|坐|站|躺|趴|看书|读书|睡觉)|"
            r"(?:画|绘制|生成|来)(?:一张|张|一下|一个)?你(?:自己)?在.{0,50}(?:坐|站|躺|趴|看书|读书|睡觉|微笑)|"
            r"\b(?:draw|paint)\s+yourself\b|\b(?:picture|portrait) of\s+you\b|\byour\s+(?:portrait|selfie)\b",
            text,
        ))

    @staticmethod
    def _sync_drawing_tool_schema() -> None:
        # Decorator registration retains AstrBot's handler metadata and plugin
        # ownership; replace its shallow docstring schema with a typed contract.
        from astrbot.core.provider.register import llm_tools
        for tool in llm_tools.func_list:
            if tool.name == "novelai_generate_image":
                tool.parameters = json.loads(json.dumps(drawing.TOOL_PARAMETERS))

    async def _empty_subject_fallback(self, event: AstrMessageEvent) -> list[dict[str, str]]:
        """Repair a missing selection only when the user's request identifies it.

        An empty model array must not bypass an explicit card request. Ordinary
        characters, scenery, multi-person requests and card-style references
        remain unbound; never infer a subject from the generated caption.
        """
        text = str(event.get_message_str() or "").lower()
        if re.search(r"(?:不用|不使用|不套(?:用)?|无需(?:使用)?|不要(?:使用|用|套用)?|别(?:使用|用|套用))(?:(?:任何|内置|绑定|默认|的)\s*)*(?:角色卡|人设卡)|\b(?:without|don't use|do not use)\b.{0,20}\b(?:character|role) card\b", text):
            return []
        if not re.search(r"(?:用|使用|按|照|根据).{0,24}(?:角色卡|人设卡)|\buse\b.{0,32}\b(?:character|role) card\b", text):
            return [{"source": "self"}] if self._explicit_self_portrait(event) else []
        if re.search(r"合影|同框|一起|与|和|多[个人张]|所有|全部|\b(?:and|with|all)\b", text):
            return []
        if re.search(r"(?:角色卡|人设卡)(?:的|里(?:的)?|中(?:的)?|原有的)?(?:画风|风格)|\bcard['’]?s?\s+(?:art\s+)?style\b", text):
            return []
        enabled = [card for card in self.presets if self._as_bool(card.get("enabled", True), True)]
        named = [card for card in self.presets if any(
            re.search(r"(?<![a-z0-9_])" + re.escape(str(card.get(key, "")).lower()) + r"(?![a-z0-9_])", text)
            for key in ("id", "name") if card.get(key))]
        if len(named) == 1:
            return [{"source": "card", "preset_id": named[0]["id"]}]
        generic = re.search(
            r"(?:用|使用|按(?:照)?|照|根据)(?:(?:我们|咱们|你|我|的|当前|已|绑定|内置|默认|现有|这张)\s*)*(?:角色卡|人设卡)(?=\s*(?:去|来|画|绘制|生成|[，。！？!?]|$))|"
            r"\buse\s+(?:(?:the|our|your|my|built-in|bound|default|current)\s+)*(?:character|role) card(?=\s*(?:to\b|for\b|[,.;!?]|$))",
            text,
        )
        if not named and not generic:
            raise ValueError("本次指定的角色卡未能匹配，请使用 available_cards 中的准确名称或 preset_id。")
        active = await self._resolve_active_preset(event) if not named else None
        selected = active or (enabled[0] if len(enabled) == 1 and not named else None)
        if not selected:
            raise ValueError("本次要求使用角色卡，请从 available_cards 选择对应卡并填写 source=card 和 preset_id，不能用空数组跳过角色卡。")
        return [{"source": "card", "preset_id": selected["id"]}]

    def _drawing_style_override_requested(self, event: AstrMessageEvent) -> bool:
        # A model-supplied default style is not a user request to erase the
        # card's artist/weighted tags. Keep explicit style changes available.
        text = str(event.get_message_str() or "")
        for clause in re.split(r"[，。；;\n]", text):
            if "style" not in self._requested_change_categories(None, request_text=clause):
                continue
            if re.search(
                r"(?:保留|保持|沿用|维持|不(?:要|用)?(?:改|变|换)|别(?:改|变|换)).{0,8}(?:画风|风格)|"
                r"(?:原(?:来|有|始)?|默认|角色卡|卡内)(?:的)?(?:画风|风格)|"
                r"(?:画风|风格).{0,8}(?:不变|不改|别改|照旧)|"
                r"\b(?:keep|preserve|retain|same|original|default)\b.{0,20}\b(?:art\s+)?style\b|"
                r"\b(?:don't|do not)\s+(?:change|replace|alter)\b.{0,20}\bstyle\b",
                clause, re.I,
            ):
                continue
            return True
        return False

    async def _complete_card_selector(self, event, subjects):
        """Repair a single omitted selector only from unambiguous user intent.

        Never read the model's prose/preamble as a selector, infer names from
        appearance tags, or apply the self card to a named different character.
        """
        if (not isinstance(subjects, list) or len(subjects) != 1 or not isinstance(subjects[0], dict)
                or subjects[0].get("source") != "card"):
            return subjects
        selector = subjects[0].get("preset_id", "")
        if not isinstance(selector, str) or selector.strip():
            return subjects
        selected, reason = None, "explicit_request"
        explicit = await self._empty_subject_fallback(event)
        if len(explicit) == 1:
            item = explicit[0]
            selected = (await self._resolve_active_preset(event) if item["source"] == "self"
                else self._find_role_card(item["preset_id"]))
        else:
            text = event.get_extra("nai_marker_body")
            if text is None:
                text = re.sub(r"^/n\s*", "", event.get_message_str(), flags=re.I).strip()
            # A framing-only request has no new named subject. Preserve the
            # last selected card, then the active binding; do not guess people.
            framing_only = re.fullmatch(
                r"(?:请|麻烦)?(?:给我|帮我)?(?:画|来|生成|绘制)?(?:一张|张|一个|个)?"
                r"(?:全身(?:的)?(?:照(?:片)?|像)|半身(?:的)?(?:照(?:片)?|像)|头像|肖像)(?:吧|呀|啊)?[。！!？?\s]*", str(text or ""))
            if framing_only:
                view = await self._story_view(event)
                previous = self._previous_drawing(view).get("plan") or {}
                previous_subjects = previous.get("subjects", [])
                if previous_subjects:
                    if len(previous_subjects) == 1 and previous_subjects[0].get("source") == "card" and previous_subjects[0].get("preset_id"):
                        selected = self._find_role_card(previous_subjects[0]["preset_id"])
                    elif len(previous_subjects) == 1 and previous_subjects[0].get("source") == "self":
                        selected = await self._resolve_active_preset(event)
                    reason = "previous_completed_drawing"
                else:
                    selected = await self._resolve_active_preset(event)
                    reason = "bound_self"
                    if not selected:
                        enabled = [c for c in self.presets if self._as_bool(c.get("enabled", True), True)]
                        selected = enabled[0] if len(enabled) == 1 else None
                        reason = "only_enabled_card"
        if not selected or not self._as_bool(selected.get("enabled", True), True):
            return subjects
        result = [{**subjects[0], "preset_id": selected["id"]}]
        self._planner_log(event, self._drawing_llm_id(), "selection", 0, None,
            phase="selection_repair", status="normalized", source=reason, preset_id=selected["id"],
            creates_model_request=False)
        return result

    async def _resolve_drawing_subjects(self, event: AstrMessageEvent, subjects: Any) -> list[dict[str, Any]]:
        if subjects is None or subjects == "":
            subjects = []
        elif isinstance(subjects, str):
            try:
                subjects = json.loads(subjects)
            except ValueError as exc:
                raise ValueError("subjects 需为 JSON 角色数组；旧字符串必须包含有效数组 JSON。") from exc
        if not isinstance(subjects, list) or len(subjects) > 6:
            raise ValueError("subjects 需为角色数组，一张图最多 6 人。")
        if not subjects:
            subjects = await self._empty_subject_fallback(event)
            if subjects:
                logger.info(f"[{PLUGIN_NAME}] 已按用户明确要求补全空的角色卡选择: {subjects}")
        subjects = await self._complete_card_selector(event, subjects)
        resolved, self_card = [], None
        for item in subjects:
            if not isinstance(item, dict):
                raise ValueError("subjects 每项必须为对象。")
            if "appearance_patch" in item:
                raise ValueError("标签增删补丁已停用。请用 outfit 提交完整当前服饰，整体替换旧服饰。")
            if set(item) - set(drawing.SUBJECT_SCHEMA["properties"]):
                raise ValueError("subjects 含未知字段；请按当前工具结构提交 source、prompt、outfit 等字段。")
            source, selector = item.get("source"), item.get("preset_id", "")
            if not isinstance(source, str) or source not in {"self", "card", "custom"}:
                raise ValueError("角色 source 只能是 self、card 或 custom。")
            if not isinstance(selector, str) or (source not in {"self", "card"} and selector):
                raise ValueError("仅 source=card 可用字符串 preset_id 选择角色卡。")
            if source == "self":
                if self_card is None:
                    self_card = await self._resolve_active_preset(event) or {}
                preset = self_card
                if selector and selector != preset.get("id"):
                    raise ValueError("source=self 的 preset_id 与当前绑定卡不符；选择其他卡请用 source=card。")
            elif source == "card":
                preset = self._find_role_card(selector)
                if not self._as_bool(preset.get("enabled", True), True):
                    raise ValueError("所选角色卡已禁用。")
            elif source == "custom":
                preset = {}
            else:
                raise ValueError("角色 source 只能是 self、card 或 custom。")
            subject = {"source": source, "preset": preset, "prompt": ""}
            for field, limit in (("prompt", 4000), ("outfit", 4000), ("appearance_prompt", 8000), ("negative_prompt", 4000)):
                if field in item:
                    if not isinstance(item[field], str) or len(item[field]) > limit:
                        raise ValueError(f"{field} 必须是不超过 {limit} 字符的字符串。")
                    subject[field] = item[field].strip()
            if "appearance_prompt" in subject and not subject["appearance_prompt"]:
                subject.pop("appearance_prompt")
            if "use_reference" in item and not isinstance(item["use_reference"], bool):
                raise ValueError("use_reference 必须为布尔值。")
            subject["use_reference"] = item.get("use_reference", False)
            subject["state_scope"] = item.get("state_scope", "image")
            if not isinstance(subject["state_scope"], str) or subject["state_scope"] not in {"image", "story"}:
                raise ValueError("state_scope 只能为 image（仅本图）或 story（剧情已换装）。")
            if "outfit" in subject:
                drawing.tags(subject["outfit"], validate=True)
            resolved.append(subject)
        return resolved

    def _contextual_cards_enabled(self) -> bool:
        return self._cfg("llm_card_mode", "context") == "context"

    def _ledger(self) -> drawing.StoryLedger:
        if not hasattr(self, "_story_ledger"):
            self._story_ledger = drawing.StoryLedger()
        return self._story_ledger

    async def _story_view(self, event: AstrMessageEvent) -> drawing.StoryView | None:
        try:
            origin = str(event.unified_msg_origin)
            manager = self.context.conversation_manager
            cid = await manager.get_curr_conversation_id(origin)
            if not origin or not cid:
                return None
            conversation = await manager.get_conversation(origin, cid)
            history = drawing.history_records(getattr(conversation, "history", None))
            if history is None:
                return None  # No unverifiable shared/fallback outfit state.
            return self._ledger().observe((origin, str(cid)), history,
                self._event_key(event, "", "generate"),
                str(event.get_extra("nai_story_source") or event.get_message_str() or "").strip())
        except Exception:
            return None

    @filter.on_decorating_result()
    async def drawing_context_lifecycle(self, event: AstrMessageEvent) -> None:
        # Core sets this only after a successful reset/new/switch, including
        # renamed commands. Never guess lifecycle from user message keywords.
        if event.get_extra("_clean_group_context_session", False):
            for task, origin in list(getattr(self, "_planner_tasks", {}).items()):
                if origin == str(event.unified_msg_origin):
                    task.cancel()
            self._ledger().clear(str(event.unified_msg_origin))
            getattr(self, "_planner_diagnostics", {}).pop(str(event.unified_msg_origin), None)
            for key in list(getattr(self, "_drawing_memory", {})):
                if key[0] == str(event.unified_msg_origin):
                    self._drop_drawing_memory(key)
            for key in list(getattr(self, "_request_snapshots", {})):
                if key == str(event.unified_msg_origin):
                    self._request_snapshots.pop(key, None)

    async def _generation_context_current(self, event: AstrMessageEvent) -> bool:
        expected = event.get_extra("nai_generation_view")
        if expected is None:
            return True
        current = await self._story_view(event)
        return bool(current and current.scope == expected.scope and current.epoch == expected.epoch)

    def _apply_story_layers(self, subjects: list[dict], view: drawing.StoryView | None, operation: str, style: str | None, scene_prompt: str = "") -> None:
        scene_outfit = drawing.inline_outfit_tags(scene_prompt)
        for subject in subjects:
            original = subject["preset"]
            if not original:
                appearance = subject.get("appearance_prompt", "")
                if appearance or subject.get("negative_prompt"):
                    subject["preset"] = {"positive_prompt": drawing.join(appearance, subject.get("outfit", "")),
                        "negative_prompt": subject.get("negative_prompt", ""), "_contextual_appearance": True}
                elif subject.get("outfit"):
                    subject["prompt"] = drawing.join(subject["prompt"], subject["outfit"])
                continue
            card = drawing.card_layers(original)
            current = self._ledger().current(view, card)
            inline_outfit = drawing.inline_outfit_tags(subject["prompt"])
            if "outfit" in subject:
                outfit = resolved_outfit = subject["outfit"]
                outfit_source = "request"
            elif inline_outfit or scene_outfit:
                # The caption is already authoritative for this image. Leave
                # it intact; do not also append the card/cached wardrobe. A
                # shared caption is never copied into every character slot.
                outfit = ""
                resolved_outfit = drawing.join(*(inline_outfit or scene_outfit))
                outfit_source = "subject_prompt" if inline_outfit else "shared_prompt"
            else:
                outfit = resolved_outfit = current.get("outfit", card["default_outfit_prompt"])
                outfit_source = "story" if current else "default"
            positive = drawing.join(card["identity_prompt"], outfit, card["style_prompt"] if style is None else "")
            if len(positive) > 8000:
                raise ValueError("角色各层合成超过 8000 字符。")
            # Remove only exact outfit prohibitions; keep every unrelated card
            # negative. Identity/style is never rewritten by the main model.
            outfit_tags = {tag.strip("{}[] ").casefold() for tag in drawing.tags(resolved_outfit)}
            negative = drawing.join(*(tag for tag in drawing.tags(card.get("negative_prompt", ""))
                if tag.strip("{}[] ").casefold() not in outfit_tags))
            effective = {**card, "positive_prompt": positive, "negative_prompt": negative, "_contextual_appearance": True}
            explicit_reference = operation.strip().lower() in {"reference", "img2img"}
            if not subject.get("use_reference") and not explicit_reference:
                effective["reference_id"] = ""
            subject["preset"] = effective
            subject["story_card"] = card
            subject["resolved_outfit"] = resolved_outfit
            subject["outfit_source"] = outfit_source
            subject["ignored_legacy_fields"] = [key for key in ("appearance_prompt", "negative_prompt") if key in subject]

    def _compose_character_prompts(self, subjects: list[dict], event: AstrMessageEvent, scene_prompt: str) -> list[dict[str, str]]:
        characters = []
        shared_style_change = "style" in self._requested_change_categories(event, scene_prompt)
        for subject in subjects:
            detail = subject["prompt"]
            if not detail and not subject["preset"].get("positive_prompt") and not subject["preset"].get("negative_prompt"):
                # Empty custom entries can accompany a complete shared
                # caption. Do not invent people or emit empty character slots.
                continue
            positive, preset_id, negative = self._compose_prompt(detail, preset_override=subject["preset"],
                request_text=detail + (", art style" if shared_style_change else ""))
            if self._contextual_cards_enabled():
                positive = drawing.character_slot(positive)
            characters.append({"prompt": positive, "negative_prompt": negative, "preset_id": preset_id})
        return characters

    def _supports_character_prompts(self) -> bool:
        return self._active_model().startswith(("nai-diffusion-4", "nai-diffusion-5"))

    @filter.on_agent_begin()
    async def drawing_agent_begin(self, event, run_context) -> None:
        request = event.get_extra("nai_main_request")
        if request is None:
            return
        event.set_extra("nai_agent_run_context", run_context)
        def observe(phase, metrics, provider_id):
            self._planner_log(event, provider_id, "main", 0, None,
                phase="main_" + phase, status="observed", http_metrics=metrics)
        try:
            observed = drawing_runtime.observe_main(event, run_context, observe)
            self._planner_log(event, event.get_extra("nai_main_provider", ""), "main", 0, None,
                phase="main_http_observation", status="observed", http_observable=observed,
                scope="current_runner_only", preserves_sdk_retries=True)
        except Exception as exc:
            logger.debug(f"[{PLUGIN_NAME}] 主请求观测不可用: {type(exc).__name__}")
        tools = (event.get_extra("nai_main_raw_tools"), getattr(request, "func_tool", None))
        drawing_runtime.guard_delivery(event, *tools)
        if event.get_extra("nai_planner_pending"):
            drawing_runtime.retire_drawing(*tools)

    def _shared_drawing_context(self):
        return self._cfg("drawing_llm_context_mode", "shared") == "shared"

    def _drawing_context_turns(self, event):
        try:
            return int(self.context.get_config(event.unified_msg_origin).get("provider_settings", {}).get("max_context_length", 50))
        except (AttributeError, TypeError, ValueError):
            return 50

    @filter.on_agent_done()
    async def drawing_agent_done(self, event, run_context, response) -> None:
        drawing_runtime.restore_observation(event)
        if self._shared_drawing_context() and event.get_extra("nai_planner_pending"):
            event.set_extra("nai_session_snapshot", drawing_session.capture(run_context.messages,
                images=self._as_bool(self._cfg("drawing_llm_use_images", False)),
                max_turns=self._drawing_context_turns(event)))

    async def _native_drawing_snapshot(self, event):
        snapshot = event.get_extra("nai_session_snapshot")
        if snapshot is not None:
            return snapshot
        images = self._as_bool(self._cfg("drawing_llm_use_images", False))
        context = event.get_extra("nai_agent_run_context")
        if context is not None and getattr(context, "messages", None):
            snapshot = drawing_session.capture(context.messages, images=images,
                max_turns=self._drawing_context_turns(event))
        elif (req := event.get_extra("nai_main_request")) is not None:
            # Alternate runners can omit AgentDone; preserve their prepared
            # request instead of silently reverting to the old text planner.
            messages = list(req.contexts or []) + [await req.assemble_context()]
            if reply := event.get_extra("nai_planner_reply"):
                messages.append({"role": "assistant", "content": reply})
            snapshot = drawing_session.capture(messages, req.system_prompt, images=images,
                source="main_request_snapshot", max_turns=self._drawing_context_turns(event))
        else:
            snapshot = await drawing_session.session_snapshot(self.context, event, images=images)
        event.set_extra("nai_session_snapshot", snapshot)
        return snapshot

    def _stamp_drawing(self, event):
        if event.get_extra("nai_drawing_sequence") is None:
            self._drawing_sequence = getattr(self, "_drawing_sequence", 0) + 1
            event.set_extra("nai_drawing_sequence", self._drawing_sequence)

    def _retire_main_drawing(self, event) -> None:
        request = event.get_extra("nai_main_request")
        drawing_runtime.retire_drawing(event.get_extra("nai_main_raw_tools"),
            getattr(request, "func_tool", None))

    def _image_tool_budget(self) -> float:
        # Only the current image tool gets this budget. LLM planning timeouts
        # are independent. Image HTTP is 180s; compatible URL download is 90s.
        attempts = self._retry_limit() + self._503_retries()
        queue = max(1, int(self._cfg("queue_timeout", 120) or 120))
        delay = max(1, min(300, float(self._cfg("retry_delay", 5) or 5)))
        download = 90 if self._provider_name() == "openai_compatible" else 0
        return attempts * (queue + 180) + max(0, attempts - 1) * delay + download + 30

    @filter.on_using_llm_tool()
    async def drawing_tool_start(self, event, tool, tool_args) -> None:
        if tool.name != "novelai_generate_image" or event.get_extra("nai_planner_pending") or self._llm_tool_claimed(event):
            return
        context = event.get_extra("nai_agent_run_context")
        if context is None:
            return
        previous = context.tool_call_timeout
        budget = max(previous, self._image_tool_budget())
        event.set_extra("nai_image_tool_timeout_restore", previous)
        event.set_extra("nai_image_tool_budget", budget)
        context.tool_call_timeout = budget
        self._planner_log(event, self._provider_name(), "image", 0, None,
            phase="image_tool_budget", status="prepared", core_tool_timeout_seconds=previous,
            image_tool_timeout_seconds=budget, scope="current_tool_only")

    @staticmethod
    def _restore_image_tool_timeout(event) -> None:
        previous = event.get_extra("nai_image_tool_timeout_restore")
        context = event.get_extra("nai_agent_run_context")
        if previous is not None and context is not None:
            context.tool_call_timeout = previous
            event.set_extra("nai_image_tool_timeout_restore", None)
            event.set_extra("nai_image_tool_budget", None)

    @filter.on_llm_request()
    async def drawing_identity_context(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        marker, end = "[NovelAI drawing identities]", "[End NovelAI drawing identities]"
        # Remove only our legacy system block. Card/catalog changes must never
        # rewrite the persona or the stable system prefix on a new request.
        req.system_prompt = re.sub(re.escape(marker) + r".*?" + re.escape(end),
            "", req.system_prompt or "", flags=re.S)
        req.extra_user_content_parts = [part for part in (getattr(req, "extra_user_content_parts", None) or [])
            if not str(part.get("text", "") if isinstance(part, dict) else getattr(part, "text", "")).startswith(marker)]
        if not self._can_use(event)[0] or not (self._mode_allows("llm_tool") or self._mode_allows("command")):
            return
        if not self._mode_allows("llm_tool") and not self._drawing_llm_id():
            return
        if event.get_extra("nai_marker_dual"):
            raw = str(event.get_message_str() or "")
            body = event.get_extra("nai_marker_body", "") or "请回应当前对话。"
            if raw and raw in (req.prompt or ""):
                before, after = req.prompt.rsplit(raw, 1)
                req.prompt = before + body + after
        self._sync_drawing_tool_schema()
        event.set_extra("nai_main_request", req)
        event.set_extra("nai_main_raw_tools", drawing_runtime.isolate_tools(req))
        # AstrBot may decorate prompt with a configured prefix; history stores
        # that prompt plus reminder/multimodal text, not just message_str.
        event.set_extra("nai_story_source", getattr(req, "prompt", "") or event.get_message_str())
        active = await self._resolve_active_preset(event)
        view = await self._story_view(event) if self._contextual_cards_enabled() or self._drawing_llm_id() else None
        event.set_extra("nai_story_view", view)
        if event.get_extra("nai_marker_dual") and event.get_extra("nai_planner_view") is None:
            event.set_extra("nai_planner_view", view)
        if event.get_extra("nai_marker_dual"):
            count = max(0, min(30, int(self._cfg("drawing_llm_context_messages", 12))))
            records = drawing.history_records(getattr(req, "contexts", None))
            event.set_extra("nai_planner_context", planner.recent_dialogue(records or (view.history if view else ()), count))
            event.set_extra("nai_planner_images", list(getattr(req, "image_urls", None) or [])[:2]
                if self._as_bool(self._cfg("drawing_llm_use_images", False)) else [])
        conversation = getattr(req, "conversation", None)
        request_cid = getattr(conversation, "cid", None)
        request_history = drawing.history_records(getattr(conversation, "history", None))
        event.set_extra("nai_story_request_stale", bool(view and (
            (request_cid and str(request_cid) != view.scope[1]) or
            (request_history is not None and view.history[:len(request_history)] != request_history))))
        catalog = []
        for original in self.presets:
            if not self._as_bool(original.get("enabled", True), True):
                continue
            card = drawing.card_layers(original)
            current = self._ledger().current(view, card)
            catalog.append({"id": card["id"], "name": card.get("name", ""), "description": card.get("description", ""),
                **({"default_outfit": card["default_outfit_prompt"],
                "current_story_outfit": current.get("outfit")} if self._contextual_cards_enabled() else {}),
                "has_reference": bool(card.get("reference_id"))})
        rules = (
            "以下是本轮绘图工具的临时素材，不是聊天人设或新剧情；服饰字段是绘图默认值/已确认状态，不表示刚发生换装。仅绘图时读取，不复述目录。"
            "画自己或用当前绑定卡选 source=self，指定卡选 source=card 配 preset_id；无卡角色选 source=custom，风景用 []，不合并未选中的卡。"
            "身份、精确角色词、画风和负面词由插件直接应用，无需抄写。按本次要求和可见剧情组织新的动作、场景与构图，不直接沿用上次画图提示词。"
            "绘图插件会直接发送图片，工具不返回待发送图片地址；无需另用 send_message_to_user 发图，禁止编造 URL。出图后直接正常回复一次。"
        )
        if self._contextual_cards_enabled():
            rules += "明确换装可用完整 outfit；默认 state_scope=image 仅本图，实际剧情换装才用 story。仅用户要求改画风时填写 style。"
        else:
            rules += "当前为严格模式：各角色 prompt 写动作和明确修改，不使用 outfit/style 或剧情状态字段。"
        if event.get_extra("nai_marker_dual"):
            rules = ("用户使用绘图暗号，本条绘图已由插件独立排队，无需你写 Tag、描述绘图意图或调用绘图工具。"
                "请按当前人设正常回应用户与剧情。插件在你的回复后独立读取同一会话生成图片，不要提前声称图片已发送。")
        elif not self._mode_allows("llm_tool"):
            rules = "当前未开放主模型绘图工具，仅将最近绘图记录用于理解对话；正常聊天，不调用绘图工具。绘图设想不表示剧情已经改变。"
        previous = self._previous_drawing(view) if self._drawing_llm_id() else {}
        context = marker + "\n" + rules + "\n" + json.dumps({
            "self_card": {"id": active.get("id", ""), "name": active.get("name", "")} if active else None,
            "available_cards": catalog,
            "recent_drawing": {"user_request": previous.get("user_request", ""), "delivered": True,
                "note": "这是已完成的绘图记录，不是本轮绘图请求，不能据此再次调用工具；图中设想不代表剧情事实。"} if previous else None}, ensure_ascii=False) + "\n" + end
        # AstrBot >= 4.27.3 preserves this flag through provider assembly and
        # strips the part when saving history, including checkpoint histories.
        req.extra_user_content_parts.append(TextPart(text=context).mark_as_temp())
        # Hook timing is the whole Agent turn, including tool execution; it is
        # deliberately not presented as an individual HTTP request duration.
        event.set_extra("nai_main_hook_started", time.perf_counter())
        provider_id, provider = "", None
        try:
            selected = event.get_extra("selected_provider")
            provider = self.context.get_provider_by_id(selected) if isinstance(selected, str) and selected else await self.context.get_using_provider_async(event.unified_msg_origin)
            config = getattr(provider, "provider_config", {})
            provider_id = config.get("id", "") if isinstance(config, dict) else ""
        except Exception:
            pass  # Diagnostics must not interfere with the main chat.
        event.set_extra("nai_main_provider", provider_id)
        descriptor = diagnostics.provider_details(provider)
        try:
            streaming = self.context.get_config(event.unified_msg_origin).get("provider_settings", {}).get("streaming_response")
            if isinstance(streaming, bool):
                descriptor["chat_streaming_configured"] = streaming
        except Exception:
            pass
        if isinstance(getattr(req, "model", None), str):
            descriptor["request_model_override"] = req.model
        self._planner_log(event, provider_id, "main", 0, None, phase="main_request_hook", status="observed",
            provider_details=descriptor, timing_scope="agent_turn_including_tools",
            system_prompt_chars=len(req.system_prompt or ""), prompt_chars=len(getattr(req, "prompt", "") or ""),
            context_messages=len(getattr(req, "contexts", None) or []),
            image_count=len(getattr(req, "image_urls", None) or []), temporary_drawing_chars=len(context))

    # --------------------------- permissions and dedupe ---------------------------
    @filter.custom_filter(DrawingMarkerFilter, priority=100)
    async def drawing_marker_event(self, event: AstrMessageEvent) -> None:
        body = planner.marker_request(str(event.get_message_str() or ""), str(self._cfg("drawing_trigger", "n")))
        if body is None or not self._drawing_llm_id() or not self._mode_allows("command"):
            return
        allowed, reason = self._can_use(event)
        if not allowed:
            await self._notify(event, reason, "error")
            event.stop_event()
            return
        if len(body) > 8000:
            await self._notify(event, "画面描述不能超过 8000 字符。", "error")
            event.stop_event()
            return
        if not self._claim_llm_tool_invocation(event):
            return
        event.set_extra("nai_marker_dual", True)
        self._stamp_drawing(event)
        event.set_extra("nai_marker_body", body)
        event.set_extra("nai_planner_provider", self._drawing_llm_id())
        event.set_extra("nai_planner_view", await self._story_view(event))
        event.set_extra("nai_planner_pending", {"prompt": body or "根据当前对话绘制当前场景", "entry": "marker"})
        event.set_extra("nai_planner_queued_at", time.perf_counter())
        self._planner_log(event, self._drawing_llm_id(), self._planner_protocol(self._drawing_llm_id()), 0, None,
            phase="waiting_reply", status="queued")
        event.set_extra("sticker_consumed", True)
        # A matching marker also wakes normal chat in groups without requiring
        # the main model to call a tool. Do not mark the LLM as already handled.
        event.is_at_or_wake_command = True

    def _drawing_llm_id(self) -> str:
        return str(self._cfg("drawing_llm_provider_id", "") or "").strip()

    def _drop_drawing_memory(self, key) -> None:
        memory = getattr(self, "_drawing_memory", {}).pop(key, {})
        if memory.get("image"):
            try:
                Path(memory["image"]).unlink(missing_ok=True)
            except OSError:
                pass

    def _previous_drawing(self, view: drawing.StoryView | None) -> dict:
        for key, row in list(getattr(self, "_drawing_memory", {}).items()):
            if time.time() - row["created_at"] > 1800:
                self._drop_drawing_memory(key)
        if not view:
            return {}
        row = getattr(self, "_drawing_memory", {}).get(view.scope, {})
        if not row:
            return {}
        index = row["position"]
        actual = view.history[index][1] if index < len(view.history) and view.history[index][0] == "user" else ""
        valid_source = row.get("direct", False) or row["turn"] == view.turn or bool(row["message"] and (actual == row["message"] or actual.startswith(row["message"] + "\n")))
        cards_current = all(self._get_preset(card_id) and drawing.StoryLedger.signature(drawing.card_layers(self._get_preset(card_id))) == signature
            for card_id, signature in row["cards"].items())
        if row["epoch"] != view.epoch or not self._ledger().valid(view) or not valid_source or not cards_current:
            self._drop_drawing_memory(view.scope)
            return {}
        return row

    def _remember_planned_drawing(self, event, result: GenerationResult) -> None:
        plan = event.get_extra("nai_planner_plan")
        view = event.get_extra("nai_generation_view")
        if not plan or not view or not self._ledger().valid(view):
            return
        if not hasattr(self, "_drawing_memory"):
            self._drawing_memory = {}
        sequence = event.get_extra("nai_drawing_sequence", 0)
        if self._drawing_memory.get(view.scope, {}).get("sequence", 0) > sequence:
            return  # An older background image must not replace a newer edit.
        self._drop_drawing_memory(view.scope)
        position = next((i for i in range(len(view.history) - 1, -1, -1)
            if view.history[i][0] == "user" and (view.history[i][1] == view.message or view.history[i][1].startswith(view.message + "\n"))), len(view.history))
        row = {"plan": plan, "epoch": view.epoch, "turn": view.turn, "position": position,
            "message": view.message, "user_request": str(event.get_message_str() or ""), "direct": bool(event.get_extra("nai_direct_command")),
            "created_at": time.time(), "image": "", "cards": {}, "sequence": sequence}
        for detail in event.get_extra("nai_drawing_details", []):
            card = self._get_preset(detail.get("preset_id", ""))
            if card:
                row["cards"][card["id"]] = drawing.StoryLedger.signature(drawing.card_layers(card))
        if self._as_bool(self._cfg("drawing_llm_use_images", False)) and result.path:
            source = Path(result.path)
            if source.exists() and source.stat().st_size <= 8 * 1024 * 1024:
                target = self.temp_dir / ("planner-" + uuid.uuid4().hex + source.suffix)
                shutil.copyfile(source, target)
                row["image"] = str(target)
                async def expire():
                    await asyncio.sleep(1800)
                    target.unlink(missing_ok=True)
                asyncio.create_task(expire())
        self._drawing_memory[view.scope] = row
        while len(self._drawing_memory) > 8:
            self._drop_drawing_memory(next(iter(self._drawing_memory)))

    def _planner_protocol(self, provider_id):
        mode = self._cfg("drawing_llm_output_mode", "auto")
        if mode != "auto":
            return mode
        try:
            provider = self.context.get_provider_by_id(provider_id)
            kind = str(provider.provider_config.get("type", "")).lower()
            return "tool" if "openai" in kind else "json"
        except Exception:
            return "json"

    def _planner_log(self, event, provider_id, mode, attempt, response, error=None, **metrics):
        text = str(getattr(response, "completion_text", "") or "")
        names = getattr(response, "tools_call_name", []) or []
        secrets = [str(self._cfg(key, "") or "") for key in ("api_token", "api_key")]
        if mode != "image" and provider_id:
            try:
                secrets += diagnostics.secrets_from(self.context.get_provider_by_id(provider_id))
            except Exception:
                pass
        detail = {"provider": provider_id, "protocol": mode, "attempt": attempt,
            "request_id": hashlib.sha256(self._event_key(event, "", "generate").encode()).hexdigest()[:10],
            "text_length": len(text), "tool_calls": names,
            "status": "ok" if error is None else getattr(error, "code", "validation"),
            "error": diagnostics.sanitize(str(error), secrets)[:300] if error else ""}
        detail.update(metrics)
        if response is not None:
            detail["response_details"] = diagnostics.response_details(response)
        if response is not None and self._as_bool(self._cfg("drawing_llm_debug", False)):
            preview = planner.response_output(response)
            # Main replies remain private even when drawing previews are enabled.
            if mode != "main":
                detail["response_preview"] = diagnostics.sanitize(preview, secrets)[:2000]
        detail = diagnostics.sanitize(detail, secrets)
        snapshots = event.get_extra("nai_diagnostic_snapshots", {})
        if detail.get("phase") in {"main_request_hook", "main_response_hook", "main_http_request", "main_http_end", "main_http_error", "request", "response", "image_end", "failed", "http_request", "http_headers", "http_end", "http_error", "selection_repair", "image_tool_budget"}:
            snapshots[detail["phase"]] = detail.copy()
            event.set_extra("nai_diagnostic_snapshots", snapshots)
        detail["snapshots"] = dict(snapshots)
        # Preserve a bounded content-free timeline in the WebUI's latest snapshot.
        timeline = event.get_extra("nai_diagnostic_timeline", [])
        timeline.append({key: detail[key] for key in ("phase", "protocol", "attempt", "status", "elapsed_ms", "provider_ms", "validation_ms", "total_ms") if key in detail})
        event.set_extra("nai_diagnostic_timeline", timeline[-24:])
        detail["timeline"] = timeline[-24:]
        if not hasattr(self, "_planner_diagnostics"):
            self._planner_diagnostics = {}
        self._planner_diagnostics[str(event.unified_msg_origin)] = detail
        while len(self._planner_diagnostics) > 20:
            self._planner_diagnostics.pop(next(iter(self._planner_diagnostics)))
        # Timeline is useful in the WebUI snapshot; repeating it on every log
        # line obscures the individual phases and grows logs unnecessarily.
        title, level, console = diagnostics.console_entry(detail)
        getattr(logger, level)(f"[{PLUGIN_NAME}] {title}: {json.dumps(console, ensure_ascii=False)}")

    async def _drawing_planning_failed(self, event, exc):
        error = exc if isinstance(exc, planner.PlanError) else planner.PlanError(
            str(exc), "validation" if isinstance(exc, ValueError) else "internal", False)
        self._planner_log(event, self._drawing_llm_id(), self._planner_protocol(self._drawing_llm_id()), 0, None,
            error, phase="failed", **diagnostics.error_details(exc))
        message = str(exc) if isinstance(exc, ValueError) else "绘图准备或执行异常，本次任务已停止；请查看插件日志。"
        await self._notify(event, message, "error")

    async def _planner_wait_log(self, event, provider_id, mode, attempt, call_started, remaining):
        while True:
            await asyncio.sleep(30)
            elapsed = time.perf_counter() - call_started
            self._planner_log(event, provider_id, mode, attempt, None,
                phase="provider_wait", status="waiting", elapsed_ms=round(elapsed * 1000),
                remaining_seconds=round(max(0, remaining - elapsed), 1),
                timing_scope="provider_call_including_sdk", first_token_observable=False,
                stream_chunk_observable=bool(event.get_extra("nai_planner_streaming")),
                stream_metrics=event.get_extra("nai_planner_stream_metrics", {}),
                http_metrics=event.get_extra("nai_planner_http_metrics", {}))

    async def _validate_planned_drawing(self, event, plan, view=None):
        plan["subjects"] = await self._complete_card_selector(event, plan["subjects"])
        subjects = await self._resolve_drawing_subjects(event, plan["subjects"])
        if not self._contextual_cards_enabled() and ("style" in plan or any("outfit" in subject for subject in subjects)):
            raise ValueError("严格模式请将本图修改放入人物 prompt，省略 outfit/style。")
        drawing.tags(plan["prompt"], validate=True)
        style = plan.get("style")
        if "style" in plan:
            drawing.tags(style, validate=True)
            if len(drawing.join(plan["prompt"], style)) > 11500:
                raise ValueError("场景与画风合成过长，请缩短本次描述。")
        if self._contextual_cards_enabled():
            if style is not None and any(subject["preset"] for subject in subjects) and not self._drawing_style_override_requested(event):
                style = None
            self._apply_story_layers(subjects, view, "generate", style, plan["prompt"])
        for subject in subjects:
            drawing.tags(subject["prompt"], validate=True)
            if len(drawing.join(subject["preset"].get("positive_prompt", ""), subject["prompt"],
                    plan["prompt"] if len(subjects) == 1 else "", (style or "") if len(subjects) == 1 else "")) > 11500:
                raise ValueError("角色、服饰和场景合成过长，请缩短本次描述；身份锚点不能被截断。")

    async def _plan_drawing(self, event, seed: dict) -> dict:
        started = time.perf_counter()
        if not self._can_use(event)[0]:
            raise ValueError("当前会话已没有绘图权限，旧请求已取消。")
        provider_id = self._drawing_llm_id()
        if not provider_id or event.get_extra("nai_planner_provider", provider_id) != provider_id:
            raise ValueError("独立绘图 LLM 配置已更改，请重新提交绘图请求。")
        mode = self._planner_protocol(provider_id)
        self._planner_log(event, provider_id, mode, 0, None, phase="prepare", status="preparing")
        view = await self._story_view(event)
        expected = event.get_extra("nai_planner_view")
        if expected and (not view or expected.scope != view.scope or expected.epoch != view.epoch):
            raise ValueError("对话已切换或清空，已取消旧绘图请求。")
        previous = event.get_extra("nai_previous_drawing")
        if previous is None:
            previous = self._previous_drawing(view)
        native_snapshot = await self._native_drawing_snapshot(event) if self._shared_drawing_context() else None
        count = max(0, min(30, int(self._cfg("drawing_llm_context_messages", 12))))
        dialogue = event.get_extra("nai_planner_context")
        if dialogue is None:
            dialogue = planner.recent_dialogue(view.history if view else (), count)
        context_source = "main_request_snapshot" if event.get_extra("nai_planner_context") is not None else "current_conversation"
        dialogue = dialogue[-count:] if count else []
        if native_snapshot:
            dialogue = []  # Shared mode sends native messages exactly once.
        active = await self._resolve_active_preset(event)
        catalog = []
        for original in self.presets:
            if not self._as_bool(original.get("enabled", True), True):
                continue
            card = drawing.card_layers(original)
            catalog.append({"id": card["id"], "name": card.get("name", ""), "description": card.get("description", ""),
                "identity_prompt": card["identity_prompt"], "default_outfit_prompt": card["default_outfit_prompt"],
                "current_story_outfit": self._ledger().current(view, card).get("outfit")})
        context_ms = round((time.perf_counter() - started) * 1000)
        images = []
        image_sources = []
        if self._as_bool(self._cfg("drawing_llm_use_images", False)):
            if native_snapshot:
                await drawing_session.prepare_native_images(native_snapshot)
            native_images = drawing_session.image_urls(native_snapshot) if native_snapshot else []
            images = list(event.get_extra("nai_planner_images", []))[:2]
            image_sources = ["main_request_image"] * len(images)
            from_main = bool(native_snapshot and native_snapshot.source.startswith("main_"))
            if from_main and native_images:
                images, image_sources = [], []  # Already present in the native main messages.
            if not images and not (from_main and native_images) and hasattr(event, "get_messages"):
                components = [component for component in event.get_messages() if isinstance(component, Image)][:2]
                try:
                    images = [await asyncio.wait_for(component.convert_to_file_path(), timeout=15) for component in components]
                    image_sources = ["current_message_image"] * len(images)
                except Exception as exc:
                    self._planner_log(event, provider_id, mode, 0, None,
                        planner.PlanError("当前附图读取失败", "image_input", False), phase="prepare",
                        total_ms=round((time.perf_counter() - started) * 1000), **diagnostics.error_details(exc))
                    raise ValueError("当前附图读取失败，本次绘图已停止；可以关闭图片理解后重试。") from exc
            if (planner.needs_previous_image(event.get_extra("nai_marker_body", seed.get("prompt", ""))
                    or event.get_message_str()) and previous.get("image") and Path(previous["image"]).is_file()):
                images.append(previous["image"])
                image_sources.append("previous_drawing")
        image_prepare_ms = round((time.perf_counter() - started) * 1000) - context_ms
        payload = {"available_cards": catalog, "self_card_id": active.get("id") if active else None,
            "card_mode": self._cfg("llm_card_mode", "context"), "preferences": self._cfg("drawing_llm_prompt", ""),
            "user_request": event.get_message_str(),
            "drawing_request": event.get_extra("nai_marker_body", seed.get("prompt", "")) or "根据当前对话绘制当前场景",
            "recent_dialogue": dialogue, "current_reply": event.get_extra("nai_planner_reply", ""),
            "previous_drawing": previous.get("plan"),
            "previous_user_request": previous.get("user_request"),
            "images": {"current_user_images": len(images) - int(bool(previous.get("image") and previous["image"] in images)),
                "previous_drawing_attached": bool(previous.get("image") and previous["image"] in images)}}
        skill_file = "drawing-session.md" if native_snapshot else "drawing-planner.md"
        skill = (Path(__file__).parent / "skills" / "novelai-painter" / "references" / skill_file).read_text(encoding="utf-8")
        if native_snapshot:
            payload["session_reminders"] = event.get_extra("nai_session_reminders", "")
        payload["output_mode"] = mode
        kwargs = {"tools": planner.plan_tools(), "tool_choice": "required"} if mode == "tool" else {}
        try:
            provider = self.context.get_provider_by_id(provider_id)
        except Exception:
            provider = None
        main_streaming = None
        try:
            value = self.context.get_config(event.unified_msg_origin).get("provider_settings", {}).get("streaming_response")
            main_streaming = value if isinstance(value, bool) else None
        except Exception:
            pass
        transport = planner_transport.select(provider, str(self._cfg("drawing_llm_transport", "auto")), main_streaming=main_streaming)
        attempt = 0
        event.set_extra("nai_planner_http_metrics", {})
        def http_record(phase, values):
            event.set_extra("nai_planner_http_metrics", values)
            self._planner_log(event, provider_id, mode, attempt, None, phase=phase,
                status="observed", http_metrics=values)
        source_provider = provider
        provider, request_overrides = planner_transport.request_provider(provider,
            str(self._cfg("drawing_llm_reasoning_effort", "inherit")),
            max(0, min(16384, int(self._cfg("drawing_llm_max_tokens", 0)))),
            mode=mode, record=http_record, debug=self._as_bool(self._cfg("drawing_llm_debug", False)))
        request_local = provider is not source_provider
        provider_descriptor = diagnostics.provider_details(provider)
        schema_text = diagnostics.json_text(kwargs["tools"].openai_schema()) if mode == "tool" else ""
        # Bound the whole planning/reformat cycle instead of allowing another
        # full timeout after a slow first response. The SDK also has retries.
        budget = max(5, min(180, int(self._cfg("drawing_llm_timeout", 60))))
        deadline = time.perf_counter() + budget
        kwargs["request_max_retries"] = 1
        prepare_ms = round((time.perf_counter() - started) * 1000)
        attempts = 2 if self._as_bool(self._cfg("drawing_llm_format_retry", True), True) else 1
        for attempt in range(1, attempts + 1):
            current = await self._story_view(event)
            if self._drawing_llm_id() != provider_id or (view and (not current or current.scope != view.scope or current.epoch != view.epoch)):
                raise ValueError("绘图规划期间配置或对话已改变，旧任务已取消。")
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise planner.PlanError("绘图规划已用完超时预算，本次未请求图片服务。", "timeout", False)
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            shared_metrics = {}
            if native_snapshot:
                request_parts, system_prompt, shared_metrics = drawing_session.request_parts(native_snapshot,
                    payload, skill, max(4096, min(128000, int(self._cfg("drawing_llm_context_chars", 48000)))))
            else:
                request_parts, system_prompt = planner.request_parts(payload), skill
            call_started = time.perf_counter()
            metrics = {"phase": "request", "status": "requesting", "request_chars": len(encoded), "context_messages": len(dialogue),
                "image_count": len(images), "prepare_ms": prepare_ms,
                "remaining_seconds": round(remaining, 1),
                "provider_details": provider_descriptor,
                "transport": "provider.text_chat_stream" if transport == "stream" else "provider.text_chat" if request_local else "context.llm_generate/text_chat",
                "streaming": transport == "stream", "first_token_observable": False,
                "stream_chunk_observable": transport == "stream",
                "request_overrides": request_overrides, "request_max_retries": 1,
                "format_retry_enabled": attempts > 1, "tool_choice": kwargs.get("tool_choice", "none"),
                "request_layout": "shared_session" if native_snapshot else "native_dialogue", "main_streaming_configured": main_streaming,
                "message_roles": ["system"] + [m["role"] for m in request_parts["contexts"]] + ["user"],
                "context_source": context_source, "context_messages_configured": count,
                "context_disabled": count == 0 and not native_snapshot, "history_available": len(view.history) if view else 0,
                "context_prepare_ms": context_ms, "image_prepare_ms": image_prepare_ms,
                "image_sources": image_sources, "previous_plan_included": bool(previous.get("plan")),
                "previous_image_available": bool(previous.get("image")),
                "previous_image_policy": "requested_edit_or_continuation",
                "payload_size": diagnostics.text_size(encoded), "system_size": diagnostics.text_size(system_prompt),
                "system_fingerprint": diagnostics.fingerprint(system_prompt),
                "tool_schema_size": diagnostics.text_size(schema_text),
                "tool_schema_fingerprint": diagnostics.fingerprint(schema_text) if schema_text else "",
                "total_text_chars": drawing_session.text_chars(request_parts) + len(system_prompt) + len(schema_text),
                "native_image_count": len(drawing_session.image_urls(native_snapshot)) if native_snapshot else 0,
                "field_chars": {key: len(diagnostics.json_text(payload.get(key))) for key in (
                    "available_cards", "preferences", "user_request", "drawing_request", "recent_dialogue",
                    "current_reply", "previous_drawing", "format_correction") if key in payload}}
            metrics.update(shared_metrics)
            if native_snapshot:
                metrics["context_messages"] = len(request_parts["contexts"])
            if event.get_extra("nai_planner_queued_at") is not None:
                metrics["reply_wait_ms"] = round((started - event.get_extra("nai_planner_queued_at")) * 1000)
            self._planner_log(event, provider_id, mode, attempt, None, **metrics)
            wait_log = asyncio.create_task(self._planner_wait_log(event, provider_id, mode, attempt, call_started, remaining))
            def stream_record(phase, stream_metrics):
                event.set_extra("nai_planner_stream_metrics", stream_metrics)
                self._planner_log(event, provider_id, mode, attempt, None, phase=phase, status="streaming", **stream_metrics)
            event.set_extra("nai_planner_stream_metrics", {})
            event.set_extra("nai_planner_streaming", transport == "stream")
            try:
                if transport == "stream" or request_local:
                    provider_kwargs = {key: value for key, value in kwargs.items() if key != "tools"}
                    if "tools" in kwargs:
                        provider_kwargs["func_tool"] = kwargs["tools"]
                    provider_kwargs.update(**request_parts, system_prompt=system_prompt, image_urls=images or None)
                    call = planner_transport.collect_stream(provider, provider_kwargs, stream_record) if transport == "stream" else provider.text_chat(**provider_kwargs)
                else:
                    call = self.context.llm_generate(chat_provider_id=provider_id, **request_parts,
                        system_prompt=system_prompt, image_urls=images or None, **kwargs)
                response = await asyncio.wait_for(call, timeout=remaining)
            except planner.PlanError as exc:
                self._planner_log(event, provider_id, mode, attempt, None, exc, phase="response", retry=False,
                    elapsed_ms=round((time.perf_counter() - call_started) * 1000))
                raise
            except asyncio.TimeoutError as exc:
                self._planner_log(event, provider_id, mode, attempt, None, planner.PlanError("规划总超时", "timeout", False),
                    phase="response", elapsed_ms=round((time.perf_counter() - call_started) * 1000),
                    retry=False, http_metrics=event.get_extra("nai_planner_http_metrics", {}),
                    stream_metrics=event.get_extra("nai_planner_stream_metrics", {}), **diagnostics.error_details(exc))
                raise planner.PlanError("独立绘图 LLM 超时，本次未请求图片服务。", "timeout", False) from exc
            except Exception as exc:
                if mode == "tool" and getattr(exc, "status_code", None) in {400, 422} and re.search(r"tool_choice|required", str(exc), re.I):
                    error = planner.PlanError("绘图接口不支持强制工具调用，请将输出协议切换为 JSON 后重试。", "tool_choice_unsupported", False)
                    self._planner_log(event, provider_id, mode, attempt, None, error, phase="response", retry=False,
                        elapsed_ms=round((time.perf_counter() - call_started) * 1000), **diagnostics.error_details(exc))
                    raise error from exc
                if mode == "json" and getattr(exc, "status_code", None) in {400, 422} and re.search(r"response_format|json_object", str(exc), re.I):
                    error = planner.PlanError("绘图接口不支持 JSON 格式约束，请将输出协议改为自动或参数工具。", "json_mode_unsupported", False)
                    self._planner_log(event, provider_id, mode, attempt, None, error, phase="response",
                        elapsed_ms=round((time.perf_counter() - call_started) * 1000), retry=False,
                        http_metrics=event.get_extra("nai_planner_http_metrics", {}), **diagnostics.error_details(exc))
                    raise error from exc
                if re.search(r"content_filter|内容安全|被拒绝", str(exc), re.I):
                    error = planner.PlanError("绘图 LLM 服务拒绝了该请求，本次未请求图片服务。", "refusal", False)
                    self._planner_log(event, provider_id, mode, attempt, None, error,
                        phase="response", elapsed_ms=round((time.perf_counter() - call_started) * 1000),
                        retry=False, **diagnostics.error_details(exc))
                    raise error from exc
                self._planner_log(event, provider_id, mode, attempt, None,
                    planner.PlanError(f"{type(exc).__name__}: {exc}", "provider", False),
                    phase="response", elapsed_ms=round((time.perf_counter() - call_started) * 1000),
                    retry=False, **diagnostics.error_details(exc))
                raise ValueError("独立绘图 LLM 调用失败，请检查 Provider；工具不兼容时可将输出协议切换为 JSON 文本。") from exc
            except asyncio.CancelledError:
                self._planner_log(event, provider_id, mode, attempt, None,
                    planner.PlanError("调用被取消", "cancelled", False), phase="response",
                    elapsed_ms=round((time.perf_counter() - call_started) * 1000), retry=False)
                raise
            finally:
                wait_log.cancel()
                with suppress(asyncio.CancelledError):
                    await wait_log
            received_at = time.perf_counter()
            provider_ms = round((received_at - call_started) * 1000)
            self._planner_log(event, provider_id, mode, attempt, response, phase="received", status="received",
                provider_ms=provider_ms, timing_scope="provider_call_including_sdk",
                **event.get_extra("nai_planner_stream_metrics", {}))
            try:
                plan = planner.response_plan(response)
                if not self._contextual_cards_enabled():
                    for subject in plan["subjects"]:
                        subject.pop("state_scope", None)
                await self._validate_planned_drawing(event, plan, current)
            except ValueError as exc:
                retry = getattr(exc, "retryable", True) and attempt < attempts and deadline - time.perf_counter() >= 3
                self._planner_log(event, provider_id, mode, attempt, response, exc,
                    phase="response", elapsed_ms=round((time.perf_counter() - call_started) * 1000),
                    provider_ms=provider_ms, validation_ms=round((time.perf_counter() - received_at) * 1000), retry=retry)
                if not retry:
                    raise
                payload["format_correction"] = {"error": str(exc)[:500],
                    "previous_output": planner.response_output(response)[:16000],
                    "instruction": "修正格式或字段，重新核对最后一条用户要求（包括换装），提交完整方案。原生工具调用必须使用 API tool_calls，禁止用 XML、代码块或文字模拟。JSON 模式只输出完整 JSON 对象。"}
                continue
            self._planner_log(event, provider_id, mode, attempt, response,
                phase="response", elapsed_ms=round((time.perf_counter() - call_started) * 1000),
                provider_ms=provider_ms, validation_ms=round((time.perf_counter() - received_at) * 1000), retry=False,
                stream_metrics=event.get_extra("nai_planner_stream_metrics", {}),
                total_ms=round((time.perf_counter() - started) * 1000))
            break
        if self._drawing_llm_id() != provider_id:
            raise ValueError("绘图规划期间 Provider 已更改，旧任务已取消。")
        if not self._contextual_cards_enabled():
            for subject in plan["subjects"]:
                subject.pop("state_scope", None)
        current = await self._story_view(event)
        if view and (not current or current.scope != view.scope or current.epoch != view.epoch):
            raise ValueError("绘图规划期间对话已切换或清空，旧任务已取消。")
        if view:
            event.set_extra("nai_story_view", view)
        event.set_extra("nai_planner_plan", plan)
        return plan

    @filter.on_llm_response()
    async def drawing_capture_reply(self, event: AstrMessageEvent, response: LLMResponse) -> None:
        if event.get_extra("nai_main_hook_started") is not None:
            self._planner_log(event, event.get_extra("nai_main_provider", ""), "main", 0, response,
                phase="main_response_hook", status="observed", timing_scope="agent_turn_including_tools",
                hook_elapsed_ms=round((time.perf_counter() - event.get_extra("nai_main_hook_started")) * 1000))
        if not event.get_extra("nai_planner_pending") or getattr(response, "tools_call_name", None) or getattr(response, "role", "") != "assistant":
            return
        event.set_extra("nai_planner_reply", str(getattr(response, "completion_text", "") or ""))
        event.set_extra("nai_planner_reply_ready", True)

    @filter.on_decorating_result()
    async def drawing_stream_finished(self, event: AstrMessageEvent) -> None:
        # AstrBot sends the stream before yielding STREAMING_FINISH, and skips
        # OnAfterMessageSent for that result. Normal replies use the hook below.
        result = event.get_result()
        if result and getattr(result, "result_content_type", None) == ResultContentType.STREAMING_FINISH:
            await self.drawing_after_reply(event)

    @filter.after_message_sent()
    async def drawing_after_reply(self, event: AstrMessageEvent) -> None:
        pending = event.get_extra("nai_planner_pending")
        if not pending or not event.get_extra("nai_planner_reply_ready") or event.get_extra("nai_planner_consumed"):
            return
        event.set_extra("nai_planner_consumed", True)
        event.set_extra("sticker_consumed", True)
        owned = []
        try:
            if self._shared_drawing_context():
                await self._native_drawing_snapshot(event)
            branch = drawing_session.BranchEvent(event)
            branch.set_extra("nai_previous_drawing", copy.deepcopy(self._previous_drawing(event.get_extra("nai_planner_view"))))
            # Freeze files before the original event's cleanup runs. LLM and
            # image work begin only in the managed task, outside this hook.
            if self._as_bool(self._cfg("drawing_llm_use_images", False)):
                owned = drawing_session.pin_inputs(branch, self.temp_dir)
            tasks = getattr(self, "_planner_tasks", None)
            if tasks is None:
                self._planner_tasks = tasks = {}
            task = asyncio.create_task(self._run_drawing_branch(branch, dict(pending), owned))
            tasks[task] = str(event.unified_msg_origin)
            def finished(completed):
                tasks.pop(completed, None)
                drawing_session.cleanup_inputs(owned)  # Also handles cancellation before first execution.
                if not completed.cancelled() and completed.exception() is not None:
                    logger.error(f"[{PLUGIN_NAME}] 后台绘图任务异常: {type(completed.exception()).__name__}")
            task.add_done_callback(finished)
            self._planner_log(branch, self._drawing_llm_id(), "task", 0, None,
                phase="background_submitted", status="queued", holds_chat_pipeline=False)
        except Exception as exc:
            drawing_session.cleanup_inputs(owned)
            await self._drawing_planning_failed(event, exc)

    async def _run_drawing_branch(self, event, pending, owned):
        try:
            entry = "command"
            if not self._drawing_llm_id() or not self._mode_allows(entry):
                raise ValueError("独立绘图 LLM 已关闭，本轮排队请求已取消。")
            plan = await self._plan_drawing(event, pending)
            await self._image_tool_entry(event, **plan, accepted=True, entry=entry)
        except Exception as exc:
            await self._drawing_planning_failed(event, exc)
        except asyncio.CancelledError:
            self._planner_log(event, self._drawing_llm_id(), "task", 0, None,
                phase="background_cancelled", status="cancelled")
            raise
        finally:
            drawing_session.cleanup_inputs(owned)

    def _event_key(self, event: AstrMessageEvent, prompt: str, operation: str) -> str:
        """Return one generation key per incoming message, regardless of tool rewrites."""
        message_obj = getattr(event, "message_obj", None)
        raw_id = None
        for attr in ("message_id", "msg_id", "id"):
            raw_id = getattr(message_obj, attr, None)
            if raw_id:
                break
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        if raw_id:
            return f"event:{origin}:{raw_id}"
        try:
            raw_message = str(event.get_message_str() or "").strip()
        except Exception:
            raw_message = ""
        raw = f"{origin}|{event.get_sender_id()}|{event.get_group_id() or ''}|{raw_message or prompt.strip()}"
        return "event-hash:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _llm_tool_claimed(self, event: AstrMessageEvent) -> bool:
        if getattr(event, "_novelai_painter_llm_tool_claim", False):
            return True
        key = self._event_key(event, "", "llm_tool")
        claims = getattr(self, "_llm_tool_claims", {})
        claimed_at = claims.get(key)
        if claimed_at is None:
            return False
        if time.time() - claimed_at <= max(300, int(self._cfg("dedupe_window_seconds", 30) or 30)):
            return True
        claims.pop(key, None)
        return False

    def _claim_llm_tool_invocation(self, event: AstrMessageEvent) -> bool:
        """Atomically accept one validated request before generation can await."""
        claim_attr = "_novelai_painter_llm_tool_claim"
        if getattr(event, claim_attr, False):
            return False
        key = self._event_key(event, "", "llm_tool")
        claims = getattr(self, "_llm_tool_claims", None)
        if claims is None:
            self._llm_tool_claims = {}
            claims = self._llm_tool_claims
        now = time.time()
        claim_ttl = max(300, int(self._cfg("dedupe_window_seconds", 30) or 30))
        for old_key, created_at in list(claims.items()):
            if now - created_at > claim_ttl:
                claims.pop(old_key, None)
        if key in claims:
            return False
        try:
            setattr(event, claim_attr, True)
        except Exception:
            pass
        claims[key] = now
        return True

    def _can_use(self, event: AstrMessageEvent) -> tuple[bool, str]:
        if event.is_private_chat():
            policy = str(self._cfg("private_access", "all"))
        else:
            policy = str(self._cfg("group_access", "admin_only"))
        is_admin = event.is_admin()
        sender = str(event.get_sender_id())
        group = str(event.get_group_id() or "")
        if policy == "disabled":
            return False, "当前会话未开启生图权限。"
        if policy == "admin_only":
            return (True, "") if is_admin else (False, "当前会话仅允许管理员使用生图功能。")
        if policy == "allowlist":
            if self._as_bool(self._cfg("admin_bypass", True)) and is_admin:
                return True, ""
            allowed_users = set(self._as_list(self._cfg("allowed_users", [])))
            allowed_groups = set(self._as_list(self._cfg("allowed_groups", [])))
            if sender not in allowed_users and (not group or group not in allowed_groups):
                return False, "当前用户或群聊不在生图白名单中。"
        return True, ""

    def _cleanup_expired(self) -> None:
        now = time.time()
        window = max(1, int(self._cfg("dedupe_window_seconds", 30) or 30))
        for key, (created, _) in list(self._recent_jobs.items()):
            if now - created > window:
                self._recent_jobs.pop(key, None)
        for job_id, delivered_at in list(getattr(self, "_delivered_jobs", {}).items()):
            if now - delivered_at > max(window, 300):
                self._delivered_jobs.pop(job_id, None)
        for key, claimed_at in list(getattr(self, "_llm_tool_claims", {}).items()):
            if now - claimed_at > max(window, 300):
                self._llm_tool_claims.pop(key, None)
        max_age = max(60, int(self._cfg("auto_clean_delay", 300) or 300) + 60)
        for path in self.temp_dir.glob("*"):
            try:
                age_limit = 1800 if path.name.startswith("planner-") else max_age
                if path.is_file() and now - path.stat().st_mtime > age_limit:
                    path.unlink(missing_ok=True)
            except Exception:
                pass

    # --------------------------- API response handling ---------------------------
    @staticmethod
    def _decode_zip(body: bytes) -> bytes | None:
        if not body.startswith(b"PK"):
            return None
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                names = [name for name in archive.namelist() if not name.endswith("/") and name.lower().endswith((".png", ".webp", ".jpg", ".jpeg"))]
                if len(names) > 1:
                    logger.warning(f"[{PLUGIN_NAME}] 服务端返回 {len(names)} 张图片，仅保留并发送第一张")
                if not names:
                    return None
                info = archive.getinfo(names[0])
                if info.file_size > MAX_IMAGE_BYTES:
                    raise ProviderError("response_too_large", "图片服务返回的图片超过 32MB 安全上限")
                return archive.read(info)
        except ProviderError:
            raise
        except Exception:
            return None

    @staticmethod
    def _decode_data_url(value: str) -> bytes:
        if value.startswith("data:") and "," in value:
            value = value.split(",", 1)[1]
        return base64.b64decode(value)

    @staticmethod
    async def _read_limited_response(response: aiohttp.ClientResponse, limit: int = MAX_RESPONSE_BYTES) -> bytes:
        try:
            content_length = int(response.headers.get("Content-Length", "") or 0)
        except (TypeError, ValueError):
            content_length = 0
        if content_length > limit:
            raise ProviderError("response_too_large", "图片服务响应超过安全上限")
        data = bytearray()
        async for chunk in response.content.iter_chunked(64 * 1024):
            data.extend(chunk)
            if len(data) > limit:
                raise ProviderError("response_too_large", "图片服务响应超过安全上限")
        return bytes(data)

    @staticmethod
    def _image_extension(image_bytes: bytes) -> str:
        if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if image_bytes.startswith(b"\xff\xd8"):
            return ".jpg"
        if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
            return ".webp"
        raise ProviderError("invalid_image", "图片服务返回了不受支持的图片格式")

    def _save_image(self, image_bytes: bytes, job_id: str) -> str:
        if len(image_bytes) > MAX_IMAGE_BYTES:
            raise ProviderError("response_too_large", "图片服务返回的图片超过 32MB 安全上限")
        path = self.temp_dir / f"{job_id}{self._image_extension(image_bytes)}"
        path.write_bytes(image_bytes)
        return str(path)

    async def _parse_image_response(self, response: aiohttp.ClientResponse, body: bytes, job_id: str) -> str:
        content_type = response.headers.get("Content-Type", "").lower()
        image_bytes = self._decode_zip(body)
        if image_bytes is None and body.startswith(b"\x89PNG"):
            image_bytes = body
        if image_bytes is None and (
            body.startswith(b"\xff\xd8")
            or (body[:4] == b"RIFF" and body[8:12] == b"WEBP")
        ):
            image_bytes = body
        if image_bytes is None and ("json" in content_type or body[:1] in {b"{", b"["}):
            try:
                data = json.loads(body.decode("utf-8"))
                items = data if isinstance(data, list) else data.get("data", [])
                first = items[0] if items else {}
                if first.get("b64_json"):
                    image_bytes = self._decode_data_url(first["b64_json"])
                elif first.get("url"):
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
                        async with session.get(first["url"]) as img_resp:
                            if img_resp.status >= 400:
                                raise ProviderError("image_download", "图片地址下载失败")
                            image_bytes = await self._read_limited_response(img_resp, MAX_IMAGE_BYTES)
            except ProviderError:
                raise
            except Exception as exc:
                raise ProviderError("response_parse", f"响应解析失败: {exc}")
        if not image_bytes:
            raise ProviderError("response_parse", "服务端返回中没有可用图片")
        return self._save_image(image_bytes, job_id)

    def _merged_negative_prompt(self, negative_override: str = "") -> str:
        negative_parts = [
            str(self._cfg("negative_prompt", DEFAULT_NEGATIVE) or "").strip(),
            str(negative_override or "").strip(),
        ]
        return ", ".join(dict.fromkeys(part for part in negative_parts if part))

    def _openai_prompt_with_negative(self, prompt: str, negative_override: str = "") -> str:
        negative = self._merged_negative_prompt(negative_override)
        if not negative:
            return prompt
        return f"{prompt}. Avoid these negative prompt traits: {negative}"

    def _official_parameters(self, prompt: str, operation: str, image_b64: str | None, reference: dict[str, Any] | None, negative_override: str = "", preset: dict[str, Any] | None = None, *, size: tuple[int, int] | None = None, character_prompts: list[dict[str, str]] | None = None) -> dict[str, Any]:
        negative = self._merged_negative_prompt(negative_override)
        params: dict[str, Any] = {
            "params_version": 3,
            "width": max(64, min(2048, int(self._cfg("width", 832) or 832))),
            "height": max(64, min(2048, int(self._cfg("height", 1216) or 1216))),
            "scale": max(1.0, min(20.0, float(self._cfg("scale", 5.0) or 5.0))),
            "sampler": str(self._cfg("sampler", "k_euler_ancestral") or "k_euler_ancestral"),
            "steps": max(1, min(50, int(self._cfg("steps", 28) or 28))),
            "n_samples": 1,
            "ucPreset": 0,
            "qualityToggle": self._preset_quality_toggle(preset),
            "negative_prompt": negative,
        }
        if size is not None:
            params["width"], params["height"] = size
        if image_b64:
            params.update({
                "image": image_b64,
                "strength": max(0.0, min(1.0, float(self._cfg("img2img_strength", 0.7) or 0.7))),
                "noise": max(0.0, min(1.0, float(self._cfg("img2img_noise", 0.1) or 0.1))),
                "color_correct": self._as_bool(self._cfg("img2img_color_correct", True)),
            })
        if self._supports_character_prompts():
            params.update({
                "use_coords": False,
                "v4_prompt": {"caption": {"base_caption": prompt, "char_captions": []}, "use_coords": False, "use_order": True},
                "v4_negative_prompt": {"caption": {"base_caption": negative, "char_captions": []}, "legacy_uc": False},
            })
            for character in character_prompts or []:
                # Matching order/centers keep positive and negative traits local
                # to the same subject. Let NovelAI place subjects automatically.
                centers = [{"x": 0.5, "y": 0.5}]
                params["v4_prompt"]["caption"]["char_captions"].append({
                    "char_caption": character["prompt"], "centers": centers,
                })
                params["v4_negative_prompt"]["caption"]["char_captions"].append({
                    "char_caption": character["negative_prompt"], "centers": centers,
                })
        if reference and reference.get("image_b64"):
            ref_type = reference.get("reference_type", reference.get("type", "character"))
            caption_type = "character&style" if ref_type in {"both", "character&style"} else ("style" if ref_type == "style" else "character")
            params.update({
                "director_reference_images": [reference["image_b64"]],
                "director_reference_descriptions": [{"caption": {"base_caption": caption_type, "char_captions": []}, "use_coords": False, "use_order": True}],
                "director_reference_strength_values": [max(0.0, min(1.0, float(self._cfg("reference_strength", 0.6) or 0.6)))],
                "director_reference_secondary_strength_values": [max(0.0, min(1.0, float(self._cfg("reference_fidelity", 0.6) or 0.6)))],
                "director_reference_information_extracted": [max(0.0, min(1.0, float(self._cfg("reference_information_extracted", 1.0) or 1.0)))],
            })
        return params

    async def _call_official(self, prompt: str, operation: str, job_id: str, image_b64: str | None, reference: dict[str, Any] | None, negative_override: str = "", preset: dict[str, Any] | None = None, *, size: tuple[int, int] | None = None, character_prompts: list[dict[str, str]] | None = None) -> str:
        base_url = str(self._cfg("base_url", "https://image.novelai.net") or "https://image.novelai.net").strip().rstrip("/")
        url = base_url if base_url.endswith("/ai/generate-image") else f"{base_url}/ai/generate-image"
        token = str(self._cfg("api_token", "") or "").strip()
        if not token:
            raise ProviderError("not_configured", "NovelAI 官方 Token 尚未配置")
        headers = {"Authorization": token if token.lower().startswith("bearer ") else f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/zip, application/json"}
        payload = {"input": prompt, "model": self._active_model(), "action": "generate", "parameters": self._official_parameters(prompt, operation, image_b64, reference, negative_override, preset, size=size, character_prompts=character_prompts)}
        return await self._post_image_request(url, headers, payload, job_id)

    async def _call_openai(
        self,
        prompt: str,
        operation: str,
        job_id: str,
        image_bytes: bytes | None,
        negative_override: str = "",
        *,
        size: tuple[int, int] | None = None,
    ) -> str:
        base_url = str(self._cfg("openai_base_url", "") or "").strip().rstrip("/")
        api_key = str(self._cfg("api_key", "") or "").strip()
        if not base_url or not api_key:
            raise ProviderError("not_configured", "OpenAI 兼容模式的 Base URL 或 API Key 尚未配置")
        endpoint_key = "openai_edit_endpoint" if image_bytes else "openai_image_endpoint"
        endpoint = str(self._cfg(endpoint_key, "/v1/images/edits" if image_bytes else "/v1/images/generations") or "")
        url = endpoint if endpoint.startswith("http") else f"{base_url}/{endpoint.lstrip('/')}"
        headers = self._openai_headers(api_key)
        prompt = self._openai_prompt_with_negative(prompt, negative_override)
        width, height = size or (int(self._cfg("width", 832)), int(self._cfg("height", 1216)))
        timeout = aiohttp.ClientTimeout(total=180)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                if image_bytes:
                    form = aiohttp.FormData()
                    form.add_field("model", self._active_model())
                    form.add_field("prompt", prompt)
                    form.add_field("n", "1")
                    form.add_field("size", f"{width}x{height}")
                    form.add_field("image", image_bytes, filename="input.png", content_type="image/png")
                    async with session.post(url, headers=headers, data=form) as response:
                        body = await self._read_limited_response(response)
                        if response.status >= 400:
                            raise self._http_error(response.status, body, response.headers)
                        return await self._parse_image_response(response, body, job_id)
                payload = {"model": self._active_model(), "prompt": prompt, "n": 1, "size": f"{width}x{height}", "response_format": "b64_json"}
                async with session.post(url, headers={**headers, "Content-Type": "application/json"}, json=payload) as response:
                    body = await self._read_limited_response(response)
                    if response.status >= 400:
                        raise self._http_error(response.status, body, response.headers)
                    return await self._parse_image_response(response, body, job_id)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise ProviderError("network", "图片服务网络请求失败；为避免重复扣费，本任务不会自动重试", retryable=False) from exc

    @staticmethod
    def _http_error(status: int, body: bytes, headers: Any = None) -> ProviderError:
        retry_after = None
        if headers:
            try:
                retry_after = float(headers.get("Retry-After", "") or 0) or None
            except (TypeError, ValueError):
                retry_after = None
        if status == 429:
            return ProviderError("429", "图片服务触发频率限制，请稍后再试", retryable=True, retry_after=retry_after)
        if status in {401, 403}:
            return ProviderError("auth", "图片服务认证失败，请检查 Key 或 Token", retryable=False)
        if status == 402:
            return ProviderError("quota", "图片服务额度不足", retryable=False)
        if status == 503:
            return ProviderError("http_503", "图片服务返回 HTTP 503", retryable=True, retry_after=retry_after)
        return ProviderError(f"http_{status}", f"图片服务返回 HTTP {status}", retryable=False)

    def _retry_limit(self) -> int:
        mode = str(self._cfg("retry_mode", "none") or "none")
        configured = {"none": 1, "rate_limit_once": 2, "rate_limit_twice": 3}.get(mode, 1)
        try:
            hard_limit = max(1, min(3, int(self._cfg("max_api_requests_per_job", 3) or 3)))
        except (TypeError, ValueError):
            hard_limit = 1
        return min(configured, hard_limit)

    def _503_retries(self) -> int:
        try:
            return max(0, min(3, int(self._cfg("retry_503_count", 0))))
        except (TypeError, ValueError):
            return 0

    def _should_retry(self, error: ProviderError, attempts: int, failure_counts=None) -> bool:
        limits = {"429": self._retry_limit() - 1, "http_503": self._503_retries()}
        failures = failure_counts.get(error.code, 0) if failure_counts is not None else attempts
        return (error.retryable and error.code in limits and failures <= limits[error.code]
            and attempts < self._retry_limit() + self._503_retries())

    async def _post_image_request(self, url: str, headers: dict[str, str], payload: dict[str, Any], job_id: str) -> str:
        timeout = aiohttp.ClientTimeout(total=180)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                async with session.post(url, headers=headers, json=payload) as response:
                    body = await self._read_limited_response(response)
                    if response.status not in {200, 201}:
                        raise self._http_error(response.status, body, response.headers)
                    return await self._parse_image_response(response, body, job_id)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise ProviderError("network", "图片服务网络请求失败；为避免重复扣费，本任务不会自动重试", retryable=False) from exc

    async def _load_reference(self, reference_id: str | None) -> tuple[bytes | None, dict[str, Any] | None]:
        if not reference_id:
            return None, None
        safe_id = Path(str(reference_id)).name
        meta_path = self.reference_dir / f"{safe_id}.json"
        if not meta_path.exists():
            return None, None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            reference_root = self.reference_dir.resolve()
            image_path = (self.reference_dir / str(meta.get("filename", ""))).resolve()
            if not image_path.exists() or image_path.parent != reference_root:
                return None, None
            data = image_path.read_bytes()
            return data, {**meta, "image_b64": base64.b64encode(data).decode("ascii")}
        except Exception:
            return None, None

    @staticmethod
    def _cached_result(result: GenerationResult) -> GenerationResult:
        return GenerationResult(result.ok, result.job_id, result.provider, result.path, result.error_code, result.message, result.attempts, False)

    async def _run_job(
        self,
        event: AstrMessageEvent,
        prompt: str,
        operation: str = "generate",
        preset_id: str | None = None,
        reference_id: str | None = None,
        reference_type: str | None = None,
        *,
        preset_override: dict[str, Any] | None = None,
        size: tuple[int, int] | None = None,
        quiet: bool = False,
        job_kind: str = "image",
        emotion: str = "",
        character_prompts: list[dict[str, str]] | None = None,
    ) -> GenerationResult:
        allowed, reason = self._can_use(event)
        job_id = uuid.uuid4().hex[:12]
        provider = self._provider_name()
        if not allowed:
            return GenerationResult(False, job_id, provider, error_code="permission", message=reason)
        if not prompt.strip():
            return GenerationResult(False, job_id, provider, error_code="invalid_prompt", message="请输入要生成的画面描述。")
        claim_attr = "_novelai_painter_generation_claim"
        claimed_job_id = getattr(event, claim_attr, None)
        if claimed_job_id:
            state = event.get_extra("nai_image_status", "running")
            stopped = state in {"failed", "cancelled"}
            return GenerationResult(
                not stopped,
                str(claimed_job_id),
                provider,
                error_code=state if stopped else None,
                message=f"本条消息的绘图状态为 {state}，重复生成请求已被拦截。",
                attempts=0,
                send_image=False,
            )
        try:
            setattr(event, claim_attr, job_id)
        except Exception:
            pass
        if operation == "img2img" and not self._as_bool(self._cfg("img2img_enabled", True)):
            return GenerationResult(False, job_id, provider, error_code="img2img_disabled", message="图生图功能当前已关闭。")
        if operation == "reference":
            if not self._as_bool(self._cfg("reference_enabled", True)):
                return GenerationResult(False, job_id, provider, error_code="reference_disabled", message="参考图功能当前已关闭。")
            if provider != "novelai_official":
                return GenerationResult(False, job_id, provider, error_code="unsupported", message="当前兼容后端不支持 NovelAI Precise Reference。")
        self._cleanup_expired()
        resolved_preset = preset_override if preset_override is not None else await self._resolve_active_preset(event, preset_id)
        resolved_preset_id = str(resolved_preset.get("id", "")) if resolved_preset else ""
        composed_prompt, active_preset_id, preset_negative = self._compose_prompt(
            prompt,
            None if job_kind == "sticker" else event,
            resolved_preset_id or None,
            # Snapshot the resolved selection, including an explicit empty
            # selection. Never fall back to a default card during composition.
            preset_override=resolved_preset or {},
        )
        if character_prompts and (provider != "novelai_official" or not self._supports_character_prompts()):
            sections = [composed_prompt, "Draw distinct subjects; keep each subject's traits separate."]
            for index, character in enumerate(character_prompts, 1):
                section = f"Subject {index}: {character['prompt']}"
                if character["negative_prompt"]:
                    section += f". Avoid for subject {index} only: {character['negative_prompt']}"
                sections.append(section)
            composed_prompt = "\n".join(sections)
        if active_preset_id:
            card_action = "已应用剧情外观" if (resolved_preset or {}).get("_contextual_appearance") else "已合并角色卡"
            logger.info(
                f"[{PLUGIN_NAME}] job={job_id} {card_action} {active_preset_id}: "
                f"正向={len(str((resolved_preset or {}).get('positive_prompt', '') or ''))} 字符, "
                f"负向={len(preset_negative)} 字符, 最终正向={len(composed_prompt)} 字符"
            )
            logger.debug(f"[{PLUGIN_NAME}] job={job_id} 最终正向提示词: {composed_prompt}")
        key = self._event_key(event, composed_prompt, operation)
        now = time.time()
        window = max(1, int(self._cfg("dedupe_window_seconds", 30) or 30))
        cached = self._recent_jobs.get(key)
        if cached and now - cached[0] <= window:
            return self._cached_result(cached[1])
        inflight_jobs = getattr(self, "_inflight_jobs", None)
        if inflight_jobs is None:
            self._inflight_jobs = {}
            inflight_jobs = self._inflight_jobs
        if existing_job_id := inflight_jobs.get(key):
            return GenerationResult(
                True,
                existing_job_id,
                provider,
                message="同一条用户消息的生成任务正在处理中，重复请求已被拦截。",
                attempts=0,
                send_image=False,
            )
        inflight_jobs[key] = job_id
        attempts = 0
        try:
            selected_preset = resolved_preset
            if not reference_id and selected_preset and operation in {"img2img", "reference"}:
                reference_id = str(selected_preset.get("reference_id", "")) or None
            effective_reference_type = reference_type
            if not effective_reference_type and operation == "reference" and selected_preset:
                effective_reference_type = str(selected_preset.get("reference_type", "character") or "character")
            if effective_reference_type not in {"character", "style", "both", "character&style"}:
                effective_reference_type = "character"
            image_bytes, reference = await self._load_reference(reference_id)
            if operation in {"img2img", "reference"} and not image_bytes:
                result = GenerationResult(False, job_id, provider, error_code="missing_reference", message="该操作需要参考图，请在 WebUI 上传并绑定到本次选中的角色卡。", attempts=0)
                self._recent_jobs[key] = (time.time(), result)
                return result
            if reference:
                reference["reference_type"] = effective_reference_type or "character"
            image_b64 = base64.b64encode(image_bytes).decode("ascii") if image_bytes and provider == "novelai_official" else None
            failure_counts = {}
            while True:
                queue_started = time.perf_counter()
                try:
                    timeout = max(1, int(self._cfg("queue_timeout", 120) or 120))
                    if not quiet and self.lock.locked() and self._as_bool(self._cfg("show_queue_notice", True)):
                        await self._notify(event, "当前生图通道繁忙，任务已排队。", "queue")
                    await asyncio.wait_for(self.lock.acquire(), timeout=timeout)
                except asyncio.TimeoutError:
                    result = GenerationResult(False, job_id, provider, error_code="queue_timeout", message="生图排队超时，任务已取消。", attempts=attempts)
                    break
                try:
                    try:
                        if not await self._generation_context_current(event):
                            raise ProviderError("stale_context", "对话已切换或清空，旧绘图任务已取消，请在当前对话重新请求。")
                        attempts += 1
                        self._capture_drawing_request(event, job_id, provider, composed_prompt,
                            operation, preset_negative, selected_preset, character_prompts,
                            reference if operation == "reference" else None, size)
                        self._planner_log(event, provider, "image", attempts, None,
                            phase="image_api_request", status="requesting", job_id=job_id,
                            queue_ms=round((time.perf_counter() - queue_started) * 1000),
                            prompt_chars=len(composed_prompt), operation=operation)
                        api_started = time.perf_counter()
                        if provider == "novelai_official":
                            path = await self._call_official(
                                composed_prompt,
                                operation,
                                job_id,
                                image_b64 if operation == "img2img" else None,
                                reference if operation == "reference" else None,
                                preset_negative,
                                selected_preset,
                                **({"size": size} if size is not None else {}),
                                **({"character_prompts": character_prompts} if character_prompts else {}),
                            )
                        else:
                            path = await self._call_openai(
                                composed_prompt,
                                operation,
                                job_id,
                                image_bytes if operation == "img2img" else None,
                                preset_negative,
                                **({"size": size} if size is not None else {}),
                            )
                        self._planner_log(event, provider, "image", attempts, None,
                            phase="image_api_response", status="received", job_id=job_id,
                            elapsed_ms=round((time.perf_counter() - api_started) * 1000))
                        if not await self._generation_context_current(event):
                            raise ProviderError("stale_context", "生成期间对话已切换或清空，旧图不再发送，请在当前对话重新请求。")
                    finally:
                        self.lock.release()
                    result = GenerationResult(True, job_id, provider, path=path, message="图片已生成并发送到当前会话。", attempts=attempts)
                    break
                except ProviderError as exc:
                    failure_counts[exc.code] = failure_counts.get(exc.code, 0) + 1
                    retry = self._should_retry(exc, attempts, failure_counts)
                    self._planner_log(event, provider, "image", attempts, None,
                        planner.PlanError(exc.message, exc.code, False), phase="image_api_error", job_id=job_id,
                        elapsed_ms=round((time.perf_counter() - queue_started) * 1000),
                        retry=retry)
                    logger.warning(f"[{PLUGIN_NAME}] job={job_id} attempt={attempts} provider={provider} code={exc.code}: {exc.message}")
                    if not retry:
                        result = GenerationResult(False, job_id, provider, error_code=exc.code, message=exc.message, attempts=attempts)
                        break
                    configured_delay = max(1.0, min(300.0, float(self._cfg("retry_delay", 5.0) or 5.0)))
                    delay = max(configured_delay, float(exc.retry_after or 0))
                    self._planner_log(event, provider, "image", attempts, None,
                        phase="image_retry_wait", status="waiting_retry", job_id=job_id, error_code=exc.code,
                        delay_seconds=delay, next_attempt=attempts + 1,
                        max_attempts=self._retry_limit() + self._503_retries())
                    if not quiet and self._as_bool(self._cfg("show_retry_notice", False)):
                        reason = "图片服务限流" if exc.code == "429" else "图片服务暂时不可用（503）"
                        await self._notify(event, f"{reason}，{delay:g} 秒后进行第 {attempts + 1} 次尝试。", "retry")
                    # Do not monopolize the global provider slot while waiting
                    # for a Retry-After window.
                    await asyncio.sleep(delay)
            self._recent_jobs[key] = (time.time(), result)
            self._record_image_job(result, operation, active_preset_id, character_prompts, job_kind, emotion)
            return result
        except (asyncio.CancelledError, Exception) as exc:
            code = "cancelled" if isinstance(exc, asyncio.CancelledError) else "internal_error"
            result = GenerationResult(False, job_id, provider, error_code=code,
                message="任务已取消；若请求已经发出，图片服务仍可能处理。" if code == "cancelled" else "图片任务异常终止。",
                attempts=attempts, send_image=False)
            self._recent_jobs[key] = (time.time(), result)
            self._record_image_job(result, operation, active_preset_id, character_prompts, job_kind, emotion)
            raise
        finally:
            if inflight_jobs.get(key) == job_id:
                inflight_jobs.pop(key, None)

    def _record_image_job(self, result, operation, preset_id, characters, kind, emotion):
        record = asdict(result)
        record["has_image"] = bool(record.pop("path", None))
        self._jobs.append({**record, "operation": operation, "preset_id": preset_id,
            "subject_preset_ids": [c["preset_id"] for c in characters or []],
            "kind": kind, "emotion": emotion, "created_at": int(time.time())})
        self._jobs = self._jobs[-50:]

    def _capture_drawing_request(self, event, job_id, provider, prompt, operation, negative,
                                 preset, characters, reference, size) -> None:
        if not hasattr(self, "_request_snapshots"):
            self._request_snapshots = {}
        origin = str(event.unified_msg_origin)
        # Diagnostic data stays in memory; omit credentials, images and history.
        snapshot = {"job_id": job_id, "created_at": int(time.time()), "provider": provider,
            "model": self._active_model(), "operation": operation,
            "prompt": prompt, "negative_prompt": self._merged_negative_prompt(negative),
            "character_prompts": characters or [], "subjects": event.get_extra("nai_drawing_details", []),
            "uses_reference": bool(reference) or operation == "img2img", "reference_type": (reference or {}).get("reference_type")}
        if provider == "novelai_official":
            snapshot["parameters"] = self._official_parameters(prompt, operation,
                "[omitted]" if operation == "img2img" else None, reference,
                negative, preset, size=size, character_prompts=characters)
            snapshot["parameters"].pop("image", None)
            snapshot["parameters"].pop("director_reference_images", None)
        else:
            snapshot["prompt"] = self._openai_prompt_with_negative(prompt, negative)
        self._request_snapshots.pop(origin, None)
        self._request_snapshots[origin] = snapshot
        while len(self._request_snapshots) > 20:
            self._request_snapshots.pop(next(iter(self._request_snapshots)))

    async def _notify(self, event: AstrMessageEvent, message: str, kind: str = "error") -> None:
        if kind == "error":
            mode = str(self._cfg("error_notify_mode", "final_only"))
            if mode == "silent":
                return
            if mode == "admin_only" and not event.is_admin():
                return
        try:
            await event.send(MessageChain().message(message))
        except Exception as exc:
            logger.debug(f"[{PLUGIN_NAME}] 发送提示失败: {exc}")

    async def _finish_event(self, event: AstrMessageEvent, result: GenerationResult) -> str:
        if not result.ok:
            if not (result.error_code == "429" and not self._as_bool(self._cfg("notify_429", True))):
                await self._notify(event, result.message, "error")
            return f"图片生成未完成：{result.message}"
        if not result.send_image:
            return result.message
        if not result.path:
            event.set_extra("nai_image_delivery", "failed")
            return "图片生成未完成：未找到图片文件。"
        if not await self._generation_context_current(event):
            event.set_extra("nai_image_delivery", "failed")
            Path(result.path).unlink(missing_ok=True)
            return "对话已切换或清空，旧图已取消发送。"
        delivered_jobs = getattr(self, "_delivered_jobs", None)
        if delivered_jobs is None:
            self._delivered_jobs = {}
            delivered_jobs = self._delivered_jobs
        if result.job_id in delivered_jobs:
            return "图片已生成；重复发送已被拦截。"
        delivered_jobs[result.job_id] = time.time()
        event.set_extra("nai_image_delivery", "sending")
        try:
            await self._send_generated_image(event, result)
        except Exception as exc:
            event.set_extra("nai_image_delivery", "failed")
            logger.warning(f"[{PLUGIN_NAME}] 发送图片失败 job={result.job_id}: {exc}")
            try:
                Path(result.path).unlink(missing_ok=True)
            except Exception:
                pass
            await self._notify(event, "图片已生成，但发送到当前会话失败。", "error")
            return "图片已生成，但发送失败。"
        event.set_extra("nai_image_delivery", "completed")
        try:
            self._remember_planned_drawing(event, result)
        except Exception as exc:
            logger.debug(f"[{PLUGIN_NAME}] 缓存连续性参考失败: {type(exc).__name__}")
        return result.message

    async def _send_generated_image(
        self,
        event: AstrMessageEvent,
        result: GenerationResult,
        chain: MessageChain | None = None,
    ) -> bool:
        """Send an already generated file, optionally alongside the final text."""
        if not result.path:
            return False
        image_chain = MessageChain().file_image(result.path)
        if chain is not None:
            outgoing = MessageChain([*chain.chain, *image_chain.chain])
        else:
            outgoing = image_chain
        await event.send(outgoing)
        delay = max(0, int(self._cfg("auto_clean_delay", 300) or 300))

        async def cleanup(path: str, seconds: int):
            if seconds > 0:
                await asyncio.sleep(seconds)
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:
                pass

        asyncio.create_task(cleanup(result.path, delay))
        return True

    def _resolve_llm_operation(
        self,
        requested: str,
        preset: dict[str, Any] | None = None,
    ) -> str:
        operation = str(requested or "generate").strip().lower()
        aliases = {
            "generate": "generate",
            "draw": "generate",
            "text2img": "generate",
            "img2img": "img2img",
            "reference": "reference",
        }
        operation = aliases.get(operation, "generate")
        if operation != "generate" or not self._as_bool(self._cfg("llm_auto_reference", True), True):
            return operation
        if not preset or not str(preset.get("reference_id", "") or "").strip():
            return operation
        if self._provider_name() == "novelai_official" and self._as_bool(self._cfg("reference_enabled", True), True):
            return "reference"
        if self._provider_name() == "openai_compatible" and self._as_bool(self._cfg("img2img_enabled", True), True):
            return "img2img"
        return operation

    def _command_help(self) -> str:
        prefix = str(self._cfg("command_prefix", "nai") or "nai").strip().lstrip("/")
        if prefix.lower() == "n":
            prefix = "nai"  # /n is the fixed image-only shortcut.
        root = f"/{prefix}"
        return (
            "可执行命令：\n"
            "/n [自然语言要求]（独立绘图 LLM 读当前上下文，只发图片）\n"
            "/nai <Tag>（当前绑定卡直接提交图片服务）\n"
            f"{self._cfg('drawing_trigger', 'n')} 放在消息首尾或单独发送（主模型回复 + 独立绘图）\n"
            f"{root} draw <画面描述>（自由绘画，不套角色卡）\n"
            f"{root} img2img <画面描述>\n"
            f"{root} reference <character|style|both> <画面描述>\n"
            f"{root} self <画面描述>（使用当前人设角色卡）\n"
            f"{root} card-draw <角色卡ID> <画面描述>（仅本次使用指定卡）\n"
            f"{root} card current（查看当前角色卡）\n"
            f"{root} card list（查看全部角色卡）\n"
            f"{root} card use <角色卡 ID 或名称>（更换角色卡）\n"
            f"{root} card clear（解除当前人设绑定或默认角色卡）\n"
            f"{root} model current（查看当前 NAI 模型）\n"
            f"{root} model list（查看可用 NAI 模型）\n"
            f"{root} model use <模型 ID 或别名>（更换 NAI 模型）"
        )

    @staticmethod
    def _truncate_command_value(value: Any, limit: int = 800) -> str:
        text = str(value or "").strip() or "未填写"
        return text if len(text) <= limit else text[:limit].rstrip() + "…"

    def _find_role_card(self, selector: str) -> dict[str, Any]:
        selector = str(selector or "").strip()
        if not selector:
            raise ValueError("请提供角色卡 ID 或完整名称。")
        by_id = self._get_preset(selector, include_disabled=True)
        if by_id:
            return by_id
        matches = [
            preset
            for preset in self.presets
            if str(preset.get("name", "") or "").strip().casefold() == selector.casefold()
        ]
        if not matches:
            raise ValueError("没有找到该角色卡，请先使用 card list 查看 ID。")
        if len(matches) > 1:
            raise ValueError("存在同名角色卡，请改用唯一的角色卡 ID。")
        return matches[0]

    def _role_card_details(self, preset: dict[str, Any], scope: str = "") -> str:
        status = "已启用" if self._as_bool(preset.get("enabled", True), True) else "已禁用"
        lines = [
            f"当前角色卡：{preset.get('name') or preset.get('id')}（{preset.get('id')}，{status}）",
        ]
        if scope:
            lines.append(f"生效来源：{scope}")
        lines.extend([
            f"正向 Tag：{self._truncate_command_value(preset.get('positive_prompt'))}",
            f"负向 Tag：{self._truncate_command_value(preset.get('negative_prompt'))}",
            f"参考图：{preset.get('reference_id') or '未绑定'}",
        ])
        return "\n".join(lines)

    async def _handle_role_card_command(
        self,
        event: AstrMessageEvent,
        arguments: str,
    ) -> str:
        action, separator, selector = str(arguments or "").strip().partition(" ")
        action = action.lower()
        if not action or action in {"current", "show", "当前", "查看"}:
            persona_id = await self._resolve_active_persona_id(event)
            raw_mapping = self._cfg("persona_preset_map", {})
            mapping = raw_mapping if isinstance(raw_mapping, dict) else {}
            mapped_preset = self._get_preset(str(mapping.get(persona_id, ""))) if persona_id else None
            preset = mapped_preset or self._get_preset(str(self._cfg("default_preset_id", "")))
            if not preset:
                return "当前没有生效的角色卡，请使用 card list 和 card use 进行选择。"
            if mapped_preset:
                scope = f"AstrBot 人设 {persona_id}"
            elif persona_id:
                scope = f"默认角色卡（AstrBot 人设 {persona_id} 未单独绑定）"
            else:
                scope = "默认角色卡"
            return self._role_card_details(preset, scope)
        if action in {"list", "all", "列表", "全部"}:
            if not self.presets:
                return "暂无角色卡，请先在 WebUI 创建。"
            lines = ["全部角色卡："]
            for index, preset in enumerate(self.presets, 1):
                status = "启用" if self._as_bool(preset.get("enabled", True), True) else "禁用"
                lines.append(
                    f"{index}. {preset.get('name') or preset.get('id')} · {preset.get('id')} · {status}"
                )
            return "\n".join(lines)
        if action in {"use", "set", "切换", "更换"}:
            if not separator or not selector.strip():
                return "用法：card use <角色卡 ID 或完整名称>"
            try:
                preset = self._find_role_card(selector)
            except ValueError as exc:
                return str(exc)
            if not self._as_bool(preset.get("enabled", True), True):
                return "该角色卡已禁用，请先在 WebUI 启用后再切换。"
            persona_id = await self._resolve_active_persona_id(event)
            previous_config = dict(self.config)
            if persona_id:
                raw_mapping = self._cfg("persona_preset_map", {})
                mapping = dict(raw_mapping) if isinstance(raw_mapping, dict) else {}
                mapping[persona_id] = str(preset.get("id", ""))
                self.config["persona_preset_map"] = mapping
                scope = f"AstrBot 人设 {persona_id}"
            else:
                self.config["default_preset_id"] = str(preset.get("id", ""))
                scope = "默认角色卡"
            try:
                self._save_config()
            except Exception as exc:
                self.config.clear()
                self.config.update(previous_config)
                logger.exception(f"[{PLUGIN_NAME}] 命令切换角色卡失败: {exc}")
                return "角色卡切换失败，配置已回滚，请检查 AstrBot 日志。"
            return f"已将 {scope} 切换为：{preset.get('name') or preset.get('id')}（{preset.get('id')}）。"
        if action in {"clear", "off", "解除", "清除"}:
            persona_id = await self._resolve_active_persona_id(event)
            previous_config = dict(self.config)
            if persona_id:
                raw_mapping = self._cfg("persona_preset_map", {})
                mapping = dict(raw_mapping) if isinstance(raw_mapping, dict) else {}
                if persona_id not in mapping:
                    return "当前 AstrBot 人设没有单独的角色卡绑定；默认角色卡未更改。"
                mapping.pop(persona_id, None)
                self.config["persona_preset_map"] = mapping
                scope = f"AstrBot 人设 {persona_id} 的角色卡绑定"
            else:
                self.config["default_preset_id"] = ""
                scope = "默认角色卡"
            try:
                self._save_config()
            except Exception as exc:
                self.config.clear()
                self.config.update(previous_config)
                logger.exception(f"[{PLUGIN_NAME}] 命令解除角色卡失败: {exc}")
                return "角色卡解除失败，配置已回滚，请检查 AstrBot 日志。"
            return f"已解除{scope}。"
        return "角色卡命令用法：card current、card list、card use <ID 或名称>、card clear。"

    def _handle_model_command(self, arguments: str) -> str:
        action, separator, selector = str(arguments or "").strip().partition(" ")
        action = action.lower()
        active_model = self._active_model()
        if not action or action in {"current", "show", "当前", "查看"}:
            return f"当前 NAI 模型：{NAI_MODELS.get(active_model, '自定义模型')}（{active_model}）"
        if action in {"list", "all", "列表", "全部"}:
            lines = ["可用 NAI 模型："]
            for model_id, label in NAI_MODELS.items():
                marker = " ← 当前" if model_id == active_model else ""
                lines.append(f"- {label}：{model_id}{marker}")
            lines.append("常用别名：v5、v5-curated、v4.5、v4.5-curated、v4、v4-curated、v3、v3-furry")
            return "\n".join(lines)
        if action in {"use", "set", "切换", "更换"}:
            if not separator or not selector.strip():
                return "用法：model use <模型 ID 或别名>"
            if self._provider_name() != "novelai_official":
                return "更换 NAI 模型只适用于 NovelAI 官方后端；请先在 WebUI 切换后端。"
            requested = selector.strip().lower()
            model_id = NAI_MODEL_ALIASES.get(requested, requested)
            if model_id not in NAI_MODELS:
                return "不支持该 NAI 模型，请先使用 model list 查看可用模型和别名。"
            previous_model = self.config.get("model", DEFAULT_MODEL)
            self.config["model"] = model_id
            try:
                self._save_config()
            except Exception as exc:
                self.config["model"] = previous_model
                logger.exception(f"[{PLUGIN_NAME}] 命令切换 NAI 模型失败: {exc}")
                return "NAI 模型切换失败，配置已回滚，请检查 AstrBot 日志。"
            return f"已切换 NAI 模型：{NAI_MODELS[model_id]}（{model_id}）。"
        return "模型命令用法：model current、model list、model use <模型 ID 或别名>。"

    # --------------------------- sticker mode ---------------------------
    def _sync_sticker_emotion_tool(self) -> None:
        """Expose the emotion tool only while sticker metadata is configured on."""
        active = self._as_bool(self._cfg("sticker_enabled", False)) and self._as_bool(
            self._cfg("sticker_emotion_tool", True), True
        )
        try:
            manager = self.context.get_llm_tool_manager()
            tool = manager.get_func(STICKER_EMOTION_TOOL_NAME)
            if tool is not None:
                # Change the runtime flag directly. Calling deactivate_llm_tool()
                # would persist a dashboard-wide manual deactivation and make a
                # later WebUI switch-on unexpectedly fail to restore the tool.
                tool.active = active
        except Exception as exc:
            logger.debug(f"[{PLUGIN_NAME}] 同步情绪工具状态失败: {exc}")

    def _sticker_card(self) -> dict[str, Any]:
        raw = self._cfg("sticker_role_card", {})
        card = dict(raw) if isinstance(raw, dict) else {}
        return {
            "id": "sticker-role-card",
            "name": str(card.get("name", "表情包角色卡") or "表情包角色卡").strip()[:80],
            "positive_prompt": str(card.get("positive_prompt", "") or "").strip()[:8000],
            "negative_prompt": str(card.get("negative_prompt", "") or "").strip()[:4000],
            "lock_positive": self._as_bool(card.get("lock_positive", True), True),
            "positive_strength": self._preset_strength(card.get("positive_strength", 1.35), 1.35),
            "quality_override": str(card.get("quality_override", "off")) if card.get("quality_override") in {"inherit", "on", "off"} else "off",
            "reference_id": str(card.get("reference_id", "") or "").strip(),
            "reference_type": str(card.get("reference_type", "character") or "character").strip(),
        }

    def _sticker_context_text(self, req: ProviderRequest) -> str:
        contexts = req.contexts if isinstance(getattr(req, "contexts", None), list) else []
        limit = max(1, min(20, int(self._cfg("sticker_context_messages", 8) or 8)))
        lines: list[str] = []
        for item in contexts[-limit:]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "user"))
            content = item.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    str(part.get("text", "")) if isinstance(part, dict) else str(part)
                    for part in content
                )
            content = str(content or "").strip()
            if content:
                lines.append(f"{role}: {content[:1200]}")
        if req.prompt:
            lines.append(f"current request: {str(req.prompt)[:1200]}")
        return "\n".join(lines)[-12000:]

    @filter.event_message_type(filter.EventMessageType.ALL, priority=5)
    async def sticker_prepare_event(self, event: AstrMessageEvent) -> None:
        """Reserve a non-streaming turn when same-bubble sticker mode is active."""
        if not self._as_bool(self._cfg("sticker_enabled", False)):
            return
        if str(self._cfg("sticker_send_mode", "after_reply")) != "same_bubble":
            return
        try:
            if str(event.get_message_str() or "").lstrip().startswith("/"):
                return
        except Exception:
            pass
        # The final response is decorated after it has been assembled. Streaming
        # has already left the adapter by then, so reserve a normal result.
        event.set_extra("enable_streaming", False)

    @filter.on_llm_request()
    async def sticker_capture_context(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        if not self._as_bool(self._cfg("sticker_enabled", False)):
            return
        try:
            if str(event.get_message_str() or "").lstrip().startswith("/"):
                return
        except Exception:
            pass
        event.set_extra("sticker_context", self._sticker_context_text(req))
        if self._as_bool(self._cfg("sticker_emotion_tool", True), True):
            req.system_prompt = (req.system_prompt or "") + (
                "\nWhen the user exchange has a clear emotional reaction, you may call "
                "novelai_set_emotion once with a short Chinese emotion label and an optional "
                "visual hint. This is internal metadata and must not be shown in your reply."
            )

    @filter.llm_tool(name=STICKER_EMOTION_TOOL_NAME)
    async def novelai_set_emotion(
        self,
        event: AstrMessageEvent,
        emotion: str = "",
        visual_hint: str = "",
    ) -> str:
        """给表情包模式提供本次回复的情绪标签；只在情绪明确时调用。

        Args:
            emotion(string): 简短情绪标签，例如 开心、无奈、嘲笑、震惊、委屈、得意。
            visual_hint(string): 可选的表情、动作或构图提示，不要重复角色卡。
        """
        if not self._as_bool(self._cfg("sticker_enabled", False)):
            return "sticker metadata disabled"
        label = str(emotion or "").strip()[:40]
        hint = str(visual_hint or "").strip()[:400]
        if label:
            event.set_extra("sticker_emotion", label)
        if hint:
            event.set_extra("sticker_visual_hint", hint)
        return "Emotion metadata recorded for the optional sticker decision. Continue the conversation normally."

    @staticmethod
    def _sticker_json(text: str) -> dict[str, Any] | None:
        cleaned = re.sub(r"```(?:json)?", "", str(text or ""), flags=re.I).replace("```", "").strip()
        match = re.search(r"\{.*\}", cleaned, flags=re.S)
        if not match:
            return None
        try:
            value = json.loads(match.group(0))
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    async def _decide_sticker(self, event: AstrMessageEvent, reply_text: str) -> dict[str, Any] | None:
        provider_id = str(self._cfg("sticker_llm_provider_id", "") or "").strip()
        try:
            if not provider_id:
                provider_id = await self.context.get_current_chat_provider_id(event.unified_msg_origin)
            card = self._sticker_card()
            prompt = (
                "你是表情包编排器。只返回 JSON，不要 Markdown，不要解释。\n"
                "字段：send(boolean)、emotion(string)、prompt(string)、reason(string)。\n"
                "send=false 表示这次回复不适合插入表情包。prompt 必须是适合文生图的英文视觉描述，"
                "只描述情绪表情、动作、构图和场景，不要改写角色卡。\n"
                f"决策偏好：{self._cfg('sticker_decision_prompt', '')}\n"
                f"表情包角色卡：{card.get('positive_prompt', '')}\n"
                f"用户和会话上下文：{event.get_extra('sticker_context', '')}\n"
                f"本次回复：{reply_text[:5000]}\n"
                f"LLM 提供的情绪标签：{event.get_extra('sticker_emotion', '')}\n"
                f"LLM 提供的视觉提示：{event.get_extra('sticker_visual_hint', '')}\n"
                '示例格式：{"send":true,"emotion":"无奈","prompt":"deadpan face, shrugging, speechless reaction","reason":"语气适合吐槽"}'
            )
            response = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    system_prompt="你只负责判断是否需要表情包并生成简短视觉描述。严格输出 JSON。",
                ),
                timeout=max(5, min(180, int(self._cfg("sticker_llm_timeout", 45) or 45))),
            )
            decision = self._sticker_json(getattr(response, "completion_text", ""))
            if not decision or not self._as_bool(decision.get("send", False)):
                return None
            decision["emotion"] = str(decision.get("emotion", "") or "").strip()[:60]
            decision["prompt"] = str(decision.get("prompt", "") or "").strip()[:1600]
            if not decision["prompt"]:
                return None
            return decision
        except Exception as exc:
            logger.debug(f"[{PLUGIN_NAME}] 表情包判断跳过: {exc}")
            return None

    async def _try_sticker(self, event: AstrMessageEvent, *, same_bubble: bool) -> None:
        if not self._as_bool(self._cfg("sticker_enabled", False)):
            return
        if event.get_extra("sticker_consumed", False) or event.get_extra("sticker_busy", False):
            return
        reply_text = str(event.get_extra("sticker_final_text", "") or "").strip()
        if not reply_text:
            return
        try:
            if any(isinstance(comp, Image) for comp in (event.get_result().chain if event.get_result() else [])):
                event.set_extra("sticker_consumed", True)
                return
        except Exception:
            pass
        event.set_extra("sticker_consumed", True)
        probability = max(0.0, min(100.0, float(self._cfg("sticker_probability", 20) or 0)))
        if random.random() * 100 >= probability:
            return
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        now = time.time()
        last = getattr(self, "_sticker_last_by_origin", {}).get(origin, 0.0)
        cooldown = max(0, int(self._cfg("sticker_cooldown_seconds", 60) or 60))
        if cooldown and now - last < cooldown:
            return
        if not hasattr(self, "_sticker_last_by_origin"):
            self._sticker_last_by_origin = {}
        self._sticker_last_by_origin[origin] = now
        event.set_extra("sticker_busy", True)
        try:
            decision = await self._decide_sticker(event, reply_text)
            if not decision:
                return
            emotion = decision.get("emotion") or str(event.get_extra("sticker_emotion", "") or "")
            visual = decision["prompt"]
            if emotion:
                visual = f"{emotion} emotion, {visual}"
            base_prompt = str(self._cfg("sticker_prompt", "") or "").strip()
            visual = ", ".join(part for part in (base_prompt, visual) if part)
            card = self._sticker_card()
            operation = "generate"
            if self._as_bool(self._cfg("sticker_auto_reference", True), True) and card.get("reference_id"):
                operation = "reference" if self._provider_name() == "novelai_official" else "img2img"
            result = await self._run_job(
                event,
                visual,
                operation,
                reference_type=card.get("reference_type"),
                preset_override=card,
                size=(
                    max(64, min(2048, int(self._cfg("sticker_width", 512) or 512))),
                    max(64, min(2048, int(self._cfg("sticker_height", 512) or 512))),
                ),
                quiet=True,
                job_kind="sticker",
                emotion=emotion,
            )
            if not result.ok or not result.path:
                return
            if same_bubble:
                current = event.get_result()
                if current is None or not current.chain:
                    return
                await self._send_generated_image(event, result, current)
                current.chain.clear()
            else:
                await self._send_generated_image(event, result)
        finally:
            event.set_extra("sticker_busy", False)

    @filter.on_llm_response()
    async def sticker_capture_reply(self, event: AstrMessageEvent, response: LLMResponse) -> None:
        if not self._as_bool(self._cfg("sticker_enabled", False)):
            return
        if str(getattr(response, "role", "")) != "assistant" or getattr(response, "tools_call_name", None):
            return
        text = str(getattr(response, "completion_text", "") or "").strip()
        if text:
            event.set_extra("sticker_final_text", text)
            event.set_extra("sticker_final_ready", True)

    @filter.on_decorating_result()
    async def sticker_decorate_reply(self, event: AstrMessageEvent) -> None:
        if not event.get_extra("sticker_final_ready", False):
            return
        if str(self._cfg("sticker_send_mode", "after_reply")) == "same_bubble":
            await self._try_sticker(event, same_bubble=True)

    @filter.after_message_sent()
    async def sticker_after_reply(self, event: AstrMessageEvent) -> None:
        if event.get_extra("sticker_final_ready", False) and not event.get_extra("sticker_consumed", False):
            await self._try_sticker(event, same_bubble=False)

    # --------------------------- AstrBot handlers ---------------------------
    @filter.llm_tool(name="novelai_generate_image")
    async def novelai_generate_image(
        self,
        event: AstrMessageEvent,
        prompt: str = "",
        operation: str = "generate",
        reference_type: str = "",
        subjects: list | str | None = None,
        style: str | None = None,
    ):
        """仅在用户明确要求生成、绘制或修改图片时调用；每条用户消息最多生成并发送一张图片。先区分绘画对象与当前聊天人设；角色卡只用于选中的绘画角色，绝不默认合并到其他角色。已知角色无需提前建卡。图片发送后不再绘图；按当前对话自然回应，不套用上次的出图回复，不将图片设想当成剧情事实。

        Args:
            prompt(string): 直接写本次完整画面，优先英文 NovelAI / Danbooru Tag。本工具不转述给独立模型。无卡角色用 custom，风景用 subjects=[]。
            operation(string): generate、img2img 或 reference。普通生图用 generate；剧情锚点模式仅在 use_reference=true 时自动使用选中卡参考图。用户明确要求基于参考图修改时可用 img2img/reference；多人图用 generate。
            reference_type(string): reference 模式用 character、style 或 both；省略时继承本次选中卡的参考类型。
            subjects(list[object]): 原生角色数组，每人一项，最多 6 人。source=self 画 AI 自己或使用当前绑定卡；card 配 preset_id 选择卡。明确用内置卡时不要传空数组后重写卡词。custom 可用 prompt 或 appearance_prompt 写角色名外貌，完整描述已在顶层时可省略。人物 prompt 可含服饰。绑定卡身份和画风由插件保留；outfit 是完整服饰，整体替换，空串明确无服饰。state_scope 默认 image 仅本图，剧情实际换装才用 story。省略 outfit 时尊重本次普通描述中的服饰；未描述服饰才沿用当前剧情服饰或默认服饰。use_reference 默认 false。不要使用 appearance_patch 或重写绑定卡的 appearance_prompt。
            style(string): 仅用户明确改画风时填写本图完整画风，替换原卡画风；常规用卡画图省略，保留原始画师名及权重。无卡画图可自由使用。
        """
        try:
            return await self._await_main_image(event, prompt, operation, reference_type, subjects, style)
        finally:
            self._restore_image_tool_timeout(event)

    async def _await_main_image(self, event, prompt, operation, reference_type, subjects, style):
        if event.get_extra("nai_main_hook_started") is not None and not event.get_extra("nai_main_first_tool_observed", False):
            event.set_extra("nai_main_first_tool_observed", True)
            self._planner_log(event, event.get_extra("nai_main_provider", ""), "main", 0, None,
                phase="main_first_drawing_tool", status="observed",
                elapsed_ms=round((time.perf_counter() - event.get_extra("nai_main_hook_started")) * 1000),
                timing_scope="main_request_hook_to_first_drawing_tool_not_http",
                arguments_size=diagnostics.text_size(diagnostics.json_text({"prompt": prompt, "subjects": subjects, "style": style})))
        # Fast images finish inline as before. Long queue/retry jobs outlive the
        # Agent's tool deadline, without a second LLM or image request.
        task = asyncio.create_task(self._image_tool_entry(event, prompt, operation, reference_type, subjects, style))
        tasks = getattr(self, "_image_tasks", None)
        if tasks is None:
            self._image_tasks = tasks = set()
        tasks.add(task)
        def completed(done):
            tasks.discard(done)
            if not done.cancelled() and done.exception() is not None:
                logger.error(f"[{PLUGIN_NAME}] 图片任务异常: {type(done.exception()).__name__}")
        task.add_done_callback(completed)
        core_timeout = None
        inline_seconds = IMAGE_TOOL_INLINE_SECONDS
        tool_budget = event.get_extra("nai_image_tool_budget")
        if tool_budget is not None:
            # A pre-execution hook extended only this tool's wait_for boundary.
            # Await delivery without an intermediate Agent turn or tool receipt.
            inline_seconds = max(0.001, tool_budget - min(5, tool_budget * 0.25))
        try:
            configured = self.context.get_config(event.unified_msg_origin).get("provider_settings", {}).get("tool_call_timeout")
            if isinstance(configured, (int, float)) and not isinstance(configured, bool) and configured > 0:
                core_timeout = configured
                if tool_budget is None:
                    inline_seconds = max(0.001, configured - min(5, configured * 0.25))
        except Exception:
            pass
        try:
            done, _ = await asyncio.wait({task}, timeout=inline_seconds)
            if done:
                return task.result()
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        event.set_extra("nai_image_deferred", True)
        self._planner_log(event, self._provider_name(), "image", 0, None,
            phase="image_tool_deferred", status="running", inline_seconds=inline_seconds,
            core_tool_timeout_seconds=core_timeout,
            reason="avoid_core_tool_timeout", creates_new_image_request=False)
        return ("FINAL_IMAGE_TOOL_RESULT: status=accepted_background. 本次绘图仍在排队、生成或重试，图片尚未发送。"
                "插件将继续同一个任务并发送结果，不要重复调用工具，不要声称已完成；自然回应用户一次即可。")

    async def _image_tool_entry(self, event, prompt="", operation="generate", reference_type="", subjects=None,
                                style=None, *, accepted=False, entry="llm_tool", quiet=False):
        if not accepted and event.get_extra("nai_planner_pending"):
            self._planner_log(event, event.get_extra("nai_main_provider", ""), "main", 0, None,
                phase="duplicate_tool", status="blocked", drawing_state="queued_or_consumed")
            return "DRAWING_REQUEST_QUEUED: ENTRY_GATE_BLOCKED_DUPLICATE. 本条绘图已排队，回复后由插件独立执行；无需再写 Tag 或调用绘图工具，继续正常回复即可。"
        if not self._mode_allows(entry):
            await self._notify(event, "当前未启用自然语言生图入口。", "error")
            return (
                "FINAL_IMAGE_TOOL_RESULT: Natural-language image generation is disabled. "
                "Do not call any tool again for this message. Reply to the user briefly."
            )
        allowed, reason = self._can_use(event)
        if not allowed:
            await self._notify(event, reason, "error")
            return f"FINAL_IMAGE_TOOL_RESULT: {reason} Do not call any tool again for this message."

        if not accepted and self._llm_tool_claimed(event):
            state = event.get_extra("nai_image_status", "accepted")
            self._planner_log(event, event.get_extra("nai_main_provider", ""), "main", 0, None,
                phase="duplicate_tool", status="blocked", drawing_state=state)
            detail = {"completed": "图片任务已完成。", "failed": "图片任务失败，不会继续发送。",
                "cancelled": "图片任务已取消，不会继续发送；上游可能仍在处理已发出的请求。"}.get(state,
                "图片任务仍在执行，尚未完成；同一任务将负责发送结果。")
            return f"FINAL_IMAGE_TOOL_RESULT: ENTRY_GATE_BLOCKED_DUPLICATE. status={state}. {detail} Do not call any tool again for this message."

        normalized_prompt = prompt.strip() if isinstance(prompt, str) else ""
        if not normalized_prompt:
            try:
                normalized_prompt = str(event.get_message_str() or "").strip()
            except Exception:
                normalized_prompt = ""
        if not normalized_prompt:
            await self._notify(event, "生图工具缺少画面描述，本次任务已停止。", "error")
            return (
                "FINAL_IMAGE_TOOL_RESULT: Generation stopped because the request had no usable description. "
                "Do not call any tool again for this message. Explain this briefly to the user."
            )

        view = event.get_extra("nai_story_view") if accepted else None
        try:
            if event.get_extra("nai_story_request_stale", False):
                raise ValueError("主模型使用的对话已切换或清空，请在当前对话重新请求。")
            if not isinstance(prompt, str):
                raise ValueError("prompt 必须为字符串。")
            if not isinstance(operation, str) or operation not in {"generate", "img2img", "reference"}:
                raise ValueError("operation 只能为 generate、img2img 或 reference。")
            if not isinstance(reference_type, str) or reference_type not in {"", "character", "style", "both"}:
                raise ValueError("reference_type 只能为 character、style 或 both。")
            if len(normalized_prompt) > 8000:
                raise ValueError("prompt 不能超过 8000 字符。")
            if style is not None:
                if not isinstance(style, str) or len(style) > 4000:
                    raise ValueError("style 必须是不超过 4000 字符的字符串。")
                drawing.tags(style, validate=True)
                if len(drawing.join(normalized_prompt, style)) > 11500:
                    raise ValueError("场景与画风合成过长，请缩短本次描述。")
            drawing_subjects = await self._resolve_drawing_subjects(event, subjects)
            if not self._contextual_cards_enabled() and (style is not None or any("outfit" in s or s["state_scope"] == "story" for s in drawing_subjects)):
                raise ValueError("严格模式请在各角色 prompt 中描述本次修改；outfit、style 和剧情服饰延续用于剧情锚点模式。")
            if self._contextual_cards_enabled():
                if style is not None and any(s["preset"] for s in drawing_subjects) and not self._drawing_style_override_requested(event):
                    # Preserve card art direction when the LLM merely copies
                    # a generic style into the optional override parameter.
                    style = None
                    logger.info(f"[{PLUGIN_NAME}] 用户未要求改画风，保留选中角色卡的原始画风，忽略工具 style 覆盖。")
                view = await self._story_view(event)
                requested_view = event.get_extra("nai_story_view")
                if requested_view and (not view or requested_view.scope != view.scope or requested_view.epoch != view.epoch):
                    raise ValueError("绘图期间对话已切换或清空，请在当前对话重新请求。")
                self._apply_story_layers(drawing_subjects, view, operation, style, normalized_prompt)
            for subject in drawing_subjects:
                combined = drawing.join(subject["preset"].get("positive_prompt", ""), subject["prompt"],
                    normalized_prompt if len(drawing_subjects) == 1 else "",
                    (style or "") if len(drawing_subjects) == 1 else "")
                # Leave room for natural-language instructions on compatible
                # providers; never cut through a character/weight group.
                if len(combined) > 11500:
                    raise ValueError("角色与场景合成过长，请缩短本次描述；身份锚点不能被截断。")
            if len(drawing_subjects) > 1 and str(operation).lower().strip() in {"img2img", "reference"}:
                raise ValueError("多人同框请使用 generate；人物参考图会影响全图，不能自动归属到单个角色。")
        except ValueError as exc:
            if accepted:
                await self._notify(event, f"独立绘图 LLM 参数无效：{exc}", "error")
                return "FINAL_IMAGE_TOOL_RESULT: 独立绘图参数校验失败，未请求图片服务。"
            failures = getattr(self, "_llm_validation_failures", None)
            if failures is None:
                self._llm_validation_failures = failures = {}
            key = self._event_key(event, "", "llm_tool")
            failures[key] = failures.get(key, 0) + 1
            while len(failures) > 256:
                failures.pop(next(iter(failures)))
            if failures[key] == 1:
                return f"IMAGE_TOOL_ARGUMENT_ERROR: {exc} No image request was sent. Correct arguments and retry once."
            self._claim_llm_tool_invocation(event)
            self._retire_main_drawing(event)
            await self._notify(event, str(exc), "error")
            return (
                f"FINAL_IMAGE_TOOL_RESULT: {exc} No image request was sent. "
                "Do not call any tool again for this message. Explain this briefly to the user."
            )
        active_preset = drawing_subjects[0]["preset"] if len(drawing_subjects) == 1 else {}
        character_prompts = None
        if len(drawing_subjects) > 1:
            character_prompts = self._compose_character_prompts(drawing_subjects, event, normalized_prompt)
        else:
            if drawing_subjects and drawing_subjects[0]["prompt"]:
                normalized_prompt += ", " + drawing_subjects[0]["prompt"]
            normalized_prompt = self._filter_llm_prompt_conflicts(normalized_prompt, event, active_preset)
        if style is not None:
            normalized_prompt = drawing.join(normalized_prompt, style)
        if not accepted and not self._claim_llm_tool_invocation(event):
            return "FINAL_IMAGE_TOOL_RESULT: ENTRY_GATE_BLOCKED_DUPLICATE. Do not call any tool again for this message."
        if not accepted:
            self._retire_main_drawing(event)
        self._stamp_drawing(event)
        if view:
            event.set_extra("nai_generation_view", view)
            for subject in drawing_subjects:
                if subject.get("story_card") and subject["state_scope"] == "story" and "outfit" in subject:
                    self._ledger().remember(view, subject["story_card"], subject["outfit"])
        event.set_extra("nai_drawing_details", [{"source": s["source"],
            "preset_id": s.get("story_card", s["preset"]).get("id", ""),
            "outfit": s.get("resolved_outfit"), "outfit_source": s.get("outfit_source"),
            "ignored_legacy_fields": s.get("ignored_legacy_fields", []),
            "state_scope": s["state_scope"]} for s in drawing_subjects])
        if not accepted and self._drawing_llm_id():
            # Main-model drawings remain available to subsequent /n edits,
            # without asking the independent model to interpret them again.
            remembered = []
            for subject in drawing_subjects:
                item = {key: subject[key] for key in ("source", "prompt", "outfit", "appearance_prompt", "negative_prompt", "use_reference") if key in subject}
                if item["source"] == "card":
                    item["preset_id"] = subject["preset"]["id"]
                remembered.append(item)
            event.set_extra("nai_planner_plan", {"prompt": prompt.strip() or normalized_prompt, "subjects": remembered,
                **({"style": style} if style is not None else {})})
        resolved_operation = self._resolve_llm_operation(operation, active_preset)
        requested_reference_type = str(reference_type or "").strip().lower()
        normalized_reference_type = (
            requested_reference_type
            if requested_reference_type in {"character", "style", "both"}
            else None
        )
        image_started = time.perf_counter()
        event.set_extra("nai_image_status", "running")
        self._planner_log(event, self._provider_name(), "image", 0, None,
            phase="image_start", status="running", entry=entry, planned=accepted, operation=resolved_operation)
        try:
            result = await self._run_job(
                event,
                normalized_prompt,
                resolved_operation,
                preset_id=str(active_preset.get("id", "")) if active_preset else None,
                reference_type=normalized_reference_type,
                preset_override=active_preset,
                **({"character_prompts": character_prompts} if character_prompts else {}),
                **({"quiet": True} if quiet else {}),
            )
            generation_ms = round((time.perf_counter() - image_started) * 1000)
            delivery_started = time.perf_counter()
            user_visible_result = await self._finish_event(event, result)
        except (asyncio.CancelledError, Exception) as exc:
            cancelled = isinstance(exc, asyncio.CancelledError)
            state = "cancelled" if cancelled else "failed"
            event.set_extra("nai_image_status", state)
            self._planner_log(event, self._provider_name(), "image", 0, None,
                phase="image_end", status=state, error_code=state if cancelled else "internal_error",
                job_id=getattr(event, "_novelai_painter_generation_claim", ""),
                elapsed_ms=round((time.perf_counter() - image_started) * 1000),
                delivery_state=event.get_extra("nai_image_delivery", "not_started"),
                error_type=type(exc).__name__, deferred=event.get_extra("nai_image_deferred", False))
            if cancelled:
                raise
            logger.exception(f"[{PLUGIN_NAME}] 图片任务异常终止")
            await self._notify(event, "图片任务异常终止，请查看插件日志。", "error")
            return "FINAL_IMAGE_TOOL_RESULT: status=failed. 图片任务异常终止，不会继续发送。不要重复调用工具。"
        completed = result.ok and event.get_extra("nai_image_delivery") != "failed"
        event.set_extra("nai_image_status", "completed" if completed else "failed")
        self._planner_log(event, self._provider_name(), "image", 0, None,
            phase="image_end", status="ok" if completed else "failed", job_id=result.job_id,
            elapsed_ms=round((time.perf_counter() - image_started) * 1000), generation_ms=generation_ms,
            delivery_ms=round((time.perf_counter() - delivery_started) * 1000),
            image_attempts=result.attempts, error_code=result.error_code,
            delivery_state=event.get_extra("nai_image_delivery", "unknown"),
            deferred=event.get_extra("nai_image_deferred", False))
        scene_summary = normalized_prompt if len(normalized_prompt) <= 800 else normalized_prompt[:400] + "\n…\n" + normalized_prompt[-400:]
        return "FINAL_IMAGE_TOOL_RESULT: " + json.dumps({
            "status": "completed" if completed else "failed",
            "delivery": user_visible_result,
            "request": str(event.get_message_str() or "")[:400],
            "requested_scene": scene_summary,
            "note": "本条绘图请求已结束，不要重复绘图。画面摘要仅限本次请求，不代表剧情变化。",
        }, ensure_ascii=False)

    async def _quick_draw(self, event: AstrMessageEvent, prompt: str) -> None:
        """Only the drawing LLM handles /n; no main-model conversation turn."""
        event.should_call_llm(False)
        event.set_extra("nai_direct_command", True)
        event.stop_event()
        if not self._mode_allows("command") or not self._can_use(event)[0]:
            await self._notify(event, self._can_use(event)[1] or "当前未启用固定命令入口。", "error")
            return
        if not self._drawing_llm_id():
            await self._notify(event, "请先在 WebUI 配置独立绘图 LLM；/n 由它理解自然语言，/nai <Tag> 才是直接提交提示词。", "error")
            return
        if len(prompt) > 8000:
            await self._notify(event, "画面描述不能超过 8000 字符。", "error")
            return
        if not self._claim_llm_tool_invocation(event):
            return
        self._stamp_drawing(event)
        try:
            event.set_extra("nai_planner_view", await self._story_view(event))
            event.set_extra("nai_planner_provider", self._drawing_llm_id())
            plan = await self._plan_drawing(event, {"prompt": prompt or "根据当前对话绘制当前场景", "entry": "/n"})
            await self._image_tool_entry(event, **plan, accepted=True, entry="command", quiet=True)
        except Exception as exc:
            await self._drawing_planning_failed(event, exc)

    async def _raw_draw(self, event: AstrMessageEvent, prompt: str) -> None:
        """The former /n direct-tag path, now available through /nai <Tag>."""
        event.should_call_llm(False)
        event.set_extra("nai_direct_command", True)
        event.stop_event()
        if not self._mode_allows("command"):
            await self._notify(event, "当前未启用固定命令入口。", "error")
            return
        allowed, reason = self._can_use(event)
        if not allowed:
            await self._notify(event, reason, "error")
            return
        if not prompt:
            await self._notify(event, "用法：/nai <Tag>。自然语言理解请用 /n。", "help")
            return
        if len(prompt) > 8000:
            await self._notify(event, "画面描述不能超过 8000 字符。", "error")
            return
        if not self._claim_llm_tool_invocation(event):
            return
        try:
            subjects = await self._resolve_drawing_subjects(event, [{"source": "self"}])
            if self._contextual_cards_enabled():
                view = await self._story_view(event)
                # Here the caption is direct user input, so an explicit style
                # request can replace the default while staying in the caption.
                style_override = "" if self._drawing_style_override_requested(event) else None
                self._apply_story_layers(subjects, view, "generate", style_override, prompt)
                if view:
                    event.set_extra("nai_generation_view", view)
            selected_card = subjects[0]["preset"]
            if len(drawing.join(selected_card.get("positive_prompt", ""), prompt)) > 11500:
                raise ValueError("角色与画面描述合成过长，请缩短描述；角色卡锚点不能被截断。")
        except ValueError as exc:
            await self._notify(event, str(exc), "error")
            return
        event.set_extra("nai_drawing_details", [{"source": "self", "preset_id": selected_card.get("id", ""),
            "outfit": subjects[0].get("resolved_outfit"), "outfit_source": subjects[0].get("outfit_source"), "state_scope": "image"}])
        result = await self._run_job(event, prompt, "generate", preset_override=selected_card, quiet=True)
        await self._finish_event(event, result)

    @filter.regex(r"^/[A-Za-z][A-Za-z0-9_-]*(?:@[A-Za-z0-9_-]+)?(?:\s|$)")
    async def cmd_draw(self, event: AstrMessageEvent):
        """NovelAI 生图、角色卡和模型固定命令入口。"""
        raw_message = event.get_message_str().strip()
        match = re.match(r"^/(?P<name>[A-Za-z][A-Za-z0-9_-]*)(?:@[A-Za-z0-9_-]+)?(?:\s+(?P<body>.*))?$", raw_message, re.S)
        if not match:
            return
        name = match.group("name").lower()
        if name == "n":
            await self._quick_draw(event, (match.group("body") or "").strip())
            return
        prefix = str(self._cfg("command_prefix", "nai") or "nai").strip().lstrip("/").lower()
        if name not in {"nai", "nai" if prefix == "n" else prefix}:
            return
        text = (match.group("body") or "").strip()
        known = {"help", "?", "菜单", "draw", "generate", "生图", "self", "自己", "card-draw",
            "img2img", "i2i", "图生图", "reference", "ref", "参考图", "card", "role", "preset", "角色卡",
            "cards", "roles", "角色卡列表", "use-card", "切换角色卡", "更换角色卡", "model", "模型",
            "models", "模型列表", "use-model", "切换模型", "更换模型"}
        if text and text.partition(" ")[0].lower() not in known and self._as_bool(self._cfg("legacy_command_enabled", True)):
            await self._raw_draw(event, text)
            return
        if not self._mode_allows("command"):
            yield event.plain_result("当前未启用固定命令入口。")
            return
        allowed, reason = self._can_use(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        if not text or text.lower() in {"help", "?", "菜单"}:
            yield event.plain_result(self._command_help())
            return
        command, separator, remainder = text.partition(" ")
        command = command.lower()
        operation = "generate"
        reference_type = None
        selected_card = {}
        body = text
        if command in {"draw", "generate", "生图"}:
            body = remainder.strip() if separator else ""
        elif command in {"self", "自己"}:
            selected_card = None
            body = remainder.strip() if separator else ""
        elif command == "card-draw":
            selector, _, body = remainder.strip().partition(" ")
            try:
                selected_card = self._find_role_card(selector)
                if not self._as_bool(selected_card.get("enabled", True), True):
                    raise ValueError("该角色卡已禁用，请先在 WebUI 启用。")
            except ValueError as exc:
                yield event.plain_result(str(exc))
                return
        elif command in {"img2img", "i2i", "图生图"}:
            selected_card = None
            operation = "img2img"
            body = remainder.strip() if separator else ""
        elif command in {"reference", "ref", "参考图"}:
            selected_card = None
            operation = "reference"
            reference_type, separator, body = remainder.strip().partition(" ")
            reference_type = reference_type.lower() or "character"
            body = body.strip() if separator else ""
            if reference_type not in {"character", "style", "both"}:
                yield event.plain_result("参考类型只能是 character、style 或 both。")
                return
        elif command in {"card", "role", "preset", "角色卡"}:
            yield event.plain_result(await self._handle_role_card_command(event, remainder))
            return
        elif command in {"cards", "roles", "角色卡列表"}:
            yield event.plain_result(await self._handle_role_card_command(event, "list"))
            return
        elif command in {"use-card", "切换角色卡", "更换角色卡"}:
            yield event.plain_result(await self._handle_role_card_command(event, f"use {remainder}"))
            return
        elif command in {"model", "模型"}:
            yield event.plain_result(self._handle_model_command(remainder))
            return
        elif command in {"models", "模型列表"}:
            yield event.plain_result(self._handle_model_command("list"))
            return
        elif command in {"use-model", "切换模型", "更换模型"}:
            yield event.plain_result(self._handle_model_command(f"use {remainder}"))
            return
        elif not self._as_bool(self._cfg("legacy_command_enabled", True)):
            yield event.plain_result("请使用 /nai draw、/nai img2img 或 /nai reference 命令。")
            return
        else:
            await self._raw_draw(event, text)
            return
        if not body.strip():
            yield event.plain_result("请补充画面描述。")
            return
        preset_id = None
        reference_id = None
        result = await self._run_job(event, body, operation, preset_id, reference_id, reference_type, preset_override=selected_card)
        await self._finish_event(event, result)

    # --------------------------- WebUI page APIs ---------------------------
    def _register_web_api(self) -> None:
        if not WEB_API_AVAILABLE or not hasattr(self.context, "register_web_api"):
            logger.info(f"[{PLUGIN_NAME}] 当前 AstrBot 版本未提供插件 Pages Web API，跳过页面 API 注册。")
            return
        routes = [
            ("settings", self.page_settings, ["GET"]),
            ("config", self.page_save_config, ["POST"]),
            ("test-provider", self.page_test_provider, ["POST"]),
            ("presets", self.page_presets, ["GET"]),
            ("presets", self.page_create_preset, ["POST"]),
            ("presets/manage", self.page_manage_preset, ["POST"]),
            ("presets/<preset_id>", self.page_update_preset, ["PUT"]),
            ("presets/<preset_id>", self.page_delete_preset, ["DELETE"]),
            ("references", self.page_references, ["GET"]),
            ("references/upload", self.page_upload_reference, ["POST"]),
            ("references/manage", self.page_manage_reference, ["POST"]),
            ("references/<reference_id>", self.page_delete_reference, ["DELETE"]),
            ("jobs", self.page_jobs, ["GET"]),
            ("last-request", self.page_last_request, ["GET"]),
            ("jobs/manage", self.page_manage_jobs, ["POST"]),
        ]
        for suffix, handler, methods in routes:
            self.context.register_web_api(f"/{PLUGIN_NAME}/{suffix}", handler, methods, f"NovelAI Painter {suffix}")

    @staticmethod
    def _page_error(message: str, status_code: int = 400):
        # 返回 200 + JSON，避免不同版本 Dashboard 对非 UTF-8 错误响应显示乱码。
        return json_response({"ok": False, "error": message, "status_code": status_code})

    def _public_config(self) -> dict[str, Any]:
        data = dict(self.config)
        for key in ("api_token", "api_key"):
            if data.get(key):
                data[key] = "********"
        return data

    def _public_presets(self) -> list[dict[str, Any]]:
        """Expose each preset's current canonical mapping in the editor.

        A preset stores at most one editor shortcut while persona_preset_map can
        map many personas. Never show a stale embedded shortcut that now maps
        to another preset, otherwise an unrelated edit could appear to undo a
        mapping selected in the dedicated WebUI mapping controls.
        """
        mapping = self._cfg("persona_preset_map", {})
        mapping = mapping if isinstance(mapping, dict) else {}
        public: list[dict[str, Any]] = []
        for preset in self.presets:
            item = dict(preset)
            preset_id = str(item.get("id", "") or "")
            embedded = str(item.get("persona_id", "") or "")
            if not embedded or str(mapping.get(embedded, "")) != preset_id:
                item["persona_id"] = next(
                    (
                        str(persona_id)
                        for persona_id, mapped_id in mapping.items()
                        if str(mapped_id) == preset_id
                    ),
                    "",
                )
            public.append(item)
        return public

    async def page_settings(self):
        personas = []
        drawing_providers = []
        try:
            items = await self.context.persona_manager.get_all_personas()
            personas = [{"id": str(p.persona_id), "name": str(p.persona_id)} for p in items]
        except Exception:
            pass
        try:
            drawing_providers = [{"id": provider.meta().id, "model": provider.get_model()} for provider in self.context.get_all_providers()]
        except Exception:
            pass
        return json_response({"config": self._public_config(), "presets": self._public_presets(), "references": self._reference_list(),
            "personas": personas, "drawing_providers": drawing_providers, "capabilities": self._capabilities()})

    async def page_save_config(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return self._page_error("配置格式错误")
        fields = {
            key: value
            for key, value in payload.items()
            if key in CONFIG_KEYS and key not in {"api_token", "api_key", "config_version"}
        }
        for key in ("api_token", "api_key"):
            if str(payload.get(key, "")).strip() and payload.get(key) != "********":
                fields[key] = str(payload[key]).strip()
        for key in ("width", "height", "steps", "images_per_request", "max_api_requests_per_job", "dedupe_window_seconds", "queue_timeout", "auto_clean_delay"):
            if key in fields:
                try:
                    fields[key] = int(fields[key])
                except (TypeError, ValueError):
                    return self._page_error(f"{key} 必须是整数")
        for key in ("scale", "retry_delay", "img2img_strength", "img2img_noise", "reference_strength", "reference_fidelity", "reference_information_extracted", "sticker_probability"):
            if key in fields:
                try:
                    fields[key] = float(fields[key])
                except (TypeError, ValueError):
                    return self._page_error(f"{key} 必须是数字")
        fields["images_per_request"] = 1
        retry_mode = str(fields.get("retry_mode", self._cfg("retry_mode", "none")) or "none")
        fields["max_api_requests_per_job"] = {"none": 1, "rate_limit_once": 2, "rate_limit_twice": 3}.get(retry_mode, 1)
        fields["width"] = min(2048, max(64, int(fields.get("width", self._cfg("width", 832)))))
        fields["height"] = min(2048, max(64, int(fields.get("height", self._cfg("height", 1216)))))
        fields["steps"] = min(50, max(1, int(fields.get("steps", self._cfg("steps", 28)))))
        for key in ("allowed_users", "allowed_groups"):
            if key in fields:
                fields[key] = self._as_list(fields[key])
        for key in (
            "admin_bypass", "legacy_command_enabled", "quality_toggle", "show_queue_notice",
            "notify_429", "show_retry_notice", "llm_auto_reference", "img2img_enabled",
            "reference_enabled", "img2img_color_correct",
            "sticker_enabled", "sticker_auto_reference", "sticker_emotion_tool",
            "drawing_llm_use_images",
            "drawing_llm_format_retry", "drawing_llm_debug",
        ):
            if key in fields:
                fields[key] = self._as_bool(fields[key])
        if "command_prefix" in fields:
            prefix = str(fields["command_prefix"] or "nai").strip().lstrip("/")
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,30}", prefix):
                return self._page_error("固定命令只能使用字母开头的 1-31 位名称")
            fields["command_prefix"] = prefix
        if "openai_auth_header" in fields and fields["openai_auth_header"] not in {"Authorization", "x-api-key"}:
            return self._page_error("兼容后端鉴权请求头不受支持")
        if "persona_preset_map" in fields and not isinstance(fields["persona_preset_map"], dict):
            return self._page_error("persona_preset_map 必须是对象")
        if "persona_preset_map" in fields:
            fields["persona_preset_map"] = {
                str(persona_id).strip(): str(mapped_id).strip()
                for persona_id, mapped_id in fields["persona_preset_map"].items()
                if str(persona_id).strip()
                and self._get_preset(str(mapped_id).strip(), include_disabled=True)
            }
        if "default_preset_id" in fields:
            default_id = str(fields["default_preset_id"] or "").strip()
            if default_id and not self._get_preset(default_id, include_disabled=True):
                return self._page_error("默认角色卡不存在")
            fields["default_preset_id"] = default_id
        if "sticker_role_card" in fields:
            raw_card = fields["sticker_role_card"]
            if not isinstance(raw_card, dict):
                return self._page_error("表情包角色卡格式错误")
            reference_id = str(raw_card.get("reference_id", "") or "").strip()
            if reference_id and (Path(reference_id).name != reference_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", reference_id)):
                return self._page_error("表情包参考图 ID 格式不正确")
            fields["sticker_role_card"] = {
                "name": str(raw_card.get("name", "表情包角色卡") or "表情包角色卡").strip()[:80],
                "positive_prompt": str(raw_card.get("positive_prompt", "") or "").strip()[:8000],
                "negative_prompt": str(raw_card.get("negative_prompt", "") or "").strip()[:4000],
                "lock_positive": self._as_bool(raw_card.get("lock_positive", True), True),
                "positive_strength": self._preset_strength(raw_card.get("positive_strength", 1.35), 1.35),
                "quality_override": str(raw_card.get("quality_override", "off")) if raw_card.get("quality_override") in {"inherit", "on", "off"} else "off",
                "reference_id": reference_id,
                "reference_type": str(raw_card.get("reference_type", "character") or "character") if raw_card.get("reference_type") in {"character", "style", "both"} else "character",
            }
        for key in ("sticker_width", "sticker_height"):
            if key in fields:
                try:
                    fields[key] = min(2048, max(64, int(fields[key])))
                except (TypeError, ValueError):
                    return self._page_error(f"{key} 必须是整数")
        if "sticker_probability" in fields:
            try:
                fields["sticker_probability"] = min(100.0, max(0.0, float(fields["sticker_probability"])))
            except (TypeError, ValueError):
                return self._page_error("sticker_probability 必须是数字")
        for key in ("sticker_cooldown_seconds", "sticker_context_messages"):
            if key in fields:
                try:
                    value = int(fields[key])
                    fields[key] = (
                        min(86400, max(0, value))
                        if key == "sticker_cooldown_seconds"
                        else min(20, max(1, value))
                    )
                except (TypeError, ValueError):
                    return self._page_error(f"{key} 必须是整数")
        if "sticker_llm_timeout" in fields:
            try:
                fields["sticker_llm_timeout"] = min(180, max(5, int(fields["sticker_llm_timeout"])))
            except (TypeError, ValueError):
                return self._page_error("sticker_llm_timeout 必须是整数")
        for key, limit in (("sticker_llm_provider_id", 200), ("sticker_prompt", 4000), ("sticker_decision_prompt", 4000)):
            if key in fields:
                fields[key] = str(fields[key] or "").strip()[:limit]
        for key, lower, upper in (("drawing_llm_context_messages", 0, 30), ("drawing_llm_timeout", 5, 180), ("drawing_llm_context_chars", 4096, 128000)):
            if key in fields:
                try:
                    fields[key] = min(upper, max(lower, int(fields[key])))
                except (TypeError, ValueError):
                    return self._page_error(f"{key} 必须是整数")
        for key, upper in (("retry_503_count", 3), ("drawing_llm_max_tokens", 16384)):
            if key in fields:
                try:
                    value = int(fields[key])
                except (TypeError, ValueError):
                    return self._page_error(f"{key} 必须是整数")
                if not 0 <= value <= upper:
                    return self._page_error(f"{key} 必须在 0–{upper} 之间")
                fields[key] = value
        for key, limit in (("drawing_llm_provider_id", 200), ("drawing_llm_prompt", 4000)):
            if key in fields:
                if not isinstance(fields[key], str) or len(fields[key]) > limit:
                    return self._page_error(f"{key} 必须是不超过 {limit} 字符的字符串")
                fields[key] = fields[key].strip()
        if "drawing_trigger" in fields:
            marker = fields["drawing_trigger"]
            if not isinstance(marker, str) or len(marker.strip()) > 16 or re.search(r"[\s/]", marker.strip()):
                return self._page_error("绘图暗号应为不含空白或 / 的 1–16 个字符；留空关闭")
            fields["drawing_trigger"] = marker.strip()
        allowed_enums = {
            "provider": {"novelai_official", "openai_compatible"},
            "invoke_mode": {"disabled", "command_only", "llm_tool_only", "both"},
            "private_access": {"all", "admin_only", "allowlist", "disabled"},
            "group_access": {"all", "admin_only", "allowlist", "disabled"},
            "retry_mode": {"none", "rate_limit_once", "rate_limit_twice"},
            "error_notify_mode": {"silent", "final_only", "admin_only"},
            "sticker_send_mode": {"after_reply", "same_bubble"},
            "llm_card_mode": {"context", "strict"},
            "drawing_llm_output_mode": {"auto", "tool", "json"},
            "drawing_llm_context_mode": {"shared", "legacy"},
            "drawing_llm_transport": {"auto", "stream", "nonstream"},
            "drawing_llm_reasoning_effort": {"inherit", "low", "medium", "high"},
        }
        for key, options in allowed_enums.items():
            if key in fields and fields[key] not in options:
                return self._page_error(f"{key} 的值不受支持")
        previous_config = dict(self.config)
        self.config.update(fields)
        self.config["config_version"] = 7
        try:
            self._save_config()
        except Exception as exc:
            self.config.clear()
            self.config.update(previous_config)
            logger.exception(f"[{PLUGIN_NAME}] WebUI 保存配置失败: {exc}")
            return self._page_error("配置写入失败，已撤销本次修改，请检查 AstrBot 日志")
        self._sync_sticker_emotion_tool()
        self._sync_marker_filter()
        return json_response({"saved": True, "message": "配置已保存", "config": self._public_config()})

    async def page_test_provider(self):
        payload = await request.json(default={})
        payload = payload if isinstance(payload, dict) else {}
        provider = str(payload.get("provider", self._provider_name()))
        if provider == "openai_compatible":
            url = str(payload.get("base_url", self._cfg("openai_base_url", ""))).rstrip("/") + "/models"
            key = str(payload.get("api_key", self._cfg("api_key", "")))
        else:
            base = str(payload.get("base_url", self._cfg("base_url", "https://image.novelai.net"))).rstrip("/")
            url = "https://api.novelai.net/user/information" if "image.novelai.net" in base else base.replace("/ai/generate-image", "") + "/user/information"
            key = str(payload.get("api_token", self._cfg("api_token", "")))
        if not url or not key or key == "********":
            return self._page_error("请先填写完整的服务地址和密钥")
        if provider == "openai_compatible":
            auth_header = str(payload.get("auth_header", self._cfg("openai_auth_header", "Authorization")) or "Authorization")
            auth_prefix = str(payload.get("auth_prefix", self._cfg("openai_auth_prefix", "Bearer")) or "").strip()
            headers = {auth_header: f"{auth_prefix} {key}".strip()}
        else:
            headers = {"Authorization": key if key.lower().startswith("bearer ") else f"Bearer {key}"}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
                async with session.get(url, headers=headers) as response:
                    if response.status >= 400:
                        raise self._http_error(response.status, await self._read_limited_response(response, 1024 * 1024))
            return json_response({"ok": True, "message": "连接测试成功"})
        except ProviderError as exc:
            return json_response({"ok": False, "message": exc.message})
        except Exception:
            return json_response({"ok": False, "message": "连接测试失败，请检查地址、密钥和网络"})

    async def page_presets(self):
        return json_response({"presets": self._public_presets()})

    async def page_create_preset(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return self._page_error("角色卡格式错误")
        try:
            preset = self._create_preset_record(payload)
        except ValueError as exc:
            return self._page_error(str(exc))
        except Exception as exc:
            logger.exception(f"[{PLUGIN_NAME}] 创建角色卡失败: {exc}")
            return self._page_error("创建角色卡失败，请检查插件数据目录权限和 AstrBot 日志")
        return json_response({"saved": True, "message": "角色卡已创建并同步人设映射", "preset": preset, "config": self._public_config()})


    async def page_manage_preset(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return self._page_error("角色卡格式错误")
        action = str(payload.get("action", "")).lower()
        preset_id = str(payload.get("id", ""))
        if action not in {"create", "update", "delete"}:
            return self._page_error("不支持的角色卡操作")
        if action == "delete":
            try:
                deleted = self._delete_preset_record(preset_id)
            except Exception as exc:
                logger.exception(f"[{PLUGIN_NAME}] 删除角色卡失败: {exc}")
                return self._page_error("删除角色卡失败，请检查插件数据目录权限和 AstrBot 日志")
            if not deleted:
                return self._page_error("角色卡不存在", status_code=404)
            return json_response({
                "saved": True,
                "message": "角色卡已删除，相关默认项和人设映射已清理",
                "presets": self.presets,
                "config": self._public_config(),
            })
        if action == "update":
            preset = self._get_preset(preset_id, include_disabled=True)
            if not preset:
                return self._page_error("角色卡不存在", status_code=404)
            try:
                self._update_preset_record(preset, payload)
            except ValueError as exc:
                return self._page_error(str(exc))
            except Exception as exc:
                logger.exception(f"[{PLUGIN_NAME}] 更新角色卡失败: {exc}")
                return self._page_error("更新角色卡失败，请检查插件数据目录权限和 AstrBot 日志")
            return json_response({"saved": True, "message": "角色卡已更新并同步人设映射", "preset": preset, "config": self._public_config()})
        return self._create_preset_response(payload)

    def _create_preset_response(self, payload: dict[str, Any]):
        try:
            preset = self._create_preset_record(payload)
        except ValueError as exc:
            return self._page_error(str(exc))
        except Exception as exc:
            logger.exception(f"[{PLUGIN_NAME}] 创建角色卡失败: {exc}")
            return self._page_error("创建角色卡失败，请检查插件数据目录权限和 AstrBot 日志")
        return json_response({"saved": True, "message": "角色卡已创建并同步人设映射", "preset": preset, "config": self._public_config()})

    async def page_update_preset(self, preset_id: str):
        preset = self._get_preset(preset_id, include_disabled=True)
        if not preset:
            return self._page_error("角色卡不存在", status_code=404)
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return self._page_error("角色卡格式错误")
        try:
            self._update_preset_record(preset, payload)
        except ValueError as exc:
            return self._page_error(str(exc))
        except Exception as exc:
            logger.exception(f"[{PLUGIN_NAME}] 更新角色卡失败: {exc}")
            return self._page_error("更新角色卡失败，请检查插件数据目录权限和 AstrBot 日志")
        return json_response({"saved": True, "message": "角色卡已更新并同步人设映射", "preset": preset, "config": self._public_config()})

    async def page_delete_preset(self, preset_id: str):
        try:
            deleted = self._delete_preset_record(preset_id)
        except Exception as exc:
            logger.exception(f"[{PLUGIN_NAME}] 删除角色卡失败: {exc}")
            return self._page_error("删除角色卡失败，请检查插件数据目录权限和 AstrBot 日志")
        if not deleted:
            return self._page_error("角色卡不存在", status_code=404)
        return json_response({
            "saved": True,
            "message": "角色卡已删除，相关默认项和人设映射已清理",
            "presets": self.presets,
            "config": self._public_config(),
        })

    def _reference_list(self) -> list[dict[str, Any]]:
        refs = []
        for meta_path in self.reference_dir.glob("*.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                refs.append({k: meta.get(k) for k in ("id", "name", "type", "filename", "created_at")})
            except Exception:
                pass
        return refs

    async def page_references(self):
        return json_response({"references": self._reference_list()})

    async def page_upload_reference(self):
        files = await request.files()
        upload = files.get("file") if files else None
        if upload is None:
            return self._page_error("请选择图片文件")
        filename = str(upload.filename or "reference.png")
        content_type = str(upload.content_type or mimetypes.guess_type(filename)[0] or "")
        if content_type not in {"image/png", "image/jpeg", "image/webp"}:
            return self._page_error("仅支持 PNG、JPEG、WebP 图片")
        data = await upload.read()
        if len(data) > 12 * 1024 * 1024:
            return self._page_error("图片不能超过 12MB")
        magic_ok = (content_type == "image/png" and data.startswith(b"\x89PNG")) or (content_type == "image/jpeg" and data.startswith(b"\xff\xd8")) or (content_type == "image/webp" and data[:4] == b"RIFF" and data[8:12] == b"WEBP")
        if not magic_ok:
            return self._page_error("图片内容与文件类型不匹配")
        ref_id = uuid.uuid4().hex[:10]
        ext = ".png" if content_type == "image/png" else ".jpg" if content_type == "image/jpeg" else ".webp"
        stored_name = f"{ref_id}{ext}"
        (self.reference_dir / stored_name).write_bytes(data)
        meta = {"id": ref_id, "name": Path(filename).stem[:80], "type": "character", "filename": stored_name, "created_at": int(time.time())}
        (self.reference_dir / f"{ref_id}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return json_response({"saved": True, "message": "参考图已上传", "reference": meta})


    async def page_manage_reference(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict) or str(payload.get("action", "")).lower() != "delete":
            return self._page_error("不支持的参考图操作")
        return await self.page_delete_reference(str(payload.get("id", "")))

    async def page_delete_reference(self, reference_id: str):
        _, meta = await self._load_reference(reference_id)
        if not meta:
            return self._page_error("参考图不存在", status_code=404)
        safe_id = Path(str(reference_id)).name
        try:
            (self.reference_dir / str(meta["filename"])).unlink(missing_ok=True)
            (self.reference_dir / f"{safe_id}.json").unlink(missing_ok=True)
            changed = False
            for preset in self.presets:
                if str(preset.get("reference_id", "")) == safe_id:
                    preset["reference_id"] = ""
                    changed = True
            if changed:
                self._save_presets()
        except Exception as exc:
            logger.exception(f"[{PLUGIN_NAME}] 删除参考图失败: {exc}")
            return self._page_error("删除参考图失败，请检查插件数据目录权限和 AstrBot 日志")
        return json_response({"saved": True, "message": "参考图已删除，相关角色卡引用已清理"})

    async def page_jobs(self):
        return json_response({"jobs": self._jobs[-50:]})

    async def page_last_request(self):
        return json_response({"requests": list(reversed(list(getattr(self, "_request_snapshots", {}).values()))),
            "planner": list(getattr(self, "_planner_diagnostics", {}).values())})

    async def page_manage_jobs(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return self._page_error("任务记录操作格式错误")
        action = str(payload.get("action", "") or "").strip().lower()
        if action == "clear":
            removed = len(self._jobs)
            self._jobs.clear()
            getattr(self, "_request_snapshots", {}).clear()
            return json_response({"ok": True, "message": f"已清空 {removed} 条任务记录", "jobs": []})
        if action == "delete":
            job_id = str(payload.get("job_id", "") or "").strip()
            if not job_id:
                return self._page_error("缺少 Job ID")
            remaining = [job for job in self._jobs if str(job.get("job_id", "")) != job_id]
            if len(remaining) == len(self._jobs):
                return self._page_error("任务记录不存在", status_code=404)
            self._jobs = remaining
            self._request_snapshots = {key: value for key, value in getattr(self, "_request_snapshots", {}).items()
                if value["job_id"] != job_id}
            return json_response({
                "ok": True,
                "message": "任务记录已删除",
                "jobs": self._jobs[-50:],
            })
        return self._page_error("不支持的任务记录操作")

    def _capabilities(self) -> dict[str, Any]:
        if self._provider_name() == "novelai_official":
            return {"text_to_image": True, "img2img": True, "precise_reference": True, "vibe_transfer": False}
        return {"text_to_image": True, "img2img": True, "precise_reference": False, "vibe_transfer": False}


NovelAIPainterPlugin._sync_drawing_tool_schema()

__all__ = ["NovelAIPainterPlugin"]
