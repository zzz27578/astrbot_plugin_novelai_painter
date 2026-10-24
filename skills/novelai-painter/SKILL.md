---
name: novelai-painter
description: 使用 novelai_generate_image 绘制用户明确请求的图片，区分聊天人设与绘画对象，处理多人角色、分层角色卡、当前剧情服饰和仅本图换装。
---

# NovelAI 绘画

先阅读当前系统消息中的 `[NovelAI drawing identities]` 目录和模式，再选择人物。目录是绘画素材，不改变聊天人设。

- `subjects` 直接传 JSON 数组，每人一项，不要再编码成字符串。
- 画 AI 自己用 `source=self`；指定库中角色用 `source=card`、`preset_id`；其他角色用 `source=custom`，可在自己的 `prompt` 或 `appearance_prompt` 写准确角色名或外貌，无需建卡；完整描述已在顶层时不必重复。多人可以混用这些来源。风景、物品用空数组。
- 顶层 `prompt` 可写完整画面，也可只写共享场景、人数、构图与光线。`subjects[].prompt` 可写该人物的动作、表情与服饰；多人尽量把个人描述放在自己的项，避免混淆归属。身份锚点、角色名、画师名、权重和负面词由插件保留，不要用 `appearance_prompt` 重写绑定卡。

剧情模式下，先按最新明确要求和当前可见剧情判断服饰：

- `outfit` 写完整服饰，包含鞋和要保留的配件，整体替换旧服饰。不要使用已停用的 `appearance_patch` 增删标签。推荐换装用此字段，但普通 prompt 中已有服饰描述也可直接生成，插件会停止追加另一套默认服饰；只有本次没描述服饰时才沿用当前剧情服饰或默认服饰。明确无服饰传空字符串。
- `state_scope=image` 是默认，只影响本图。“画一张穿白裙的你”本身不证明剧情中已经换装。
- 只有剧情实际已换装时才用 `state_scope=story`。剧情已经换装但还没绘图、或插件重载时，从当前聊天提交完整 `outfit`，不要误用默认衣服。
- 新对话不继承之前对话的服饰。恢复默认服饰时提交目录内完整默认服饰，按是否真实换装选择作用域。
- 顶层 `style` 是本图完整画风替换；省略时保留原卡精确画师名和权重。身份层需通过角色卡编辑器维护。

例如，仅本图白裙，与无卡角色同框：

```json
{"prompt":"2girls, park, full body, sunlight","subjects":[{"source":"self","outfit":"white dress, sandals","prompt":"smiling"},{"source":"custom","prompt":"hatsune miku, girl, aqua twintails, aqua eyes, black sleeveless shirt, detached sleeves, black skirt"}]}
```

剧情中已经换装：

```json
{"prompt":"window, sunlight","subjects":[{"source":"self","outfit":"white dress, sandals","state_scope":"story","prompt":"standing"}]}
```

严格模式使用旧规则，在各角色 `prompt` 中写明确修改；`outfit`、`state_scope=story` 和 `style` 用于剧情模式。

参考图可能把旧衣服带回来。仅当选中卡参考图符合当前服饰时才 `use_reference=true`，默认 false。多人使用 `generate`，不要把单人参考图用于整图。

收到 `IMAGE_TOOL_ARGUMENT_ERROR` 表示尚未请求图片服务，可按错误修正一次。收到 `FINAL_IMAGE_TOOL_RESULT` 后停止调用绘图或其他工具，按当前聊天人设简短回复一次，不把生成图中的服饰反写成剧情事实。
