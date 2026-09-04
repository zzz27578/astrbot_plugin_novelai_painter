# astrbot_plugin_novelai_painter

NovelAI 专用生图画师插件，适用于 [AstrBot](https://github.com/AstrBotDevs/AstrBot)。

## 特性

- **最新模型支持**：适配 NovelAI 最新 V5 系列模型（`nai-diffusion-5-full` / `nai-diffusion-5-curated`）及 V4.5、V4、V3 系列，支持手动输入自定义模型 ID。
- **智能 LLM 工具调用**：提供 `@filter.llm_tool`，大模型可在对话情境中自主调用并生成高质量 Danbooru tags 生图。
- **指令快捷生图**：支持 `/nai <tags>` 命令手动生图。
- **排队锁与 429 频控熔断**：内置异步排队锁，防止拼车并发踩踏；遇 429 自动阶梯式避让退避与超时熔断，保护账号安全。
- **ZIP 解包与存储自清理**：自动解析官方 ZIP 数据包并提取原画，发送后定时自动清理临时文件，不积压服务器磁盘。

## 安装

在 AstrBot 控制台插件市场中通过 GitHub 仓库链接安装，或克隆至 `data/plugins/` 目录：

```bash
git clone https://github.com/zzz27578/astrbot_plugin_novelai_painter.git
```

## 配置项说明

在 AstrBot 管理后台插件配置中填写：

| 配置项 | 说明 | 默认值 |
| :--- | :--- | :--- |
| `api_token` | NovelAI Persistent API Token (设置 -> Account 获取) | `""` |
| `base_url` | 生图端点 Base URL（直连或反代端点） | `https://image.novelai.net` |
| `model` | 预设模型选择 | `nai-diffusion-5-full` |
| `custom_model`| 自定义模型 ID | `""` |
| `width` / `height` | 分辨率宽高（标准推荐 832x1216 或 1024x1024） | `832` / `1216` |
| `steps` | 采样迭代步数 | `28` |
| `scale` | 提示词引导强度 (Prompt Guidance) | `5.0` |
| `sampler` | 采样器算法 | `k_euler_ancestral` |
| `negative_prompt` | 全局负面提示词 | 常见质量排除词 |
| `max_retries` | 429 或网络异常最大重试次数 | `3` |
| `auto_clean_delay` | 临时图片发送后自动清理延迟 (秒) | `60` |
