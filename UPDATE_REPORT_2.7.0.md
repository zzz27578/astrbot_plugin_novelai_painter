# 2.7.0：分层角色卡与剧情服饰

旧逻辑把整段图片外观当作下一轮的角色状态，并依赖主模型逐词删除旧衣服。漏删、改写精确标签，或 reset 保留同一个对话 ID 时，都可能导致服饰叠加、跨新对话残留、人物不像。本版移除该路径，保存角色素材和剧情事实，按本次请求重新合成。

## 新结构

- `drawing.py` 独立处理角色卡分层迁移、完整权重标签解析、工具 JSON Schema、历史校验与剧情服饰账本。
- 工具 `subjects` 改为原生数组，嵌套声明 `self/card/custom`、完整 `outfit`、`image/story` 作用域等字段。读取旧版有效数组字符串以兼容旧调用；新说明不再引导二次 JSON 编码。
- 卡拆为 `identity_prompt`、`default_outfit_prompt`、`style_prompt`，保留原始正向词和原生权重。完整 `outfit` 只替换服饰层，身份和默认画风不经主模型改写。顶层 `style` 可替换本图画风。
- 默认 `state_scope=image` 仅改变本图；只有模型判断当前剧情实际已换装并显式提交 `story` 时，保存完整服饰。成功/失败的图片都不会被当作剧情证据。服务失败也不撤销已经明确发生的剧情换装。
- 有卡角色的旧完整外观重写被忽略，快照会标记被忽略字段；`appearance_patch` 和未知字段在调用服务前报错。允许一次参数纠正，接受任务后仍有并发闸门及原有发图去重。
- V4/V5 的角色正负词保持独立；场景层负责人数，人物槽内独立的 `1girl/1boy/solo` 按官方多角色用法处理。多人不使用全图人物参考。

## 对话生命周期

状态按会话来源、对话 ID、角色卡 ID 隔离。使用 AstrBot 成功 reset/new/switch 后的 `_clean_group_context_session` 标记清除状态，不依赖用户是否输入字面 `/reset`。

另读取真实持久历史，比较前缀摘要，验证产生服饰状态的用户轮次仍在原来的历史位置。兼容 AstrBot 提示前缀、多模态文本块和附加系统提醒，避免裸 `message_str` 与实际历史不一致。历史被清空或改写、卡内容改变，旧状态失效；无法读取历史时不使用共享状态。账本最多保存 256 个作用域和 256 份服饰状态，只保留历史摘要，不复制整份聊天记录。

排队请求在调用服务前再次校验对话；服务返回后也会检查，防止向重置后的对话发送旧图。已提交到上游的请求不能撤销，仍可能产生费用。

## 编辑器、迁移和技能

角色卡页分别编辑身份锚点、默认完整服饰和画风，可展开迁移前正向原文。旧卡会一次性辅助分组，显示“待检查分层”，在保存迁移数据前备份 `presets.before-layers.json`。未知词和混合类别权重组需要人工检查；不会在运行时反复猜测并删除服饰。

你提供的精灵卡中，`{{elf (isekai ojisan)}}`、`{forehead jewel}`、`yellow split skirt`、`{atelier pontdarc}` 及原有负面词都经过实际官方请求序列化验证，模板占位文字仅从绘图层移除，原文仍可恢复。

新增插件技能 [novelai-painter](skills/novelai-painter/SKILL.md)，同步工具说明、每轮目录说明、配置提示和 README。技能只指导该插件绘图。自行安装的旧技能或人设内硬编码规则仍需同步清理。

任务页新增请求快照：显示实际场景、最终正负词、独立人物槽、服饰来源、作用域、参考类型及生成参数。每会话只保留最近一份，全局最多 20 份；仅驻留内存，排除密钥、图片数据和聊天历史。删除对应任务或清空记录也清理快照。

## 验证

- `python -m unittest discover -s tests -q`：86 项通过。
- 覆盖原生/旧字符串参数等价、一次参数纠正、并发单次请求、完整换装/空服饰、本图与剧情隔离、服务失败后的剧情延续、reset 同 ID、新 ID、历史修改、卡编辑、多模态历史来源校验、排队/生成中重置、多人服饰和负面词、画风替换、原生权重、迁移备份及幂等性、快照与官方序列化内容一致、参考图快照去图片数据。
- 保留入口权限、人设绑定、严格模式、固定命令、参考图、429 重试、去重、表情包和任务管理回归。
- Playwright Chromium 模拟 AstrBot Bridge 完成实际页面加载、分层编辑与保存、迁移提示、原文保留、快照纯文本显示、390px 布局检查，无页面脚本异常。
- Python 编译、JavaScript 语法、技能格式、配置/版本一致性和 `git diff --check` 检查通过。
- 图片服务通过 mock HTTP/provider 验证，未调用付费生图，不能据此保证图像模型对人物/画师标签的识别效果。模型对剧情和仅本图要求的语义判断仍可能出错，可在请求快照中核对。

## 设计参考

参考的是职责边界和数据组织方式，没有引入第三方实现代码：

- [NovelAI 官方多角色文档](https://docs.novelai.net/en/image/multiplecharacters/)：共享提示词与独立角色正负词。
- [SillyTavern stable-diffusion 扩展](https://github.com/SillyTavern/SillyTavern/blob/06bde939fb1e9c4c8d8641d810f0a916b5bce127/public/scripts/extensions/stable-diffusion/index.js)：场景、角色前缀和生成模式的职责边界。
- [st-novelai-imagegen](https://github.com/DesmondFox/st-novelai-imagegen/blob/main/index.js)：稳定外貌与服饰分离、聊天作用域素材。
- [st-novelai-images 素材目录](https://github.com/gowwwther/st-novelai-images/blob/main/src/catalog.js)：角色、画风和负面素材分别解析。

调研中的 SillyTavern PR #5620、#5587 为未合并参考，不作为已发布功能的依据；本版行为以以上实现和回归用例为准。
