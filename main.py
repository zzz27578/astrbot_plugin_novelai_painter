from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import mimetypes
import os
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

from astrbot.core.message.message_event_result import MessageChain

PLUGIN_NAME = "astrbot_plugin_novelai_painter"
VERSION = "2.1.0"
DEFAULT_MODEL = "nai-diffusion-5-full"
DEFAULT_NEGATIVE = (
    "lowres, blurry, bad anatomy, bad hands, text, watermark, error, missing fingers, "
    "extra digit, fewer digits, cropped, worst quality, low quality, jpeg artifacts, "
    "signature, username"
)


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
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@register(PLUGIN_NAME, "AstrBot 社区", "NovelAI 生图与参考图工具", VERSION)
class NovelAIPainterPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.temp_dir = self.data_dir / "temp"
        self.reference_dir = self.data_dir / "references"
        self.presets_path = self.data_dir / "presets.json"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.reference_dir.mkdir(parents=True, exist_ok=True)
        self.lock = asyncio.Lock()
        self._recent_jobs: dict[str, tuple[float, GenerationResult]] = {}
        self._jobs: list[dict[str, Any]] = []
        self.presets = self._load_presets()
        self._ensure_defaults()
        self._register_web_api()

    # --------------------------- configuration ---------------------------
    def _ensure_defaults(self) -> None:
        defaults = {
            "config_version": 2,
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
            "max_api_requests_per_job": 1,
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
            "img2img_enabled": True,
            "reference_enabled": True,
            "img2img_strength": 0.7,
            "img2img_noise": 0.1,
            "img2img_color_correct": True,
            "reference_strength": 0.6,
            "reference_fidelity": 0.6,
            "reference_information_extracted": 1.0,
        }
        changed = False
        for key, value in defaults.items():
            if key not in self.config:
                self.config[key] = value
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
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 读取预设失败: {exc}")
            return []

    def _save_presets(self) -> None:
        tmp = self.presets_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.presets, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.presets_path)

    def _get_preset(self, preset_id: str | None) -> dict[str, Any] | None:
        if not preset_id:
            return None
        return next((p for p in self.presets if p.get("id") == preset_id), None)

    def _resolve_persona_id(self, event: AstrMessageEvent | None = None) -> str:
        candidates: list[Any] = []
        try:
            if event is not None:
                session_cfg = self.context.get_config(umo=event.unified_msg_origin)
                candidates.extend([session_cfg.get("persona_id"), session_cfg.get("persona")])
        except Exception:
            pass
        candidates.extend([self._cfg("persona_id", ""), self._cfg("persona", "")])
        for candidate in candidates:
            if isinstance(candidate, dict):
                candidate = candidate.get("id") or candidate.get("persona_id")
            if candidate:
                return str(candidate)
        return ""

    def _resolve_preset(self, event: AstrMessageEvent | None = None, preset_id: str | None = None):
        if preset_id and self._get_preset(preset_id):
            return self._get_preset(preset_id)
        persona_id = self._resolve_persona_id(event)
        mapping = self._cfg("persona_preset_map", {})
        if isinstance(mapping, dict) and persona_id:
            mapped = self._get_preset(str(mapping.get(persona_id, "")))
            if mapped:
                return mapped
        return self._get_preset(str(self._cfg("default_preset_id", "")))

    def _compose_prompt(self, prompt: str, event: AstrMessageEvent | None = None, preset_id: str | None = None) -> tuple[str, str, str]:
        preset = self._resolve_preset(event, preset_id)
        parts: list[str] = []
        if self._as_bool(self._cfg("quality_toggle", True)):
            parts.append("masterpiece, best quality, highres")
        if preset:
            style_prompt = str(preset.get("style_prompt", "")).strip()
            character_prompt = str(preset.get("character_prompt", "")).strip()
            parts.extend([style_prompt, character_prompt])
            if character_prompt and preset.get("lock_character", True):
                parts.append("keep the same character identity, facial features, hairstyle, outfit details and accessories; only change the requested action, pose, expression or scene")
        parts.append(prompt.strip())
        composed = ", ".join(part for part in parts if part)
        preset_negative = str(preset.get("negative_prompt", "")).strip() if preset else ""
        return composed[:12000], str(preset.get("id", "")) if preset else "", preset_negative

    # --------------------------- permissions and dedupe ---------------------------
    def _event_key(self, event: AstrMessageEvent, prompt: str, operation: str) -> str:
        message_obj = getattr(event, "message_obj", None)
        raw_id = None
        for attr in ("message_id", "msg_id", "id"):
            raw_id = getattr(message_obj, attr, None)
            if raw_id:
                break
        if raw_id:
            return f"id:{event.unified_msg_origin}:{raw_id}:{operation}"
        raw = f"{event.unified_msg_origin}|{event.get_sender_id()}|{operation}|{prompt.strip()}"
        return "hash:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _can_use(self, event: AstrMessageEvent) -> tuple[bool, str]:
        if event.is_private_chat():
            policy = str(self._cfg("private_access", "all"))
        else:
            policy = str(self._cfg("group_access", "admin_only"))
        if self._as_bool(self._cfg("admin_bypass", True)) and event.is_admin():
            return True, ""
        sender = str(event.get_sender_id())
        group = str(event.get_group_id() or "")
        if policy == "disabled":
            return False, "当前会话未开启生图权限。"
        if policy == "admin_only":
            return False, "当前会话仅允许管理员使用生图功能。"
        if policy == "allowlist":
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
                return archive.read(names[0]) if names else None
        except Exception:
            return None

    @staticmethod
    def _decode_data_url(value: str) -> bytes:
        if value.startswith("data:") and "," in value:
            value = value.split(",", 1)[1]
        return base64.b64decode(value)

    def _save_image(self, image_bytes: bytes, job_id: str) -> str:
        path = self.temp_dir / f"{job_id}.png"
        path.write_bytes(image_bytes)
        return str(path)

    async def _parse_image_response(self, response: aiohttp.ClientResponse, body: bytes, job_id: str) -> str:
        content_type = response.headers.get("Content-Type", "").lower()
        image_bytes = self._decode_zip(body)
        if image_bytes is None and body.startswith(b"\x89PNG"):
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
                            image_bytes = await img_resp.read()
            except ProviderError:
                raise
            except Exception as exc:
                raise ProviderError("response_parse", f"响应解析失败: {exc}")
        if not image_bytes:
            raise ProviderError("response_parse", "服务端返回中没有可用图片")
        return self._save_image(image_bytes, job_id)

    def _official_parameters(self, prompt: str, operation: str, image_b64: str | None, reference: dict[str, Any] | None, negative_override: str = "") -> dict[str, Any]:
        model = self._active_model()
        negative_parts = [str(self._cfg("negative_prompt", DEFAULT_NEGATIVE) or "").strip(), str(negative_override or "").strip()]
        negative = ", ".join(dict.fromkeys(part for part in negative_parts if part))
        params: dict[str, Any] = {
            "params_version": 3,
            "width": max(64, min(2048, int(self._cfg("width", 832) or 832))),
            "height": max(64, min(2048, int(self._cfg("height", 1216) or 1216))),
            "scale": max(1.0, min(20.0, float(self._cfg("scale", 5.0) or 5.0))),
            "sampler": str(self._cfg("sampler", "k_euler_ancestral") or "k_euler_ancestral"),
            "steps": max(1, min(50, int(self._cfg("steps", 28) or 28))),
            "n_samples": 1,
            "ucPreset": 0,
            "qualityToggle": self._as_bool(self._cfg("quality_toggle", True)),
            "negative_prompt": negative,
        }
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

    async def _call_official(self, prompt: str, operation: str, job_id: str, image_b64: str | None, reference: dict[str, Any] | None, negative_override: str = "") -> str:
        base_url = str(self._cfg("base_url", "https://image.novelai.net") or "https://image.novelai.net").strip().rstrip("/")
        url = base_url if base_url.endswith("/ai/generate-image") else f"{base_url}/ai/generate-image"
        token = str(self._cfg("api_token", "") or "").strip()
        if not token:
            raise ProviderError("not_configured", "NovelAI 官方 Token 尚未配置")
        headers = {"Authorization": token if token.lower().startswith("bearer ") else f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/zip, application/json"}
        payload = {"input": prompt, "model": self._active_model(), "action": "generate", "parameters": self._official_parameters(prompt, operation, image_b64, reference, negative_override)}
        return await self._post_image_request(url, headers, payload, job_id)

    async def _call_openai(self, prompt: str, operation: str, job_id: str, image_bytes: bytes | None) -> str:
        base_url = str(self._cfg("openai_base_url", "") or "").strip().rstrip("/")
        api_key = str(self._cfg("api_key", "") or "").strip()
        if not base_url or not api_key:
            raise ProviderError("not_configured", "OpenAI 兼容模式的 Base URL 或 API Key 尚未配置")
        endpoint_key = "openai_edit_endpoint" if image_bytes else "openai_image_endpoint"
        endpoint = str(self._cfg(endpoint_key, "/v1/images/edits" if image_bytes else "/v1/images/generations") or "")
        url = endpoint if endpoint.startswith("http") else f"{base_url}/{endpoint.lstrip('/')}"
        headers = self._openai_headers(api_key)
        timeout = aiohttp.ClientTimeout(total=180)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                if image_bytes:
                    form = aiohttp.FormData()
                    form.add_field("model", self._active_model())
                    form.add_field("prompt", prompt)
                    form.add_field("n", "1")
                    form.add_field("size", f"{int(self._cfg('width', 832))}x{int(self._cfg('height', 1216))}")
                    form.add_field("image", image_bytes, filename="input.png", content_type="image/png")
                    async with session.post(url, headers=headers, data=form) as response:
                        body = await response.read()
                        if response.status >= 400:
                            raise self._http_error(response.status, body)
                        return await self._parse_image_response(response, body, job_id)
                payload = {"model": self._active_model(), "prompt": prompt, "n": 1, "size": f"{int(self._cfg('width', 832))}x{int(self._cfg('height', 1216))}", "response_format": "b64_json"}
                async with session.post(url, headers={**headers, "Content-Type": "application/json"}, json=payload) as response:
                    body = await response.read()
                    if response.status >= 400:
                        raise self._http_error(response.status, body)
                    return await self._parse_image_response(response, body, job_id)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise ProviderError("network", "图片服务网络请求失败；为避免重复扣费，本任务不会自动重试", retryable=False) from exc

    @staticmethod
    def _http_error(status: int, body: bytes) -> ProviderError:
        if status == 429:
            return ProviderError("429", "图片服务触发频率限制，请稍后再试", retryable=False)
        if status in {401, 403}:
            return ProviderError("auth", "图片服务认证失败，请检查 Key 或 Token", retryable=False)
        if status == 402:
            return ProviderError("quota", "图片服务额度不足", retryable=False)
        return ProviderError(f"http_{status}", f"图片服务返回 HTTP {status}", retryable=False)

    async def _post_image_request(self, url: str, headers: dict[str, str], payload: dict[str, Any], job_id: str) -> str:
        timeout = aiohttp.ClientTimeout(total=180)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                async with session.post(url, headers=headers, json=payload) as response:
                    body = await response.read()
                    if response.status not in {200, 201}:
                        raise self._http_error(response.status, body)
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
            image_path = self.reference_dir / str(meta.get("filename", ""))
            if not image_path.exists() or image_path.parent != self.reference_dir:
                return None, None
            data = image_path.read_bytes()
            return data, {**meta, "image_b64": base64.b64encode(data).decode("ascii")}
        except Exception:
            return None, None

    @staticmethod
    def _cached_result(result: GenerationResult) -> GenerationResult:
        return GenerationResult(result.ok, result.job_id, result.provider, result.path, result.error_code, result.message, result.attempts, False)

    async def _run_job(self, event: AstrMessageEvent, prompt: str, operation: str = "generate", preset_id: str | None = None, reference_id: str | None = None) -> GenerationResult:
        allowed, reason = self._can_use(event)
        job_id = uuid.uuid4().hex[:12]
        provider = self._provider_name()
        if not allowed:
            return GenerationResult(False, job_id, provider, error_code="permission", message=reason)
        if not prompt.strip():
            return GenerationResult(False, job_id, provider, error_code="invalid_prompt", message="请输入要生成的画面描述。")
        if operation == "img2img" and not self._as_bool(self._cfg("img2img_enabled", True)):
            return GenerationResult(False, job_id, provider, error_code="img2img_disabled", message="图生图功能当前已关闭。")
        if operation == "reference":
            if not self._as_bool(self._cfg("reference_enabled", True)):
                return GenerationResult(False, job_id, provider, error_code="reference_disabled", message="参考图功能当前已关闭。")
            if provider != "novelai_official":
                return GenerationResult(False, job_id, provider, error_code="unsupported", message="当前兼容后端不支持 NovelAI Precise Reference。")
        self._cleanup_expired()
        composed_prompt, active_preset_id, preset_negative = self._compose_prompt(prompt, event, preset_id)
        key = self._event_key(event, composed_prompt, operation)
        now = time.time()
        window = max(1, int(self._cfg("dedupe_window_seconds", 30) or 30))
        cached = self._recent_jobs.get(key)
        if cached and now - cached[0] <= window:
            return self._cached_result(cached[1])
        try:
            timeout = max(1, int(self._cfg("queue_timeout", 120) or 120))
            if self.lock.locked() and self._as_bool(self._cfg("show_queue_notice", True)):
                await self._notify(event, "当前生图通道繁忙，任务已排队。", "queue")
            await asyncio.wait_for(self.lock.acquire(), timeout=timeout)
        except asyncio.TimeoutError:
            result = GenerationResult(False, job_id, provider, error_code="queue_timeout", message="生图排队超时，任务已取消。")
            self._recent_jobs[key] = (time.time(), result)
            return result
        try:
            cached = self._recent_jobs.get(key)
            if cached and time.time() - cached[0] <= window:
                return self._cached_result(cached[1])
            selected_preset = self._get_preset(active_preset_id)
            if not reference_id and selected_preset and operation in {"img2img", "reference"}:
                reference_id = str(selected_preset.get("reference_id", "")) or None
            if operation == "reference" and selected_preset:
                reference_type = selected_preset.get("reference_type", "character")
            else:
                reference_type = "character"
            image_bytes, reference = await self._load_reference(reference_id)
            if operation in {"img2img", "reference"} and not image_bytes:
                result = GenerationResult(False, job_id, provider, error_code="missing_reference", message="请先在 WebUI 上传参考图并绑定到默认预设。", attempts=0)
                self._recent_jobs[key] = (time.time(), result)
                return result
            if reference:
                reference["reference_type"] = reference_type
            image_b64 = base64.b64encode(image_bytes).decode("ascii") if image_bytes and provider == "novelai_official" else None
            try:
                if provider == "novelai_official":
                    path = await self._call_official(composed_prompt, operation, job_id, image_b64 if operation == "img2img" else None, reference if operation == "reference" else None, preset_negative)
                else:
                    path = await self._call_openai(composed_prompt, operation, job_id, image_bytes if operation == "img2img" else None)
                result = GenerationResult(True, job_id, provider, path=path, message="图片已生成并发送到当前会话。", attempts=1)
            except ProviderError as exc:
                logger.warning(f"[{PLUGIN_NAME}] job={job_id} provider={provider} code={exc.code}: {exc.message}")
                result = GenerationResult(False, job_id, provider, error_code=exc.code, message=exc.message, attempts=1)
            self._recent_jobs[key] = (time.time(), result)
            self._jobs.append({**asdict(result), "operation": operation, "preset_id": active_preset_id, "created_at": int(time.time())})
            self._jobs = self._jobs[-50:]
            return result
        finally:
            if self.lock.locked():
                self.lock.release()

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
        if not result.path:
            return "图片生成未完成：未找到图片文件。"
        if not result.send_image:
            return result.message
        try:
            await event.send(MessageChain().file_image(result.path))
            delay = max(0, int(self._cfg("auto_clean_delay", 300) or 300))
            async def cleanup(path: str, seconds: int):
                if seconds > 0:
                    await asyncio.sleep(seconds)
                try:
                    Path(path).unlink(missing_ok=True)
                except Exception:
                    pass
            asyncio.create_task(cleanup(result.path, delay))
            return result.message
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 发送图片失败 job={result.job_id}: {exc}")
            try:
                Path(result.path).unlink(missing_ok=True)
            except Exception:
                pass
            await self._notify(event, "图片已生成，但发送到当前会话失败。", "error")
            return "图片已生成，但发送失败。"

    # --------------------------- AstrBot handlers ---------------------------
    @filter.llm_tool(name="novelai_generate_image")
    async def novelai_generate_image(self, event: AstrMessageEvent, prompt: str):
        """仅在用户明确要求生成、绘制或修改图片时调用。根据用户需求生成一张图片并发送到当前会话。不要因为可能适合配图而主动调用；不要把内部错误、API Key 或内部路径写入回复。"""
        if not self._mode_allows("llm_tool"):
            return "当前未启用自然语言生图入口。"
        result = await self._run_job(event, prompt, "generate")
        return await self._finish_event(event, result)

    @filter.regex(r"^/[A-Za-z][A-Za-z0-9_-]*(?:@[A-Za-z0-9_-]+)?(?:\s|$)")
    async def cmd_draw(self, event: AstrMessageEvent):
        """NovelAI 命令入口：/nai draw <描述>、/nai img2img <描述>、/nai reference <character|style|both> <描述>。"""
        if not self._mode_allows("command"):
            yield event.plain_result("当前未启用固定命令生图入口。")
            return
        raw_message = event.get_message_str().strip()
        match = re.match(r"^/(?P<name>[A-Za-z][A-Za-z0-9_-]*)(?:@[A-Za-z0-9_-]+)?(?:\s+(?P<body>.*))?$", raw_message, re.S)
        if not match or match.group("name").lower() != str(self._cfg("command_prefix", "nai") or "nai").strip().lstrip("/").lower():
            return
        text = (match.group("body") or "").strip()
        if not text or text.lower() in {"help", "?", "菜单"}:
            yield event.plain_result("用法：/nai draw <画面描述>；/nai img2img <画面描述>；/nai reference <character|style|both> <画面描述>；/nai preset list")
            return
        parts = text.split(maxsplit=2)
        operation = "generate"
        reference_type = None
        body = text
        if parts[0].lower() in {"draw", "generate", "生图"}:
            body = parts[1] if len(parts) == 2 else parts[2]
        elif parts[0].lower() in {"img2img", "i2i", "图生图"}:
            operation = "img2img"
            body = parts[1] if len(parts) == 2 else parts[2]
        elif parts[0].lower() in {"reference", "ref", "参考图"}:
            operation = "reference"
            reference_type = parts[1].lower() if len(parts) > 1 else "character"
            body = parts[2] if len(parts) > 2 else ""
        elif parts[0].lower() == "preset":
            if len(parts) > 1 and parts[1].lower() == "list":
                names = [str(p.get("name", p.get("id", ""))) for p in self.presets]
                yield event.plain_result("可用预设：" + ("、".join(names) if names else "暂无预设，请在 WebUI 创建。"))
            else:
                yield event.plain_result("预设管理请使用 AstrBot WebUI 页面。")
            return
        elif not self._as_bool(self._cfg("legacy_command_enabled", True)):
            yield event.plain_result("请使用 /nai draw、/nai img2img 或 /nai reference 命令。")
            return
        if not body.strip():
            yield event.plain_result("请补充画面描述。")
            return
        preset_id = None
        reference_id = None
        if operation == "reference":
            selected = self._resolve_preset(event)
            if selected:
                reference_id = str(selected.get("reference_id", "")) or None
                selected["reference_type"] = reference_type or "character"
        result = await self._run_job(event, body, operation, preset_id, reference_id)
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

    async def page_settings(self):
        personas = []
        try:
            items = await self.context.persona_manager.get_all_personas()
            personas = [{"id": str(p.persona_id), "name": str(p.persona_id)} for p in items]
        except Exception:
            pass
        return json_response({"config": self._public_config(), "presets": self.presets, "references": self._reference_list(), "personas": personas, "capabilities": self._capabilities()})

    async def page_save_config(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return self._page_error("配置格式错误")
        fields = {key: value for key, value in payload.items() if key not in {"api_token", "api_key"}}
        for key in ("api_token", "api_key"):
            if str(payload.get(key, "")).strip() and payload.get(key) != "********":
                fields[key] = str(payload[key]).strip()
        for key in ("width", "height", "steps", "images_per_request", "max_api_requests_per_job", "dedupe_window_seconds", "queue_timeout", "auto_clean_delay"):
            if key in fields:
                try:
                    fields[key] = int(fields[key])
                except (TypeError, ValueError):
                    return self._page_error(f"{key} 必须是整数")
        for key in ("scale", "retry_delay", "img2img_strength", "img2img_noise", "reference_strength", "reference_fidelity", "reference_information_extracted"):
            if key in fields:
                try:
                    fields[key] = float(fields[key])
                except (TypeError, ValueError):
                    return self._page_error(f"{key} 必须是数字")
        fields["images_per_request"] = 1
        fields["max_api_requests_per_job"] = 1
        fields["width"] = min(2048, max(64, int(fields.get("width", self._cfg("width", 832)))))
        fields["height"] = min(2048, max(64, int(fields.get("height", self._cfg("height", 1216)))))
        fields["steps"] = min(50, max(1, int(fields.get("steps", self._cfg("steps", 28)))))
        for key in ("allowed_users", "allowed_groups"):
            if key in fields:
                fields[key] = self._as_list(fields[key])
        if "command_prefix" in fields:
            prefix = str(fields["command_prefix"] or "nai").strip().lstrip("/")
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,30}", prefix):
                return self._page_error("固定命令只能使用字母开头的 1-31 位名称")
            fields["command_prefix"] = prefix
        if "openai_auth_header" in fields and fields["openai_auth_header"] not in {"Authorization", "x-api-key"}:
            return self._page_error("兼容后端鉴权请求头不受支持")
        if "persona_preset_map" in fields and not isinstance(fields["persona_preset_map"], dict):
            return self._page_error("persona_preset_map 必须是对象")
        allowed_enums = {
            "provider": {"novelai_official", "openai_compatible"},
            "invoke_mode": {"disabled", "command_only", "llm_tool_only", "both"},
            "private_access": {"all", "admin_only", "allowlist", "disabled"},
            "group_access": {"all", "admin_only", "allowlist", "disabled"},
            "retry_mode": {"none"},
            "error_notify_mode": {"silent", "final_only", "admin_only"},
        }
        for key, options in allowed_enums.items():
            if key in fields and fields[key] not in options:
                return self._page_error(f"{key} 的值不受支持")
        self.config.update(fields)
        self.config["config_version"] = 2
        saver = getattr(self.config, "save_config", None)
        if callable(saver):
            saver()
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
                        raise self._http_error(response.status, await response.read())
            return json_response({"ok": True, "message": "连接测试成功"})
        except ProviderError as exc:
            return json_response({"ok": False, "message": exc.message})
        except Exception:
            return json_response({"ok": False, "message": "连接测试失败，请检查地址、密钥和网络"})

    async def page_presets(self):
        return json_response({"presets": self.presets})

    async def page_create_preset(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict) or not str(payload.get("name", "")).strip():
            return self._page_error("预设名称不能为空")
        preset = {
            "id": uuid.uuid4().hex[:10],
            "name": str(payload.get("name")).strip()[:80],
            "description": str(payload.get("description", "")).strip()[:300],
            "style_prompt": str(payload.get("style_prompt", "")).strip()[:4000],
            "character_prompt": str(payload.get("character_prompt", "")).strip()[:4000],
            "negative_prompt": str(payload.get("negative_prompt", "")).strip()[:4000],
            "reference_id": str(payload.get("reference_id", "")).strip(),
            "reference_type": str(payload.get("reference_type", "character")),
            "lock_character": bool(payload.get("lock_character", True)),
            "persona_id": str(payload.get("persona_id", "")).strip(),
            "enabled": bool(payload.get("enabled", True)),
        }
        self.presets.append(preset)
        self._save_presets()
        return json_response({"saved": True, "message": "预设已创建", "preset": preset})


    async def page_manage_preset(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return self._page_error("预设格式错误")
        action = str(payload.get("action", "")).lower()
        preset_id = str(payload.get("id", ""))
        if action == "delete":
            before = len(self.presets)
            self.presets = [p for p in self.presets if p.get("id") != preset_id]
            if len(self.presets) == before:
                return self._page_error("预设不存在", status_code=404)
            self._save_presets()
            return json_response({"saved": True, "message": "预设已删除"})
        if action == "update":
            preset = self._get_preset(preset_id)
            if not preset:
                return self._page_error("预设不存在", status_code=404)
            for key in ("name", "description", "style_prompt", "character_prompt", "negative_prompt", "reference_id", "reference_type", "lock_character", "persona_id", "enabled"):
                if key in payload:
                    preset[key] = payload[key]
            self._save_presets()
            return json_response({"saved": True, "message": "预设已更新", "preset": preset})
        if not str(payload.get("name", "")).strip():
            return self._page_error("预设名称不能为空")
        preset = {
            "id": uuid.uuid4().hex[:10],
            "name": str(payload.get("name")).strip()[:80],
            "description": str(payload.get("description", "")).strip()[:300],
            "style_prompt": str(payload.get("style_prompt", "")).strip()[:4000],
            "character_prompt": str(payload.get("character_prompt", "")).strip()[:4000],
            "negative_prompt": str(payload.get("negative_prompt", "")).strip()[:4000],
            "reference_id": str(payload.get("reference_id", "")).strip(),
            "reference_type": str(payload.get("reference_type", "character")),
            "lock_character": bool(payload.get("lock_character", True)),
            "persona_id": str(payload.get("persona_id", "")).strip(),
            "enabled": bool(payload.get("enabled", True)),
        }
        self.presets.append(preset)
        self._save_presets()
        return json_response({"saved": True, "message": "预设已创建", "preset": preset})

    async def page_update_preset(self, preset_id: str):
        preset = self._get_preset(preset_id)
        if not preset:
            return self._page_error("预设不存在", status_code=404)
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return self._page_error("预设格式错误")
        for key in ("name", "description", "style_prompt", "character_prompt", "negative_prompt", "reference_id", "reference_type", "lock_character", "persona_id", "enabled"):
            if key in payload:
                preset[key] = payload[key]
        self._save_presets()
        return json_response({"saved": True, "message": "预设已更新", "preset": preset})

    async def page_delete_preset(self, preset_id: str):
        before = len(self.presets)
        self.presets = [p for p in self.presets if p.get("id") != preset_id]
        if len(self.presets) == before:
            return self._page_error("预设不存在", status_code=404)
        self._save_presets()
        return json_response({"saved": True, "message": "预设已删除"})

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
        try:
            (self.reference_dir / str(meta["filename"])).unlink(missing_ok=True)
            (self.reference_dir / f"{Path(reference_id).name}.json").unlink(missing_ok=True)
            changed = False
            for preset in self.presets:
                if preset.get("reference_id") == reference_id:
                    preset["reference_id"] = ""
                    changed = True
            if changed:
                self._save_presets()
        except Exception:
            pass
        return json_response({"saved": True, "message": "参考图已删除"})

    async def page_jobs(self):
        return json_response({"jobs": self._jobs[-50:]})

    def _capabilities(self) -> dict[str, Any]:
        if self._provider_name() == "novelai_official":
            return {"text_to_image": True, "img2img": True, "precise_reference": True, "vibe_transfer": False}
        return {"text_to_image": True, "img2img": True, "precise_reference": False, "vibe_transfer": False}


__all__ = ["NovelAIPainterPlugin"]
