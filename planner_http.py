"""Request-local HTTP observations. Borrow the Provider's connection pool unchanged."""
from __future__ import annotations

import asyncio
import inspect
import json
import time

import httpx
try:
    import httpx2
except ImportError:
    httpx2 = None


class HTTPObservation:
    def __init__(self, record, debug=False, clock=None):
        self.record = record
        self.debug = debug
        self.clock = clock or time.perf_counter
        self.metrics = {"http_attempts": 0}
        self.started = self.clock()
        self.preview = ""

    def elapsed(self):
        return round((self.clock() - self.started) * 1000)

    def emit(self, phase):
        # The event keeps this live counter dictionary for periodic progress;
        # the logger sanitizes/copies it when persisting each phase snapshot.
        self.record(phase, self.metrics)

    def begin(self, request):
        attempts = self.metrics["http_attempts"] + 1
        self.started = self.clock()
        self.preview = ""
        self.metrics = {"http_attempts": attempts, "body_bytes": 0,
                        "content_chars": 0, "reasoning_chars": 0, "tool_arguments_chars": 0}
        try:
            body = json.loads(request.content)
            self.metrics["wire"] = {
                "fields": sorted(body), "model": body.get("model"), "stream": body.get("stream", False),
                "message_roles": [m.get("role") for m in body.get("messages", [])],
                "request_bytes": len(request.content),
                "tools": [t.get("function", {}).get("name") for t in body.get("tools", [])],
                "tool_choice": body.get("tool_choice"),
                "response_format_type": (body.get("response_format") or {}).get("type"),
            }
            self.metrics["wire"].update({key: body[key] for key in
                ("reasoning_effort", "max_tokens", "max_completion_tokens") if key in body})
        except (ValueError, TypeError, AttributeError, httpx.RequestNotRead):
            self.metrics["wire_unavailable"] = True
        self.emit("http_request")

    def accept(self, data, streaming):
        if not isinstance(data, dict):
            return
        self.metrics["response_events"] = self.metrics.get("response_events", 0) + 1
        for key in ("id", "model"):
            if isinstance(data.get(key), str):
                self.metrics["response_" + key] = data[key][:160]
        if isinstance(data.get("usage"), dict):
            usage = data["usage"]
            self.metrics["usage"] = {key: usage[key] for key in
                ("prompt_tokens", "completion_tokens", "total_tokens") if isinstance(usage.get(key), int)}
            for group, key in (("completion_tokens_details", "reasoning_tokens"),
                               ("prompt_tokens_details", "cached_tokens")):
                value = usage.get(group) or {}
                if isinstance(value, dict) and isinstance(value.get(key), int):
                    self.metrics["usage"][key] = value[key]
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return
        choice = choices[0]
        if isinstance(choice.get("finish_reason"), str):
            self.metrics["finish_reason"] = choice["finish_reason"][:80]
        message = choice.get("delta" if streaming else "message") or {}
        if not isinstance(message, dict):
            return
        reasoning = next((message[key] for key in ("reasoning_content", "reasoning", "analysis", "thinking")
                          if isinstance(message.get(key), str)), "")
        content = message.get("content")
        content = content if isinstance(content, str) else ""
        calls = message.get("tool_calls") or []
        arguments = "".join(call.get("function", {}).get("arguments", "")
                            for call in calls if isinstance(call, dict)
                            and isinstance(call.get("function"), dict)
                            and isinstance(call["function"].get("arguments"), str)) if isinstance(calls, list) else ""
        for value, key in ((content, "content"), (reasoning, "reasoning"), (arguments, "tool_arguments")):
            self.metrics[key + "_chars"] += len(value)
            if value and "first_" + key + "_ms" not in self.metrics:
                self.metrics["first_" + key + "_ms"] = self.elapsed()
                self.emit("http_first_" + key)
        # Never log reasoning text, credentials, full bodies or images. A bounded
        # drawing-output preview is opt-in, including for cancelled streams.
        if self.debug and len(self.preview) < 2000:
            self.preview += (content + arguments)[:2000 - len(self.preview)]

    def finish(self, outcome):
        self.metrics["body_elapsed_ms"] = self.elapsed()
        self.metrics["outcome"] = outcome
        if self.debug:
            self.metrics["partial_response_preview"] = self.preview
        self.emit("http_end")


