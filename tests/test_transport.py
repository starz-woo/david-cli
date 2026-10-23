"""Transport tests use only a real in-process loopback HTTP server."""
import contextlib
import importlib
import json
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@contextlib.contextmanager
def serve(body, status=200, headers=None, pieces=None, tls=None, delay=0, slow_headers=False):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_POST(self):
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            requests.append({"path": self.path, "headers": dict(self.headers),
                             "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
            if slow_headers:
                try:
                    raw_headers = b"HTTP/1.0 200 OK\r\nX-Padding: " + b"x" * 60 + b"\r\n\r\n"
                    for index in range(0, len(raw_headers), 5):
                        self.wfile.write(raw_headers[index:index + 5])
                        self.wfile.flush()
                        time.sleep(delay)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            try:
                for piece in pieces or [body]:
                    self.wfile.write(piece)
                    self.wfile.flush()
                    if delay:
                        time.sleep(delay)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    if tls is not None:
        server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        scheme = "https" if tls is not None else "http"
        yield f"{scheme}://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def response(message=None, reason="stop"):
    return json.dumps({"choices": [{"index": 0, "message": message or {
        "role": "assistant", "content": "hello"}, "finish_reason": reason}]}).encode()


def event(delta, finish=None):
    return ("data: " + json.dumps({"choices": [{"index": 0, "delta": delta,
             "finish_reason": finish}]}, ensure_ascii=False) + "\n\n").encode()


class TransportTests(unittest.TestCase):
    def test_configured_large_requests_use_actual_utf8_wire_byte_limit(self):
        from david.transport import ModelError, OpenAIClient
        for text in ('a' * 2200000, '한' * 750000):
            with self.subTest(character=text[0]), serve(response()) as (url, requests):
                messages = [{'role': 'user', 'content': text}]
                config = {'base_url': url, 'name': 'local', 'stream': False,
                          'max_request_bytes': 8 * 1024 * 1024}
                result = OpenAIClient(config).complete(messages)
                self.assertEqual(result['content'], 'hello')
                self.assertEqual(requests[0]['body']['messages'], messages)
                body = json.dumps(requests[0]['body'], ensure_ascii=False, allow_nan=False).encode('utf-8')
                size = len(body)
                self.assertGreater(size, 2 * 1024 * 1024)
                self.assertEqual(int(requests[0]['headers']['Content-Length']), size)
                config['max_request_bytes'] = size
                OpenAIClient(config).complete(messages)
                config['max_request_bytes'] = size - 1
                with self.assertRaisesRegex(ModelError, 'request size limit'):
                    OpenAIClient(config).complete(messages)
                config.pop('max_request_bytes')
                with self.assertRaisesRegex(ModelError, 'request size limit'):
                    OpenAIClient(config).complete(messages)
                self.assertEqual(len(requests), 2)


    def test_incomplete_diagnostics_distinguish_output_limit_framing_and_finish(self):
        from david.transport import ModelError, OpenAIClient
        call = {'id': 'x', 'type': 'function', 'function': {'name': 'read', 'arguments': '{'}}
        cases = [
            (False, response(reason='length'), 'output limit.*model.max_tokens'),
            (False, response({'role': 'assistant', 'tool_calls': [call]}, 'length'), 'output limit'),
            (False, response(reason='tool_calls'), 'finish_reason'),
            (True, event({'content': 'preview'}, 'length') + b'data: [DONE]\n\n', 'output limit'),
            (True, event({'tool_calls': [dict(call, index=0)]}, 'length') + b'data: [DONE]\n\n', 'output limit'),
            (True, event({'content': 'preview'}, 'stop'), r'missing.*\[DONE\]'),
            (True, event({'content': 'preview'}, 'tool_calls') + b'data: [DONE]\n\n', 'finish_reason'),
        ]
        for stream, body, diagnostic in cases:
            with self.subTest(stream=stream, diagnostic=diagnostic), serve(body) as (url, requests):
                with self.assertRaisesRegex(ModelError, diagnostic):
                    OpenAIClient({'base_url': url, 'name': 'local', 'stream': stream}).complete([])
                self.assertEqual(len(requests), 1)

    def test_direct_client_validates_bounded_request_limit(self):
        from david.transport import ModelError, OpenAIClient
        for value in (True, 0, 1023, 67108865, 'large', 4096.5, None):
            with self.subTest(value=value), self.assertRaises(ModelError):
                OpenAIClient({'base_url': 'http://127.0.0.1/v1', 'name': 'local',
                              'max_request_bytes': value})

    def test_wall_deadline_bounds_trickling_body_and_headers(self):
        from david.transport import ModelError, OpenAIClient
        for stream, slow_headers in [(True, False), (False, False), (True, True)]:
            pieces = [event({"content": "preview"})] + [b":"] * 20 if stream else [b" "] * 20
            with self.subTest(stream=stream, slow_headers=slow_headers), serve(
                    b"", pieces=pieces, delay=0.08, slow_headers=slow_headers) as (url, requests):
                client = OpenAIClient({"base_url": url, "name": "local", "stream": stream, "timeout_seconds": 0.4})
                texts = []
                before = time.monotonic()
                with self.assertRaises(ModelError):
                    client.complete([], on_text=texts.append)
                self.assertLess(time.monotonic() - before, 1.2)
                self.assertEqual(len(requests), 1)
                if stream and not slow_headers:
                    self.assertEqual(texts, ["preview"])

    def test_metrics_measure_client_elapsed_and_first_text_without_invented_tokens(self):
        from david.transport import ModelError, OpenAIClient
        with serve(event({"content": "hi"}) + event({}, "stop") + b"data: [DONE]\n\n") as (url, _):
            client = OpenAIClient({"base_url": url, "name": "local"})
            before = time.monotonic()
            client.complete([])
            elapsed = time.monotonic() - before
            self.assertEqual(set(client.last_metrics), {"elapsed_seconds", "first_text_seconds"})
            self.assertGreater(client.last_metrics["first_text_seconds"], 0)
            self.assertLessEqual(client.last_metrics["first_text_seconds"], client.last_metrics["elapsed_seconds"])
            self.assertLessEqual(client.last_metrics["elapsed_seconds"], elapsed)
            with self.assertRaises(ModelError):
                client.complete("bad")
            self.assertIsNone(client.last_metrics["first_text_seconds"])

    @unittest.skipUnless(shutil.which("openssl"), "openssl is needed to generate a local TLS test certificate")
    def test_tls_requires_trust_and_hostname_and_supports_explicit_ca(self):
        from david.transport import ModelError, OpenAIClient
        with tempfile.TemporaryDirectory() as folder:
            cert, key = os.path.join(folder, "cert.pem"), os.path.join(folder, "key.pem")
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key,
                            "-out", cert, "-days", "1", "-subj", "/CN=localhost", "-addext",
                            "subjectAltName=DNS:localhost"], check=True, capture_output=True, timeout=20)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            with serve(response(), tls=context) as (url, requests):
                good = url.replace("127.0.0.1", "localhost")
                with self.assertRaises(ModelError):
                    OpenAIClient({"base_url": good, "name": "local", "stream": False}).complete([])
                with self.assertRaises(ModelError):
                    OpenAIClient({"base_url": url, "name": "local", "stream": False, "ca_file": cert}).complete([])
                result = OpenAIClient({"base_url": good, "name": "local", "stream": False, "ca_file": cert}).complete([])
                self.assertEqual(result["content"], "hello")
                self.assertEqual(len(requests), 1)
                with self.assertRaises(ModelError) as caught:
                    OpenAIClient({"base_url": good, "name": "local", "ca_file": cert + ".missing"}).complete([])
                self.assertNotIn(folder, str(caught.exception))

    def test_only_explicit_key_is_sent_and_environment_proxies_are_ignored(self):
        from david.transport import ModelError, OpenAIClient
        with serve(response()) as (proxy_url, proxy_requests), serve(response()) as (url, requests):
            env = {"DAVID_TEST_KEY": "explicit-test-key", "OPENAI_API_KEY": "never-send-this",
                   "http_proxy": proxy_url, "HTTP_PROXY": proxy_url, "https_proxy": proxy_url,
                   "HTTPS_PROXY": proxy_url, "ALL_PROXY": proxy_url, "all_proxy": proxy_url,
                   "no_proxy": "", "NO_PROXY": ""}
            with patch.dict(os.environ, env):
                OpenAIClient({"base_url": url, "name": "local", "stream": False,
                              "api_key_env": "DAVID_TEST_KEY"}).complete([])
                OpenAIClient({"base_url": url, "name": "local", "stream": False}).complete([])
            self.assertEqual(requests[0]["headers"]["Authorization"], "Bearer explicit-test-key")
            self.assertNotIn("Authorization", requests[1]["headers"])
            self.assertEqual(proxy_requests, [])
            for key in ["", "secret\r\nInjected: yes", "secret🙂"]:
                with patch.dict(os.environ, {"DAVID_TEST_KEY": key}), self.assertRaises(ModelError) as caught:
                    OpenAIClient({"base_url": url, "name": "local", "stream": False,
                                  "api_key_env": "DAVID_TEST_KEY"}).complete([])
                self.assertNotIn("secret", str(caught.exception))
            self.assertEqual(len(requests), 2)

    def test_nonstream_rejects_malformed_or_incomplete_messages(self):
        from david.transport import ModelError, OpenAIClient
        call = {"id": "a", "type": "function", "function": {"name": "read", "arguments": "{}"}}
        messages = [{"role": "user", "content": "wrong"}, {"role": "assistant", "content": []},
                    {"role": "assistant", "content": None, "tool_calls": "bad"},
                    {"role": "assistant", "content": None, "tool_calls": [call] * 17},
                    {"role": "assistant", "content": None, "tool_calls": [call, call]},
                    {"role": "assistant", "function_call": {"name": "legacy", "arguments": "{}"}}]
        for changed in [{"id": ""}, {"type": "other"}, {"function": {}},
                        {"function": {"name": "read", "arguments": {}}},
                        {"function": {"name": "read", "arguments": "{"}},
                        {"function": {"name": "read", "arguments": "[]"}},
                        {"function": {"name": "read", "arguments": '{"a":NaN}'}},
                        {"function": {"name": "read", "arguments": '{"a":1,"a":2}'}},
                        {"function": {"name": "read", "arguments": '{"x":"' + "x" * 65536 + '"}'}}]:
            messages.append({"role": "assistant", "content": None, "tool_calls": [dict(call, **changed)]})
        bodies = [response(m, "tool_calls" if m.get("tool_calls") else "stop") for m in messages]
        bodies += [b"not-json-secret", b"\xff", b"{}", b"[]", b'{"choices":[]}',
                   response(reason=None), response(reason="length"),
                   response({"role": "assistant", "content": None, "tool_calls": [call]}, "stop"),
                   b'{"choices":[],"choices":[]}']
        for i, body in enumerate(bodies):
            with self.subTest(case=i), serve(body) as (url, _):
                with self.assertRaises(ModelError) as caught:
                    OpenAIClient({"base_url": url, "name": "local", "stream": False}).complete([])
                self.assertNotIn("secret", str(caught.exception))

    def test_stream_rejects_bad_shapes_indices_and_tool_payloads(self):
        from david.transport import ModelError, OpenAIClient
        base = {"index": 0, "id": "a", "type": "function", "function": {"name": "read", "arguments": "{}"}}
        deltas = [{"role": "user"}, {"content": 42}, {"content": ["bad"]},
                  {"function_call": {"name": "legacy"}}, {"tool_calls": {}}, {"tool_calls": [None]}]
        for change in [{"index": -1}, {"index": 16}, {"index": True}, {"index": "0"}, {"index": 1},
                       {"type": "other"}, {"id": None}, {"id": ""}, {"function": None},
                       {"function": {"name": 42}}, {"function": {"name": "read", "arguments": "{"}},
                       {"function": {"name": "read", "arguments": "[]"}},
                       {"function": {"name": "read", "arguments": '{"x":"' + "x" * 65536 + '"}'}}]:
            deltas.append({"tool_calls": [dict(base, **change)]})
        bodies = [event(d, "tool_calls" if "tool_calls" in d else "stop") + b"data: [DONE]\n\n" for d in deltas]
        bodies += [b"data: not-json-secret\n\n", b"data: \xff\n\n", b"data: []\n\n",
                   b'data: {"choices":[{"index":1,"delta":{},"finish_reason":"stop"}]}\n\n',
                   b'data: {"choices":[],"error":"secret"}\n\n', b'data: {"choices":[]}\n\n']
        for i, body in enumerate(bodies):
            with self.subTest(case=i), serve(body) as (url, requests):
                with self.assertRaises(ModelError) as caught:
                    OpenAIClient({"base_url": url, "name": "local"}).complete([])
                self.assertNotIn("secret", str(caught.exception))
                self.assertEqual(len(requests), 1)

    def test_stream_unicode_and_interleaved_tool_fragments(self):
        from david.transport import OpenAIClient
        body = (b": heartbeat\r\n\r\n" + event({"role": "assistant", "content": "안녕🙂"})
                + event({"tool_calls": [{"index": 1, "id": "call_b", "type": "function",
                                        "function": {"name": "read", "arguments": '{"p":'}},
                                       {"index": 0, "id": "call_a", "type": "function",
                                        "function": {"name": "write", "arguments": '{"x":'}}]})
                + event({"tool_calls": [{"index": 0, "function": {"arguments": "1}"}},
                                        {"index": 1, "function": {"arguments": '"é"}'}}]})
                + event({}, "tool_calls") + b'data: {"choices":[],"usage":{"total_tokens":123}}\n\n'
                + b"data: [DONE]\n\n")
        with serve(body, pieces=[body[i:i + 1] for i in range(len(body))]) as (url, requests):
            client = OpenAIClient({"base_url": url, "name": "local"})
            texts = []
            tools = [{"type": "function", "function": {"name": "read", "parameters": {}}}]
            result = client.complete([], tools, texts.append)
            self.assertEqual(result, {"role": "assistant", "content": "안녕🙂", "tool_calls": [
                {"id": "call_a", "type": "function", "function": {"name": "write", "arguments": '{"x":1}'}},
                {"id": "call_b", "type": "function", "function": {"name": "read", "arguments": '{"p":"é"}'}}]})
            self.assertEqual(texts, ["안녕🙂"])
            self.assertEqual(requests[0]["body"]["tools"], tools)
            self.assertTrue(requests[0]["body"]["stream"])

    def test_stream_requires_successful_finish_and_done_without_retry(self):
        from david.transport import ModelError, OpenAIClient
        partial = event({"tool_calls": [{"index": 0, "id": "a", "type": "function",
                                        "function": {"name": "read", "arguments": "{}"}}]})
        cases = [b"", partial, partial + b"data: [DONE]\n\n", partial + event({}, "tool_calls"),
                 partial + event({}, "length") + b"data: [DONE]\n\n",
                 partial + event({}, "stop") + b"data: [DONE]\n\n",
                 partial + event({}, "tool_calls") + b"data: [DONE]\n",
                 event({}, "stop") + event({"content": "late"}) + b"data: [DONE]\n\n"]
        for body in cases:
            with self.subTest(body=body), serve(body) as (url, requests):
                texts = []
                with self.assertRaises(ModelError):
                    OpenAIClient({"base_url": url, "name": "local"}).complete([], on_text=texts.append)
                self.assertEqual(len(requests), 1)

    def test_limits_request_response_and_sse_accumulation(self):
        from david.transport import ModelError, OpenAIClient
        cases = [(False, response({"role": "assistant", "content": "x" * 600}), 512),
                 (True, b": " + b"x" * 600 + b"\n\n", 512),
                 (True, b":" + b"x" * 262144 + b"\n\n", 2097152),
                 (True, (b"data: " + b"x" * 8192 + b"\n") * 40, 2097152),
                 (True, (b": heartbeat\n\n" * 100), 512)]
        for stream, body, limit in cases:
            with self.subTest(stream=stream, length=len(body)), serve(body) as (url, requests):
                with self.assertRaisesRegex(ModelError, "limit"):
                    OpenAIClient({"base_url": url, "name": "local", "stream": stream,
                                  "max_response_bytes": limit}).complete([])
                self.assertEqual(len(requests), 1)
        with serve(response()) as (url, requests):
            with self.assertRaises(ModelError):
                OpenAIClient({"base_url": url, "name": "local", "stream": False}).complete(
                    [{"role": "user", "content": "x" * 2097152}])
            self.assertEqual(requests, [])

    def test_invalid_configuration_and_request_data_fail_safely_before_io(self):
        from david.transport import ModelError, OpenAIClient
        changes = [{"name": ""}, {"name": 12}, {"stream": "false"}, {"allow_insecure_http": 1},
                   {"timeout_seconds": 0}, {"timeout_seconds": float("inf")}, {"timeout_seconds": True},
                   {"temperature": float("nan")}, {"temperature": "hot"}, {"max_tokens": 0},
                   {"max_tokens": True}, {"max_response_bytes": -1}, {"max_response_bytes": 1.5},
                   {"max_response_bytes": True}, {"api_key_env": []}, {"api_key_env": "bad=key"},
                   {"ca_file": True}, {"ca_file": "secret\x00path"}]
        with serve(response()) as (url, requests):
            for change in changes:
                with self.subTest(change=change), self.assertRaises(ModelError):
                    OpenAIClient(dict({"base_url": url, "name": "local", "stream": False}, **change))
            for messages, tools in [("not-list", None), (["not-dict"], None), ([], {}),
                                    ([], ["not-dict"]), ([{"content": object()}], None),
                                    ([{"content": float("nan")}], None)]:
                with self.subTest(messages=messages, tools=tools), self.assertRaises(ModelError):
                    OpenAIClient({"base_url": url, "name": "local", "stream": False}).complete(messages, tools)
            self.assertEqual(requests, [])

    def test_endpoint_policy(self):
        from david.transport import ModelError, OpenAIClient
        bad = ["ftp://localhost/v1", "http://example.com/v1", "http://127.0.0.2/v1",
               "http://localhost.evil/v1", "http://127.1/v1", "http://[::ffff:127.0.0.1]/v1",
               "https://user:password@example.com/v1", "https://example.com/v1?secret=x",
               "https://example.com/v1#secret", "https://example.com/v1?", "https://example.com/v1#",
               "https:///v1", "https://example.com:bad/v1", "https://example.com:0/v1",
               " https://example.com/v1", "https://example.com/\nsecret", "https://example.com/é"]
        for url in bad:
            with self.subTest(url=url):
                with self.assertRaises(ModelError) as caught:
                    OpenAIClient({"base_url": url, "name": "local"})
                self.assertNotIn(url, str(caught.exception))
        for url in ["http://localhost/v1", "http://127.0.0.1/v1", "http://[::1]/v1", "https://example.com/v1"]:
            OpenAIClient({"base_url": url, "name": "local"})
        OpenAIClient({"base_url": "http://example.com/v1", "name": "local", "allow_insecure_http": True})

    def test_http_errors_and_redirects_are_safe_and_never_followed(self):
        from david.transport import ModelError, OpenAIClient
        for status in [301, 302, 307, 308, 401, 500]:
            with self.subTest(status=status), serve(response()) as (target, target_requests):
                with serve(b"private server secret", status, {"Location": target + "/stolen"}) as (url, requests):
                    client = OpenAIClient({"base_url": url + "/sensitive", "name": "local", "stream": False})
                    with self.assertRaises(ModelError) as caught:
                        client.complete([])
                    self.assertIn(str(status), str(caught.exception))
                    self.assertNotIn("private", str(caught.exception))
                    self.assertNotIn(url, str(caught.exception))
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(target_requests, [])

    def test_nonstream_rejects_truncated_http_framing_even_if_json_is_complete(self):
        from david.transport import ModelError, OpenAIClient
        body = response()
        for headers, wire in [({"Content-Length": str(len(body) + 5)}, body),
                              ({"Transfer-Encoding": "chunked"}, f"{len(body):x}\r\n".encode() + body + b"\r\n")]:
            with self.subTest(headers=headers), serve(wire, headers=headers) as (url, requests):
                with self.assertRaises(ModelError):
                    OpenAIClient({"base_url": url, "name": "local", "stream": False}).complete([])
                self.assertEqual(len(requests), 1)

    def test_invalid_unicode_and_nonfinite_json_never_reach_callbacks(self):
        from david.transport import ModelError, OpenAIClient
        invalid_content = response({"role": "assistant", "content": "\ud800"})
        invalid_stream = b'data: {"choices":[{"index":0,"delta":{"content":"\\ud800"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
        bodies = [(False, invalid_content), (True, invalid_stream)]
        for args in ['{"x":1e309}', '{"x":"\\ud800"}']:
            bodies.append((False, response({"role": "assistant", "content": None, "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "read", "arguments": args}}]}, "tool_calls")))
        for stream, body in bodies:
            with self.subTest(stream=stream, body=body), serve(body) as (url, _):
                texts = []
                with self.assertRaises(ModelError):
                    OpenAIClient({"base_url": url, "name": "local", "stream": stream}).complete([], on_text=texts.append)
                self.assertEqual(texts, [])

    def test_valid_tool_limits_nonstream_and_chunked_sse(self):
        from david.transport import OpenAIClient
        calls = [{"id": f"call_{i}", "type": "function", "function": {"name": "read", "arguments": "{}"}} for i in range(16)]
        calls[0]["function"]["arguments"] = '{"x":"' + "x" * (65536 - len('{"x":""}')) + '"}'
        expected = {"role": "assistant", "content": None, "tool_calls": calls}
        streamed = event({"tool_calls": [dict(call, index=i) for i, call in enumerate(calls)]}, "tool_calls")
        streamed += b"data: [DONE]\n\n"
        wire = b"".join(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n"
                        for chunk in [streamed[i:i + 31] for i in range(0, len(streamed), 31)]) + b"0\r\n\r\n"
        for stream, body, headers in [(False, response(expected, "tool_calls"), {}),
                                       (True, wire, {"Transfer-Encoding": "chunked"})]:
            with self.subTest(stream=stream), serve(body, headers=headers) as (url, requests):
                client = OpenAIClient({"base_url": url, "name": "local", "stream": stream})
                self.assertEqual(client.complete([]), expected)
                self.assertIsNone(client.last_metrics["first_text_seconds"])
                self.assertEqual(len(requests), 1)

    def test_nonstream_round_trip(self):
        transport = importlib.import_module("david.transport")
        with serve(response()) as (url, requests):
            client = transport.OpenAIClient({"base_url": url, "name": "local", "stream": False})
            texts = []
            self.assertEqual(client.complete([{"role": "user", "content": "hi"}], on_text=texts.append),
                             {"role": "assistant", "content": "hello"})
            self.assertEqual(texts, ["hello"])
            self.assertEqual(requests[0]["path"], "/v1/chat/completions")
            self.assertEqual(requests[0]["body"], {"model": "local", "messages": [{"role": "user", "content": "hi"}],
                                                  "stream": False, "temperature": 0.2, "max_tokens": 2048})
            self.assertNotIn("Authorization", requests[0]["headers"])
