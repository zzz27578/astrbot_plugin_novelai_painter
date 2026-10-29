---
name: novelai-painter
description: 使用 novelai_generate_image 绘制用户明确请求的图片，区分聊天人设与绘画对象，处理多人角色、分层角色卡、当前剧情服饰和仅本图换装。
---

# NovelAI 绘画

绘图时阅读本轮临时工具上下文中的 `[NovelAI drawing identities]` 目录和模式，再选择人物。这份目录不写入聊天历史；其中的默认/当前服饰是绘图状态，不是新发生的剧情，不改变聊天人设。角色身份和画风由插件直接应用，不需要抄写全部卡词。

普通绘图请求始终由主模型编写完整 `prompt` 和人物参数，调用 `novelai_generate_image` 直接出图。即使配置了独立绘图 LLM，本工具也不会转交意图；不要只传“画现在的你”等简短意图。

本轮提示若说明绘图暗号已排队，只需正常聊天，完全不必写 Tag、转交意图或调用绘图工具。插件会独立读取用户原话与同一会话的当前上下文，在回复完成后出图。临时素材中的最近绘图记录可用于理解后续指代，但绘图设想不是剧情事实。

独立绘图 LLM 只服务暗号和 `/n`。默认共享当前主会话的人设、原生历史及工具结果；`n` 在本轮完整回复发送后固定快照并后台出图，`/n` 直接读取当前会话，不改主模型设置。规则见 [references/drawing-session.md](references/drawing-session.md)，由插件自动加载。历史工具里的完成回执仅适用于过去那轮，不表示本轮图片已生成。旧文本模式使用 [references/drawing-planner.md](references/drawing-planner.md)。以下完整 Tag 示例用于主模型工具调用。

- `subjects` 直接传 JSON 数组，每人一项，不要再编码成字符串。
- 画 AI 自己用 `source=self`；指定库中角色用 `source=card`、`preset_id`；其他角色用 `source=custom`，可在自己的 `prompt` 或 `appearance_prompt` 写准确角色名或外貌，无需建卡；完整描述已在顶层时不必重复。多人可以混用这些来源。风景、物品用空数组。
- 用户要求“用内置/绑定角色卡画一张”时，当前自身卡用 `subjects=[{"source":"self"}]`，指定的其他卡用 `card` 配准确 ID。不要传 `[]` 后自行改写卡词；插件选卡后直接保留原词及负面词。未能确定是哪张卡时按目录选择或澄清，不随意合并。
- 顶层 `prompt` 可写完整画面，也可只写共享场景、人数、构图与光线。`subjects[].prompt` 可写该人物的动作、表情与服饰；多人尽量把个人描述放在自己的项，避免混淆归属。身份锚点、角色名、画师名、权重和负面词由插件保留，不要用 `appearance_prompt` 重写绑定卡。

剧情模式下，先按最新明确要求和当前可见剧情判断服饰：

- `outfit` 写完整服饰，包含鞋和要保留的配件，整体替换旧服饰。不要使用已停用的 `appearance_patch` 增删标签。推荐换装用此字段，但普通 prompt 中已有服饰描述也可直接生成，插件会停止追加另一套默认服饰；只有本次没描述服饰时才沿用当前剧情服饰或默认服饰。明确无服饰传空字符串。
- `state_scope=image` 是默认，只影响本图。“画一张穿白裙的你”本身不证明剧情中已经换装。
- 只有剧情实际已换装时才用 `state_scope=story`。剧情已经换装但还没绘图、或插件重载时，从当前聊天提交完整 `outfit`，不要误用默认衣服。
- 新对话不继承之前对话的服饰。恢复默认服饰时提交目录内完整默认服饰，按是否真实换装选择作用域。
- 只有用户明确要求改变画风时，才用顶层 `style` 提交本图完整画风替换；常规用卡画图省略它，不要抄写一份通用画风替代原卡画师名和权重。无卡画图可自由使用。身份层需通过角色卡编辑器维护。

例如，直接使用当前绑定卡，服饰和画风均由卡及当前剧情提供：

```json
{"prompt":"full body, standing, looking at viewer, shy smile, soft indoor lighting","subjects":[{"source":"self"}]}
```

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

收到 `IMAGE_TOOL_ARGUMENT_ERROR` 表示尚未请求图片服务，可按错误修正一次。收到 `FINAL_IMAGE_TOOL_RESULT` 后不再重复绘图。结果中的 `requested_scene` 是本次请求的画面摘要，不是对实际成图的视觉检查；如需回复，按本次用户要求和当前对话自然回应，不照搬上次出图后的说辞，也不把图片设想反写成剧情事实。

`/n [自然语言要求]` 只唤醒独立绘图 LLM，直接读取当前会话上下文并出图；无参数表示画当前场景。不调用主模型，不输出聊天文本；未配置独立模型时给出配置提示。`/nai <Tag>` 保留原来的直接提交入口，不经过绘图 LLM。两者都不需要主模型再调用工具。
