"""Request-local planning transport; never change a shared Provider's settings."""
from __future__ import annotations

import copy
import time
from contextlib import aclosing

if __package__:
    from . import planner
else:
    import planner


def is_openai(provider):
    config = getattr(provider, "provider_config", {})
    return isinstance(config, dict) and "openai" in str(config.get("type", "")).lower()


def select(provider, transport):
    supported = callable(getattr(provider, "text_chat_stream", None))
    if transport == "stream":
        if not supported:
            raise planner.PlanError("该 Provider 没有流式接口，请选择非流式调用。", "transport", False)
        return "stream"
    return "stream" if transport == "auto" and is_openai(provider) and supported else "nonstream"


def request_provider(provider, effort, max_tokens):
    overrides = {}
    if effort != "inherit":
        overrides["reasoning_effort"] = effort
    if max_tokens:
        overrides["max_completion_tokens"] = max_tokens
    if not overrides:
        return provider, overrides
    if not is_openai(provider):
        raise planner.PlanError("绘图推理强度和输出预算覆盖仅支持 OpenAI 兼容 Provider；请选择继承/0。", "transport", False)
    # Arbitrary kwargs are discarded by AstrBot 4.27.3's OpenAI payload
    # builder. A local config copy makes overrides reach custom_extra_body in
    # both _query and _query_stream, without modifying the main chat Provider.
    local = copy.copy(provider)
    local.provider_config = dict(provider.provider_config)
    base = local.provider_config.get("custom_extra_body", {})
    extra = dict(base) if isinstance(base, dict) else {}
    if max_tokens:
        extra.pop("max_tokens", None)
        extra.pop("max_completion_tokens", None)
    extra.update(overrides)
    local.provider_config["custom_extra_body"] = extra
    return local, overrides


async def collect_stream(provider, kwargs, record, clock=None):
    """Consume to a final SDK response, never generate from partial JSON.

    AstrBot reuses a mutable chunk response object, so read lengths immediately
    and use its separate non-chunk final response as the authoritative result.
    """
    now = clock or time.perf_counter
    started = now()
    metrics = {"chunks": 0, "streamed_text_chars": 0, "streamed_reasoning_chars": 0}
    final = None
    async with aclosing(provider.text_chat_stream(**kwargs)) as stream:
        async for response in stream:
            text = str(getattr(response, "completion_text", "") or "")
            reasoning = str(getattr(response, "reasoning_content", "") or "")
            elapsed = round((now() - started) * 1000)
            if getattr(response, "is_chunk", False):
                if "first_chunk_ms" not in metrics:
                    metrics["first_chunk_ms"] = elapsed
                    record("first_chunk", dict(metrics))
                metrics["chunks"] += 1
                metrics["streamed_text_chars"] += len(text)
                metrics["streamed_reasoning_chars"] += len(reasoning)
                for value, key, phase in ((text, "first_text_ms", "first_text"),
                                           (reasoning, "first_reasoning_ms", "first_reasoning")):
                    if value and key not in metrics:
                        metrics[key] = elapsed
                        record(phase, dict(metrics))
            else:
                final = response
                metrics["final_response_ms"] = elapsed
    metrics["stream_total_ms"] = round((now() - started) * 1000)
    record("stream_end", dict(metrics))
    if final is None:
        raise planner.PlanError("流式连接结束但未返回完整最终结果，本次未请求图片服务；可切换非流式检查兼容性。", "incomplete_stream", False)
    return final
