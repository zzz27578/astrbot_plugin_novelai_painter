# 2.10.4：独立绘图请求重整与 HTTP 观测

## 问题与判断边界

用户提供的 18:36–18:45 日志中，两次 /n 在 120 秒预算内未得到完整结果；第一次无历史、无图片，本地准备仅 4 毫秒，110 秒后才收到首段可见文字。相同 Provider 的主 Agent 两次在约 83 / 43.5 秒后提交图片工具参数，图片服务仅 16.64 / 13.49 秒。2.10.3 的流式调整没有消除差距。

本次针对请求结构、输出契约和隐藏重试尝试修复。不能从这些日志认定全部延迟来自某一种原因；真实速度须用用户的 Grok 中转复测，没有通过延长超时或付费服务调用证明提速。

## 修复

- n 与 /n 继续共用独立规划器；n 等本轮完整回复发送后启动，/n 不调用主聊天模型。
- 历史以原生 user/assistant 消息传入，本轮要求直接作为最后一条用户消息。n 的本轮用户要求和完整助手回复保持顺序；角色目录、上一张方案及用户原文保留在标记清楚的辅助素材消息。避免把全部对话编码进一个 JSON。
- 精简独立绘图技能，保留选卡、服饰、原始权重、画风、上一张指代及拒绝处理规则。卡身份和画风仍由后端无损合成；规划结果仅作用于本图。
- 工具模式使用请求内的 novelai_generate_image，复用主绘图工具 prompt/subjects/style 的字段定义，tool_choice=auto 与主 Agent 一致。只收参数，不注册全局工具或运行聊天工具循环；通过校验后出图。兼容旧 submit_drawing_plan 回包。
- 显式 JSON 模式对 OpenAI Chat Completions 兼容 Provider 发送 response_format=json_object。接口不支持时明确报错，不静默降级再请求；其他 Provider 仍按技能返回 JSON。
- 自动调用方式跟随主聊天的 streaming_response；无法取得设置时，OpenAI 兼容接口沿用流式回退。显式流式/非流式设置继续生效，原 JSON 设置也保留。
- 对 OpenAI 兼容绘图请求复制 SDK 客户端，max_retries=0，阻止 SDK 隐式重试；沿用 Provider 的原 HTTP 客户端、代理和连接池，不修改主模型客户端或 api_key。AstrBot 的 Key/兼容性回退仍可能发起额外 HTTP 请求，所有实际 send 均计数。格式纠正仍至多一次，共用原超时预算；图片 503 重试沿用 2.10.3 配置。

## 新增观测

局部 HTTP 包装器记录 http_request、http_headers、http_first_body、http_first_content、http_first_reasoning、http_first_tool_arguments、http_end、http_error。支持 httpx/httpx2、流式/非流式与压缩响应；借用连接池，关闭局部副本不关闭主客户端。不改共享 hook 或共享 transport。

- wire：实际序列化字段、模型、stream、消息角色、请求字节数、工具名称、格式约束与允许记录的推理/预算参数，不记录请求正文和图片。
- http_attempts：一次规划周期内实际进入 HTTP send 的累计次数。格式纠正会增加，Key/兼容性回退也会增加；不包含中转内部行为。
- headers_ms：send 到收到响应头，包含本地连接池、网络和上游等待；不是纯上游计算时间。
- first_body_ms / first_content_ms / first_tool_arguments_ms：原始响应字节和可解析内容出现的时间。非流式服务可能直到生成完成才发送正文；不能据此推断其首 Token。
- network_events：若底层支持 httpcore trace，记录连接、TLS、请求头发送和响应头接收的开始/完成/失败时点，不记录 trace 中的地址和头信息。连接复用时没有新建连接事件是正常的。
- body_bytes、content_chars、reasoning_chars、tool_arguments_chars、usage、http_status、server_request_id、outcome：输出进展与服务结果。压缩正文单独解码观察，不干扰 SDK 读取。
- 超时保留已收到的 HTTP 指标；回包预览开关开启时额外保留最多 2000 字符绘图正文/工具参数片段。始终不记录推理正文。

## 验证

在 AstrBot 4.27.3 隔离环境通过 **185 项完整测试**，包含真实 OpenAI SDK、实际 Provider payload 构造和 MockTransport。验证原生消息顺序、共享工具字段、JSON 约束、主聊天流式跟随、SDK 重试关闭、主客户端鉴权/配置不变、响应头和正文阶段超时、流取消与局部资源释放、工具增量、httpx/httpx2 及 gzip 回包。既有选卡、会话失效、503 重试和一次发图回归继续通过。模型和图片服务使用模拟 HTTP；另启动本机 HTTP 服务器验证真实连接及请求头/响应头 trace，没有调用付费模型或图片 API。配置页面保存回显、全部协议、诊断展示、桌面与手机布局及页面脚本检查通过；Python 语法、配置默认值和版本一致性通过。

最小用户测试见 [DIAGNOSTIC_TEST_GUIDE.md](DIAGNOSTIC_TEST_GUIDE.md)。
