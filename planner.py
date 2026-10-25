"""Stateless contracts for the optional, independent drawing LLM."""
from __future__ import annotations

import json
import re

DEFAULTS = {
    "drawing_llm_provider_id": "",
    "drawing_llm_context_messages": 12,
    "drawing_llm_timeout": 60,
    "drawing_llm_use_images": False,
    "drawing_llm_prompt": "",
}


def recent_dialogue(records, count: int) -> list[dict[str, str]]:
    """Only conversational text; exclude tools, system prompts and catalogs."""
    result = []
    for role, text in records or ():
        if role not in {"user", "assistant"} or not text:
            continue
        text = re.sub(r"\[NovelAI drawing identities\].*?\[End NovelAI drawing identities\]", "", text, flags=re.S).strip()
        if text:
            result.append({"role": role, "text": text[-2000:]})
    return result[-count:] if count else []


def parse_plan(text: str) -> dict:
    if not isinstance(text, str) or len(text) > 24000:
        raise ValueError("绘图 LLM 返回过长或无效内容。")
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    try:
        result = json.loads(cleaned)
    except ValueError as exc:
        raise ValueError("绘图 LLM 未返回有效 JSON；本次没有请求图片服务。") from exc
    if not isinstance(result, dict) or set(result) - {"prompt", "subjects", "style"}:
        raise ValueError("绘图 LLM 应返回 prompt、subjects 和可选 style。")
    if not isinstance(result.get("prompt"), str) or not result["prompt"].strip() or len(result["prompt"]) > 8000:
        raise ValueError("绘图 LLM 缺少有效画面描述。")
    if not isinstance(result.get("subjects"), list) or len(result["subjects"]) > 6 or any(not isinstance(s, dict) for s in result["subjects"]):
        raise ValueError("绘图 LLM 的 subjects 应为最多 6 人的原生数组。")
    if "style" in result and (not isinstance(result["style"], str) or len(result["style"]) > 4000):
        raise ValueError("绘图 LLM 的 style 应为字符串。")
    # Planner interpretations and previous images only affect this image.
    for subject in result["subjects"]:
        subject["state_scope"] = "image"
    return result
