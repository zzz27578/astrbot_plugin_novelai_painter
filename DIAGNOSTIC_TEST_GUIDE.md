# 2.11.2：后台绘图队列与主模型对照测试

本版修复继续聊天导致后台绘图误取消，并让同一会话的 `n`、`/n` 按进入后台队列的顺序执行。保留 JSON 参数修复及共享上下文。真实的 Grok 单次规划等待仍需用同一 Provider 对照，不能以插件测试通过代替延迟实测。

## 先验证本次问题

更新重载后确认 **2.11.2**，保持原 Provider、协议和调用方式。

1. 发送 `画一张窗边看书的画面 n`，主聊天回复后、图片尚未返回时，再发普通聊天“继续聊天，告诉我书里写了什么”。普通聊天应能继续，第一张仍对应原本的窗边画面，不再因历史变化取消。
2. 第一张仍在运行时，再发 `画一张花园散步的画面 n`，等主回复后再发 `/n 画一张夜晚街景`。三张应按后台入队顺序完成；后两张开始规划前应等待前序任务发送或失败。普通聊天不用等队列完成。
3. 单独测试 `/n`：发 `/n 画一张山间风景`，尚未出图时发普通聊天。主模型应正常回答，绘图也应继续；`/n` 自己不会另发聊天 AI 回复。
4. 另开一组测试，在一张运行、另一张等待时执行真正的 reset。两张旧任务都应停止，随后新请求正常执行。无需为测试手工改历史；若核心正常发生上下文压缩，保留该阶段日志即可。

检查 `background_submitted.queue_position`、`task_started.queue_wait_ms` 和 `image_end`。`holds_chat_pipeline=false` 表示独立绘图不继续占用聊天流水线；等待不消耗规划预算。新增“独立绘图排队超时”默认为 600 秒，保留默认即可，只有实际队列等待超过该值才报排队超时。

`task_history_changed` 的 `status=preserved` 表示历史发生变化但保留了当前任务，沿用自己的固定上下文；`task_context_invalid.reason` 则说明真正失效的原因，如生命周期重置、对话切换或历史已清空。两者都不是新的模型 HTTP 请求。

## 最小测试

更新并重载插件，确认版本 **2.11.2**，主聊天和独立绘图都选择同一个 Grok 4.7 Provider ID，并绑定同一张角色卡。要复测之前的 JSON 故障，保留 JSON 模式，不需要为了测试更换上游或调高超时。

建议先用以下设置执行两次测试，无需跑完多组排列：

| 设置 | 取值 |
|---|---|
| 绘图输出协议 | JSON 文本；保持与这份故障日志一致 |
| 绘图调用方式 | 保持当前设置；日志中的测试为非流式 |
| 绘图推理强度 | 继承 |
| 绘图输出 Token 预算 | 0 |
| 绘图上下文来源 | 共享主会话（默认） |
| 共享上下文字符上限 | 48000；首次不要提高 |
| 旧文本模式的最近消息数 | 可以保持 0，但共享模式不使用此值；只有选择旧文本模式才关闭旧历史 |
| 读取附图和最近成图 | 首次关闭 |
| 格式错误纠正一次 | 开启；日志能区分首次请求与唯一一次纠错 |
| 规划超时 | 保持当前值，不因测试提高；模型 Provider 自身超时另计 |
| 独立绘图排队超时 | 保持默认 600 秒；与图片 API 队列超时、规划预算分别计时 |
| 记录绘图回包预览 | 开启，超时时也可保留有限正文/参数片段；测试后可关闭 |

