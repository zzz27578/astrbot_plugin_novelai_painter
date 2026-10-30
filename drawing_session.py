"""Native, read-only conversation inputs for a request-local drawing model."""
from __future__ import annotations

import asyncio
import copy
import json
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

if __package__:
    from . import planner
else:
    import planner

MARKER = re.compile(r"\[NovelAI drawing identities\].*?\[End NovelAI drawing identities\]", re.S)


def clean_text(text):
    # Keep all other bytes, including leading whitespace in the system prefix.
    return MARKER.sub("", text) if isinstance(text, str) else text


def native_messages(records, *, images=False):
    """Copy wire message fields, retaining complete native tool-call pairs.

    Reasoning/signatures/checkpoints and provider-private metadata are not
    portable to another model. Never turn tool results into new user orders.
    """
    if isinstance(records, str):
        records = json.loads(records)
    messages = []
    for raw in records or []:
        raw = raw.model_dump(exclude_none=True) if hasattr(raw, "model_dump") else raw
        if not isinstance(raw, dict) or raw.get("role") not in {"system", "user", "assistant", "tool"}:
            continue
        row = {k: copy.deepcopy(raw[k]) for k in ("role", "content", "tool_calls", "tool_call_id", "name") if k in raw}
        content = row.get("content")
        if isinstance(content, list):
            parts = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in {"text", "input_text"}:
                    text = clean_text(part.get("text", ""))
                    if text:
                        parts.append({"type": "text", "text": text})
                elif images and part.get("type") == "image_url" and part.get("image_url"):
                    parts.append({"type": "image_url", "image_url": copy.deepcopy(part["image_url"])})
            row["content"] = parts
        elif isinstance(content, str):
            row["content"] = clean_text(content)
        if row.get("content") or row.get("tool_calls") or row["role"] == "tool":
            messages.append(row)
    # Preserve a tool block only if all calls have exactly one adjacent result.
    result, i = [], 0
    while i < len(messages):
        row = messages[i]
        if row.get("tool_calls"):
            calls = row["tool_calls"]
            calls = calls if isinstance(calls, list) else []
            ids = [c.get("id") for c in calls if isinstance(c, dict)]
            end = i + 1
            while end < len(messages) and messages[end]["role"] == "tool":
                end += 1
            tools = messages[i + 1:end]
            received = [m.get("tool_call_id") for m in tools]
            valid = (row["role"] == "assistant" and ids and len(ids) == len(calls)
                and all(isinstance(x, str) and x for x in ids + received)
                and all(isinstance(c.get("function"), dict)
                    and isinstance(c["function"].get("name"), str)
                    and isinstance(c["function"].get("arguments"), str) for c in calls))
            if valid and len(ids) == len(set(ids)) and sorted(ids) == sorted(received):
                row["tool_calls"] = [{"id": c["id"], "type": "function",
                    "function": {k: c["function"][k] for k in ("name", "arguments")}} for c in calls]
                result.extend([row, *tools])
            elif row.get("content"):
                result.append({k: v for k, v in row.items() if k != "tool_calls"})
            i = end
        else:
            if row["role"] != "tool":
                result.append(row)
            i += 1
    if images:
        # Keep at most the two most recent native pictures. Text and call pairs
        # stay intact; the separately requested last drawing is handled later.
        left = 2
        for row in reversed(result):
            if isinstance(row.get("content"), list):
                parts = []
                for part in reversed(row["content"]):
                    if part.get("type") == "image_url":
                        if not left:
                            continue
                        left -= 1
                    parts.append(part)
                row["content"] = list(reversed(parts))
    return result


@dataclass
class Snapshot:
    system: str
    messages: list
    source: str
    max_turns: int = 50


def capture(records, system="", *, images=False, source="main_agent_snapshot", max_turns=50):
    messages = native_messages(records, images=images)
    if not system and messages and messages[0]["role"] == "system":
        first = messages.pop(0)["content"]
        system = first if isinstance(first, str) else "\n".join(p.get("text", "") for p in first)
    return Snapshot(clean_text(system or ""), messages, source, max_turns)


