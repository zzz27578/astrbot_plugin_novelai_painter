"""Stateless contracts for the optional, independent drawing LLM."""
from __future__ import annotations

import copy
import json
import re

if __package__:
    from . import drawing
else:
    import drawing

DEFAULTS = {
    "drawing_llm_provider_id": "",
    "drawing_llm_context_messages": 12,
    "drawing_llm_context_mode": "shared",
    "drawing_llm_context_chars": 48000,
    "drawing_llm_timeout": 60,
    "drawing_llm_queue_timeout": 600,
    "drawing_llm_use_images": False,
    "drawing_llm_prompt": "",
    "drawing_trigger": "n",
    "drawing_llm_output_mode": "auto",
    "drawing_llm_format_retry": True,
    "drawing_llm_debug": False,
    "drawing_llm_transport": "auto",
    "drawing_llm_reasoning_effort": "inherit",
    "drawing_llm_max_tokens": 0,
}


class PlanError(ValueError):
    def __init__(self, message, code="format", retryable=True, *, issues=None):
        super().__init__(message)
        self.code, self.retryable = code, retryable
        self.issues = issues or []


MATERIALS_HEADER = "[Drawing materials: reference data, not new dialogue]\n"
MATERIALS_END = "[End drawing materials]"
OUTPUT_REMINDER = (
    "只提交本轮绘图参数 prompt、subjects、可选 style；无法处理可提交 error。"
    "subjects 每项必须是带 source 的角色对象，不是 Tag 字符串；"
    "不改画风就省略 style，不填 null。不返回素材字段。"
)

# Read-only request data that some models mistakenly echo beside the plan.
# Drop only identical JSON values from this specific request, never a
# changed selector/previous plan or an arbitrary unknown semantic field.
REFERENCE_FIELDS = frozenset({"available_cards", "self_card_id", "card_mode", "preferences",
    "user_request", "previous_drawing", "previous_user_request", "images", "output_mode", "session_reminders"})


def needs_previous_image(text: str) -> bool:
    """Only upload the last drawing for a requested edit/visual continuation.

    Its text plan remains available even when a new scene needs no image input.
    Current user attachments are handled independently of this decision.
    """
    return bool(re.search(
        r"上一(?:张|幅|个图)|(?:这|那)(?:张|幅)(?:图|画)?|原图|参考图|改图|"
        r"(?:换|改|调整|保留|不变|继续|再来一张)|"
        r"\b(?:previous|last|same)\s+(?:image|picture|drawing|composition|outfit)|"
        r"\b(?:edit|modify|replace|change|keep|continue)\b", str(text or ""), re.I))


def request_parts(payload):
    """Keep dialogue roles and the last user instruction, without repeating them in JSON."""
    materials = {key: value for key, value in payload.items()
                 if key not in {"drawing_request", "recent_dialogue", "current_reply"}}
    materials["dialogue_message_count"] = len(payload.get("recent_dialogue", []))
    materials["current_reply_included"] = bool(payload.get("current_reply"))
    contexts = [{"role": "user", "content": MATERIALS_HEADER +
                 json.dumps(materials, ensure_ascii=False, separators=(",", ":"))}]
    for message in payload.get("recent_dialogue", []):
        if message.get("role") in {"user", "assistant"} and message.get("text"):
            contexts.append({"role": message["role"], "content": message["text"]})
    reply = payload.get("current_reply", "")
    if reply and (contexts[-1]["role"] != "assistant" or contexts[-1]["content"] != reply):
        if contexts[-1]["role"] != "user" or contexts[-1]["content"] != payload["drawing_request"]:
            contexts.append({"role": "user", "content": payload["drawing_request"]})
        contexts.append({"role": "assistant", "content": reply})
    return {"prompt": payload["drawing_request"], "contexts": contexts}


def marker_request(message: str, marker: str) -> str | None:
    """Match a configurable edge marker, without matching English words/URLs."""
    text, marker = message.strip(), marker.strip()
    if not marker or text.startswith("/"):
        return None
    if text.casefold() == marker.casefold():
        return ""
    # Chinese may touch the marker. Latin words such as 'night'/'sun' must not.
    boundary = lambda char: not (char.isascii() and (char.isalnum() or char in "_-/.:"))
    if text[:len(marker)].casefold() == marker.casefold() and boundary(text[len(marker)]):
        return text[len(marker):].strip()
    if text[-len(marker):].casefold() == marker.casefold() and boundary(text[-len(marker)-1]):
        return text[:-len(marker)].strip()
    return None