1. 在一段已有聊天中发送：`请用绑定角色卡画一张你坐在窗边看书的画面，简短回应。` 等主 Grok 直接工具出图完成。
2. **同一对话不 reset**，发送 `/n 请用绑定角色卡画一张你坐在窗边看书的画面。`；完成后再发同样要求，末尾加 `n`。记录三组从模型 HTTP 开始到工具参数返回的时间，图片 API 耗时另计。`n` 等主聊天回复的时间也另计。
3. 再重复一次 `/n` 和普通主模型出图，观察预热后变化。需要严格排除先后顺序影响时，再开新对话按反序测试一组。每张完成或报错后才开始下一张。
4. 单独验证阻塞修复：发送末尾带 `n` 的请求，主聊天回复后、图片仍未完成时，发送“这条先只回复收到”。第二轮应能开始，不必等上一张图片完成；最终图片仍应对应之前那轮的场景。

检查 `request_layout=shared_session`。`n` 的 `context_source` 通常为 `main_agent_snapshot`，`/n` 为 `session_persona_skills`；有历史时 `native_message_count` 不应因旧条数配置 0 而归零。`trimmed_turns` 表示因字符上限或核心轮次限制去掉的旧轮次。超过上限会明确提示，不要用提高模型超时来处理。

若需要对照原输入逻辑，只切换“旧版最近 N 条文本”，保留原消息数，其余设置不变，再测一次 `/n`。共享历史可能增加输入长度，不能预先假定一定更快。

JSON 修复的成功标准：`request.json_contract_size.chars>0`、`http_request.wire.response_format_type=json_object`，回包的 `subjects` 为人物对象数组。有效参数或可确定的格式冗余应只有一次规划 HTTP 请求，随后 `image_start`。出现 `plan_normalized` 时，`creates_model_request=false`，不应因此发起第二次请求。`style:null` 不会清空卡画风；只读回显仅在与本次素材完全一致时移除。

`subjects=["1girl", ...]` 仍需纠正，不会自动当成自身卡。若出现格式问题，检查 `validation_issues` 是否集中给出结构错误及字段路径（最多 16 项），最多纠正一次；成功后的 `response.format_retry_count` 应为 0 或 1。`provider_total_ms` 累计已经完成的规划调用时间，图片 API 耗时另计。首次仍慢且没有重试时，继续比较 HTTP 指标，不把它算成格式纠正耗时。

选卡回归可发 `/n 全身照`。card 的 ID 若漏填且对象可以唯一确定，应出现 selection_repair、creates_model_request=false，然后继续出图；不能因为补选再调用一次模型。明确指定其他角色仍应选择该角色，不套当前绑定卡。

主模型直出时检查 image_tool_budget：只调整当前图片工具预算，image_end 后返回最终结果。正常 46–52 秒的出图不应再在第 45 秒出现 image_tool_deferred。若同批次误用 send_message_to_user 发图，应返回 IMAGE_DELIVERY_HANDLED，不能再报 generated_image_url_here 无效。本轮后续可用工具不再包含已受理的绘图入口；新用户消息仍可再次绘图。

若要验证附图优化，开启图片理解，先完成一张图，再分别发 `/n 画一张森林风景` 和 `/n 上一张改成夜景，保留人物`。前者应不附上一张图片，后者应有 `image_sources=previous_drawing`；本轮直接上传的图片仍照常提供。

若服务恰好出现 503，可设重试 1 次验证：同一个 job_id 只有图片 API 重试，随后 image_end 记录最终结果。不必为测试故意触发限流。

若 JSON 测试后仍需与工具协议对照，只改输出协议为“自动”或“绘图参数工具”，其余不变。首次 `request.tool_choice` 和 `http_request.wire.tool_choice` 应为 required；原生回包 tools_call_name 含 novelai_generate_image。该对照会改变协议和工具集合，不能据此断言两次请求的缓存条件相同；不必反复排列测试。接口不支持当前约束时会明确提示，不静默切协议重发。

如果仍慢，保留当前设置并提供日志，不需要继续反复调整超时、推理预算或流式开关。已有图片 503 重试配置保持生效，它只重试图片 API。

## 提供的日志

给出每次从收到消息到图片发送或最终报错的完整日志区间，注明 /n、n 或主模型直出，以及协议、调用方式、推理强度。保留 Provider 的报错行，密钥无需提供。

