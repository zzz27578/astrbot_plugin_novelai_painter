# 2.10.5：独立绘图与主模型对照测试

本版修复选卡误报、取消状态及长任务超时，并按需发送上一张图片。真实的 Grok 规划等待仍需用同一 Provider 对照，不能以插件测试通过代替延迟实测。

## 最小测试

更新并重载插件，确认版本 **2.10.5**，主聊天和独立绘图都选择同一个 Grok 4.7 Provider ID，并绑定同一张角色卡。

建议先用以下设置执行两次测试，无需跑完多组排列：

| 设置 | 取值 |
|---|---|
| 绘图输出协议 | 自动（OpenAI 兼容使用与主绘图同名的参数工具） |
| 绘图调用方式 | 自动（跟随主聊天的流式设置） |
| 绘图推理强度 | 继承 |
| 绘图输出 Token 预算 | 0 |
| 最近对话消息数 | 保持之前的 0；这是主动关闭历史，本轮原话和 n 的完整回复仍传入 |
| 读取附图和最近成图 | 首次关闭 |
| 格式错误纠正一次 | 开启；日志能区分首次请求与唯一一次纠错 |
| 规划超时 | 保持原来的 120 秒 |
| 记录绘图回包预览 | 开启，超时时也可保留有限正文/参数片段；测试后可关闭 |

1. 新对话发送：`/n 请用绑定角色卡画一张你坐在窗边看书的画面。` 应只走独立规划和生图，不调用主模型。
2. 等上一项完成或报错，再新对话发送：`请用绑定角色卡画一张你坐在窗边看书的画面，简短回应。n` 应先正常回复，再由独立模型出图。
3. 新对话发送同样场景并去掉 n，由主 Grok 直接调用工具。三组均先等前一次完成，再开始下一组。若比较热身差异，在同一对话再重复一次，并注明是否 reset。

若要验证附图优化，开启图片理解，先完成一张图，再分别发 `/n 画一张森林风景` 和 `/n 上一张改成夜景，保留人物`。前者应不附上一张图片，后者应有 `image_sources=previous_drawing`；本轮直接上传的图片仍照常提供。

若服务恰好出现 503，可设重试 1 次验证：同一个 job_id 只有图片 API 重试，超过工具等待阈值会出现 `image_tool_deferred`，随后 `image_end` 记录最终结果；主模型不能再说图片已经完成。不必为测试故意触发限流。

若要验证你之前使用的 JSON 模式，只改输出协议为 JSON，再试一次 /n。本版会发送真正的 json_object 格式约束；接口若不支持，会明确提示改用自动/工具模式，不静默重发。

如果仍慢，保留当前设置并提供日志，不需要继续反复调整超时、推理预算或流式开关。已有图片 503 重试配置保持生效，它只重试图片 API。

## 提供的日志

给出每次从收到消息到图片发送或最终报错的完整日志区间，注明 /n、n 或主模型直出，以及协议、调用方式、推理强度。保留 Provider 的报错行，密钥无需提供。

新增日志均带 request_id，图片阶段另有 job_id。WebUI 请求快照保存本轮请求、HTTP 结果与超时信息。

## 新日志如何定位

| 阶段或字段 | 含义 |
|---|---|
| request.request_layout=native_dialogue | 已使用新消息结构；原生角色对话和最后一条自然语言绘图要求 |
| request.provider_details.sdk_max_retries=0 | 绘图专用 SDK 副本已关闭隐式重试，主聊天配置不变 |
| http_request / wire | 实际 HTTP 请求开始；记录最终字段、模型、消息角色、工具名、stream、格式约束 |
| wire.tools=[novelai_generate_image] | 正在使用与主模型同名的绘图参数工具 |
| wire.response_format_type=json_object | JSON 模式已发送 API 格式约束 |
| http_attempts | 实际 HTTP send 累计次数；格式纠正、AstrBot Key/兼容性回退均会增加，不包含中转内部重试 |
| network_events | 支持 httpcore trace 时记录连接/TLS/发送请求头/接收响应头的阶段；复用连接时可能没有连接建立事件 |
| http_headers / headers_ms | 已收到响应头；耗时包含本地连接池、网络和上游等待，不能等同于纯上游时间 |
| http_first_body / first_body_ms | 开始收到响应体字节；SSE 心跳也可能先到 |
| http_first_content / first_content_ms | 可解析的正文内容开始出现 |
| http_first_tool_arguments / first_tool_arguments_ms | 工具参数开始出现，即使 AstrBot 尚未向插件交付最终参数 |
| http_first_reasoning / reasoning_chars | 服务器显式返回了推理字段；只记长度，不记录内容；隐藏推理未必返回 |
| http_end / body_elapsed_ms、outcome | 正文接收结束或取消，包含字节数、正文/参数长度与服务提供的 Token 用量 |
| http_error / headers_received=false | 在拿到响应头之前异常或取消；仍需结合异常类型判断网络、连接池或服务等待 |
| timeout / http_metrics | 超时发生时已取得的 HTTP 指标；开启预览才含有限正文/工具参数片段 |
| provider_wait | 每 30 秒进度，包含最近的 HTTP 和 SDK 片段统计，不会发新请求 |
| image_api_request / image_api_response | 真正图片服务的开始和结束，和前面的 LLM 参数生成分开计时 |
| main_first_drawing_tool.elapsed_ms | 主请求钩子到首次绘图工具的耗时，含 Agent 准备，不是纯 HTTP；不包含此工具后续生图耗时 |
| request.previous_image_policy / image_sources | 是否因改图或延续画面附带上一张图片 |
| request.tool_choice=required | 唯一一次工具参数纠错使用原生工具约束，首次仍为 auto |
| image_tool_deferred | 长任务继续在插件内执行，避免被核心工具超时截断，没有新建绘图请求 |
| image_end.delivery_state / deferred | 图片发送状态、是否延续执行；失败和取消也会有终态记录 |

响应头很快、内容很晚：继续查模型/中转生成过程。响应头也很晚：结合 HTTP 错误和上游请求 ID 查连接池、网络或中转等待。正文持续增长而结束很晚：查多余输出/显式推理。http_attempts 大于 1：先找格式纠正或 AstrBot 回退行。仅这些客户端日志不能直接测出服务内部排队、隐藏推理或中转内部重试。
