"""Explicit local session persistence; conversations are not encrypted.

POSIX storage uses descriptor-relative, no-follow operations. Other platforms
use bounded lstat/identity checks and exclusive temporary creation. The portable
path-based backend rejects links/reparse points but cannot prevent a hostile
same-user process racing directory replacement between checks and filesystem
operations. It is not the POSIX backend's descriptor-pinned boundary. Windows
privacy depends on the storage directory's inherited ACL, not POSIX mode bits.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import stat
import uuid


class SessionError(ValueError):
    """An invalid, unsafe, missing, or incompatible saved session."""


MAX_BYTES = 8 * 1024 * 1024
MAX_DEPTH = 64
MAX_NODES = 200_000
MAX_ENTRIES = 1000


def _json_tree(value):
    """Bound traversal before serializing, including cyclic caller input."""
    remaining = MAX_NODES

    def visit(item, depth):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > MAX_DEPTH:
            raise SessionError('Session JSON exceeds structural limits.')
        kind = type(item)
        if kind is str:
            try:
                if len(item) > MAX_BYTES or len(item.encode('utf-8')) > MAX_BYTES:
                    raise SessionError('Session string is too large.')
            except UnicodeError as exc:
                raise SessionError('Session strings must be valid UTF-8.') from exc
        elif kind is dict:
            if len(item) > MAX_NODES:
                raise SessionError('Session object is too large.')
            for key, val in item.items():
                if type(key) is not str:
                    raise SessionError('Session JSON keys must be strings.')
                visit(key, depth + 1)
                visit(val, depth + 1)
        elif kind is list:
            if len(item) > MAX_NODES:
                raise SessionError('Session array is too large.')
            for val in item:
                visit(val, depth + 1)
        elif kind is float:
            if not math.isfinite(item):
                raise SessionError('Session numbers must be finite.')
        elif kind is int:
            if item.bit_length() > 4096:
                raise SessionError('Session integer is too large.')
        elif item is not None and kind is not bool:
            raise SessionError('Session contains a non-JSON value.')

    visit(value, 1)


def _payload(value):
    required = {'history', 'requests', 'journal', 'transcript'}
    if type(value) is not dict or not required <= value.keys() or value.keys() - (required | {'title', 'tool_mode'}):
        raise SessionError('Invalid session payload fields.')
    if any(type(value[key]) is not list for key in required):
        raise SessionError('Session payload collections must be lists.')
    if any(type(item) is not str for item in value['requests']):
        raise SessionError('Session requests must be strings.')
    if any(type(item) is not dict for item in value['transcript']):
        raise SessionError('Session transcript must contain event objects.')
    if 'title' in value and type(value['title']) is not str:
        raise SessionError('Session title must be a string.')
    if 'tool_mode' in value:
        _tool_mode(value['tool_mode'])
    _json_tree(value)
    _history(value['history'], value.get('tool_mode'))
    _transcript(value['transcript'])


def _transcript(events):
    """Validate replay inputs, not arbitrary tool-specific JSON display data."""
    for event in events:
        if 'type' not in event:
            # Historical display records used kind/text; keep them unchanged.
            if type(event.get('kind')) is str and event['kind'] and type(event.get('text')) is str:
                continue
            raise SessionError('Invalid legacy session transcript event.')
        kind = event['type']
        if kind not in ('message', 'text', 'tool', 'summary'):
            raise SessionError('Invalid session transcript event type.')
        if kind != 'tool':
            if type(event.get('text')) is not str:
                raise SessionError('Session transcript text must be a string.')
            if kind == 'message' and type(event.get('role')) is not str:
                raise SessionError('Session transcript message role must be a string.')
        else:
            if type(event.get('name')) is not str or not event['name']:
                raise SessionError('Session transcript tool name must be a nonempty string.')
            if type(event.get('result')) is not dict:
                raise SessionError('Session transcript tool result must be an object.')
            # These fields drive outcomes and command summaries. Other result
            # fields are tool-specific JSON, rendered as data rather than read.
            fields = {'error': str, 'ok': bool, 'timed_out': bool, 'exit_code': int}
            if event['name'] == 'run_command':
                fields.update({'name': str, 'output': str, 'truncated': bool})
            for field, expected in fields.items():
                if field in event['result'] and type(event['result'][field]) is not expected:
                    raise SessionError('Invalid session transcript tool result field: ' + field)


def _tool_mode(value):
    if type(value) is not str or value not in ('native', 'json'):
        raise SessionError('Session tool mode must be native or json.')


def _history(turns, tool_mode=None):
    """Validate complete turns using the recorded protocol, never guess a mode.

    Native assistant content is opaque text, even when it resembles a JSON tool
    request. JSON mode tracks content-encoded tool calls and their user-role
    results; native call batches are not valid in that mode. None retains the
    original mixed-format validator for payloads predating the tool_mode field.
    This conservative legacy fallback still rejects ambiguous dangling JSON
    calls rather than reinterpreting them as native final answers.
    """
    for turn in turns:
        if type(turn) is not list or len(turn) < 2:
            raise SessionError('History must contain complete message groups.')
        pending, seen = set(), set()
        json_pending = None
        for index, message in enumerate(turn):
            if type(message) is not dict or not {'role', 'content'} <= message.keys():
                raise SessionError('Invalid history message.')
            role, content = message['role'], message['content']
            allowed = {'user': {'role', 'content'},
                       'assistant': {'role', 'content', 'tool_calls'},
                       'tool': {'role', 'content', 'tool_call_id'}}
            if not isinstance(role, str) or role not in allowed or message.keys() - allowed[role]:
                raise SessionError('Invalid history role or message fields.')
            if content is not None and not isinstance(content, str):
                raise SessionError('History content must be a string or null.')
            if index == 0:
                if role != 'user' or not isinstance(content, str):
                    raise SessionError('A complete turn must start with a user request.')
                continue
            if role == 'tool':
                ident = message.get('tool_call_id')
                if not isinstance(ident, str) or ident not in pending or not isinstance(content, str):
                    raise SessionError('Dangling or duplicate tool result.')
                pending.remove(ident)
            elif role == 'user':
                if not json_pending or not isinstance(content, str) or not content.startswith(
                        'TOOL_RESULT (untrusted data) ' + json_pending + ': '):
                    raise SessionError('Unexpected user message inside a saved turn.')
                json_pending = None
            else:
                if pending or json_pending:
                    raise SessionError('Assistant message precedes pending tool results.')
                calls = message.get('tool_calls', [])
                if type(calls) is not list:
                    raise SessionError('Invalid native tool call list.')
                if tool_mode == 'json' and calls:
                    raise SessionError('Native tool calls are invalid in JSON-mode history.')
                for call in calls:
                    if type(call) is not dict or set(call) != {'id', 'type', 'function'}:
                        raise SessionError('Invalid native tool call.')
                    ident, function = call['id'], call['function']
                    if (not isinstance(ident, str) or not ident or ident in seen
                            or call['type'] != 'function' or type(function) is not dict
                            or set(function) != {'name', 'arguments'}
                            or not isinstance(function['name'], str) or not function['name']
                            or not isinstance(function['arguments'], str)):
                        raise SessionError('Invalid or duplicate native tool call.')
                    seen.add(ident)
                    pending.add(ident)
                if tool_mode != 'native' and not calls and isinstance(content, str):
                    try:
                        protocol = json.loads(content)
                    except (ValueError, RecursionError):
                        protocol = None
                    if isinstance(protocol, dict) and protocol.get('type') == 'tool':
                        if (set(protocol) != {'type', 'name', 'arguments'}
                                or not isinstance(protocol['name'], str) or not protocol['name']
                                or type(protocol['arguments']) is not dict):
                            raise SessionError('Invalid JSON-mode tool call.')
                        json_pending = protocol['name']
                if not pending and not json_pending and index != len(turn) - 1:
                    raise SessionError('A final assistant message must end the turn.')
            if index == len(turn) - 1 and (role != 'assistant' or pending or json_pending):
                raise SessionError('Saved turn has no final assistant response.')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SessionError('Duplicate key in saved session JSON.')
        result[key] = value
    return result


def _invalid_constant(value):
    raise SessionError('Non-finite number in saved session JSON.')


def _decode(raw):
    if len(raw) > MAX_BYTES:
        raise SessionError('Saved session exceeds 8 MiB.')
    # Reject excessive nesting before the recursive stdlib decoder sees it.
    depth, quoted, escaped = 0, False, False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            if depth > MAX_DEPTH:
                raise SessionError('Session JSON exceeds nesting limit.')
        elif byte in (93, 125):
            depth -= 1
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                       parse_constant=_invalid_constant)
    _envelope(value)
    return value


def _envelope(value):
    fields = {'version', 'workspace', 'model_key', 'updated_at', 'payload'}
    if type(value) is not dict or set(value) != fields:
        raise SessionError('Invalid saved session envelope fields.')
    if type(value['version']) is not int or value['version'] != 1:
        raise SessionError('Unsupported saved session version.')
    _json_tree(value)
    workspace = value['workspace']
    if (not isinstance(workspace, str) or not Path(workspace).is_absolute()
            or '..' in Path(workspace).parts or str(Path(workspace)) != workspace):
        raise SessionError('Saved workspace must be a canonical absolute path.')
    if not isinstance(value['model_key'], str) or not value['model_key']:
        raise SessionError('Invalid saved model fingerprint.')
    try:
        stamp = value['updated_at']
        if not isinstance(stamp, str) or 'T' not in stamp:
            raise ValueError()
        parsed = datetime.fromisoformat(stamp)
        offset = parsed.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise ValueError()
    except (TypeError, ValueError) as exc:
        raise SessionError('Saved timestamp must be UTC ISO format.') from exc
    _payload(value['payload'])


def _id(value):
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{32}', value) is None:
        raise SessionError('Session ID must be 32 lowercase hexadecimal characters.')
    return value


def _identity(workspace, model_key):
    if not isinstance(model_key, str) or not model_key:
        raise SessionError('Invalid model fingerprint.')
    return str(Path(workspace).resolve()), model_key


def _safe_stat(info, directory=False):
    reparse = getattr(info, 'st_file_attributes', 0) & 0x400
    if reparse or stat.S_ISLNK(info.st_mode):
        raise SessionError('Session storage must not contain links or reparse points.')
    if directory:
        # Directory st_nlink normally counts child directories, not aliases.
        if not stat.S_ISDIR(info.st_mode):
            raise SessionError('Session storage ancestor is not a directory.')
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise SessionError('Session file must be regular and have a single link.')


def _file_stat(fd, name):
    try:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    _safe_stat(info)
    return info


def _descriptor_backend():
    return (os.name != 'nt'
            and all(hasattr(os, name) for name in ('O_NOFOLLOW', 'O_DIRECTORY',
                                                    'O_NONBLOCK', 'fchmod'))
            and all(func in os.supports_dir_fd for func in
                    (os.open, os.stat, os.mkdir, os.unlink, os.rename))
            and os.stat in os.supports_follow_symlinks
            and os.scandir in os.supports_fd)


def _same_file(left, right):
    if left is None or right is None:
        return left is right
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _portable_file_stat(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    _safe_stat(info)
    return info


class _PortableDirectory:
    """Checked paths, not pinned directories; see module threat-model caveat."""

    def __init__(self, path, create):
        self.path = path
        self.ancestors = []
        self.exists = False
        if len(path.parts) > 256:
            raise SessionError('Session storage path has too many ancestors.')
        for ancestor in reversed((path, *path.parents)):
            self.check()
            try:
                info = ancestor.lstat()
            except FileNotFoundError:
                if not create:
                    return
                try:
                    ancestor.mkdir(mode=0o700)
                except FileExistsError:
                    pass
                info = ancestor.lstat()
            _safe_stat(info, directory=True)
            self.ancestors.append((ancestor, info))
        self.check()
        if create and os.name != 'nt':
            if hasattr(os, 'getuid') and self.ancestors[-1][1].st_uid != os.getuid():
                raise SessionError('Session directory is not owned by the current user.')
            path.chmod(0o700)
            self.check()
        self.exists = True

    def check(self):
        for path, before in self.ancestors:
            info = path.lstat()
            _safe_stat(info, directory=True)
            if not _same_file(before, info):
                raise SessionError('Session directory changed during access.')

    def read(self, session_id):
        self.check()
        path = self.path / (_id(session_id) + '.json')
        before = _portable_file_stat(path)
        if before is None:
            raise SessionError('Saved session does not exist.')
        flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0)
        if os.name != 'nt':
            flags |= getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        source = os.open(path, flags)
        with os.fdopen(source, 'rb') as stream:
            info = os.fstat(stream.fileno())
            _safe_stat(info)
            self.check()
            if not _same_file(info, before) or not _same_file(info, _portable_file_stat(path)):
                raise SessionError('Session file changed during access.')
            if info.st_size > MAX_BYTES:
                raise SessionError('Saved session exceeds 8 MiB.')
            raw = stream.read(MAX_BYTES + 1)
            _safe_stat(os.fstat(stream.fileno()))
            self.check()
            if not _same_file(info, _portable_file_stat(path)):
                raise SessionError('Session file changed during access.')
        return _decode(raw)

    def save(self, session_id, data):
        self.check()
        path = self.path / (session_id + '.json')
        before = _portable_file_stat(path)
        temporary = self.path / ('.' + uuid.uuid4().hex + '.tmp')
        owned = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0)
            out = os.open(temporary, flags, 0o600)
            with os.fdopen(out, 'wb') as stream:
                owned = os.fstat(stream.fileno())
                _safe_stat(owned)
                self.check()
                if not _same_file(owned, _portable_file_stat(temporary)):
                    raise SessionError('Session temporary file changed during access.')
                if os.name != 'nt':
                    if hasattr(os, 'fchmod'):
                        os.fchmod(stream.fileno(), 0o600)
                    else:
                        temporary.chmod(0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                _safe_stat(os.fstat(stream.fileno()))
            self.check()
            if (not _same_file(before, _portable_file_stat(path))
                    or not _same_file(owned, _portable_file_stat(temporary))):
                raise SessionError('Session file changed during access.')
            # Close the temporary before replacing: required by native Windows.
            os.replace(temporary, path)
        finally:
            # Never delete a pre-existing collision or somebody else's replacement.
            if owned is not None:
                try:
                    self.check()
                    if _same_file(owned, _portable_file_stat(temporary)):
                        temporary.unlink()
                except (OSError, SessionError):
                    pass


class SessionStore:
    def __init__(self, directory: Path):
        # Do not resolve: that would hide symlink ancestors before inspection.
        path = Path(directory)
        self.directory = path if path.is_absolute() else Path.cwd() / path

    @contextmanager
    def _directory(self, create=False):
        fd = None
        try:
            if '..' in self.directory.parts:
                raise SessionError('Parent traversal is not allowed in session storage paths.')
            if not _descriptor_backend():
                portable = _PortableDirectory(self.directory, create)
                yield portable if portable.exists else None
                return
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            fd = os.open(self.directory.anchor, flags)
            for part in self.directory.parts[1:]:
                try:
                    info = os.stat(part, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    if not create:
                        yield None
                        return
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                    info = os.stat(part, dir_fd=fd, follow_symlinks=False)
                _safe_stat(info, directory=True)
                child = os.open(part, flags, dir_fd=fd)
                actual = os.fstat(child)
                if (actual.st_dev, actual.st_ino) != (info.st_dev, info.st_ino):
                    os.close(child)
                    raise SessionError('Session directory changed during access.')
                os.close(fd)
                fd = child
            if create:
                if hasattr(os, 'getuid') and os.fstat(fd).st_uid != os.getuid():
                    raise SessionError('Session directory is not owned by the current user.')
                os.fchmod(fd, 0o700)
            yield fd
        except OSError as exc:
            raise SessionError('Cannot safely access session storage.') from exc
        finally:
            if fd is not None:
                os.close(fd)

    def save(self, session_id: str, workspace: Path, model_key: str, payload: dict) -> None:
        _id(session_id)
        canonical, model_key = _identity(workspace, model_key)
        envelope = {'version': 1, 'workspace': canonical,
                    'model_key': model_key, 'updated_at': datetime.now(timezone.utc).isoformat(),
                    'payload': payload}
        _envelope(envelope)
        data = bytearray()
        encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        for chunk in encoder.iterencode(envelope):
            data.extend(chunk.encode('utf-8'))
            if len(data) > MAX_BYTES:
                raise SessionError('Saved session exceeds 8 MiB.')
        with self._directory(create=True) as fd:
            if isinstance(fd, _PortableDirectory):
                fd.save(session_id, data)
                return
            name = session_id + '.json'
            _file_stat(fd, name)
            temporary = '.' + uuid.uuid4().hex + '.tmp'
            try:
                out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                              0o600, dir_fd=fd)
                with os.fdopen(out, 'wb') as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                _file_stat(fd, name)
                os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
                os.fsync(fd)
            finally:
                try:
                    os.unlink(temporary, dir_fd=fd)
                except FileNotFoundError:
                    pass

    def _read(self, fd, session_id):
        try:
            if isinstance(fd, _PortableDirectory):
                return fd.read(session_id)
            name = _id(session_id) + '.json'
            before = _file_stat(fd, name)
            if before is None:
                raise SessionError('Saved session does not exist.')
            source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(source, 'rb') as stream:
                info = os.fstat(stream.fileno())
                _safe_stat(info)
                if (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino):
                    raise SessionError('Session file changed during access.')
                if info.st_size > MAX_BYTES:
                    raise SessionError('Saved session exceeds 8 MiB.')
                return _decode(stream.read(MAX_BYTES + 1))
        except SessionError:
            raise
        except (OSError, ValueError, RecursionError) as exc:
            raise SessionError('Cannot read saved session: invalid or unsafe file.') from exc

    def load(self, session_id: str, workspace: Path, model_key: str, *, tool_mode=None) -> dict:
        """Load without changing the file; optionally require the caller's tool mode.

        Explicit payload modes must match, independently of the opaque model key.
        Legacy payloads without tool_mode first pass the original validator,
        then the caller's mode validator if supplied. They are never rewritten
        or relaxed based on the caller's mode. None preserves the existing
        inspection API; resume callers must supply their configured mode.
        """
        if tool_mode is not None:
            _tool_mode(tool_mode)
        _id(session_id)
        canonical, model_key = _identity(workspace, model_key)
        with self._directory() as fd:
            if fd is None:
                raise SessionError('Saved session does not exist.')
            data = self._read(fd, session_id)
        if data['workspace'] != canonical or data['model_key'] != model_key:
            raise SessionError('Session belongs to a different workspace or model.')
        if (tool_mode is not None and 'tool_mode' in data['payload']
                and data['payload']['tool_mode'] != tool_mode):
            raise SessionError('Session belongs to a different tool mode.')
        if tool_mode is not None and 'tool_mode' not in data['payload']:
            _history(data['payload']['history'], tool_mode)
        return data['payload']

    def list(self, workspace: Path, model_key: str) -> list[dict]:
        canonical, model_key = _identity(workspace, model_key)
        result = []
        with self._directory() as fd:
            if fd is None:
                return result
            with os.scandir(fd.path if isinstance(fd, _PortableDirectory) else fd) as entries:
                for number, entry in enumerate(entries):
                    if number >= MAX_ENTRIES:
                        break
                    if not entry.name.endswith('.json'):
                        continue
                    ident = entry.name[:-5]
                    try:
                        data = self._read(fd, ident)
                    except SessionError:
                        continue
                    if data['workspace'] == canonical and data['model_key'] == model_key:
                        result.append({'id': ident, 'updated_at': data['updated_at'],
                                       'title': data['payload'].get('title', '')})
            if isinstance(fd, _PortableDirectory):
                fd.check()
        return result
