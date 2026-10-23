"""Small synchronous OpenAI-compatible HTTP transport (standard library only)."""
from __future__ import annotations

import http.client
import json
import math
import os
import queue
import re
import socket
import ssl
import threading
import time
from collections.abc import Callable
from typing import NoReturn
from urllib.parse import urlsplit
from .cancellation import check_cancel

MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_SSE_LINE_BYTES = 256 * 1024
MAX_SSE_EVENT_BYTES = 256 * 1024
MAX_TOOL_CALLS = 16
MAX_TOOL_ARGUMENT_BYTES = 64 * 1024


class ModelError(Exception):
    """Safe-to-display model configuration, network, or protocol failure."""


def _invalid() -> NoReturn:
    raise ModelError("Invalid model response format.")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _invalid()
        result[key] = value
    return result


def _json(value):
    try:
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        result = json.loads(value, object_pairs_hook=_pairs, parse_constant=lambda _: _invalid())
        pending = [result]
        while pending:
            item = pending.pop()
            if isinstance(item, str):
                item.encode("utf-8")  # Reject unpaired JSON surrogate escapes.
            elif isinstance(item, float) and not math.isfinite(item):
                _invalid()
            elif isinstance(item, list):
                pending.extend(item)
            elif isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
        return result
    except (ValueError, TypeError, RecursionError):
        raise ModelError("Invalid model JSON response.") from None


def _choice(obj, allow_usage=False):
    if not isinstance(obj, dict) or "error" in obj:
        _invalid()
    choices = obj.get("choices")
    if allow_usage and choices == [] and isinstance(obj.get("usage"), dict):
        return None
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        _invalid()
    choice = choices[0]
    if type(choice.get("index", 0)) is not int or choice.get("index", 0) != 0:
        _invalid()
    return choice


def _message(message, finish):
    if not isinstance(message, dict) or message.get("role") != "assistant" or "function_call" in message:
        _invalid()
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        _invalid()
    calls = message.get("tool_calls", [])
    if not isinstance(calls, list) or len(calls) > MAX_TOOL_CALLS:
        _invalid()
    if finish == "length":
        raise ModelError("Model reported a generation/context output limit (finish_reason=length). "
                         "Review model.max_tokens and the server context limit; incomplete tools were not executed.")
    if finish != ("tool_calls" if calls else "stop"):
        raise ModelError("Model response has a missing, unsuccessful, or mismatched finish_reason; "
                         "incomplete tools were not executed.")
    result = {"role": "assistant", "content": content}
    cleaned = []
    ids = set()
    for call in calls:
        if not isinstance(call, dict) or call.get("type") != "function":
            _invalid()
        ident = call.get("id")
        function = call.get("function")
        if not isinstance(ident, str) or not ident or len(ident) > 256 or ident in ids:
            _invalid()
        ids.add(ident)
        if not isinstance(function, dict):
            _invalid()
        name, args = function.get("name"), function.get("arguments")
        if not isinstance(name, str) or not name or len(name) > 128:
            _invalid()
        if not isinstance(args, str) or len(args.encode("utf-8")) > MAX_TOOL_ARGUMENT_BYTES:
            _invalid()
        if not isinstance(_json(args), dict):
            _invalid()
        cleaned.append({"id": ident, "type": "function", "function": {"name": name, "arguments": args}})
    if cleaned:
        result["tool_calls"] = cleaned
    return result


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ModelError("Model request deadline exceeded.")
    return remaining


