# NovelAI Painter 2.3.0 更新与功能保证报告

## 修复结论

本版本依据 AstrBot 4.27.3 实际日志重构提示词与工具完成流程。LLM 工具参数只负责描述当前动作、姿势、表情、构图、场景和明确修改项；插件后端再解析当前 AstrBot 人设绑定的角色卡，将角色卡正向 Tag、LLM 本次 Tag、角色卡负面 Tag 和全局负面提示词组合后提交到图片服务。

自然语言生图完成后，插件不再返回 `None`，而是返回带 `FINAL_IMAGE_TOOL_RESULT` 标记的最终工具结果，明确要求 Agent 停止调用工具并按当前人设简短回复一次。固定 `/nai` 命令仍只发送图片，不触发额外 Agent 回复。

## 角色卡重构

### 数据结构

- `positive_prompt`：统一保存稳定的人物主体、脸部、头发、眼睛、种族、服装、配饰和画风 Tag。
- `negative_prompt`：保存该角色不应出现的人物特征、意外服装、画风漂移和其他负面 Tag。
- `lock_positive`：默认开启，锁定用户未明确要求修改的人物与画风信息。
- `positive_strength`：统一控制角色卡正向锚定强度，NovelAI V4/V5 默认 `1.35`。
- `quality_override`：继续允许角色卡选择关闭、继承或强制开启模型质量标签。

旧 `style_prompt` 与 `character_prompt` 会按原顺序合并为 `positive_prompt`；旧两组锁定值折叠为统一锁定值，旧两组强度取较大值，原 `negative_prompt` 原样保留。迁移在插件启动时写回 `presets.json`，使用临时文件和原子替换，避免半写入文件。

### 最终请求顺序

NovelAI 官方后端的正向提示词顺序为：

1. 加权后的角色卡正向 Tag。
2. 与未修改类别对应的人物和画风一致性约束。
3. LLM 根据当前用户要求生成的 Tag。

负面提示词由全局负面提示词与角色卡负面 Tag 合并，同时写入 `negative_prompt` 和 V4/V5 的 `v4_negative_prompt.caption.base_caption`。OpenAI 兼容后端没有统一的独立负面参数，因此将两部分转换为明确的 `Avoid these negative prompt traits` 指令并放入最终 prompt。

### 本次请求覆盖

插件从用户原始消息识别主体、头发、眼睛、种族/耳朵、服装、身体特征和画风修改。用户没有明确修改某类属性时，LLM 在工具参数中自行补写的同类 Tag 会被移除；用户明确要求修改时，角色卡正向和负向 Tag 中该类别只在本次请求让位，其他类别继续锁定，磁盘中的角色卡不会改变。

例如角色卡为 `blonde hair, side ponytail, black bodysuit, anime screencap`，负面 Tag 为 `pink hair, wedding dress, lowres`：

- “画你现在的样子”不会接受 LLM 自行补写的 `pink hair` 或 `wedding dress`。
- “把衣服换成婚纱”会临时移除正向的 `black bodysuit` 与负向的 `wedding dress`，保留金发、单马尾、原画风及 `pink hair, lowres`。

## 人设绑定与 WebUI

- WebUI 将原“人物 / 画风预设”统一显示为“角色卡”。
- 编辑器提供角色卡正向 Tag、角色卡负面 Tag、锁定开关、统一强度、质量策略、人设绑定和参考图绑定。
- `persona_preset_map` 仍是权威映射；角色卡编辑器中的人设 ID 与独立映射表即时同步。
- 运行时继续通过 AstrBot 4.27.3+ 的 `PersonaManager.resolve_selected_persona()` 获取与主 Agent 一致的会话人设。
- 禁用的角色卡不会参与默认回退、人设映射、提示词合成或参考图自动选择。

AstrBot runner 日志中的工具参数是 LLM 调用函数时的原始参数，所以不会显示后端稍后加入的角色卡 Tag。角色卡命中后插件会记录 `已合并角色卡 <id>`，并输出角色卡正向、负向和最终正向提示词长度；调试日志可查看最终正向提示词。是否真正提交应以该插件日志和最终 API payload 路径为准，而不是只看 runner 的原始工具参数行。

## Agent 收尾与防重复

- LLM 工具直接发送图片后返回非空最终结果，避免 AstrBot 4.27.3 将 Agent 直接标记完成而没有人设回复。
- 返回结果明确说明图片已经发送、禁止再次调用任何工具、要求当前人设只回复一次。
- 事件 claim 在同一事件对象上拦截第二次调用。
- 事件消息 ID/指纹短窗口去重不受 LLM 改写 prompt 或 operation 影响。
- 进行中任务表避免并发重复请求，Job ID 发送表避免重复发图。
- `/nai` 命令消费发送结果但不 yield 成功文本，因此成功时只发图。

工具返回文本可以为 Agent 提供明确的收尾指令，但插件不能从函数内部绝对控制第三方 LLM 的下一 token 决策。即使模型违背指令再次调用，多层去重仍保证同一条入站消息最多创建一个图片服务任务和发送一张图片，避免重复扣费。

## 验证结果

- Python 单元测试：36 项通过。
- AstrBot 4.27.3 源码环境：36 项通过。
- AstrBot 4.27.5 源码环境：36 项通过。
- JavaScript：`node --check pages/settings/app.js` 通过。
- 配置 Schema：`python -m json.tool _conf_schema.json` 通过。
- Python 语法：`python -m py_compile main.py tests/test_core.py` 通过。
- Git 空白检查：`git diff --check` 通过。

测试覆盖角色卡迁移、统一正向锚定、正负 Tag 请求级覆盖、NovelAI 最终 payload、OpenAI 兼容负面指令、人设解析与绑定、禁用状态、自动参考图、LLM 工具非空完成结果、重复工具调用单后端任务、`/nai` 成功无文本输出、权限、429 重试硬上限、单图发送和 WebUI 关键字段。

## 功能保证边界

2.3.0 可以保证代码层面的以下行为：

- 命中启用角色卡时，其正向 Tag 一定先于 LLM 本次 Tag 进入最终请求。
- 角色卡负面 Tag 与全局负面提示词一定在对应后端的最终请求中生效。
- WebUI 保存的人设绑定会参与运行时角色卡选择。
- 用户未明确要求的人物与画风幻觉 Tag 会按已覆盖类别过滤；明确修改只作用于本次请求。
- 自然语言工具返回可供 Agent 收尾的非空结果，固定命令不追加聊天回复。
- 同一入站消息最多建立一个后端生图任务，并最多发送一张图片。

生成模型本身具有随机性，代码无法保证每张图在视觉上 100% 复现同一人物。角色卡权重、负面 Tag、冲突过滤和参考图能显著降低漂移；最终一致性仍受模型版本、采样参数、Tag 质量、参考图质量和上游服务实现影响。
