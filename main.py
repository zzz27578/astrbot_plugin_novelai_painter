import io
import os
import time
import json
import zipfile
import asyncio
import aiohttp
from typing import Optional

from astrbot.api.star import register, Star, Context
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api import logger

TEMP_DIR = "/AstrBot/data/temp/novelai"
os.makedirs(TEMP_DIR, exist_ok=True)

@register("astrbot_plugin_novelai_painter", "小莫", "NovelAI 专用生图画师插件", "1.1.0")
class NovelAIPainterPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.lock = asyncio.Lock()

    def _get_active_model(self) -> str:
        custom = str(self.config.get("custom_model", "") or "").strip()
        if custom:
            return custom
        model = str(self.config.get("model", "nai-diffusion-5-full") or "nai-diffusion-5-full").strip()
        if model == "custom":
            return "nai-diffusion-5-full"
        return model

    def _clean_expired_temp_files(self, max_age: int = 300):
        """巡检清理超过 max_age 秒的残留孤儿临时图片"""
        try:
            now = time.time()
            if os.path.exists(TEMP_DIR):
                for name in os.listdir(TEMP_DIR):
                    p = os.path.join(TEMP_DIR, name)
                    if os.path.isfile(p) and (now - os.path.getmtime(p) > max_age):
                        try:
                            os.remove(p)
                            logger.info(f"[NovelAI-Painter] 巡检自动清理过期临时图片: {p}")
                        except Exception:
                            pass
        except Exception as e:
            logger.warning(f"[NovelAI-Painter] 临时目录巡检清理异常: {e}")

    def _schedule_cleanup(self, file_path: str):
        """发送后异步延迟清理图片文件，防止服务器存储堆积"""
        delay = int(self.config.get("auto_clean_delay", 60) or 60)
        async def _clean():
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                    logger.info(f"[NovelAI-Painter] 临时图片已自动清理: {file_path}")
            except Exception as e:
                logger.warning(f"[NovelAI-Painter] 自动清理图片失败 {file_path}: {e}")
        asyncio.create_task(_clean())

    @staticmethod
    def _decode_novelai_zip(body: bytes) -> Optional[bytes]:
        """NovelAI 接口返回 ZIP 文件包，从中解压提取 PNG 图片字节流"""
        if body.startswith(b"PK"):
            try:
                with zipfile.ZipFile(io.BytesIO(body)) as archive:
                    names = [
                        name for name in archive.namelist()
                        if not name.endswith("/") and name.lower().endswith((".png", ".webp", ".jpg", ".jpeg"))
                    ]
                    if names:
                        return archive.read(names[0])
            except Exception as e:
                logger.error(f"[NovelAI-Painter] 解压 ZIP 失败: {e}")
                return None
        return None

    async def _call_novelai_api(self, prompt: str, event: AstrMessageEvent = None) -> Optional[str]:
        """请求 NovelAI 官方绘图接口，集成 429 熔断降级与重试机制"""
        api_token = str(self.config.get("api_token", "") or "").strip()
        if not api_token:
            logger.warning("[NovelAI-Painter] 未配置 api_token，无法发起生图请求")
            if event:
                try:
                    await event.send(MessageChain().message("【系统通告】图片没加载出来：插件尚未配置 NovelAI API Token，请先在后台填写。"))
                except Exception:
                    pass
            return None

        base_url = str(self.config.get("base_url", "") or "https://image.novelai.net").strip().rstrip("/")
        if base_url.endswith("/ai/generate-image"):
            url = base_url
        else:
            url = f"{base_url}/ai/generate-image"

        model = self._get_active_model()
        width = int(self.config.get("width", 832) or 832)
        height = int(self.config.get("height", 1216) or 1216)
        steps = int(self.config.get("steps", 28) or 28)
        scale = float(self.config.get("scale", 5.0) or 5.0)
        sampler = str(self.config.get("sampler", "k_euler_ancestral") or "k_euler_ancestral")
        negative_prompt = str(self.config.get("negative_prompt", "") or "").strip()
        quality_toggle = bool(self.config.get("quality_toggle", True))

        max_retries = int(self.config.get("max_retries", 3) or 3)
        retry_delay = float(self.config.get("retry_delay", 5.0) or 5.0)

        parameters = {
            "params_version": 3,
            "width": width,
            "height": height,
            "scale": scale,
            "sampler": sampler,
            "steps": steps,
            "n_samples": 1,
            "ucPreset": 0,
            "qualityToggle": quality_toggle,
            "negative_prompt": negative_prompt,
        }

        # V4/V4.5/V5 结构参数增强
        if "4" in model or "5" in model:
            parameters.update({
                "use_coords": False,
                "v4_prompt": {
                    "caption": {"base_caption": prompt, "char_captions": []},
                    "use_coords": False,
                    "use_order": True,
                },
                "v4_negative_prompt": {
                    "caption": {"base_caption": negative_prompt, "char_captions": []},
                    "legacy_uc": False,
                },
            })

        auth_header = api_token if api_token.lower().startswith("bearer ") else f"Bearer {api_token}"
        headers = {
            "Authorization": auth_header,
            "Content-Type": "application/json",
            "Accept": "*/*",
        }
        payload = {
            "input": prompt,
            "model": model,
            "action": "generate",
            "parameters": parameters,
        }

        logger.info(f"[NovelAI-Painter] 正在向 {url} 请求生成图片，模型: {model}，尺寸: {width}x{height}")

        async with aiohttp.ClientSession() as session:
            for attempt in range(1, max_retries + 1):
                try:
                    async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=180)) as resp:
                        # 429 频控熔断退避处理
                        if resp.status == 429:
                            body_text = (await resp.read()).decode("utf-8", errors="ignore")[:200]
                            logger.warning(f"[NovelAI-Painter] 触发 429 频控 (第 {attempt}/{max_retries} 次): {body_text}")
                            if attempt < max_retries:
                                wait_sec = int(retry_delay * attempt)
                                if event:
                                    try:
                                        await event.send(MessageChain().message(f"【系统提示】触发频率限制(429)，拼车通道拥挤，正在自动避让并在 {wait_sec} 秒后重试 ({attempt}/{max_retries})..."))
                                    except Exception:
                                        pass
                                await asyncio.sleep(wait_sec)
                                continue
                            else:
                                logger.error(f"[NovelAI-Painter] 429 避让重试超限 ({max_retries})，强制中断")
                                if event:
                                    try:
                                        await event.send(MessageChain().message(f"【系统通告】图片没加载出来：拼车通道频繁触发 429 限制，重试超过最大次数 ({max_retries})，已强制中断。"))
                                    except Exception:
                                        pass
                                return None

                        body = await resp.read()
                        if resp.status not in (200, 201):
                            err_msg = body.decode("utf-8", errors="ignore")[:300]
                            logger.error(f"[NovelAI-Painter] API 请求失败 HTTP {resp.status}: {err_msg}")
                            if event:
                                try:
                                    await event.send(MessageChain().message(f"【系统通告】图片没加载出来：NovelAI 接口返回异常状态 HTTP {resp.status}。"))
                                except Exception:
                                    pass
                            return None

                        img_bytes = self._decode_novelai_zip(body)
                        if not img_bytes:
                            if body.startswith(b"\x89PNG"):
                                img_bytes = body
                            else:
                                logger.error("[NovelAI-Painter] 响应体既不是有效 ZIP 也不是 PNG")
                                if event:
                                    try:
                                        await event.send(MessageChain().message("【系统通告】图片没加载出来：响应数据格式解析失败。"))
                                    except Exception:
                                        pass
                                return None

                        file_path = os.path.join(TEMP_DIR, f"nai_{int(time.time()*1000)}.png")
                        with open(file_path, "wb") as f:
                            f.write(img_bytes)
                        logger.info(f"[NovelAI-Painter] 成功生成并解压图片: {file_path} ({len(img_bytes)//1024} KB)")
                        return file_path

                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    logger.warning(f"[NovelAI-Painter] 网络请求异常 (第 {attempt}/{max_retries} 次): {e}")
                    if attempt < max_retries:
                        wait_sec = int(retry_delay * attempt)
                        if event:
                            try:
                                await event.send(MessageChain().message(f"【系统提示】网络请求超时或波动，正在第 {attempt}/{max_retries} 次重试..."))
                            except Exception:
                                pass
                        await asyncio.sleep(wait_sec)
                        continue
                    else:
                        if event:
                            try:
                                await event.send(MessageChain().message(f"【系统通告】图片没加载出来：网络连接异常且重试超限 ({max_retries})，已中断。"))
                            except Exception:
                                pass
                        return None
                except Exception as e:
                    logger.error(f"[NovelAI-Painter] 未知异常: {e}")
                    if event:
                        try:
                            await event.send(MessageChain().message(f"【系统通告】图片没加载出来：内部处理发生异常 ({e})。"))
                        except Exception:
                            pass
                    return None

        return None

    @filter.llm_tool(name="novelai_generate_image")
    async def novelai_generate_image(self, event: AstrMessageEvent, prompt: str):
        '''当用户希望看到画面、老爸要求配图、或者在对话情境中需要展现具体二次元/视觉场景时调用。生成符合动漫美学的详细英文 Danbooru tags 提示词。注意：此工具在后台直接渲染图片并发送，不要在对用户的最终文字回复中泄露或复述任何英文 tags 提示词。

        Args:
            prompt(string): 必须是高质量的二次元英文 Danbooru 风格提示词（例如 1girl, silver hair, blue eyes, hoodie, masterpiece, highres 等），千万不要填中文。
        '''
        queue_timeout = int(self.config.get("queue_timeout", 120) or 120)
        is_busy = self.lock.locked()
        if is_busy:
            try:
                await event.send(MessageChain().message("【系统提示】NovelAI 通道正忙，当前生图任务已进入排队队列，请稍候..."))
            except Exception:
                pass

        try:
            await asyncio.wait_for(self.lock.acquire(), timeout=queue_timeout)
        except asyncio.TimeoutError:
            try:
                await event.send(MessageChain().message("【系统通告】图片没加载出来：排队等待超时，已强制中断当前生图任务。"))
            except Exception:
                pass
            return "图片没加载出来：排队等待超时，已强制中断。"

        try:
            self._clean_expired_temp_files()
            img_path = await self._call_novelai_api(prompt, event)
            if not img_path:
                return "NovelAI 生图未完成，已向用户发出系统通告。请以自然的低电量傲娇少女语气回复一两句，绝对不要在回复中包含英文 tags 提示词！"

            # 构造纯图片消息链发送给当前会话
            try:
                chain = MessageChain().file_image(img_path)
                await event.send(chain)
                self._schedule_cleanup(img_path)
                return "图片已成功生成并发送至当前会话。请以自然的少女口吻向老爸回复一两句话，严禁在回复中提及或列举提示词(tags)！"
            except Exception as e:
                logger.error(f"[NovelAI-Painter] 发送图片失败: {e}")
                if os.path.exists(img_path):
                    try:
                        os.remove(img_path)
                    except Exception:
                        pass
                return f"生成成功但发送失败: {e}"
        finally:
            if self.lock.locked():
                self.lock.release()

    @filter.command("nai")
    async def cmd_draw(self, event: AstrMessageEvent, *, prompt: str = ""):
        """手动生图命令: /nai <英文提示词或场景描述>"""
        if not prompt.strip():
            yield event.plain_result("提示词呢？用 /nai <英文tags> 让我画，比如 /nai 1girl, cat ears")
            return

        queue_timeout = int(self.config.get("queue_timeout", 120) or 120)
        is_busy = self.lock.locked()
        if is_busy:
            try:
                await event.send(MessageChain().message("【系统提示】NovelAI 通道正忙，当前任务已进入排队队列，请稍候..."))
            except Exception:
                pass

        try:
            await asyncio.wait_for(self.lock.acquire(), timeout=queue_timeout)
        except asyncio.TimeoutError:
            yield event.plain_result("【系统通告】图片没加载出来：生图排队超时，已自动取消当前任务")
            return

        try:
            self._clean_expired_temp_files()
            img_path = await self._call_novelai_api(prompt.strip(), event)
            if not img_path:
                return

            await event.send(MessageChain().file_image(img_path))
            self._schedule_cleanup(img_path)
        finally:
            if self.lock.locked():
                self.lock.release()
