import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class CancellationTests(unittest.TestCase):
    def test_cancel_during_dns_never_connects(self):
        from unittest.mock import patch
        from david.transport import OpenAIClient
        from david.cancellation import Cancelled
        entered, release, cancel = threading.Event(), threading.Event(), threading.Event()
        result = []

        def resolve(*args, **kwargs):
            entered.set()
            release.wait(3)
            return []

        client = OpenAIClient({'base_url': 'http://localhost:9/v1', 'name': 'fixture',
                               'timeout_seconds': 30}, cancel_event=cancel)

        def request():
            try:
                client.complete([])
            except BaseException as exc:
                result.append(exc)

        with patch('socket.getaddrinfo', side_effect=resolve), patch('socket.socket') as connect:
            worker = threading.Thread(target=request, daemon=True)
            try:
                worker.start()
                self.assertTrue(entered.wait(1))
                cancel.set()
                worker.join(0.8)
                self.assertFalse(worker.is_alive())
                self.assertIsInstance(result[0], Cancelled)
                connect.assert_not_called()
            finally:
                release.set()
                worker.join(2)

    def test_cancel_during_tls_socket_wrapping_closes_before_handshake(self):
        import socket
        import ssl
        from unittest.mock import patch
        from david.transport import OpenAIClient
        from david.cancellation import Cancelled

        wrapped_ready, release, expired = threading.Event(), threading.Event(), threading.Event()
        cancel = threading.Event()
        context = ssl.create_default_context()
        wrapped_sockets, handshakes, result = [], [], []
        original_shutdown = socket.socket.shutdown

        class GatedContext:
            def wrap_socket(self, sock, **kwargs):
                wrapped = context.wrap_socket(sock, **kwargs)
                wrapped_sockets.append(wrapped)
                handshake = wrapped.do_handshake
                def do_handshake(block=False):
                    handshakes.append(True)
                    return handshake(block=block)
                wrapped.do_handshake = do_handshake
                wrapped_ready.set()
                release.wait(3)
                return wrapped

        def shutdown(sock, how):
            if sock.fileno() == -1 and threading.current_thread().name == 'david-cancel':
                expired.set()  # The monitor attempted shutdown on the detached original.
            return original_shutdown(sock, how)

        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        listener.settimeout(2)
        client = OpenAIClient({'base_url': f'https://127.0.0.1:{listener.getsockname()[1]}/v1',
                               'name': 'fixture', 'timeout_seconds': 30}, cancel_event=cancel)
        def request():
            try:
                client.complete([])
            except BaseException as exc:
                result.append(exc)

        worker = threading.Thread(target=request, daemon=True)
        peer = None
        with patch('david.transport.ssl.create_default_context', return_value=GatedContext()), \
                patch.object(socket.socket, 'shutdown', shutdown), \
                patch('david.transport.http.client.HTTPConnection.request') as post:
            try:
                worker.start()
                peer, _ = listener.accept()  # Keep TCP open, but never answer TLS.
                self.assertTrue(wrapped_ready.wait(1))
                cancel.set()
                self.assertTrue(expired.wait(1))
                release.set()
                worker.join(0.8)
                self.assertFalse(worker.is_alive(), 'cancellation was lost during TLS socket replacement')
                self.assertEqual(len(result), 1)
                self.assertIsInstance(result[0], Cancelled)
                self.assertEqual(handshakes, [])
                self.assertEqual(wrapped_sockets[0].fileno(), -1)
                post.assert_not_called()
            finally:
                release.set()
                if peer is not None:
                    peer.close()
                listener.close()
                worker.join(2)

    def test_cancel_monitor_survives_stale_socket_until_replacement_is_interrupted(self):
        import socket
        from unittest.mock import patch
        from david.transport import OpenAIClient
        from david.cancellation import Cancelled

        cancel, stale_attempt = threading.Event(), threading.Event()
        stale = socket.socket()
        stale.close()
        replacement, peer = socket.socketpair()
        original_shutdown = socket.socket.shutdown
        result = []

        def shutdown(sock, how):
            if sock is stale:
                stale_attempt.set()
            return original_shutdown(sock, how)

        def connect(host, port, deadline, active_socket, context, cancel_event):
            active_socket[0] = stale
            cancel.set()
            if not stale_attempt.wait(1):
                raise AssertionError('monitor never attempted cancellation')
            active_socket[0] = replacement
            # Model the check-to-block race after publication: the monitor must
            # keep interrupting active IO, not exit after one stale FD failure.
            replacement.recv(1)
            return replacement

        client = OpenAIClient({'base_url': 'http://localhost:9/v1', 'name': 'fixture',
                               'timeout_seconds': 30}, cancel_event=cancel)
        def request():
            try:
                client.complete([])
            except BaseException as exc:
                result.append(exc)

        worker = threading.Thread(target=request, daemon=True)
        with patch('david.transport._connect', side_effect=connect), \
                patch.object(socket.socket, 'shutdown', shutdown), \
                patch('david.transport.http.client.HTTPConnection.request') as post:
            try:
                worker.start()
                worker.join(0.8)
                self.assertFalse(worker.is_alive(), 'monitor stopped after stale socket shutdown')
                self.assertEqual(len(result), 1)
                self.assertIsInstance(result[0], Cancelled)
                self.assertEqual(replacement.fileno(), -1)
                post.assert_not_called()
            finally:
                peer.close()
                worker.join(2)
                replacement.close()

    def test_client_cancel_interrupts_waiting_for_headers(self):
        from david.transport import OpenAIClient
        from david.cancellation import Cancelled
        entered, release, cancel = threading.Event(), threading.Event(), threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                entered.set()
                release.wait(4)
                self.close_connection = True

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        result = []
        client = OpenAIClient({'base_url': f'http://127.0.0.1:{server.server_port}/v1',
                               'name': 'fixture', 'stream': False, 'timeout_seconds': 30},
                              cancel_event=cancel)

        def request():
            try:
                client.complete([{'role': 'user', 'content': 'hello'}])
            except BaseException as exc:
                result.append(exc)

        worker = threading.Thread(target=request, daemon=True)
        try:
            worker.start()
            self.assertTrue(entered.wait(2))
            started = time.monotonic()
            cancel.set()
            worker.join(1.5)
            self.assertFalse(worker.is_alive(), 'cancel must not wait for the model timeout')
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual(len(result), 1)
            self.assertIsInstance(result[0], Cancelled)
        finally:
            release.set()
            worker.join(2)
            server.shutdown()
            server.server_close()
            serving.join(2)