def _connect(host, port, deadline, active_socket, context, cancel_event=None):
    # Resolver APIs have no portable timeout. A daemon resolves only (never
    # connects or sends a request); the caller waits within its total deadline.
    resolved = queue.Queue(maxsize=1)

    def resolve():
        try:
            resolved.put(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except OSError:
            resolved.put(None)

    threading.Thread(target=resolve, daemon=True, name="david-dns").start()
    while True:
        check_cancel(cancel_event)
        try:
            addresses = resolved.get(timeout=min(0.05, _remaining(deadline)))
            break
        except queue.Empty:
            continue
    check_cancel(cancel_event)
    if not addresses:
        raise ModelError("Model host resolution failed.")
    for family, kind, proto, _, address in addresses:
        check_cancel(cancel_event)
        sock = socket.socket(family, kind, proto)
        active_socket[0] = sock
        try:
            check_cancel(cancel_event)
            sock.settimeout(_remaining(deadline))
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.connect(address)
            check_cancel(cancel_event)
            if context is not None:
                sock = context.wrap_socket(sock, server_hostname=host, do_handshake_on_connect=False)
                active_socket[0] = sock
                # wrap_socket detaches the original FD before returning. A
                # cancellation monitor may have seen only that stale socket.
                check_cancel(cancel_event)
                sock.settimeout(_remaining(deadline))
                sock.do_handshake()
            check_cancel(cancel_event)
            _remaining(deadline)
            return sock
        except ssl.SSLError:
            sock.close()
            check_cancel(cancel_event)
            raise
        except OSError:
            sock.close()
            check_cancel(cancel_event)
            _remaining(deadline)
        except BaseException:
            sock.close()
            raise
    raise ModelError("Model connection failed.")


class OpenAIClient:
    """One configured endpoint; no discovery, redirects, retries, or env proxies.

    ``last_metrics`` reports client-observed elapsed_seconds and
    first_text_seconds (None if no nonempty content arrived). These include
    network/client overhead and are NOT server TTFT or token-rate estimates.
    Instances are synchronous and not intended for concurrent complete calls.

    UTF-8 request bodies are capped at max_request_bytes (default 2 MiB, max
    64 MiB); response bodies at max_response_bytes
    (default 2 MiB). SSE lines/events are each capped at 256 KiB. At most 16
    function calls are returned, with 64 KiB UTF-8 arguments per call. Arguments
    must be complete JSON objects, IDs unique, and stream indices contiguous.
    Streaming requires both a successful finish_reason and a framed [DONE].
    on_text previews may precede an error; tools are returned only on success.
    timeout_seconds bounds network activity, including trickling headers/body
    and waiting for DNS. A timed-out OS resolver can finish in a daemon thread
    but cannot connect or issue HTTP requests. Callbacks must not block.
    """
    def __init__(self, model_config: dict, *, cancel_event=None):
        if not isinstance(model_config, dict):
            raise ModelError("Invalid model configuration.")
        self.config = dict(model_config)
        self.cancel_event = cancel_event
        self.last_metrics = {"elapsed_seconds": 0.0, "first_text_seconds": None}
        self.max_request_bytes = self.config.get("max_request_bytes", MAX_REQUEST_BYTES)
        self.max_response_bytes = self.config.get("max_response_bytes", 2 * 1024 * 1024)
        self._validate_config()
        try:
            raw = self.config["base_url"]
            if not isinstance(raw, str) or any(ord(c) <= 32 or ord(c) >= 127 for c in raw):
                raise ValueError
            self.url = urlsplit(raw)
            if (self.url.scheme not in ("http", "https") or not self.url.hostname
                    or self.url.username is not None or self.url.password is not None
                    or "?" in raw or "#" in raw or "\\" in raw
                    or (self.url.port is not None and not 1 <= self.url.port <= 65535)):
                raise ValueError
            if (self.url.scheme == "http"
                    and self.url.hostname not in ("localhost", "127.0.0.1", "::1")
                    and self.config.get("allow_insecure_http") is not True):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ModelError("Invalid or unsafe model base URL.") from None

    def _validate_config(self):
        if type(self.max_request_bytes) is not int or not 1024 <= self.max_request_bytes <= 64 * 1024 * 1024:
            raise ModelError("Invalid model.max_request_bytes: expected 1024..67108864 bytes.")

        for name, default in (("stream", True), ("allow_insecure_http", False)):
            if type(self.config.get(name, default)) is not bool:
                raise ModelError("Invalid model configuration.")
        for name, default in (("max_tokens", 2048), ("max_response_bytes", 2 * 1024 * 1024)):
            value = self.config.get(name, default)
            if type(value) is not int or value <= 0:
                raise ModelError("Invalid model size configuration.")
        for name, default in (("timeout_seconds", 60), ("temperature", 0.2)):
            value = self.config.get(name, default)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ModelError("Invalid model numeric configuration.")
            if (name == "timeout_seconds" and value <= 0) or (name == "temperature" and not 0 <= value <= 2):
                raise ModelError("Invalid model numeric configuration.")
        name = self.config.get("name")
        key_env = self.config.get("api_key_env", "")
        ca_file = self.config.get("ca_file", "")
        if not isinstance(name, str) or not name.strip():
            raise ModelError("Invalid model name.")
        if not isinstance(key_env, str) or (key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env)):
            raise ModelError("Invalid model key environment variable.")
        if not isinstance(ca_file, str) or "\x00" in ca_file:
            raise ModelError("Invalid model CA file configuration.")

    def complete(self, messages: list, tools: list | None = None,
                 on_text: Callable[[str], None] | None = None) -> dict:
        check_cancel(self.cancel_event)
        start = time.monotonic()
        self.last_metrics = {"elapsed_seconds": 0.0, "first_text_seconds": None}

        def emit(text):
            if self.last_metrics["first_text_seconds"] is None:
                self.last_metrics["first_text_seconds"] = time.monotonic() - start
            if on_text is not None:
                on_text(text)

        try:
            return self._complete(messages, tools, emit, start + self.config.get("timeout_seconds", 60))
        except Exception:
            check_cancel(self.cancel_event)
            raise
        finally:
            self.last_metrics["elapsed_seconds"] = time.monotonic() - start

    def validate_request(self, messages, tools=None):
        """Preflight the exact wire payload without sending a request."""
        self._prepare_request(messages, tools)

    def _prepare_request(self, messages, tools):
        if not isinstance(messages, list) or any(not isinstance(m, dict) for m in messages):
            raise ModelError("Invalid model request messages.")
        if tools is not None and (not isinstance(tools, list) or any(not isinstance(t, dict) for t in tools)):
            raise ModelError("Invalid model request tools.")
        payload = {"model": self.config["name"], "messages": messages,
                   "stream": self.config.get("stream", True),
                   "temperature": self.config.get("temperature", 0.2),
                   "max_tokens": self.config.get("max_tokens", 2048)}
        if tools is not None:
            payload["tools"] = tools
        try:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, RecursionError):
            raise ModelError("Invalid model request JSON.") from None
        if len(body) > self.max_request_bytes:
            raise ModelError("Model request size limit exceeded (model.max_request_bytes).")
        return payload, body

    def _complete(self, messages, tools, on_text, deadline):
        payload, body = self._prepare_request(messages, tools)
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream" if payload["stream"] else "application/json"}
        key_env = self.config.get("api_key_env", "")
        if key_env:
            key = os.environ.get(key_env, "")
            if not key or any(ord(c) <= 32 or ord(c) >= 127 for c in key):
                raise ModelError("Model API key is missing or invalid.")
            headers["Authorization"] = "Bearer " + key
        conn = None
        reply = None
        active_socket = [None]
        finished = threading.Event()

        def expire():
            # shutdown interrupts buffered readline/read/getresponse even when
            # bytes keep trickling faster than a per-operation socket timeout.
            sock = active_socket[0]
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        timer = threading.Timer(_remaining(deadline), expire)
        timer.daemon = True
        timer.start()
        def watch_cancel():
            while not finished.wait(0.05):
                if self.cancel_event is not None and self.cancel_event.is_set():
                    # Cancellation stays active through socket replacement (TLS
                    # wrapping detaches the old socket before publishing the new).
                    # Retry until the request's finally block signals completion.
                    expire()

        watcher = None
        if self.cancel_event is not None:
            watcher = threading.Thread(target=watch_cancel, daemon=True, name="david-cancel")
            watcher.start()
        try:
            context = None
            if self.url.scheme == "https":
                context = ssl.create_default_context(cafile=self.config.get("ca_file") or None)
            port = self.url.port or (443 if context is not None else 80)
            host = self.url.hostname
            assert host is not None  # Validated before any network activity.
            conn = http.client.HTTPConnection(host, port, timeout=_remaining(deadline))
            conn.sock = _connect(host, port, deadline, active_socket, context, self.cancel_event)
            check_cancel(self.cancel_event)
            conn.request("POST", self.url.path.rstrip("/") + "/chat/completions",
                         body, headers)
            reply = conn.getresponse()
            check_cancel(self.cancel_event)
            if reply.status != 200:
                raise ModelError(f"Model HTTP request failed (status {reply.status}).")
            if payload["stream"]:
                message = self._stream(reply, on_text)
                check_cancel(self.cancel_event)
                _remaining(deadline)
                return message
            body = reply.read(self.max_response_bytes + 1)
            check_cancel(self.cancel_event)
            if len(body) > self.max_response_bytes:
                raise ModelError("Model response size limit exceeded.")
            if reply.length not in (None, 0):
                raise ModelError("Model HTTP response was truncated.")
            choice = _choice(_json(body))
            if choice is None:
                _invalid()
            message = _message(choice.get("message"), choice.get("finish_reason"))
            if on_text and message.get("content"):
                on_text(message["content"])
            _remaining(deadline)
            return message
        except (OSError, http.client.HTTPException):
            _remaining(deadline)
            raise ModelError("Model connection or TLS request failed.") from None
        finally:
            finished.set()
            if watcher is not None:
                watcher.join()
            timer.cancel()
            timer.join()
            if reply is not None:
                reply.close()
            if conn is not None:
                conn.close()
            if active_socket[0] is not None:
                active_socket[0].close()

    def _stream(self, reply, on_text):
        texts = []
        calls = {}
        data = []
        finish = None
        done = False
        total = 0
        event_bytes = 0
        while True:
            raw = reply.readline(min(MAX_SSE_LINE_BYTES, self.max_response_bytes - total) + 1)
            if not raw:
                break
            total += len(raw)
            event_bytes += len(raw)
            if (total > self.max_response_bytes or len(raw) > MAX_SSE_LINE_BYTES
                    or event_bytes > MAX_SSE_EVENT_BYTES):
                raise ModelError("Model stream size limit exceeded.")
            try:
                line = raw.decode("utf-8").rstrip("\r\n")
            except UnicodeError:
                raise ModelError("Invalid model stream encoding.") from None
            if not line:
                event_bytes = 0
            if line.startswith("data:"):
                data.append(line[5:].removeprefix(" "))
            elif not line and data:
                value = "\n".join(data)
                data = []
                if value == "[DONE]":
                    done = True
                    break
                choice = _choice(_json(value), allow_usage=True)
                if choice is None:
                    continue
                if finish is not None:
                    raise ModelError("Model sent data after completion.")
                finish = choice.get("finish_reason")
                delta = choice.get("delta")
                if (not isinstance(delta, dict) or delta.get("role", "assistant") != "assistant"
                        or "function_call" in delta):
                    _invalid()
                content = delta.get("content")
                if content is not None and not isinstance(content, str):
                    _invalid()
                if delta.get("content"):
                    texts.append(delta["content"])
                    if on_text:
                        on_text(delta["content"])
                items = delta.get("tool_calls", [])
                if not isinstance(items, list) or len(items) > MAX_TOOL_CALLS:
                    _invalid()
                for item in items:
                    if not isinstance(item, dict):
                        _invalid()
                    index = item.get("index")
                    if type(index) is not int or not 0 <= index < MAX_TOOL_CALLS:
                        _invalid()
                    if "type" in item and item["type"] != "function":
                        _invalid()
                    call = calls.setdefault(index, {"id": "", "type": None,
                                           "function": {"name": "", "arguments": ""}})
                    if "type" in item:
                        call["type"] = item["type"]
                    ident = item.get("id", "")
                    if not isinstance(ident, str) or len(call["id"]) + len(ident) > 256:
                        _invalid()
                    call["id"] += ident
                    function = item.get("function", {})
                    if not isinstance(function, dict):
                        _invalid()
                    for field, limit in (("name", 128), ("arguments", MAX_TOOL_ARGUMENT_BYTES)):
                        fragment = function.get(field, "")
                        if not isinstance(fragment, str):
                            _invalid()
                        combined = call["function"][field] + fragment
                        if len(combined.encode("utf-8")) > limit:
                            raise ModelError("Model tool field size limit exceeded.")
                        call["function"][field] = combined
        if not done:
            raise ModelError("Model stream is missing framed [DONE]; incomplete tools were not executed.")
        message = {"role": "assistant", "content": "".join(texts) if texts else None}
        if calls:
            if sorted(calls) != list(range(len(calls))):
                _invalid()
            message["tool_calls"] = [calls[index] for index in sorted(calls)]
        return _message(message, finish)
