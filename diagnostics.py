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
        reason = field(choices[0], "finish_reason")
        if isinstance(reason, str):
            result["finish_reason"] = reason[:80]
    usage = field(raw, "usage") or field(response, "usage")
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