class ObservedStream(httpx.AsyncByteStream):
    def __init__(self, source, observation, streaming, encoding=""):
        self.source, self.observation, self.streaming = source, observation, streaming
        # OpenAI may iterate response.stream again to drain bytes after [DONE].
        # Resume one transport iterator rather than replaying buffered chunks.
        self.iterator = source.__aiter__()
        self.buffer = b""
        self.finished = False
        self.overflow = False
        # Use a separate decoder so observing compressed responses never consumes
        # the decoder belonging to the response that the SDK will read.
        self.decoder = httpx.Response(200, headers={"Content-Encoding": encoding})._get_content_decoder()

    def parse(self, value):
        try:
            self.observation.accept(json.loads(value), self.streaming)
        except (ValueError, UnicodeError):
            pass

    def accept_bytes(self, chunk):
        self.buffer += chunk
        if self.streaming:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                if line.startswith(b"data:") and len(line) <= 131072:
                    self.parse(line[5:].strip())
        if len(self.buffer) > 131072:
            self.overflow = True
            self.buffer = b""
            self.observation.metrics["preview_truncated"] = True

    async def __aiter__(self):
        obs = self.observation
        try:
            async for chunk in self.iterator:
                if chunk:
                    if "first_body_ms" not in obs.metrics:
                        obs.metrics["first_body_ms"] = obs.elapsed()
                        obs.emit("http_first_body")
                    obs.metrics["body_bytes"] += len(chunk)
                    self.accept_bytes(self.decoder.decode(chunk))
                yield chunk
            self.accept_bytes(self.decoder.flush())
            if not self.overflow and self.buffer.strip():
                self.parse(self.buffer[5:].strip() if self.streaming and self.buffer.startswith(b"data:") else self.buffer)
            if not self.finished:
                self.finished = True
                obs.finish("complete")
        except BaseException as exc:
            if not self.finished:
                obs.metrics["body_exception_type"] = type(exc).__name__
                self.finished = True
                obs.finish("cancelled" if isinstance(exc, asyncio.CancelledError) else "error")
            raise

    async def aclose(self):
        try:
            await self.source.aclose()
        finally:
            if not self.finished:
                self.finished = True
                self.observation.finish("closed_before_end")


class ObservedHTTPClient(httpx.AsyncClient):
    """SDK-compatible borrowed client; no hooks, transport or settings are mutated.

    Only build_request/send are used by OpenAI's SDK. All network operations
    still use the configured AstrBot client, including proxies and connection
    limits. Closing a request-local SDK copy must not close that shared pool.
    """
    def __init__(self, delegate, observation):
        self.delegate, self.observation = delegate, observation

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    @property
    def is_closed(self):
        return self.delegate.is_closed

    def build_request(self, *args, **kwargs):
        return self.delegate.build_request(*args, **kwargs)

    async def send(self, request, *, stream=False, **kwargs):
        obs = self.observation
        obs.begin(request)
        original_trace = request.extensions.get("trace")
        async def trace(name, info):
            # httpcore exposes connection/TLS/header phases through this
            # request-local extension. Do not log its info (URLs/headers).
            operation = name.split(".")[-2:]
            if len(operation) == 2 and operation[0] in {
                    "connect_tcp", "connect_unix_socket", "start_tls", "send_request_headers", "receive_response_headers"}:
                events = obs.metrics.setdefault("network_events", [])
                if len(events) < 32:
                    events.append({"event": name, "elapsed_ms": obs.elapsed()})
            if callable(original_trace):
                result = original_trace(name, info)
                if inspect.isawaitable(result):
                    await result
        request.extensions["trace"] = trace
        try:
            response = await self.delegate.send(request, stream=True, **kwargs)
        except BaseException as exc:
            obs.metrics.update(exception_type=type(exc).__name__, headers_received=False,
                               elapsed_ms=obs.elapsed())
            obs.emit("http_error")
            raise
        obs.metrics.update(http_status=response.status_code, headers_received=True, headers_ms=obs.elapsed())
        for key in ("x-request-id", "request-id", "x-correlation-id"):
            if response.headers.get(key):
                obs.metrics["server_request_id"] = response.headers[key][:160]
                break
        obs.emit("http_headers")
        is_sse = "text/event-stream" in response.headers.get("content-type", "").lower()
        # MockTransport may return a response whose content is already buffered.
        if response.is_stream_consumed:
            obs.metrics["body_bytes"] = len(response.content)
            obs.metrics["first_body_ms"] = obs.elapsed()
            obs.metrics["body_already_buffered"] = True
            if is_sse:
                for line in response.content.splitlines():
                    if line.startswith(b"data:") and len(line) <= 131072:
                        try:
                            obs.accept(json.loads(line[5:].strip()), True)
                        except (ValueError, UnicodeError):
                            pass
            else:
                try:
                    obs.accept(response.json(), False)
                except (ValueError, UnicodeError):
                    pass
            obs.finish("buffered")
        else:
            wrapper = ObservedHTTPX2Stream if httpx2 and isinstance(response, httpx2.Response) else ObservedStream
            response.stream = wrapper(response.stream, obs, is_sse, response.headers.get("content-encoding", ""))
        if not stream:
            try:
                await response.aread()
            finally:
                await response.aclose()
        return response

    async def aclose(self):
        # The original Provider owns and closes the underlying HTTP client.
        pass


if httpx2:
    # New OpenAI SDKs use httpx2; older AstrBot environments use httpx. Keep
    # each response's AsyncByteStream type compatible with its own HTTP stack.
    class ObservedHTTPX2Stream(ObservedStream, httpx2.AsyncByteStream):
        pass

    class ObservedHTTPX2Client(ObservedHTTPClient, httpx2.AsyncClient):
        pass


def borrowed_client(client, observation):
    wrapper = ObservedHTTPX2Client if httpx2 and isinstance(client, httpx2.AsyncClient) else ObservedHTTPClient
    return wrapper(client, observation)
