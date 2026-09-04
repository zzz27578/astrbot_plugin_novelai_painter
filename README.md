# AstrBot NovelAI Painter

面向 AstrBot 公共插件生态的 NovelAI 生图扩展。重点是可控、可审计、避免重复请求：固定命令、LLM 工具、权限策略、单任务请求上限、人物/画风预设、参考图与 WebUI 管理均可配置。

## 主要能力

- NovelAI 官方后端：文生图、图生图、V5 / V4.5 / V4 / V3 官方模型下拉选择、Precise Reference / Character Reference / Style Reference。
- OpenAI / NewAPI 兼容后端：/v1/images/generations 与 /v1/images/edits。
- 入口四选一：全部禁用、仅固定命令、仅 LLM 工具、两者都启用；固定命令名可在 WebUI 修改，默认是 /nai。
- 私聊 / 群聊权限：所有人、仅管理员、白名单、禁用；管理员绕过白名单可单独控制。
- 严格安全上限：默认一次事件最多一个任务、一次 API 请求、一次一张图片；超时和 429 默认不自动重试，避免重复扣费或重复生图。
- 消息去重：按消息 ID 或事件指纹做短窗口去重。
- WebUI 页面：连接测试、入口与权限、基础参数、人物/画风预设、参考图上传、图生图参数、任务记录，均提供保存按钮和成功/失败反馈。
- 公共插件中性回复：不注入任何个人称谓、固定人格或个人标签；最终对话风格由 AstrBot 当前人设决定。

## 安装

在 AstrBot 插件市场安装，或执行：

    git clone https://github.com/zzz27578/astrbot_plugin_novelai_painter.git

安装后进入 AstrBot WebUI 的插件详情页，打开 NovelAI Painter 设置页面完成配置。自 2.1.1 起要求 AstrBot >= 4.17.0，以确保插件 Pages Web API 和 Bridge 可用。

## 命令

默认入口模式为“仅固定命令”，命令前缀为 /nai：

    /nai draw <画面描述>
    /nai img2img <画面描述>
    /nai reference character <画面描述>
    /nai reference style <画面描述>
    /nai reference both <画面描述>
    /nai preset list

/nai <描述> 旧格式默认兼容，可在 WebUI 关闭。参考图需要先在 WebUI 上传并绑定到预设；图生图优先使用绑定的参考图。

LLM 工具名称为 novelai_generate_image。只有在入口模式启用、用户通过权限检查，并且用户明确要求生成/绘制/修改图片时才会执行。

## WebUI 配置

页面中的设置分为：

1. 连接与模型：NovelAI 官方模型下拉列表、OpenAI/NewAPI 兼容模式、地址、密钥、鉴权请求头/前缀、尺寸、Steps、Scale、Sampler、负面提示词。
2. 入口与权限：命令 / LLM 工具开关、私聊和群聊策略、白名单、管理员绕过、排队提示、429 提示、错误通知、去重窗口、临时文件清理和固定关闭的自动重试策略。
3. 人物 / 画风预设：创建、编辑、删除、绑定 AstrBot 人设 ID、保存人物锚定词和画风锚定词。
4. 参考图与图生图：总开关、上传、删除、绑定、图生图 Strength/Noise、颜色校正、Precise Reference Strength/Fidelity。
5. 任务记录：查看本次运行期间的最近任务状态，不保存完整提示词和密钥。

点击顶部或底部“保存配置”后会立即写入插件配置，并通过页面提示保存结果。保存失败也会返回可读的中文错误，不依赖 Dashboard 的原始错误响应。

## 2.1.1 修复

- 修复已有预设时 WebUI 初始化失败的问题。根因是单元素选择器被当成元素列表调用 `forEach()`。
- 修复保存配置时同类选择器错误导致的红色失败提示。
- 修复预设删除按钮未绑定、编辑器删除失败后仍关闭的问题。
- 删除预设时同步清理默认预设和 AstrBot 人设映射，避免残留无效 ID。
- WebUI 请求增加超时、按钮忙碌状态和可读错误反馈；预设和参考图操作后重新同步服务端状态。
- 增加 AstrBot 最低版本声明，避免旧版本缺少插件 Pages API 时仍被当作完整兼容安装。

## 安全行为

- 自动重试默认关闭。网络超时、连接中断、429、认证错误均不会无条件再次发起生图 POST。
- images_per_request 和 max_api_requests_per_job 在插件内部强制为 1，不能通过 WebUI 或请求体提高。
- 错误通知默认只发送最终结果，不发送原始异常、Token、路径或代理细节。
- 临时图片和参考图存储在 AstrBot 插件数据目录，不使用写死的系统绝对路径。
- 参考图上传限制为 PNG/JPEG/WebP，单文件不超过 12MB，服务端重新命名并校验路径。

## 开发说明

插件配置 Schema 位于 _conf_schema.json，复杂页面位于 pages/settings/，插件运行数据位于 AstrBot 的插件数据目录。升级旧版本时，缺少的新配置项会自动补齐；原有 api_token、base_url、模型和生图参数仍可继续使用。

## 许可证

本项目暂未声明独立许可证。使用 NovelAI 或第三方兼容服务时，请遵守相应服务条款和 AstrBot 社区规范。