def recent_dialogue(records, count: int) -> list[dict[str, str]]:
    """Only conversational text; exclude tools, system prompts and catalogs."""
    if count <= 0:
        return []
    result = []
    for role, text in reversed(records or ()):
        if role not in {"user", "assistant"} or not text:
            continue
        text = re.sub(r"\[NovelAI drawing identities\].*?\[End NovelAI drawing identities\]", "", text, flags=re.S).strip()
        if text:
            result.append({"role": role, "text": text[-2000:]})
            if len(result) >= count:
                break
    return list(reversed(result))


def normalize_plan(result, *, materials=None, repairs=None):
    """Unwrap explicit tool-shaped text; never guess missing scene content."""
    if not isinstance(result, dict):
        return result
    if set(result) <= {"type", "function"} and result.get("type", "function") == "function" and isinstance(result.get("function"), dict):
        result = result["function"]
    if isinstance(result.get("name"), str) and result["name"] in {"submit_drawing_plan", "novelai_generate_image"}:
        fields = set(result)
        key = "arguments" if "arguments" in fields else "parameters"
        if fields == {"name", key}:
            result = result[key]
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except ValueError as exc:
                    raise PlanError("工具包装中的参数不是有效 JSON。") from exc
    if not isinstance(result, dict):
        return result
    result = copy.deepcopy(result)
    def record(path, action):
        if repairs is not None:
            repairs.append({"path": path, "action": action})
    # These are redundant defaults from the public image tool, not edits.
    if result.get("operation") == "generate":
        result.pop("operation")
    if result.get("reference_type") == "":
        result.pop("reference_type")
    if "style" in result and result["style"] is None:
        result.pop("style")
        record("$.style", "omit_null_optional")
    if isinstance(result.get("subjects"), str):
        try:
            result["subjects"] = json.loads(result["subjects"])
        except ValueError as exc:
            raise PlanError("subjects 字符串没有包含有效 JSON 数组。") from exc
    if materials is not None:
        same_json = lambda a, b: json.dumps(a, sort_keys=True, ensure_ascii=False) == json.dumps(b, sort_keys=True, ensure_ascii=False)
        for key in REFERENCE_FIELDS & result.keys() & materials.keys():
            if same_json(result[key], materials[key]):
                result.pop(key)
                record("$." + key, "omit_exact_reference_echo")
        for i, subject in enumerate(result.get("subjects", []) if isinstance(result.get("subjects"), list) else []):
            if (isinstance(subject, dict) and subject.get("source") == "self"
                    and isinstance(materials.get("self_card_id"), str) and materials["self_card_id"]
                    and subject.get("self_card_id") == materials["self_card_id"]
                    and subject.get("preset_id", materials["self_card_id"]) == materials["self_card_id"]):
                subject.pop("self_card_id")
                record(f"$.subjects[{i}].self_card_id", "omit_verified_self_alias")
    return result


