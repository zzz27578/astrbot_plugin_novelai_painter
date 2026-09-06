from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import mimetypes
import os
import random
import re
import time
import uuid
import zipfile
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
from astrbot.core.message.components import Image
from astrbot.core.message.message_event_result import MessageChain

PLUGIN_NAME = "astrbot_plugin_novelai_painter"
VERSION = "2.4.1"
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
    "max_api_requests_per_job", "dedupe_window_seconds", "queue_timeout", "retry_mode", "retry_delay",
    "error_notify_mode", "show_queue_notice", "notify_429", "show_retry_notice", "auto_clean_delay",
    "default_preset_id", "persona_preset_map", "llm_auto_reference", "img2img_enabled",
    "reference_enabled", "img2img_strength", "img2img_noise", "img2img_color_correct",
    "reference_strength", "reference_fidelity", "reference_information_extracted",
} | STICKER_DEFAULTS.keys()


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
        self._preset_schema_migrated = False
        self.presets = self._load_presets()
        self._ensure_defaults()
        self._sync_sticker_emotion_tool()
        if self._preset_schema_migrated:
            try:
                self._save_presets()
            except Exception as exc:
                logger.warning(f"[{PLUGIN_NAME}] 保存角色卡迁移结果失败: {exc}")
        self._migrate_embedded_persona_bindings()
        self._register_web_api()

    # --------------------------- configuration ---------------------------
    def _ensure_defaults(self) -> None:
        defaults = {
            "config_version": 6,
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
            "error_notify_mode": "final_only",
            "show_queue_notice": True,
            "notify_429": True,
            "show_retry_notice": False,
            "auto_clean_delay": 300,
            "default_preset_id": "",
            "persona_preset_map": {},
            "llm_auto_reference": True,
            "img2img_enabled": True,
            "reference_enabled": True,
            "img2img_strength": 0.7,
            "img2img_noise": 0.1,
            "img2img_color_correct": True,
            "reference_strength": 0.6,
            "reference_fidelity": 0.6,
            "reference_information_extracted": 1.0,
            **STICKER_DEFAULTS,
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
        return mode in {entry, "both"}

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
                legacy_style = str(preset.get("style_prompt", "") or "").strip()
                legacy_character = str(preset.get("character_prompt", "") or "").strip()
                positive_prompt = str(preset.get("positive_prompt", "") or "").strip()
                if not positive_prompt:
                    positive_prompt = ", ".join(
                        part for part in (legacy_style, legacy_character) if part
                    )
                if any(key not in preset for key in ("positive_prompt", "lock_positive", "positive_strength")) or any(
                    key in preset
                    for key in (
                        "style_prompt", "character_prompt", "lock_style",
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
                    "style_prompt", "character_prompt", "lock_style",
                    "lock_character", "style_strength", "character_strength",
                ):
                    preset.pop(legacy_key, None)
                normalized.append(preset)
            return normalized
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 读取角色卡失败: {exc}")
            return []

    def _save_presets(self) -> None:
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
        return {
            # Preset IDs are server-owned and immutable. Accepting an ID from
            # a create/update payload can create duplicates or orphan mappings.
            "id": preset_id,
            "name": name[:80],
            "description": str(merged.get("description", "") or "").strip()[:300],
            "positive_prompt": positive_prompt[:8000],
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
    ) -> set[str]:
        try:
            raw_request = str(event.get_message_str() or "").lower().replace("_", " ") if event else ""
        except Exception:
            raw_request = ""
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
    ) -> str:
        """Apply request-local role-card overrides without mutating the card."""
        if not locked:
            return positive_prompt
        requested_changes = self._requested_change_categories(event, candidate_prompt)
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
    ) -> str:
        """Let an explicit request override role-card negatives for that category."""
        requested_changes = self._requested_change_categories(event, candidate_prompt)
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

    def _compose_prompt(self, prompt: str, event: AstrMessageEvent | None = None, preset_id: str | None = None, *, preset_override: dict[str, Any] | None = None) -> tuple[str, str, str]:
        preset = preset_override if preset_override is not None else self._resolve_preset(event, preset_id)
        user_prompt = str(prompt or "").strip()
        if not preset:
            if self._provider_name() == "openai_compatible" and self._as_bool(self._cfg("quality_toggle", True)):
                user_prompt = f"high quality, highly detailed, {user_prompt}"
            return user_prompt[:12000], "", ""

        positive_prompt = str(preset.get("positive_prompt", "") or "").strip()
        lock_positive = self._as_bool(preset.get("lock_positive", True), True)
        positive_strength = self._preset_strength(preset.get("positive_strength", 1.35), 1.35)
        effective_positive = self._adapt_positive_prompt(
            positive_prompt,
            event,
            lock_positive,
            user_prompt,
        )
        requested_changes = self._requested_change_categories(event, user_prompt)

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
        if not preset:
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

    # --------------------------- permissions and dedupe ---------------------------
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

    def _claim_llm_tool_invocation(self, event: AstrMessageEvent) -> bool:
        """Atomically gate duplicate LLM tool calls before any async preparation."""
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
                if path.is_file() and now - path.stat().st_mtime > max_age:
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

    def _official_parameters(self, prompt: str, operation: str, image_b64: str | None, reference: dict[str, Any] | None, negative_override: str = "", preset: dict[str, Any] | None = None, *, size: tuple[int, int] | None = None) -> dict[str, Any]:
        model = self._active_model()
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
        if "4" in model or "5" in model:
            params.update({
                "use_coords": False,
                "v4_prompt": {"caption": {"base_caption": prompt, "char_captions": []}, "use_coords": False, "use_order": True},
                "v4_negative_prompt": {"caption": {"base_caption": negative, "char_captions": []}, "legacy_uc": False},
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

    async def _call_official(self, prompt: str, operation: str, job_id: str, image_b64: str | None, reference: dict[str, Any] | None, negative_override: str = "", preset: dict[str, Any] | None = None, *, size: tuple[int, int] | None = None) -> str:
        base_url = str(self._cfg("base_url", "https://image.novelai.net") or "https://image.novelai.net").strip().rstrip("/")
        url = base_url if base_url.endswith("/ai/generate-image") else f"{base_url}/ai/generate-image"
        token = str(self._cfg("api_token", "") or "").strip()
        if not token:
            raise ProviderError("not_configured", "NovelAI 官方 Token 尚未配置")
        headers = {"Authorization": token if token.lower().startswith("bearer ") else f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/zip, application/json"}
        payload = {"input": prompt, "model": self._active_model(), "action": "generate", "parameters": self._official_parameters(prompt, operation, image_b64, reference, negative_override, preset, size=size)}
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
        return ProviderError(f"http_{status}", f"图片服务返回 HTTP {status}", retryable=False)

    def _retry_limit(self) -> int:
        mode = str(self._cfg("retry_mode", "none") or "none")
        configured = {"none": 1, "rate_limit_once": 2, "rate_limit_twice": 3}.get(mode, 1)
        try:
            hard_limit = max(1, min(3, int(self._cfg("max_api_requests_per_job", 3) or 3)))
        except (TypeError, ValueError):
            hard_limit = 1
        return min(configured, hard_limit)

    def _should_retry(self, error: ProviderError, attempts: int) -> bool:
        return error.code == "429" and error.retryable and attempts < self._retry_limit()

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
            return GenerationResult(
                True,
                str(claimed_job_id),
                provider,
                message="同一条用户消息的重复生成请求已被拦截。",
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
            None if preset_override is not None else event,
            resolved_preset_id or None,
            preset_override=preset_override,
        )
        if active_preset_id:
            logger.info(
                f"[{PLUGIN_NAME}] job={job_id} 已合并角色卡 {active_preset_id}: "
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
                result = GenerationResult(False, job_id, provider, error_code="missing_reference", message="请先在 WebUI 上传参考图并绑定到当前角色卡。", attempts=0)
                self._recent_jobs[key] = (time.time(), result)
                return result
            if reference:
                reference["reference_type"] = effective_reference_type or "character"
            image_b64 = base64.b64encode(image_bytes).decode("ascii") if image_bytes and provider == "novelai_official" else None
            attempts = 0
            while True:
                try:
                    timeout = max(1, int(self._cfg("queue_timeout", 120) or 120))
                    if not quiet and self.lock.locked() and self._as_bool(self._cfg("show_queue_notice", True)):
                        await self._notify(event, "当前生图通道繁忙，任务已排队。", "queue")
                    await asyncio.wait_for(self.lock.acquire(), timeout=timeout)
                except asyncio.TimeoutError:
                    result = GenerationResult(False, job_id, provider, error_code="queue_timeout", message="生图排队超时，任务已取消。", attempts=attempts)
                    break
                try:
                    attempts += 1
                    try:
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
                    finally:
                        self.lock.release()
                    result = GenerationResult(True, job_id, provider, path=path, message="图片已生成并发送到当前会话。", attempts=attempts)
                    break
                except ProviderError as exc:
                    logger.warning(f"[{PLUGIN_NAME}] job={job_id} attempt={attempts} provider={provider} code={exc.code}: {exc.message}")
                    if not self._should_retry(exc, attempts):
                        result = GenerationResult(False, job_id, provider, error_code=exc.code, message=exc.message, attempts=attempts)
                        break
                    configured_delay = max(1.0, min(300.0, float(self._cfg("retry_delay", 5.0) or 5.0)))
                    delay = max(configured_delay, float(exc.retry_after or 0))
                    if not quiet and self._as_bool(self._cfg("show_retry_notice", False)):
                        await self._notify(event, f"图片服务限流，{delay:g} 秒后进行第 {attempts + 1} 次尝试。", "retry")
                    # Do not monopolize the global provider slot while waiting
                    # for a Retry-After window.
                    await asyncio.sleep(delay)
            self._recent_jobs[key] = (time.time(), result)
            job_record = asdict(result)
            job_record["has_image"] = bool(job_record.pop("path", None))
            self._jobs.append({**job_record, "operation": operation, "preset_id": active_preset_id, "kind": job_kind, "emotion": emotion, "created_at": int(time.time())})
            self._jobs = self._jobs[-50:]
            return result
        finally:
            if inflight_jobs.get(key) == job_id:
                inflight_jobs.pop(key, None)

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
            return "图片生成未完成：未找到图片文件。"
        delivered_jobs = getattr(self, "_delivered_jobs", None)
        if delivered_jobs is None:
            self._delivered_jobs = {}
            delivered_jobs = self._delivered_jobs
        if result.job_id in delivered_jobs:
            return "图片已生成；重复发送已被拦截。"
        delivered_jobs[result.job_id] = time.time()
        try:
            await self._send_generated_image(event, result)
            return result.message
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 发送图片失败 job={result.job_id}: {exc}")
            try:
                Path(result.path).unlink(missing_ok=True)
            except Exception:
                pass
            await self._notify(event, "图片已生成，但发送到当前会话失败。", "error")
            return "图片已生成，但发送失败。"

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
        root = f"/{prefix}"
        return (
            "可执行命令：\n"
            f"{root} draw <画面描述>\n"
            f"{root} img2img <画面描述>\n"
            f"{root} reference <character|style|both> <画面描述>\n"
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
    ):
        """仅在用户明确要求生成、绘制或修改图片时调用；每条用户消息最多生成并发送一张图片。插件会强制应用当前 AstrBot 人设映射或默认角色卡正向/负向 Tag，并可自动使用角色卡绑定的参考图。图片发送后必须停止调用工具，并用当前人设向用户简短回复。

        Args:
            prompt(string): 必须传入根据用户本次要求整理出的动作、姿势、表情、构图、场景或明确修改项。优先整理为具体的英文 NovelAI / Danbooru Tag；不要重复角色卡的固定主体与画风，插件会在后端拼接角色卡正向 Tag 并加入负面 Tag。不得省略该参数。
            operation(string): 生成方式，只能是 generate、img2img 或 reference。普通生图使用 generate；用户明确要求基于绑定图片修改时使用 img2img；NovelAI 精确参考使用 reference。省略时若开启自动参考且当前角色卡绑定了参考图，插件会自动选择合适方式。
            reference_type(string): reference 模式的参考类型，只能是 character、style 或 both；省略时继承当前角色卡保存的参考类型，无角色卡时使用 character。
        """
        if not self._mode_allows("llm_tool"):
            await self._notify(event, "当前未启用自然语言生图入口。", "error")
            return (
                "FINAL_IMAGE_TOOL_RESULT: Natural-language image generation is disabled. "
                "Do not call any tool again for this message. Reply to the user briefly."
            )

        if not self._claim_llm_tool_invocation(event):
            return (
                "FINAL_IMAGE_TOOL_RESULT: ENTRY_GATE_BLOCKED_DUPLICATE. An image generation call for this "
                "exact user message was already accepted before backend preparation. Do not call any tool "
                "again. The accepted call is responsible for sending the image; reply to the user once only "
                "after that accepted call completes."
            )

        normalized_prompt = str(prompt or "").strip()
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

        active_preset = await self._resolve_active_preset(event)
        normalized_prompt = self._filter_llm_prompt_conflicts(
            normalized_prompt,
            event,
            active_preset,
        )
        resolved_operation = self._resolve_llm_operation(operation, active_preset)
        requested_reference_type = str(reference_type or "").strip().lower()
        normalized_reference_type = (
            requested_reference_type
            if requested_reference_type in {"character", "style", "both"}
            else None
        )
        result = await self._run_job(
            event,
            normalized_prompt,
            resolved_operation,
            preset_id=str(active_preset.get("id", "")) if active_preset else None,
            reference_type=normalized_reference_type,
        )
        user_visible_result = await self._finish_event(event, result)
        if result.ok:
            return (
                "FINAL_IMAGE_TOOL_RESULT: The image generation task is complete and the image has already "
                "been sent to the current conversation. Do not call novelai_generate_image or any other "
                "tool again for this user message. Now reply to the user once, briefly, in your current "
                f"persona. Internal status: {user_visible_result}"
            )
        return (
            "FINAL_IMAGE_TOOL_RESULT: The image generation task ended with a final error already shown to "
            "the user. Do not call novelai_generate_image or any other tool again for this user message. "
            f"Reply once with a brief explanation. Internal status: {user_visible_result}"
        )

    @filter.regex(r"^/[A-Za-z][A-Za-z0-9_-]*(?:@[A-Za-z0-9_-]+)?(?:\s|$)")
    async def cmd_draw(self, event: AstrMessageEvent):
        """NovelAI 生图、角色卡和模型固定命令入口。"""
        raw_message = event.get_message_str().strip()
        match = re.match(r"^/(?P<name>[A-Za-z][A-Za-z0-9_-]*)(?:@[A-Za-z0-9_-]+)?(?:\s+(?P<body>.*))?$", raw_message, re.S)
        if not match or match.group("name").lower() != str(self._cfg("command_prefix", "nai") or "nai").strip().lstrip("/").lower():
            return
        if not self._mode_allows("command"):
            yield event.plain_result("当前未启用固定命令入口。")
            return
        allowed, reason = self._can_use(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        text = (match.group("body") or "").strip()
        if not text or text.lower() in {"help", "?", "菜单"}:
            yield event.plain_result(self._command_help())
            return
        command, separator, remainder = text.partition(" ")
        command = command.lower()
        operation = "generate"
        reference_type = None
        body = text
        if command in {"draw", "generate", "生图"}:
            body = remainder.strip() if separator else ""
        elif command in {"img2img", "i2i", "图生图"}:
            operation = "img2img"
            body = remainder.strip() if separator else ""
        elif command in {"reference", "ref", "参考图"}:
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
        if not body.strip():
            yield event.plain_result("请补充画面描述。")
            return
        preset_id = None
        reference_id = None
        result = await self._run_job(event, body, operation, preset_id, reference_id, reference_type)
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
        try:
            items = await self.context.persona_manager.get_all_personas()
            personas = [{"id": str(p.persona_id), "name": str(p.persona_id)} for p in items]
        except Exception:
            pass
        return json_response({"config": self._public_config(), "presets": self._public_presets(), "references": self._reference_list(), "personas": personas, "capabilities": self._capabilities()})

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
        allowed_enums = {
            "provider": {"novelai_official", "openai_compatible"},
            "invoke_mode": {"disabled", "command_only", "llm_tool_only", "both"},
            "private_access": {"all", "admin_only", "allowlist", "disabled"},
            "group_access": {"all", "admin_only", "allowlist", "disabled"},
            "retry_mode": {"none", "rate_limit_once", "rate_limit_twice"},
            "error_notify_mode": {"silent", "final_only", "admin_only"},
            "sticker_send_mode": {"after_reply", "same_bubble"},
        }
        for key, options in allowed_enums.items():
            if key in fields and fields[key] not in options:
                return self._page_error(f"{key} 的值不受支持")
        previous_config = dict(self.config)
        self.config.update(fields)
        self.config["config_version"] = 6
        try:
            self._save_config()
        except Exception as exc:
            self.config.clear()
            self.config.update(previous_config)
            logger.exception(f"[{PLUGIN_NAME}] WebUI 保存配置失败: {exc}")
            return self._page_error("配置写入失败，已撤销本次修改，请检查 AstrBot 日志")
        self._sync_sticker_emotion_tool()
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

    async def page_manage_jobs(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return self._page_error("任务记录操作格式错误")
        action = str(payload.get("action", "") or "").strip().lower()
        if action == "clear":
            removed = len(self._jobs)
            self._jobs.clear()
            return json_response({"ok": True, "message": f"已清空 {removed} 条任务记录", "jobs": []})
        if action == "delete":
            job_id = str(payload.get("job_id", "") or "").strip()
            if not job_id:
                return self._page_error("缺少 Job ID")
            remaining = [job for job in self._jobs if str(job.get("job_id", "")) != job_id]
            if len(remaining) == len(self._jobs):
                return self._page_error("任务记录不存在", status_code=404)
            self._jobs = remaining
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


__all__ = ["NovelAIPainterPlugin"]