class BranchEvent:
    """Local metadata/result state, while delivery uses the original adapter."""
    def __init__(self, event):
        self.original = event
        self.unified_msg_origin = event.unified_msg_origin
        self.message = event.get_message_str()
        self.message_obj = copy.copy(event.message_obj)
        self.result = None
        self.extras = {}
        try:
            extra = event.get_extra()
        except TypeError:  # Minimal SDK/test adapters.
            extra = getattr(event, "_extras", getattr(event, "extras", {}))
        for key, value in (extra or {}).items():
            if key.startswith(("nai_", "sticker_")) and key not in {"nai_main_request", "nai_agent_run_context", "nai_main_raw_tools", "nai_main_observation"}:
                self.extras[key] = copy.deepcopy(value)
        self.components = copy.deepcopy(event.get_messages()) if hasattr(event, "get_messages") else []

    def __getattr__(self, name):
        return getattr(self.original, name)

    def get_extra(self, key=None, default=None):
        return self.extras if key is None else self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_message_str(self):
        return self.message

    def get_messages(self):
        return self.components

    def get_result(self):
        return self.result

    def set_result(self, result):
        self.result = result

    def clear_result(self):
        self.result = None


async def session_snapshot(context, event, *, images=False):
    """Use core persona/skill resolution without running its chat pipeline.

    No conversation creation, title generation, retrieval, model invocation or
    history writes. Other plugins' per-turn hooks are only available in an
    actual main Agent snapshot, not reconstructed for a standalone /n.
    """
    from astrbot.api.provider import ProviderRequest
    from astrbot.core.astr_main_agent import (
        _ensure_persona_and_skills, _apply_workspace_extra_prompt,
        _append_system_reminders, _apply_llm_safety_mode, MainAgentBuildConfig,
        TOOL_CALL_PROMPT,
    )
    manager = context.conversation_manager
    cid = await manager.get_curr_conversation_id(event.unified_msg_origin)
    conversation = await manager.get_conversation(event.unified_msg_origin, cid) if cid else None
    history = json.loads(conversation.history) if conversation and isinstance(conversation.history, str) else getattr(conversation, "history", [])
    cfg = context.get_config(event.unified_msg_origin)
    settings = cfg.get("provider_settings", {})
    request = ProviderRequest(prompt="", contexts=copy.deepcopy(history or []), system_prompt="",
        conversation=copy.copy(conversation) if conversation else SimpleNamespace(persona_id=None, cid=""))
    local_event = BranchEvent(event)
    await _ensure_persona_and_skills(request, settings, context, local_event)
    await _apply_workspace_extra_prompt(local_event, request, context)
    if settings.get("llm_safety_mode", True):
        _apply_llm_safety_mode(MainAgentBuildConfig(tool_call_timeout=120,
            safety_mode_strategy=settings.get("safety_mode_strategy", "system_prompt")), request)
    request.system_prompt += "\n" + TOOL_CALL_PROMPT
    _append_system_reminders(local_event, request, settings, cfg.get("timezone"))
    # Reminder content is temporary task metadata, not another chat turn.
    reminders = "\n".join(p.text for p in request.extra_user_content_parts if hasattr(p, "text"))
    event.set_extra("nai_session_reminders", reminders)
    return capture(request.contexts, request.system_prompt, images=images,
        source="session_persona_skills", max_turns=int(settings.get("max_context_length", 50)))


def request_parts(snapshot, payload, instruction, limit):
    system = snapshot.system + "\n\n" + instruction
    materials = {k: v for k, v in payload.items() if k not in {"recent_dialogue", "current_reply", "drawing_request"}}
    # The user request and current reply already exist in native messages for n.
    prompt = ("[Drawing turn: submit parameters only]\n" + payload["drawing_request"] + "\n"
        + planner.MATERIALS_HEADER + json.dumps(materials, ensure_ascii=False, separators=(",", ":"))
        + "\n" + planner.MATERIALS_END + "\n" + planner.OUTPUT_REMINDER)
    groups = []
    for row in copy.deepcopy(snapshot.messages):
        if not groups or row["role"] == "user":
            groups.append([])
        groups[-1].append(row)
    removed = 0
    size = text_chars
    fixed = len(system) + len(prompt)
    while len(groups) > 1 and ((snapshot.max_turns > 0 and len(groups) > snapshot.max_turns)
            or fixed + size(groups) > limit):
        groups.pop(0)
        removed += 1
    messages = [row for group in groups for row in group]
    if fixed + size(messages) > limit:
        raise ValueError("当前人设和本轮上下文超过共享上下文字符上限，请提高上限或精简本轮内容。")
    metrics = {"context_source": snapshot.source, "context_mode": "shared",
        "native_message_count": len(messages), "native_tool_pairs": sum(len(m.get("tool_calls", [])) for m in messages),
        "trimmed_turns": removed, "context_chars": size(messages), "shared_context_char_limit": limit}
    return {"prompt": prompt, "contexts": messages}, system, metrics


