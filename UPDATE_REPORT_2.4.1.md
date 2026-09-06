# NovelAI Painter 2.4.1 修复报告

## 修复内容

2.4.0 中 `novelai_set_emotion` 使用类装饰器注册后，工具默认会进入 AstrBot 的全局工具管理器。即使表情包总开关关闭，工具仍可能出现在 Agent 的工具描述中，LLM 便会尝试调用它，日志中表现为：

```text
Agent 使用工具: ['novelai_set_emotion', ...]
Tool novelai_set_emotion Result: sticker metadata disabled
```

2.4.1 在插件启动和 WebUI 保存配置成功后，直接同步该工具的运行时 `active` 状态：

- 表情包总开关关闭：工具不活跃，不进入后续 Agent 工具列表。
- 表情包总开关开启且允许情绪标签：工具恢复活跃。
- “允许 LLM 提供情绪标签”关闭：工具保持不活跃。

同步使用运行时状态，不调用 AstrBot 的全局停用 API，因此不会把用户的表情包配置误写成 Dashboard 的永久工具停用偏好。

## 验证结果

- Python 单元测试：46 项通过。
- 新增回归测试覆盖总开关关闭、开启和情绪标签开关关闭三种工具状态。
- Python 语法、JavaScript 语法、配置 Schema 和 `git diff --check` 均通过。

如果在总开关关闭前已经开始了一轮 Agent 工具调用，该轮请求可能仍持有旧的工具列表；配置保存后的新一轮请求会使用同步后的工具状态。
