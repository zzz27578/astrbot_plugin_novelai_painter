# 2.10.3：独立绘图调用修复与 503 重试

## 本轮发现

用户日志的 n 两轮 Provider 等待 68.17 / 59.72 秒，本地准备 5–6 毫秒、校验 3 毫秒；最新 /n 无主回复等待，却有 115.36 秒 Provider 等待，437 字符可见 JSON 报告 6153 输出 Token，均一次解析成功。这些数据证明慢点位于独立调用内，也显示与用户主 Grok 快速直接调用工具存在明显差异；尚不能仅凭 Token 统计认定隐藏推理是全部原因。

代码和实际 AstrBot 4.27.3 源码确认：旧独立路线固定 `Context.llm_generate → Provider.text_chat → stream=False`，而主 Agent 可以调用 `text_chat_stream`。同时 OpenAI `_prepare_chat_payload` 不会将任意 kwargs 自动转成模型参数，直接添加 reasoning_effort/max_tokens 可能被忽略。本次针对这两处接口差异修复，速度改善需真实中转验证。

## 调用修复

- 增加独立的 transport 层，n 和 /n 共用。自动模式下 OpenAI 兼容 Provider 使用公开 `text_chat_stream`，其他 Provider 保持原非流式调用；提供流式/非流式选择做兼容和速度对照。输出协议仍按原配置选择 JSON 或参数工具，不擅自迁移用户 JSON 设置。
- 全程消费到 SDK 最终结果，再做原有格式与人物校验；不从半截 JSON 提前生图。正确处理 AstrBot 复用的可变 chunk 对象、最终工具参数和 usage；取消/超时关闭迭代器。缺失最终结果明确报错，不静默回退旧路径再花一次时间。
- 新增绘图专用 reasoning_effort（继承/低/中/高）、max_completion_tokens（0 继承）配置。只对 OpenAI 兼容 Provider 的请求局部配置副本覆盖 custom_extra_body，让参数实际进入适配器的请求构造。主聊天 Provider 配置不变；不拦截或替换其 HTTP 客户端。
- 截断不会自动按相同预算再跑一遍规划；告知调整预算/推理强度。正常格式错误仍最多纠正一次，所有调用继续共用原规划超时预算。
- 保留 n 的回复后时序、用户原话、完整主回复、同一会话上下文及图片开关，不通过丢弃这些信息换取速度。
- 新日志记录实际 transport、请求覆盖参数、首段 SDK 可见文字/推理时间、累计长度、最终结果时间、原始正文/推理字段长度和用量来源。主模型记录配置的流式状态。工具增量可能被 AstrBot 留到最终结果才交付，不据此伪造 HTTP 首 Token 时间。
- 修复图片后端类型 openai_compatible/novelai_official 被诊断函数误当聊天 Provider ID 查询造成的告警；真实不存在的绘图 LLM ID 仍正常报错。

## 图片服务 503 重试

- 新配置 `retry_503_count=0`，允许额外 0–3 次，默认关闭。只对明确 HTTP 503 重试，不重跑绘图规划。
- 429 额度与 503 额度分别计数，总请求上限为首次 1 次 + 两种错误的重试额度，最大 6 次。原 429 模式及其隐藏次数限制继续生效。
- 共用基础等待（1–300 秒），遵守更长 Retry-After；等待时释放通道。保留任务去重、一次发图、对话失效检查，成功立即停止。网络错误、超时、其他 5xx 不自动重试。
- 逐次记录错误码、是否重试、等待、下一次与最多次数、同一 job_id；WebUI 支持次数保存/回显、等待开关和总次数状态。提示 503 重发可能重复计费。

## 验证

实际 AstrBot 4.27.3 隔离环境通过 **176 项测试**。新增 15 项覆盖 503 关闭/上限/混合额度/通道释放/失效取消/一次规划一次发送、误报警回归、配置局部覆盖、可变流片段/最终结果、断流与超时、截断不重跑、n 与 /n 共享流式路径。

使用真实 OpenAI Provider 验证 JSON 和原生参数工具的流式请求；另通过真实 OpenAI SDK + 实际 AstrBot payload builder + httpx MockTransport 检查序列化 HTTP 请求体中的 stream、reasoning_effort、max_completion_tokens 与 SSE 回包解析。无付费模型或图片服务调用。完整对照步骤见 [DIAGNOSTIC_TEST_GUIDE.md](DIAGNOSTIC_TEST_GUIDE.md)。

WebUI 模拟 Bridge 验证三种调用方式、推理强度/输出预算/503 次数的保存回显、503 开关控制等待字段、总尝试次数，以及桌面和 390px 页面无溢出/脚本错误。Python 编译、JavaScript 语法、配置与默认值、版本和差异空白检查通过。
