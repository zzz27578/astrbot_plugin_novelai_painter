# 2.10.3：验证调用修复与 503 重试

新 /n 日志中，无主回复等待，准备 19 毫秒、Provider 调用 115.36 秒、可见 JSON 437 字符，却报告 6153 个输出 Token。2.10.3 修复独立路线固定非流式的调用差异，支持流式与本次请求的推理/输出预算覆盖，同时增加 503 次数配置。本轮检查实际耗时是否改善，并进一步区分等待首段与后续输出耗时；不预先承诺降到某个秒数。

## 更新与基线设置

更新插件并重载，确认版本为 **2.10.3**。主聊天和插件选择**同一个 Grok 4.7 Provider ID**。入口选择“两者都启用”，绑定同一张角色卡，生图模型、尺寸和步数保持相同。

先记录之前慢轮所用的输出协议、最近对话消息数、视觉开关、格式纠正开关和超时值，然后使用以下诊断设置：

| 设置 | 本轮取值 | 原因 |
|---|---|---|
| 绘图输出协议 | JSON 文本 | 与此前慢日志一致，先建立基线 |
| 绘图 LLM 调用方式 | 流式 | 验证本次修复，日志应有 streaming=true |
| 绘图 LLM 推理强度 | 继承 Provider | 首先只改变流式调用，不同时改变推理设置 |
| 绘图 LLM 输出 Token 预算 | 0 | 继承 Provider，避免预算包含隐藏推理而截断 |
| 最近对话消息数 | 0 | 保持你原来的设置；0 是主动关闭历史，仍传本轮原话和完整回复 |
| 读取附图和最近成图 | 关闭 | 排除当前/上张图片的视觉处理时间 |
| 格式错误纠正一次 | 关闭 | 先测单次调用，格式错误会直接留下诊断结果 |
| 规划总超时预算 | 180 秒 | 让约 117 秒的慢响应有机会完整返回；这是上限，不是固定等待 |
| 记录绘图回包预览 | 开启 | 输出最多 2000 字符绘图回包，便于检查格式；测试后可关闭 |
| 429 / 503 自动重试 | 暂时关闭 | 先单独测绘图模型；503 功能另行验证 |

保持 Grok Provider 的推理参数、最大输出长度和主聊天流式设置原样，记录它们即可。测试期间每次等上一张图完成或报错后再继续。

## 第一组：比较三条入口

每项测试前 reset/新建对话，确认仍绑定同一卡；用下面这个普通场景，无须发送图片。最小测试是 B、C 各一次；已有主 Grok 的近期直出基准可直接提供，无须重复许多次。

| 编号 | 发送内容 | 应出现的流程 |
|---|---|---|
| A | `请用绑定角色卡画一张你坐在窗边看书的画面，简短回应。` | 主 Grok 写参数并调用 novelai_generate_image，直接生图 |
| B | `请用绑定角色卡画一张你坐在窗边看书的画面，简短回应。n` | 主 Grok 正常回复，发送后独立 Grok 规划并生图 |
| C | `/n 请用绑定角色卡画一张你坐在窗边看书的画面。` | 独立 Grok 规划并生图，不调用主模型 |

记录发送时间、主回复时间（C 没有）、图片到达或报错时间。A 的主模型在图后追加回复时，也保留那一段日志；它能判断出图后是否又经历工具循环。

## 仍慢时：一次只改变一个设置

先将“推理强度”从继承改为**低**，保持流式、JSON、预算 0，重复 C 一次。日志应有 `request_overrides.reasoning_effort=low`。如果服务不支持该字段，保留 400 等报错并改回继承，插件不会悄悄再发一次。

需要进一步比较协议时，保持相同推理强度，将“输出协议”改为**内部参数提交工具**再试 C。需要比较旧调用路径时，保持 JSON、继承、预算 0，只把“调用方式”改为**非流式**再试 C。按出现的差异选择一项即可，不必一次跑完所有组合。

## 验证 503 次数配置

将“503 自动重试次数”设为 **1**，“重试基础等待”设为 **3 秒**，429 重试保持关闭。正常出图即可，不需要故意填错地址。遇到服务真实 503 时应出现：

1. `image_api_error`：`error_code/status=http_503`、`retry=true`。
2. `image_retry_wait`：`next_attempt=2`、`max_attempts=2`；上游 Retry-After 更长时遵守更长等待。
3. 同一 job_id 的第二次 `image_api_request`。绘图 LLM 不重新调用，最终成功只发一张图片。
4. 第二次仍 503 则停止并报错，不出现第三次图片 API 请求。

次数设为 0 关闭，最多额外 3 次。429 和 503 各自计数，总上限为首次 1 次加两种重试额度，最多 6 次。503 重发可能重复计费。网络中断、超时、其他 5xx 不自动重试；重试等待中 reset 会阻止旧图继续请求。

## 返回哪些资料

提供每次从收到消息到图片发送/最终报错的**完整时间区间日志**，包含 Core 的工具调用、Provider 警告和插件的“绘图规划”行。注明 A/B/C、协议、调用方式、推理强度和 Provider ID。日志中的 `request_id` 关联同一条消息，`job_id` 关联图片任务。Provider/API 密钥无需提供。

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
| transport、streaming、request_overrides | 实际选择的调用路径与本次请求覆盖参数 |
| first_chunk_ms、first_text_ms、first_reasoning_ms | Provider 向插件交付首段流式文字/推理的时间，非原始 HTTP 首 Token；没提供相应片段就不填 |
| chunks、streamed_text_chars、streamed_reasoning_chars、final_response_ms | SDK 可见输出片段数量、累计长度和最终结果时间；工具模式可能只有最终参数，chunks=0 不等于没使用流式 HTTP |
| raw_content_chars、raw_reasoning_fields | 适配器清理前的正文及常见推理字段长度，有助于追查短 JSON 与大量计费 Token 的差异 |
| provider_wait | 每 30 秒仍在等待，既不发新请求也不重试 |
| response / validation_ms、retry、attempt | 本地校验时间，是否追加过格式纠正调用 |
| provider_details | Provider、当前模型、接口指纹、SDK 重试配置及部分配置参数；这些是观测到的配置，不是拦截到的最终 HTTP 参数 |
| response_details | 上游返回的模型、结束原因、请求 ID、Token/缓存/推理用量；空 usage 表示未取得数据，不能当 0 |
| main_request_hook、main_response_hook | 主模型输入统计及回包元数据；hook_elapsed_ms 包含整个 Agent 阶段，可能包含工具和生图时间，不能与 provider_ms 直接比较 |
| image_api_request / queue_ms、image_api_response | 本地生图通道等待和图片服务调用时间（包含上传、处理、下载） |
| image_end / generation_ms、delivery_ms | 生图流程与图片发送各自耗时 |
| duplicate_tool / drawing_state | 主模型是否在已排队或已完成后又调用绘图工具；此类调用不会再生图 |
| failed、exception_type、http_status、server_request_id | 准备、SDK 或服务异常；状态和请求 ID 仅在可取得时输出 |

如果流式明显改善而非流式复现慢，支持调用/中转流式路径差异。如果首段很快但最终结果很慢且累计输出很长，优先追查多余输出或推理；如果降低推理强度后改善，支持推理预算相关。Provider 调用内部的实际 HTTP 排队和重试次数仍需结合 Provider 警告或上游请求记录确认。插件使用请求局部配置副本，不改写共享 Provider 或拦截其 HTTP 客户端。