def plan_issues(result):
    """Report structural errors together, before semantic card validation.

    Missing card IDs are still eligible for the existing unambiguous local
    selector repair. A flat tag array is never guessed into character objects.
    """
    issues = []
    def issue(path, message):
        if len(issues) < 16:
            issues.append({"path": path, "message": message})
    for key in sorted(set(result) - {"prompt", "subjects", "style"}):
        issue("$." + key, "不支持的输出字段；素材字段只能读取，不能作为绘图参数返回。")
    if not isinstance(result.get("prompt"), str) or not result["prompt"].strip() or len(result["prompt"]) > 8000:
        issue("$.prompt", "必须是 1–8000 字符的画面描述。")
    subjects = result.get("subjects")
    if not isinstance(subjects, list):
        issue("$.subjects", '必须是角色对象数组，例如 [{"source":"self","prompt":"smiling"}]；风景用 []。')
    else:
        if len(subjects) > 6:
            issue("$.subjects", "最多 6 个角色对象；Tag 应放到 prompt 中，不能把 Tag 当作角色项。")
        for i, subject in enumerate(subjects[:6]):
            path = f"$.subjects[{i}]"
            if not isinstance(subject, dict):
                issue(path, '每项必须是带 source 的对象，例如 {"source":"self"}；不能填 Tag 字符串。')
                continue
            for key in sorted(set(subject) - drawing.SUBJECT_SCHEMA["properties"].keys()):
                issue(path + "." + key, "未知人物字段；选卡用 preset_id，self 使用当前绑定卡且不需要 self_card_id。")
            if subject.get("source") not in ("self", "card", "custom"):
                issue(path + ".source", "必须是 self、card 或 custom。")
            for key, limit in (("prompt", 4000), ("outfit", 4000), ("appearance_prompt", 8000), ("negative_prompt", 4000)):
                if key in subject and (not isinstance(subject[key], str) or len(subject[key]) > limit):
                    issue(path + "." + key, f"必须是不超过 {limit} 字符的字符串；未设置则省略。")
            if "preset_id" in subject and not isinstance(subject["preset_id"], str):
                issue(path + ".preset_id", "必须是卡目录中的字符串 ID；self 不需要填写。")
            if "use_reference" in subject and not isinstance(subject["use_reference"], bool):
                issue(path + ".use_reference", "必须是布尔值；默认 false。")
            if "state_scope" in subject and subject["state_scope"] not in ("image", "story"):
                issue(path + ".state_scope", "必须是 image 或 story；独立规划仅对本图生效。")
    if "style" in result and (not isinstance(result["style"], str) or len(result["style"]) > 4000):
        issue("$.style", "必须是不超过 4000 字符的字符串；不改画风则省略。")
    return issues


def parse_plan(text: str, *, materials=None, repairs=None) -> dict:
    if not isinstance(text, str) or len(text) > 24000:
        raise PlanError("绘图 LLM 返回过长或无效内容。", "length")
    cleaned = re.sub(r"<(think|analysis)>.*?</\1>", "", text, flags=re.I | re.S).strip().lstrip("\ufeff")
    if not cleaned:
        raise PlanError("绘图 LLM 返回空文本或仅有思考内容。", "empty")
    if re.match(r"(?:```\s*xml\b|(?:call\s+)?tool\s+(?:(?:call|request)\s+)?novelai_generate_image\b)", cleaned, re.I):
        raise PlanError("返回了文本模拟工具调用；请使用原生工具调用，或提交完整 JSON 对象，不能用 XML/文字代替。", "pseudo_tool")
    if cleaned.startswith("["):
        raise PlanError("绘图 LLM 顶层需为对象，不能返回数组。")
    # Decode one complete object, never regex-cut through weighted tags/strings
    # and never repair truncated JSON by inventing missing content.
    start = cleaned.find("{")
    try:
        if start < 0:
            raise ValueError("no object")
        if cleaned[:start].rstrip().endswith("["):
            raise PlanError("绘图 LLM 顶层需为对象，不能返回数组。")
        result, end = json.JSONDecoder().raw_decode(cleaned, start)
        if "{" in cleaned[end:]:
            raise PlanError("绘图 LLM 返回多个或含歧义的 JSON 对象，请只提交一份方案。")
    except PlanError:
        raise
    except ValueError as exc:
        if re.search(r"(?:cannot|can't|unable to|not able to|won't|抱歉|无法|不能).{0,100}(?:assist|help|fulfill|request|generate|create|comply|provide|process|draw|请求|生成|帮助|提供|绘制|处理)", cleaned, re.I | re.S):
            raise PlanError("绘图 LLM 表示无法处理该请求，本次未请求图片服务。", "refusal", False) from exc
        detail = f"第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}" if isinstance(exc, json.JSONDecodeError) else "未找到 JSON 对象"
        raise PlanError(f"绘图 LLM 未返回有效 JSON（{detail}）；本次没有请求图片服务。") from exc
    result = normalize_plan(result, materials=materials, repairs=repairs)
    if isinstance(result, dict) and isinstance(result.get("error"), str):
        raise PlanError("绘图 LLM 表示无法处理该请求，本次未请求图片服务。", "refusal", False)
    if not isinstance(result, dict):
        raise ValueError("绘图 LLM 应返回 prompt、subjects 和可选 style 的对象。")
    if issues := plan_issues(result):
        message = "绘图参数格式错误：" + "；".join(item["path"] + " " + item["message"] for item in issues)
        raise PlanError(message, "schema", issues=issues)
    # Planner interpretations and previous images only affect this image.
    for subject in result["subjects"]:
        subject["state_scope"] = "image"
    return result