def text_chars(value):
    """Budget text only; base64 pixels must not consume the text allowance."""
    if isinstance(value, dict):
        if value.get("type") == "image_url":
            return len('[image]')
        return sum(len(str(k)) + text_chars(v) + 4 for k, v in value.items()) + 2
    if isinstance(value, list):
        return sum(text_chars(v) + 1 for v in value) + 2
    return len(json.dumps(value, ensure_ascii=False))


def image_urls(snapshot):
    return [p["image_url"].get("url") if isinstance(p["image_url"], dict) else p["image_url"]
        for m in snapshot.messages if isinstance(m.get("content"), list)
        for p in m["content"] if p.get("type") == "image_url"]


async def prepare_native_images(snapshot):
    """Core histories normally contain data URLs; resolve local references too."""
    from astrbot.core.utils.media_utils import MediaResolver
    for row in snapshot.messages:
        for part in row.get("content", []) if isinstance(row.get("content"), list) else []:
            if part.get("type") != "image_url":
                continue
            image = part["image_url"]
            url = image.get("url") if isinstance(image, dict) else image
            if isinstance(url, str) and not url.startswith(("http://", "https://", "data:")):
                resolved = await asyncio.wait_for(MediaResolver(url, media_type="image").to_data_url(strict=True), 15)
                part["image_url"] = {**image, "url": resolved} if isinstance(image, dict) else {"url": resolved}


def cleanup_inputs(paths):
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass  # Also retried by the task done callback.


def pin_inputs(event, directory):
    """Own local input files before the original pipeline cleans its files."""
    owned, mapped = [], {}
    def pin(value):
        if not isinstance(value, str) or value.startswith(("https://", "http://", "data:", "base64://")):
            return value
        local = unquote(urlsplit(value).path).lstrip("/") if value.startswith("file:///") and len(urlsplit(value).path) > 3 and urlsplit(value).path[2] == ":" else unquote(urlsplit(value).path) if value.startswith("file:") else value
        path = Path(local)
        if not path.is_file():
            return value
        if path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("绘图上下文的本地图片超过 32 MB。")
        key = str(path.resolve())
        if key not in mapped:
            target = directory / ("drawing-input-" + uuid.uuid4().hex + path.suffix)
            shutil.copyfile(path, target)
            mapped[key] = str(target)
            owned.append(target)
        return mapped[key]
    try:
        event.set_extra("nai_planner_images", [pin(x) for x in event.get_extra("nai_planner_images", [])])
        previous = event.get_extra("nai_previous_drawing")
        if previous and previous.get("image"):
            previous["image"] = pin(previous["image"])
        from astrbot.core.message.components import Image
        for component in event.get_messages():
            if isinstance(component, Image):
                for key in ("file", "url"):
                    value = getattr(component, key, None)
                    if value:
                        pinned = pin(value)
                        if pinned != value:
                            setattr(component, key, Path(pinned).as_uri() if key == "file" else pinned)
        snapshot = event.get_extra("nai_session_snapshot")
        if snapshot:
            for row in snapshot.messages:
                if isinstance(row.get("content"), list):
                    for part in row["content"]:
                        if part.get("type") == "image_url":
                            image = part["image_url"]
                            if isinstance(image, dict):
                                image["url"] = pin(image.get("url"))
                            else:
                                part["image_url"] = pin(image)
        return owned
    except BaseException:
        cleanup_inputs(owned)
        raise
