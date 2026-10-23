"""Bounded, lazy MCP stdio tools client (stdlib transport, no SDK dependency).

Public API: MCPManager(servers, workspace, approve, cancel_check, read_only=False),
list_tools(server) -> {'tools': [...]}, call_tool(server, name, arguments) -> the
validated result dict, and idempotent close(). MCPError subclasses ToolError;
exceptions raised by cancel_check propagate unchanged after connection cleanup.
Call from one thread at a time. Approval/cancellation/read_only are mutable so
integration can replace them between operations. No tool call is retried.

The executable is TRUSTED ARBITRARY CODE, not a workspace sandbox. Explicit env
mapping avoids accidental secret inheritance, not access to credentials on disk.
Start approval names the argv and cwd; every tool call separately approves its
complete bounded argument snapshot. Read-only mode forbids launch and calls.
POSIX process groups / Windows taskkill perform best-effort descendant cleanup;
intentionally detached processes and executable replacement races are not an OS
security boundary. stderr is discarded directly to DEVNULL (zero retained bytes).

Wire limits: max_response_bytes bounds each stdout frame, queued bytes, and all
received bytes in one operation (including initialize and every list page).
The queue holds at most 64 frames; an operation accepts at most 64 notifications
or server requests, 32 list pages and 1024 tools. Server request ID history holds
at most 1024 IDs of 128 characters. Call approval JSON is at most the smaller of
max_response_bytes and 262144 bytes. The complete prospective tools/call JSON-RPC
envelope (including its ID) is checked before any approval or launch: depth 32 /
10000 JSON nodes, and at most the approval byte limit plus 4096 envelope bytes.
Network waits, blocked pipe writes, and pagination share one wall deadline,
excluding human start approval. Failures inside an operation and exceptions from
call approval (including cancellation) invalidate the connection; a subsequent
explicit user operation may request a fresh approved launch. Preflight rejection
or a false call-approval return sends no wire messages and launches nothing, but
retains an already-running, previously start-approved server until close() or a
later operation failure. Denial is not cancellation or revocation of that prior
launch approval; every subsequent tool call still requires its own approval.

Only initialize, initialized, tools/list, tools/call and ping replies are
implemented. Other server requests receive -32601. No HTTP/OAuth, resources,
prompts, sampling, task support, resource URI fetching or image decoding.
Handshake validation checks supported protocol/tools capability and serverInfo
object/name/version string shape, not the full MCP schema. Tool schemas are
exposed as data, not fully JSON-Schema validated. Server instructions are
ignored; returned descriptions, annotations and results remain UNTRUSTED data.
The caller must retain that trust boundary and sanitize terminal rendering.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import queue
import signal
import subprocess
import sys
import threading
import time

from .workspace import ToolError


class MCPError(ToolError):
    """An MCP operation failed or was rejected."""


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MCPError('MCP duplicate JSON key')
        result[key] = value
    return result


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise MCPError('MCP non-finite JSON number')
    return number


def _constant(value):
    raise MCPError('MCP non-finite JSON number')


def _decode(line):
    try:
        message = json.loads(line.decode('utf-8'), object_pairs_hook=_object,
                             parse_constant=_constant, parse_float=_float)
    except (ValueError, UnicodeError, RecursionError):
        raise MCPError('MCP malformed JSON') from None
    if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
        raise MCPError('MCP malformed JSON-RPC envelope')
    return message


def _json_text(value, limit):
    # Validate before encoding: JSON must not silently coerce mapping keys/types.
    count = 0
    characters = 0

    def visit(item, depth):
        nonlocal count, characters
        count += 1
        if count > 10000 or depth > 32:
            raise MCPError('MCP JSON complexity limit exceeded')
        if isinstance(item, str):
            characters += len(item)
            if characters > limit:
                raise MCPError('MCP argument byte limit exceeded')
        elif isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise MCPError('MCP JSON object keys must be strings')
                visit(key, depth + 1)
                visit(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
        elif item is not None and type(item) not in (bool, int, float):
            raise MCPError('MCP arguments must contain only JSON values')

    visit(value, 0)
    try:
        text = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
    except (ValueError, TypeError, RecursionError):
        raise MCPError('MCP invalid JSON arguments') from None
    if len(text) > limit:
        raise MCPError('MCP argument byte limit exceeded')
    return text


def _command(value):
    if (not isinstance(value, list) or not 1 <= len(value) <= 128
            or any(not isinstance(arg, str) or '\x00' in arg or len(arg) > 8192 for arg in value)
            or sum(map(len, value)) > 32768):
        raise MCPError('MCP command must be a bounded argv list')
    command = list(value)
    if command[0] == '{python}':
        command[0] = sys.executable
    executable = Path(command[0])
    if not executable.is_absolute():
        raise MCPError('MCP executable must be absolute or {python}')
    try:
        resolved = executable.resolve(strict=True)
    except (OSError, ValueError, RuntimeError):
        raise MCPError('MCP executable cannot be resolved') from None
    if any(PureWindowsPath(str(path)).suffix.lower() in ('.bat', '.cmd')
           for path in (executable, resolved)):
        raise MCPError('MCP batch executables are forbidden')
    if not resolved.is_file() or not os.access(executable, os.X_OK):
        raise MCPError('MCP executable is not runnable')
    return command


def _servers(servers):
    if not isinstance(servers, dict) or len(servers) > 32:
        raise MCPError('MCP servers must be an object with at most 32 entries')
    validated = {}
    for name, config in servers.items():
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', name):
            raise MCPError('MCP invalid server name')
        if not isinstance(config, dict) or set(config) - {
                'command', 'env', 'timeout_seconds', 'max_response_bytes'}:
            raise MCPError('MCP invalid stdio server configuration')
        command = _command(config.get('command'))
        timeout = config.get('timeout_seconds', 30)
        limit = config.get('max_response_bytes', 262144)
        env = config.get('env', {})
        if type(timeout) not in (int, float) or not 1 <= timeout <= 120:
            raise MCPError('MCP timeout_seconds must be between 1 and 120')
        if type(limit) is not int or not 1024 <= limit <= 2097152:
            raise MCPError('MCP max_response_bytes must be between 1024 and 2097152')
        if not isinstance(env, dict) or len(env) > 64 or any(
                not isinstance(value, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', value)
                for pair in env.items() for value in pair):
            raise MCPError('MCP env must map child names to source environment names')
        validated[name] = {'command': command, 'env': dict(env),
                           'timeout_seconds': timeout, 'max_response_bytes': limit}
    return validated


def _environment(mapping):
    # No HOME, PATH, credentials or Python startup settings are inherited.
    env = {}
    if os.name == 'nt' and 'SystemRoot' in os.environ:
        env['SystemRoot'] = os.environ['SystemRoot']
    for child, source in mapping.items():
        value = os.environ.get(source)
        if value is None or '\x00' in value:
            raise MCPError('MCP explicit environment source missing or invalid')
        env[child] = value
    return env


class _Connection:
    def __init__(self, command, workspace, env, max_bytes):
        self.process = subprocess.Popen(command, cwd=workspace, env=env,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, bufsize=0, shell=False,
                                        start_new_session=os.name != 'nt',
                                        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
        assert self.process.stdin is not None and self.process.stdout is not None
        self.stdin = self.process.stdin
        self.stdout = self.process.stdout
        self.writer = None
        self.max_bytes = max_bytes
        self.failure = None
        self.incoming = queue.Queue(maxsize=64)
        self.queue_lock = threading.Lock()
        self.queued_bytes = 0
        self.received_bytes = 0
        self.server_messages = 0
        self.server_ids = set()
        self.expected_id = None
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _enqueue(self, line):
        with self.queue_lock:
            size = len(line) if line is not None else 0
            if self.queued_bytes + size > self.max_bytes:
                raise MCPError('MCP stdout queue byte limit exceeded')
            try:
                self.incoming.put_nowait(line)
            except queue.Full:
                raise MCPError('MCP stdout queue count limit exceeded') from None
            self.queued_bytes += size

    def take(self, timeout):
        line = self.incoming.get(timeout=timeout)
        if line is not None:
            with self.queue_lock:
                self.queued_bytes -= len(line)
            self.received_bytes += len(line)
            if self.received_bytes > self.max_bytes:
                raise MCPError('MCP operation response byte limit exceeded')
        return line

    def _read(self):
        pending = bytearray()
        try:
            while True:
                chunk = self.stdout.read(4096)
                if not chunk:
                    if pending:
                        self.failure = 'MCP unterminated stdout frame'
                    self._enqueue(None)
                    return
                pending.extend(chunk)
                while b'\n' in pending:
                    end = pending.index(b'\n') + 1
                    if end > self.max_bytes:
                        raise MCPError('MCP stdout byte limit exceeded')
                    line = bytes(pending[:end])
                    message = _decode(line)
                    if 'method' not in message:
                        with self.queue_lock:
                            if type(message.get('id')) is not int or message['id'] != self.expected_id:
                                raise MCPError('MCP duplicate or unexpected response ID')
                            self.expected_id = None
                    self._enqueue(line)
                    del pending[:end]
                if len(pending) > self.max_bytes:
                    raise MCPError('MCP stdout byte limit exceeded')
        except MCPError as exc:
            self.failure = str(exc)
            self.kill()
        except (OSError, ValueError):
            self.failure = 'MCP stdout read failed'

    def kill(self):
        if os.name == 'nt':
            # Best effort tree cleanup, not a Windows Job Object sandbox.
            system_root = os.environ.get('SystemRoot', r'C:\Windows')
            taskkill = str(Path(system_root) / 'System32' / 'taskkill.exe')
            try:
                subprocess.run([taskkill, '/PID', str(self.process.pid), '/T', '/F'],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, env={'SystemRoot': system_root},
                               shell=False, timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
            if self.process.poll() is None:
                self.process.kill()
        else:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def close(self):
        if self.writer is not None and self.writer.is_alive():
            self.kill()
            self.writer.join(timeout=0.2)
        self.stdin.close()
        try:
            self.process.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            pass
        # Also remove descendants retaining pipes after their parent has exited.
        self.kill()
        self.process.wait(timeout=1)
        self.reader.join(timeout=0.3)
        self.stdout.close()


class MCPManager:
    def __init__(self, servers: dict, workspace: Path, approve, cancel_check,
                 read_only: bool = False):
        self.servers = _servers(servers)
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise MCPError('MCP workspace must be an existing directory')
        if not callable(approve) or (cancel_check is not None and not callable(cancel_check)):
            raise MCPError('MCP approval and cancellation callbacks must be callable')
        if type(read_only) is not bool:
            raise MCPError('MCP read_only must be a boolean')
        self.approve = approve
        self.cancel_check = cancel_check
        self.read_only = read_only
        self._connections = {}
        self._next_id = 0

    def _check(self, deadline):
        self._writable()
        if self.cancel_check is not None:
            self.cancel_check()
        if time.monotonic() >= deadline:
            raise MCPError('MCP operation timeout')

    def _request(self, connection, method, params, deadline):
        self._next_id += 1
        with connection.queue_lock:
            connection.expected_id = self._next_id
        self._send(connection, {'jsonrpc': '2.0', 'id': self._next_id,
                                'method': method, 'params': params}, deadline)
        while True:
            self._check(deadline)
            if connection.failure:
                raise MCPError(connection.failure)
            try:
                line = connection.take(timeout=0.02)
            except queue.Empty:
                continue
            if line is None:
                raise MCPError('MCP server exited')
            response = _decode(line)
            if 'method' in response:
                self._server_message(connection, response, deadline)
                continue
            if (type(response.get('id')) is not int or response['id'] != self._next_id
                    or 'method' in response or ('result' in response) == ('error' in response)):
                raise MCPError('MCP malformed or unexpected response ID')
            if 'error' in response:
                raise MCPError('MCP server returned a protocol error')
            if not isinstance(response['result'], dict):
                raise MCPError('MCP result must be an object')
            return response['result']

    def _server_message(self, connection, message, deadline):
        connection.server_messages += 1
        if connection.server_messages > 64:
            raise MCPError('MCP server notification/request count limit exceeded')
        if (not isinstance(message['method'], str) or not message['method']
                or 'result' in message or 'error' in message
                or ('params' in message and not isinstance(message['params'], dict))):
            raise MCPError('MCP malformed server request or notification')
        if 'id' not in message:
            return  # Notifications and their text never become client instructions.
        request_id = message['id']
        if type(request_id) not in (int, str) or len(str(request_id)) > 128:
            raise MCPError('MCP invalid server request ID')
        if request_id in connection.server_ids or len(connection.server_ids) >= 1024:
            raise MCPError('MCP duplicate server request ID or lifetime ID limit exceeded')
        connection.server_ids.add(request_id)
        reply = {'jsonrpc': '2.0', 'id': request_id}
        if message['method'] == 'ping':
            reply['result'] = {}
        else:
            reply['error'] = {'code': -32601, 'message': 'Method not supported'}
        self._send(connection, reply, deadline)

    def _send(self, connection, message, deadline):
        if connection.failure:
            raise MCPError(connection.failure)
        data = (_json_text(message, min(connection.max_bytes, 262144) + 4096) + '\n').encode()
        done = threading.Event()
        errors = []

        def write():
            try:
                view = memoryview(data)
                while view:
                    count = connection.stdin.write(view)
                    if not count:
                        raise OSError('closed pipe')
                    view = view[count:]
            except (OSError, ValueError):
                errors.append(True)
            finally:
                done.set()

        self._check(deadline)
        connection.writer = threading.Thread(target=write, daemon=True)
        connection.writer.start()
        while not done.wait(0.02):
            self._check(deadline)
        self._check(deadline)
        if errors:
            raise MCPError('MCP server write failed')

    def _connection(self, server, deadline):
        if server not in self._connections:
            command = _command(self.servers[server]['command'])
            detail = json.dumps({'server': server, 'command': command,
                                 'cwd': str(self.workspace)}) + '\nTrusted executable: not sandboxed.'
            approval_started = time.monotonic()
            if not self.approve('mcp_start', detail):
                raise MCPError('MCP launch approval denied')
            deadline += time.monotonic() - approval_started
            self._check(deadline)
            command = _command(command)  # Approval may have yielded to a filesystem change.
            env = _environment(self.servers[server]['env'])
            try:
                connection = _Connection(command, self.workspace, env,
                                         self.servers[server]['max_response_bytes'])
            except OSError:
                raise MCPError('MCP server launch failed') from None
            self._connections[server] = connection
            initialized = self._request(connection, 'initialize', {
                'protocolVersion': '2025-11-25', 'capabilities': {},
                'clientInfo': {'name': 'david', 'version': '1'}}, deadline)
            if initialized.get('protocolVersion') not in (
                    '2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05'):
                raise MCPError('MCP unsupported protocol version')
            server_info = initialized.get('serverInfo')
            if (not isinstance(server_info, dict)
                    or not isinstance(server_info.get('name'), str)
                    or not isinstance(server_info.get('version'), str)):
                raise MCPError('MCP malformed serverInfo: name and version must be strings')
            capabilities = initialized.get('capabilities')
            if not isinstance(capabilities, dict) or not isinstance(capabilities.get('tools'), dict):
                raise MCPError('MCP server did not negotiate tools capability')
            self._send(connection, {'jsonrpc': '2.0', 'method': 'notifications/initialized'}, deadline)
        return self._connections[server], deadline

    def _operation(self, server, method, params):
        if not isinstance(server, str) or server not in self.servers:
            raise MCPError('MCP unknown server')
        deadline = time.monotonic() + self.servers[server]['timeout_seconds']
        try:
            self._check(deadline)
            if server in self._connections:
                self._connections[server].received_bytes = 0
                self._connections[server].server_messages = 0
            connection, deadline = self._connection(server, deadline)
            if method == 'tools/list':
                return self._list(connection, deadline)
            result = self._request(connection, method, params, deadline)
            self._validate_result(result)
            return result
        except BaseException:
            connection = self._connections.pop(server, None)
            if connection is not None:
                connection.close()
            raise

    @staticmethod
    def _validate_result(result):
        if not isinstance(result.get('content'), list):
            raise MCPError('MCP tool result content must be an array')
        if 'isError' in result and type(result['isError']) is not bool:
            raise MCPError('MCP tool result isError must be a boolean')
        if 'structuredContent' in result and not isinstance(result['structuredContent'], dict):
            raise MCPError('MCP structuredContent must be an object')
        for item in result['content']:
            if not isinstance(item, dict) or not isinstance(item.get('type'), str):
                raise MCPError('MCP malformed tool content')
            kind = item['type']
            fields = {'text': ('text',), 'image': ('data', 'mimeType'),
                      'audio': ('data', 'mimeType'), 'resource_link': ('uri', 'name')}
            if kind in fields:
                if any(not isinstance(item.get(field), str) for field in fields[kind]):
                    raise MCPError('MCP malformed tool content fields')
            elif kind == 'resource':
                if not isinstance(item.get('resource'), dict):
                    raise MCPError('MCP malformed embedded resource')
            else:
                raise MCPError('MCP unsupported tool content type')

    def _list(self, connection, deadline):
        tools = []
        names = set()
        cursors = set()
        params = {}
        for _ in range(32):
            page = self._request(connection, 'tools/list', params, deadline)
            entries = page.get('tools')
            if not isinstance(entries, list) or len(tools) + len(entries) > 1024:
                raise MCPError('MCP invalid tools list or tool count limit exceeded')
            for tool in entries:
                if (not isinstance(tool, dict) or not isinstance(tool.get('name'), str)
                        or not 1 <= len(tool['name']) <= 128
                        or tool['name'] in names or not isinstance(tool.get('inputSchema'), dict)):
                    raise MCPError('MCP malformed or duplicate tool definition')
                names.add(tool['name'])
                tools.append(tool)
            if 'nextCursor' not in page:
                return {'tools': tools}
            cursor = page['nextCursor']
            if not isinstance(cursor, str) or len(cursor) > 4096 or cursor in cursors:
                raise MCPError('MCP invalid or repeated pagination cursor')
            cursors.add(cursor)
            params = {'cursor': cursor}
        raise MCPError('MCP pagination page limit exceeded')

    def list_tools(self, server: str) -> dict:
        self._writable()
        return self._operation(server, 'tools/list', {})

    def _writable(self):
        if self.read_only:
            raise MCPError('MCP is disabled in read-only mode: servers are not sandboxed')

    def call_tool(self, server: str, name: str, arguments: dict) -> dict:
        self._writable()
        if not isinstance(server, str) or server not in self.servers:
            raise MCPError('MCP unknown server')
        if not isinstance(name, str) or not 1 <= len(name) <= 128 or '\x00' in name:
            raise MCPError('MCP invalid tool name')
        if not isinstance(arguments, dict):
            raise MCPError('MCP tool arguments must be an object')
        detail = _json_text({'server': server, 'tool': name, 'arguments': arguments},
                            min(self.servers[server]['max_response_bytes'], 262144))
        approved_arguments = json.loads(detail)['arguments']
        params = {'name': name, 'arguments': approved_arguments}
        # A fresh connection consumes one ID for initialize before tools/call.
        # Validate the full prospective frame, not the shallower approval display:
        # its params wrapper, keys and ID also consume depth, nodes and bytes.
        request_id = self._next_id + (1 if server in self._connections else 2)
        _json_text({'jsonrpc': '2.0', 'id': request_id,
                    'method': 'tools/call', 'params': params},
                   min(self.servers[server]['max_response_bytes'], 262144) + 4096)
        try:
            approved = self.approve('mcp_call', detail)
        except BaseException:
            connection = self._connections.pop(server, None)
            if connection is not None:
                connection.close()
            raise
        if not approved:
            raise MCPError('MCP call approval denied')
        return self._operation(server, 'tools/call', params)

    def close(self) -> None:
        for connection in self._connections.values():
            connection.close()
        self._connections.clear()
