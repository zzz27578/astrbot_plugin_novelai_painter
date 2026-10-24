"""Drawing contracts, lossless card layers and history-scoped story state.

This module deliberately has no provider or AstrBot dependency. Render results
are never written back as character identity or story state.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any


OUTFIT_TAG = re.compile(
    r"\b(dress|skirt|bodysuit|boots?|shoes?|socks?|stockings?|tights|pantyhose|"
    r"shirt|blouse|pants|trousers|shorts|jeans|uniform|kimono|yukata|swimsuit|"
    r"bikini|underwear|lingerie|bra|panties|coat|jacket|hoodie|sweater|robe|"
    r"armor|armour|cape|cloak|apron|gloves?|hat|cap|scarf|belt|necktie|"
    r"necklace|earrings?|bracelet|hair tie|hair ribbon|nude|naked|topless|bottomless)\b", re.I,
)
STYLE_TAG = re.compile(
    r"\b(watercolor|oil painting|anime screencap|visual novel|light novel cover|"
    r"atelier|style|lineart|line art|shading|shadows|highlights|lighting|"
    r"masterpiece|best quality|high quality|vibrant colors|absurdres|aesthetic|"
    r"photorealistic|realistic|3d|sketch|chibi)\b|^@", re.I,
)
LAYERS = ("identity_prompt", "default_outfit_prompt", "style_prompt")


def tags(prompt: str, *, validate: bool = False) -> list[str]:
    """Keep weighted groups and names with parentheses intact."""
    result, stack = [], []
    start = 0
    pairs = {"}": "{", "]": "[", ")": "("}
    for match in re.finditer(r"-?(?:\d+(?:\.\d+)?|\.\d+)::|::|[{}\[\](),\r\n]", prompt):
        token = match.group()
        if token in ("{", "[", "("):
            stack.append(token)
        elif token in pairs:
            if stack and stack[-1] == pairs[token]:
                stack.pop()
            elif validate:
                raise ValueError("标签括号或权重组不完整，请保留完整原词。")
        elif token == "::":
            if stack and stack[-1] == "::":
                stack.pop()
            elif validate:
                raise ValueError("数值权重组不完整。")
        elif token.endswith("::"):
            stack.append("::")
        elif not stack:
            if part := prompt[start:match.start()].strip():
                result.append(part)
            start = match.end()
    if stack and validate:
        raise ValueError("标签括号或权重组不完整，请保留完整原词。")
    if part := prompt[start:].strip():
        result.append(part)
    return result


def join(*parts: str) -> str:
    return ", ".join(str(part).strip() for part in parts if str(part).strip())


def character_slot(prompt: str) -> str:
    """NAI character slots use girl/boy; scene counts belong in the base caption."""
    result = []
    for tag in tags(prompt):
        match = re.fullmatch(r"([\[{]*)(1girl|1boy|1other|solo)([\]}]*)", tag)
        if match:
            if match[2] == "solo":
                continue
            tag = match[1] + match[2][1:] + match[3]
        result.append(tag)
    return join(*result)


def inline_outfit_tags(prompt: str) -> list[str]:
    """A soft fallback for ordinary captions, never a validation requirement.

    Keep the original caption in place. Detecting clothing only prevents us
    from adding a second, fallback wardrobe; it cannot establish story state
    or assign an unscoped garment to a particular character.
    """
    result = []
    for tag in tags(prompt):
        words = tag.replace("_", " ")
        if not OUTFIT_TAG.search(words):
            continue
        worn = re.search(r"\b(wearing|dressed|clad)\b", words, re.I)
        if not worn and re.search(r"\b(holding|carrying|shop|store|wardrobe|lift|lifting|pulling|adjusting|tugging|unbuttoning|unzipping)\b", words, re.I):
            continue
        result.append(tag)
    return result


def card_layers(card: dict) -> dict:
    """Migrate once, preserve original input and flag all legacy splits for review.

    The classifier is a migration aid, never a wardrobe conflict resolver.
    Unrecognized tags remain intact in identity; users can move them in WebUI.
    """
    if card.get("layers_version") == 1 and all(key in card for key in LAYERS):
        return dict(card)
    original = str(card.get("positive_prompt", "") or "").strip()
    groups = {key: [] for key in LAYERS}
    notes = []
    for tag in tags(original):
        if tag.startswith("[在此"):
            notes.append("已从绘图层移除模板占位文字，原文仍保留。")
            continue
        plain = re.sub(r"[{}\[\]]|-?(?:\d+(?:\.\d+)?|\.\d+)::|::", "", tag).strip()
        # Mixed identity + clothing inside a weighted group must be inspected,
        # rather than moved wholesale to a replaceable outfit field.
        mixed = "," in plain and re.search(r"\b(hair|eyes|ears|skin|elf|girl|boy)\b", plain, re.I)
        key = "identity_prompt"
        if OUTFIT_TAG.search(plain.replace("_", " ")) and not mixed:
            key = "default_outfit_prompt"
        elif STYLE_TAG.search(plain.replace("_", " ")) and not mixed:
            key = "style_prompt"
        if mixed:
            notes.append("包含多个类别的权重组保留在身份锚点，请检查分组。")
        groups[key].append(tag)
    return {
        **card, **{key: join(*value) for key, value in groups.items()},
        "layers_version": 1, "migration_original_prompt": original,
        "layers_need_review": bool(original), "migration_notes": list(dict.fromkeys(notes)),
    }


def text_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(str(x.get("text", "")) for x in content if isinstance(x, dict) and x.get("type") in {"text", "input_text"}).strip()
    return ""


def history_records(history: Any) -> tuple[tuple[str, str], ...] | None:
    if isinstance(history, str):
        try:
            history = json.loads(history)
        except ValueError:
            return None
    if not isinstance(history, list):
        return None
    return tuple((str(item.get("role", "")), text_content(item.get("content", ""))) for item in history if isinstance(item, dict))


@dataclass(frozen=True)
class StoryView:
    scope: tuple[str, str]
    epoch: int
    history: tuple[tuple[str, str], ...]
    turn: str
    message: str


class StoryLedger:
    """Store only explicit narrative outfit snapshots, attested by chat history."""
    def __init__(self):
        self.scopes: dict[tuple[str, str], tuple[int, int, str]] = {}
        self.states: dict[tuple[str, str, str], dict] = {}
        self.serial = 0

    def clear(self, origin: str) -> None:
        self.serial += 1
        self.scopes = {key: value for key, value in self.scopes.items() if key[0] != origin}
        self.states = {key: value for key, value in self.states.items() if key[0] != origin}

    def observe(self, scope: tuple[str, str], history: tuple, turn: str, message: str) -> StoryView:
        previous = self.scopes.get(scope)
        digest = lambda records: hashlib.sha256(json.dumps(records, ensure_ascii=False).encode()).hexdigest()
        if previous is None or len(history) < previous[1] or digest(history[:previous[1]]) != previous[2]:
            self.serial += 1
            self.states = {key: value for key, value in self.states.items() if key[:2] != scope}
            epoch = self.serial
        else:
            epoch = previous[0]
        self.scopes.pop(scope, None)
        # Retain only a prefix digest, never a second copy of full chat history.
        self.scopes[scope] = (epoch, len(history), digest(history))
        while len(self.scopes) > 256:
            expired = next(iter(self.scopes))
            self.scopes.pop(expired)
            self.states = {key: value for key, value in self.states.items() if key[:2] != expired}
        return StoryView(scope, epoch, history, turn, message)

    def valid(self, view: StoryView | None) -> bool:
        return bool(view and self.scopes.get(view.scope, (None,))[0] == view.epoch)

    @staticmethod
    def signature(card: dict) -> str:
        return hashlib.sha256(json.dumps([card.get(key, "") for key in (*LAYERS, "negative_prompt", "reference_id")], ensure_ascii=False).encode()).hexdigest()

    def current(self, view: StoryView | None, card: dict) -> dict:
        if not self.valid(view):
            return {}
        key = (*view.scope, str(card.get("id", "")))
        state = self.states.get(key, {})
        if state.get("signature") != self.signature(card):
            return {}
        # Match the producing turn at its original history position, allowing
        # AstrBot's extra text blocks after the decorated user prompt. A later
        # identical user message must not attest an earlier discarded turn.
        if state.get("turn") != view.turn:
            position = state.get("source_position", len(view.history))
            actual = next((text for role, text in view.history[position:] if role == "user"), "")
            expected = state.get("message", "")
            if not expected or not (actual == expected or actual.startswith(expected + "\n")):
                self.states.pop(key, None)
                return {}
        return state

    def remember(self, view: StoryView | None, card: dict, outfit: str) -> None:
        if not self.valid(view) or not view.message or not card.get("id"):
            return
        key = (*view.scope, str(card["id"]))
        self.states.pop(key, None)
        self.states[key] = {"outfit": outfit, "signature": self.signature(card), "turn": view.turn,
            "message": view.message, "source_position": len(view.history)}
        while len(self.states) > 256:
            self.states.pop(next(iter(self.states)))


SUBJECT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "source": {"type": "string", "enum": ["self", "card", "custom"]},
        "preset_id": {"type": "string", "description": "source=card 的角色卡 ID 或完整名称。"},
        "prompt": {"type": "string", "description": "该人物的描述，可含动作、表情、服饰。custom 可写角色名或外貌；完整描述已在 appearance_prompt 或顶层 prompt 时可省略。"},
        "outfit": {"type": "string", "description": "完整当前服饰（含鞋、配饰），整体替换；空串明确无服饰。省略沿用当前剧情服饰或默认值。"},
        "state_scope": {"type": "string", "enum": ["image", "story"], "description": "默认 image 只影响本图；剧情实际换装才用 story。"},
        "use_reference": {"type": "boolean", "description": "仅参考图符合当前服饰时为 true，默认 false。"},
        "appearance_prompt": {"type": "string", "description": "仅无卡角色的完整外貌；绑定卡不会使用此字段重写身份。"},
        "negative_prompt": {"type": "string", "description": "无卡角色的负面词；绑定卡使用卡内负面词。"},
    }, "required": ["source"],
}

TOOL_PARAMETERS = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "prompt": {"type": "string", "description": "完整画面描述，或共享场景、人数、构图和光线。允许服饰和人物叙述；多人建议各自描述放 subjects，换装推荐 outfit。"},
        "subjects": {"type": "array", "items": SUBJECT_SCHEMA, "maxItems": 6, "description": "原生 JSON 角色数组，绝不再编码成字符串。self/card/custom 分别指定人物，空数组画无卡场景。"},
        "operation": {"type": "string", "enum": ["generate", "img2img", "reference"]},
        "reference_type": {"type": "string", "enum": ["character", "style", "both"]},
        "style": {"type": "string", "description": "本图完整画风；提供时替换卡画风，省略保留原卡精确画师名与权重。"},
    }, "required": ["prompt", "subjects"],
}