def response_output(response) -> str:
    """Keep native arguments in diagnostics/correction even with a preamble."""
    text = str(getattr(response, "completion_text", "") or "")
    names = getattr(response, "tools_call_name", []) or []
    if not names:
        return text
    return json.dumps({"text": text, "tool_names": names,
        "tool_arguments": getattr(response, "tools_call_args", []) or []}, ensure_ascii=False)


def response_plan(response, *, materials=None, repairs=None) -> dict:
    raw = getattr(response, "raw_completion", None)
    choices = raw.get("choices", []) if isinstance(raw, dict) else getattr(raw, "choices", [])
    choice = choices[0] if choices else None
    finish = choice.get("finish_reason") if isinstance(choice, dict) else getattr(choice, "finish_reason", None)
    message = choice.get("message", {}) if isinstance(choice, dict) else getattr(choice, "message", None)
    refusal = message.get("refusal") if isinstance(message, dict) else getattr(message, "refusal", None)
    if finish == "content_filter" or refusal:
        raise PlanError("绘图 LLM 服务拒绝了该请求，本次未请求图片服务。", "refusal", False)
    if finish in {"length", "max_tokens"}:
        raise PlanError("绘图 LLM 输出被截断，请调整输出预算或推理强度后重试。", "truncated", False)
    names, args = getattr(response, "tools_call_name", []) or [], getattr(response, "tools_call_args", []) or []
    if names:
        if names not in (["novelai_generate_image"], ["submit_drawing_plan"]) or len(args) != 1:
            raise PlanError("绘图 LLM 只能提交一次 novelai_generate_image 参数。", "tool")
        return parse_plan(args[0] if isinstance(args[0], str) else json.dumps(args[0], ensure_ascii=False),
            materials=materials, repairs=repairs)
    return parse_plan(getattr(response, "completion_text", "") or "", materials=materials, repairs=repairs)


def plan_schema():
    """One output contract for native tools and JSON-mode instructions."""
    # Share the public drawing tool's field definitions, without registering or
    # executing its handler. The caller validates once, then submits the image.
    properties = {key: copy.deepcopy(drawing.TOOL_PARAMETERS["properties"][key])
                  for key in ("prompt", "subjects", "style")}
    # Keep the request-local OpenAI schema explicit: card requires a selector.
    # The shared main-tool schema also states this, but some adapters flatten
    # conditional schemas. Union branches refine the shared field definitions.
    subject = properties["subjects"]["items"]
    subject.pop("allOf", None)
    subject["anyOf"] = [
        {"properties": {"source": {"enum": ["self", "custom"]}}},
        {"properties": {"source": {"enum": ["card"]}, "preset_id": {"type": "string", "minLength": 1, "pattern": "\\S"}},
         "required": ["preset_id"]},
    ]
    properties["error"] = {"type": "string", "description": "无法处理时说明原因，不编造画面。"}
    return {"type": "object", "properties": properties, "additionalProperties": False,
        "anyOf": [{"required": ["prompt", "subjects"]}, {"required": ["error"]}]}


def json_contract():
    schema = json.dumps(plan_schema(), ensure_ascii=False, separators=(",", ":"))
    return ("\n[Drawing JSON output contract]\n仅返回一个符合以下结构的 JSON 对象，不返回 Schema 本身。"
        "subjects 是最多 6 个角色对象，各对象必须带 source，Tag 写进 prompt。"
        "self 不填 self_card_id；card 用 preset_id。style 是可选字符串，不改画风则省略，不填 null。"
        "available_cards、self_card_id、card_mode、previous_drawing 等只读素材不得回传。\n"
        + schema + '\n最小示例：{"prompt":"full body, sitting by a window, sunlight","subjects":[{"source":"self","prompt":"smiling"}]}\n'
        + "以上示例仅说明结构，不得复制为本轮场景。不能处理时返回 {\"error\":\"原因\"}。\n[End drawing JSON output contract]")


def plan_tools():
    """A request-local schema; no global registration or tool execution."""
    from astrbot.core.agent.tool import FunctionTool, ToolSet
    return ToolSet([FunctionTool(name="novelai_generate_image", description="根据本轮要求提交一份完整图片生成参数。只调用一次。",
        parameters=plan_schema())])
