"""Read-only, bounded metadata for comparing chat and drawing Provider calls.

No HTTP interception: timing at these hooks includes SDK work and upstream wait.
Never serialize an entire Provider, configuration, response, or request here.
"""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit


def field(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def fingerprint(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def text_size(text):
    return {"chars": len(text), "utf8_bytes": len(text.encode("utf-8"))}


def console_entry(detail):
    """Keep full snapshots in memory; print one small waiting/progress event."""
    phase = detail.get("phase", "")
    row = {k: v for k, v in detail.items() if k not in {"timeline", "snapshots"}
           and not (k == "response_preview" and not v)}
    title, level = "绘图事件", "info"
    if phase.startswith("main_http_"):
        title = "主模型 HTTP"
        if phase not in {"main_http_request", "main_http_end", "main_http_error", "main_http_observation"}:
            level = "debug"
    elif phase.startswith("main_") or phase == "duplicate_tool":
        title = "主模型绘图"
    elif phase.startswith("image_"):
        title = "图片任务"
    elif phase == "http_request":
        title = "绘图 HTTP 请求"
    elif phase in {"http_end", "http_error"}:
        title = "绘图 HTTP 结果"
        if phase == "http_end" and isinstance(row.get("http_metrics"), dict):
            row["http_metrics"] = {k: v for k, v in row["http_metrics"].items() if k != "partial_response_preview"}
    elif phase.startswith("http_") or phase in {"first_chunk", "first_text", "first_reasoning", "stream_end"}:
        title, level = "绘图传输细节", "debug"
    elif phase == "provider_wait":
        title = "绘图等待进度"
        row = {k: row[k] for k in ("provider", "protocol", "request_id", "attempt", "phase", "status", "elapsed_ms", "remaining_seconds") if k in row}
        http = detail.get("http_metrics", {})
        row.update({k: http[k] for k in ("http_attempts", "headers_received", "body_bytes", "content_chars", "tool_arguments_chars") if k in http})
        row["sends_request"] = False
    elif phase == "request":
        title = "绘图调用参数"
    elif phase == "received":
        title = "绘图模型回包"
        row.pop("response_preview", None)  # Printed once at validation.
    elif phase == "response":
        title = "绘图参数校验"
    elif phase == "selection_repair":
        title = "绘图选卡"
    elif phase == "plan_normalized":
        title = "绘图格式归一"
    elif phase == "failed":
        title = "绘图失败"
    return title, level, row


def json_text(value):
    # Only used for known request fields/schema, never for arbitrary SDK objects.
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def provider_details(provider):
    if provider is None:
        return {"available": False}
    config = getattr(provider, "provider_config", {})
    config = config if isinstance(config, dict) else {}
    client = getattr(provider, "client", None)
    result = {"adapter": type(provider).__name__}
    for key in ("id", "type", "model"):
        value = config.get(key)
        if isinstance(value, str):
            result[key] = value[:160]
    model = getattr(provider, "model_name", None)
    if isinstance(model, str):
        result["model"] = model[:160]
    for key, value in (("provider_timeout", getattr(provider, "timeout", None)),
                       ("sdk_max_retries", getattr(client, "max_retries", None))):
        if isinstance(value, (int, float)):
            result[key] = value
    endpoint = getattr(client, "base_url", None) or config.get("api_base")
    if endpoint:
        try:
            url = urlsplit(str(endpoint))
            # Path may contain a credential; log its fingerprint only. Strip
            # userinfo/query/fragment even from the fingerprint input.
            if url.hostname:
                result["endpoint_host"] = url.hostname[:200]
                result["endpoint_fingerprint"] = fingerprint(f"{url.scheme}://{url.hostname}:{url.port}{url.path}")
        except (ValueError, TypeError):
            result["endpoint_unavailable"] = True
    extras = config.get("custom_extra_body", {})
    if isinstance(extras, dict):
        selected = {}
        for key in ("reasoning_effort", "max_tokens", "max_completion_tokens", "temperature", "top_p", "stream"):
            value = extras.get(key)
            if isinstance(value, (str, int, float, bool)):
                selected[key] = value[:80] if isinstance(value, str) else value
        for key, keys in (("reasoning", ("effort", "max_tokens")), ("thinking", ("type", "budget_tokens"))):
            value = extras.get(key)
            if isinstance(value, dict):
                selected[key] = {k: v[:80] if isinstance(v, str) else v for k, v in value.items()
                                 if k in keys and isinstance(v, (str, int, float, bool))}
        result["configured_extra_body"] = selected
    return result


def response_details(response):
    raw = field(response, "raw_completion")
    result = {"reasoning_chars": len(str(field(response, "reasoning_content", "") or ""))}
    model = field(raw, "model")
    if isinstance(model, str):
        result["returned_model"] = model[:160]
    # Server IDs enable the user to locate the matching gateway request.
    for key in ("id", "_request_id"):
        value = field(raw, key)
        if isinstance(value, str):
            result["response_id" if key == "id" else "server_request_id"] = value[:160]
    choices = field(raw, "choices", [])
    if isinstance(choices, (list, tuple)) and choices:
        message = field(choices[0], "message")
        content = field(message, "content")
        if isinstance(content, str):
            result["raw_content_chars"] = len(content)
        result["raw_reasoning_fields"] = {key: len(value) for key in (
            "reasoning_content", "reasoning", "analysis", "thinking")
            if isinstance(value := field(message, key), str)}
        reason = field(choices[0], "finish_reason")
        if isinstance(reason, str):
            result["finish_reason"] = reason[:80]
    raw_usage = field(raw, "usage")
    usage = raw_usage or field(response, "usage")
    result["usage_source"] = "raw_completion" if raw_usage else "astrbot_aggregate" if usage is not None else "unavailable"
    tokens = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens"):
        value = field(usage, key)
        if isinstance(value, int):
            tokens[key] = value
    # AstrBot TokenUsage (also used for aggregated streaming responses).
    for key, source in (("input_tokens", "input"), ("output_tokens", "output"),
                        ("cached_tokens", "input_cached"), ("total_tokens", "total")):
        value = field(usage, source)
        if isinstance(value, int):
            tokens.setdefault(key, value)
    for group, keys in (("prompt_tokens_details", ("cached_tokens",)),
                        ("completion_tokens_details", ("reasoning_tokens",))):
        for key in keys:
            value = field(field(usage, group), key)
            if isinstance(value, int):
                tokens[key] = value
    result["usage"] = tokens  # Empty means unavailable, never zero tokens.
    arguments = field(response, "tools_call_args", []) or []
    if isinstance(arguments, (list, tuple)):
        result["tool_arguments_chars"] = sum(len(a) if isinstance(a, str) else len(json_text(a))
                                             for a in arguments if isinstance(a, (str, dict)))
    return result


def error_details(exc):
    result = {"exception_type": type(exc).__name__}
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        result["http_status"] = status
    request_id = getattr(exc, "request_id", None)
    if isinstance(request_id, str):
        result["server_request_id"] = request_id[:160]
    return result


def secrets_from(provider):
    config = getattr(provider, "provider_config", {})
    config = config if isinstance(config, dict) else {}
    result = []
    for value in [config.get(k) for k in ("key", "api_key", "api_keys", "api_token")] + [
            getattr(provider, "api_keys", None), getattr(provider, "api_key", None)]:
        for item in value if isinstance(value, (list, tuple)) else [value]:
            if isinstance(item, str) and len(item) >= 6:
                result.append(item)
    return result


def sanitize(value, secrets=()):
    if isinstance(value, str):
        for secret in sorted(set(secrets), key=len, reverse=True):
            if secret:
                value = value.replace(secret, "[redacted]")
        value = re.sub(r"https?://[^\s\"'<>]+", "[url redacted]", value)
        return re.sub(r"(?i)(bearer\s+|sk-)[A-Za-z0-9_.-]+", "[redacted]", value)
    if isinstance(value, dict):
        return {k: sanitize(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v, secrets) for v in value]
    return value
