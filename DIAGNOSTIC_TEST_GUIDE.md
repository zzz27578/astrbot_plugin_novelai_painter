# 2.10.2：独立绘图 LLM 延迟对照测试

本轮目标是确定约 117 秒花在哪里。已有日志证明慢轮的大部分时间位于独立 Provider 调用内，无法区分上游推理、服务排队和 SDK 内部重试。本版修复实际工具 schema 的旧转述提示，并补充测量；没有宣称解决长等待。

## 更新与基线设置

更新插件并重载，确认版本为 **2.10.2**。主聊天和插件选择**同一个 Grok 4.7 Provider ID**：仅模型名称相同不足以确保使用相同服务、参数或路由。入口选择“两者都启用”，绑定同一张角色卡，生图模型、尺寸和步数保持相同。

先记录之前慢轮所用的输出协议、最近对话消息数、视觉开关、格式纠正开关和超时值，然后使用以下诊断设置：

| 设置 | 本轮取值 | 原因 |
|---|---|---|
| 绘图输出协议 | JSON 文本 | 与此前慢日志一致，先建立基线 |
| 最近对话消息数 | 0 | 保持你原来的设置；0 是主动关闭历史，仍传本轮原话和完整回复 |
| 读取附图和最近成图 | 关闭 | 排除当前/上张图片的视觉处理时间 |
| 格式错误纠正一次 | 关闭 | 先测单次调用，格式错误会直接留下诊断结果 |
| 规划总超时预算 | 180 秒 | 让约 117 秒的慢响应有机会完整返回；这是上限，不是固定等待 |
| 记录绘图回包预览 | 开启 | 输出最多 2000 字符绘图回包，便于检查格式；测试后可关闭 |

保持 Grok Provider 的推理参数、最大输出长度和主聊天流式设置原样，记录它们即可。测试期间每次等上一张图完成或报错后再继续。

## 第一组：比较三条入口

每项测试前 reset/新建对话，确认仍绑定同一卡；用下面这个普通场景，无须发送图片。每条入口至少一次，有条件再按同一顺序复测一次。

| 编号 | 发送内容 | 应出现的流程 |
|---|---|---|
| A | `请用绑定角色卡画一张你坐在窗边看书的画面，简短回应。` | 主 Grok 写参数并调用 novelai_generate_image，直接生图 |
| B | `请用绑定角色卡画一张你坐在窗边看书的画面，简短回应。n` | 主 Grok 正常回复，发送后独立 Grok 规划并生图 |
| C | `/n 请用绑定角色卡画一张你坐在窗边看书的画面。` | 独立 Grok 规划并生图，不调用主模型 |

记录发送时间、主回复时间（C 没有）、图片到达或报错时间。A 的主模型在图后追加回复时，也保留那一段日志；它能判断出图后是否又经历工具循环。

## 第二组：只更换参数协议

将“绘图输出协议”改为**内部参数提交工具**，其他设置不变，重复 B 和 C。这样可以比较同一 Provider 的 JSON 与 `tool_choice=required` 参数工具路径。上游不支持工具时保留报错即可，不必反复重试。

首轮只需做 A/B/C 三项；第二组用于进一步区分协议问题。有明显快慢差异时，提供各一次完整日志即可继续分析。

## 返回哪些资料

提供每次从收到消息到图片发送/最终报错的**完整时间区间日志**，包含 Core 的工具调用、Provider 警告和插件的“绘图规划”行。一起注明 A/B/C、协议、Provider ID、上述配置和主聊天是否流式。日志中的 `request_id` 关联同一条消息，`job_id` 关联图片任务。Provider/API 密钥无需提供。

WebUI“任务记录 → 最近一次请求快照”也可复制；其中 `snapshots.request`、`snapshots.response` 保存规划输入统计和回包元数据，即使图片已发送也保留。完整日志更适合比较多轮。

## 日志读法与判断边界

| 字段/阶段 | 能判断什么 |
|---|---|
| waiting_reply → prepare / reply_wait_ms | n 等主模型回复和发送的时间 |
| prepare_ms、context_prepare_ms、image_prepare_ms | 本地素材准备、会话读取和图片路径转换的时间 |
| payload_size、system_size、tool_schema_size、total_text_chars | 业务 JSON、内置技能、工具 schema 的输入大小；不是上游实际 Token 或完整 HTTP 大小 |
| field_chars | 原话、完整回复、历史、角色目录、上一张方案各自占多大 |
| context_messages_configured、context_disabled、context_source | 历史是否主动关闭、来自主请求快照还是当前会话 |
| image_sources | 本轮主请求图片、本轮消息图片或上一张成图；路径/URL 不输出 |
| request → received / provider_ms | 一次 Context/Provider 调用耗时，含 SDK 和上游等待；不是首 Token 时间 |
| provider_wait | 每 30 秒仍在等待，既不发新请求也不重试 |
| response / validation_ms、retry、attempt | 本地校验时间，是否追加过格式纠正调用 |
| provider_details | Provider、当前模型、接口指纹、SDK 重试配置及部分配置参数；这些是观测到的配置，不是拦截到的最终 HTTP 参数 |
| response_details | 上游返回的模型、结束原因、请求 ID、Token/缓存/推理用量；空 usage 表示未取得数据，不能当 0 |
| main_request_hook、main_response_hook | 主模型输入统计及回包元数据；hook_elapsed_ms 包含整个 Agent 阶段，可能包含工具和生图时间，不能与 provider_ms 直接比较 |
| image_api_request / queue_ms、image_api_response | 本地生图通道等待和图片服务调用时间（包含上传、处理、下载） |
| image_end / generation_ms、delivery_ms | 生图流程与图片发送各自耗时 |
| duplicate_tool / drawing_state | 主模型是否在已排队或已完成后又调用绘图工具；此类调用不会再生图 |
| failed、exception_type、http_status、server_request_id | 准备、SDK 或服务异常；状态和请求 ID 仅在可取得时输出 |

如果 B 慢而 C 快，优先比较 B 的完整回复、输入构成和回复后流程。如果 B/C 都慢而 A 快，优先比较独立请求的协议、系统技能、推理与缓存用量、流式/非流式服务路径。仅凭差异仍不能断言根因；Provider 调用内部的 HTTP 排队、首 Token 和实际重试次数，需要结合 AstrBot Provider 警告或上游网关的请求记录确认。日志不会改写共享 Provider 或拦截其 HTTP 客户端。