日志按阶段分类，均带 request_id，图片阶段另有 job_id。只有“绘图 HTTP 请求”代表进入一次实际 HTTP send；“绘图等待进度”每 30 秒记录一次，sends_request=false，不发新请求。首字节等细节在 DEBUG 日志，完整 HTTP 结果仍保留 network_events。WebUI 请求快照保存本轮请求、HTTP 结果与超时信息。

## 新日志如何定位

| 阶段或字段 | 含义 |
|---|---|
| request.request_layout=native_dialogue | 已使用新消息结构；原生角色对话和最后一条自然语言绘图要求 |
| request.request_layout=shared_session | 2.11.0 共享当前主会话的系统提示和原生历史 |
| context_source / native_message_count / native_tool_pairs | 快照来源、实际历史消息数量及完整工具配对数量 |
| background_submitted / holds_chat_pipeline=false | 已提交后台任务，回复钩子不继续等待绘图；此行不代表又发起了一次 HTTP 请求 |
| background_submitted.queue_position / queue_timeout_seconds | 当前会话中入队时的位置、允许排队等待的秒数；n 和 /n 共用队列 |
| task_started.queue_wait_ms | 真正轮到本任务时的队列等待时间，此后才开始模型规划预算 |
| task_history_changed / status=preserved | 历史正常改变，任务仍使用固定上下文，没有因服饰缓存代次变化而取消 |
| task_context_invalid.reason | 实际失效原因：lifecycle_changed、conversation_changed、history_cleared 或 conversation_unavailable |
| main_http_observation.http_observable | 当前内置主 Agent 是否成功挂接实际 HTTP 观测 |
| main_http_request / main_http_end | 主模型每次实际请求和完成；首个绘图工具之前那次才是写参数请求，生图后的回复请求要分开看 |
| main_http_end.http_metrics.usage.cached_tokens | 上游实际返回的缓存 Token；未提供时不能判断是否命中 |
| request.provider_details.sdk_max_retries=0 | 绘图专用 SDK 副本已关闭隐式重试，主聊天配置不变 |
| http_request / wire | 实际 HTTP 请求开始；记录最终字段、模型、消息角色、工具名、stream、格式约束 |
| wire.tools=[novelai_generate_image] | 正在使用与主模型同名的绘图参数工具 |
| wire.response_format_type=json_object | JSON 模式已发送 API 格式约束 |
| request.json_contract_size / json_contract_fingerprint | 本轮 JSON 输出结构说明的大小和指纹；工具模式为空 |
| response.validation_issues | 一次列出的结构错误，带字段路径；供唯一一次格式纠正使用 |
| plan_normalized / normalizations | 本地移除可确定的格式冗余，记录字段和处理方式，不发模型请求、不改变场景 |
| response.format_retry_count | 已发生的格式纠正次数，0 为首次，1 为唯一一次纠正；不是网络重试次数 |
| response.provider_total_ms | 此次绘图所有已完成规划调用的累计耗时；不含图片 API，超时另看 timeout 行 |
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
| request.tool_choice=required | 首次请求与最多一次纠错都要求原生工具调用 |
| selection_repair | 在对象明确时补齐漏填卡 ID，不调用模型；来源在 source 中 |
| image_tool_budget | 只作用于当前图片工具的预算，调用结束后恢复核心原值 |
| image_tool_deferred | 仅预算将尽或核心钩子不可用的兜底；继续同一个任务，不新建绘图请求 |
| image_end.delivery_state / deferred | 图片发送状态、是否延续执行；失败和取消也会有终态记录 |

响应头很快、内容很晚：继续查模型/中转生成过程。响应头也很晚：结合 HTTP 错误和上游请求 ID 查连接池、网络或中转等待。正文持续增长而结束很晚：查多余输出/显式推理。http_attempts 大于 1：先找格式纠正或 AstrBot 回退行。仅这些客户端日志不能直接测出服务内部排队、隐藏推理或中转内部重试。
