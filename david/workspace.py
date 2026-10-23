"""Approved workspace tools. Named commands are trusted code, NOT a sandbox.

Path checks reject links and special files. They are not isolation against a
hostile local process racing directory renames or the final check/replace gap;
use an OS sandbox for that. Protected paths also protect their entire subtree.

Approved commands run with the user's privileges: they can read outside the
workspace, access credentials on disk, and modify protected files. A minimal
environment and disposable HOME reduce accidental exposure, not malicious code
execution. Timeout cleanup uses POSIX process groups or Windows taskkill /T;
intentionally detached/reparented descendants can escape those mechanisms.
The caller must sanitize text for terminal display and bound serialized output.

Managed UTF-8 operations keep snapshots in RAM and merge each turn's first
before-state with its last after-state. end_turn()/changes() return metadata;
only explicit export_journal() exposes contents for optional persistence. Undo
is not a subprocess rollback or a crash-consistent filesystem transaction.
Ordinary failures trigger guarded rollback; rollback failures identify the
remaining paths. Imported journals require validation, live-state checks, and
approval just like in-memory undo. Limits: 20 files per patch, 200 paths per
turn, 100 turns / 2000 paths / 16 MiB of snapshot bytes per session journal.
"""
from __future__ import annotations

from collections.abc import Callable
import difflib
import fnmatch
import hashlib
import json
import math
import os
from pathlib import Path, PureWindowsPath
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata

from pathspec import GitIgnoreSpec


class ToolError(Exception):
    """A rejected tool operation, safe for the caller to report."""


DEFAULTS = {
    'read_only': False, 'enable_commands': False, 'max_file_bytes': 262144,
    'max_output_chars': 16000, 'max_scan_entries': 2000,
    'command_output_bytes': 131072,
    'denied_names': ['.git', '.ssh', '.aws', '.env', 'setting.json'],
    'denied_globs': ['.env.*', '*.pem', '*.key', '*.p12', '*.pfx', '*credentials*'],
}


class WorkspaceTools:
    def __init__(self, root: Path, security: dict, commands: dict,
                 approve: Callable[[str, str], bool],
                 protected_paths: list[Path] | None = None,
                 cancel_check: Callable[[], None] | None = None):
        if cancel_check is not None and not callable(cancel_check):
            raise ToolError('cancel_check must be callable')
        self.cancel_check = cancel_check
        self.root = Path(root).absolute()
        self.security = {**DEFAULTS, **security}
        for key in ('read_only', 'enable_commands'):
            if type(self.security[key]) is not bool:
                raise ToolError(f'{key} must be boolean')
        for key in ('max_file_bytes', 'max_output_chars', 'max_scan_entries', 'command_output_bytes'):
            if type(self.security[key]) is not int or self.security[key] < 1:
                raise ToolError(f'{key} must be a positive integer')
        for key in ('denied_names', 'denied_globs'):
            value = self.security[key]
            if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
                raise ToolError(f'{key} must be a list of nonempty strings')
            self.security[key] = list(value)
        self._turn: dict[str, dict] = {}
        self._undo: list[dict] = []
        self.commands = dict(commands)
        self.approve = approve
        self.protected_paths = [(Path(p) if Path(p).is_absolute() else self.root / p).resolve()
                                for p in (protected_paths or [])]
        self._path('.')  # Check raw ancestors before canonicalization (do not hide symlinks).
        self.root = self.root.resolve(strict=True)
        self._path('.')  # Protected-path comparisons use the canonical selected root.

    def schemas(self) -> list:
        string = {'type': 'string'}
        integer = {'type': 'integer', 'minimum': 1}
        specs = {
            'read_file': ('Read UTF-8 lines and full-file SHA256.',
                          {'path': string, 'offset': integer, 'limit': integer}, ['path']),
            'list_files': ('Recursively list allowed workspace entries, bounded by scan limits.',
                           {'path': string, 'max_entries': integer}, []),
            'search_files': ('Find literal substrings in UTF-8 files (not regex).',
                             {'pattern': string, 'path': string, 'max_results': integer, 'glob': string}, ['pattern']),
        }
        if not self.security['read_only']:
            specs.update({
                'move_file': ('Move a regular UTF-8 file without overwriting; requires digest and approval.',
                              {'source': string, 'destination': string, 'expected_sha256': string},
                              ['source', 'destination', 'expected_sha256']),
                'delete_file': ('Delete a regular UTF-8 file; requires digest and approval.',
                                {'path': string, 'expected_sha256': string}, ['path', 'expected_sha256']),
                'make_directory': ('Create one directory; parent must exist. Requires approval.',
                                   {'path': string}, ['path']),
                'apply_patch': ('Create/replace up to 20 UTF-8 files as one approved transaction.',
                                {'changes': {'type': 'array', 'minItems': 1, 'maxItems': 20,
                                 'items': {'type': 'object', 'properties': {
                                     'path': string, 'content': string, 'expected_sha256': string},
                                     'required': ['path', 'content'], 'additionalProperties': False}}},
                                ['changes']),
                'write_file': ('Create UTF-8 text or overwrite with expected_sha256 from read_file; requires approval.',
                               {'path': string, 'content': string, 'expected_sha256': string}, ['path', 'content']),
                'replace_text': ('Replace exactly one literal occurrence using expected_sha256; requires approval.',
                                 {'path': string, 'old': string, 'new': string, 'expected_sha256': string},
                                 ['path', 'old', 'new', 'expected_sha256']),
            })
            if self.security['enable_commands'] and self.commands:
                specs['run_command'] = (
                    'Run an approved, configured recipe as trusted code (not sandboxed).',
                    {'name': {'type': 'string', 'enum': list(self.commands)}}, ['name'])
        return [{'type': 'function', 'function': {
            'name': name, 'description': description, 'parameters': {
                'type': 'object', 'properties': properties, 'required': required,
                'additionalProperties': False}}}
                for name, (description, properties, required) in specs.items()]

    def _poll(self):
        if self.cancel_check is not None:
            self.cancel_check()

    def execute(self, name: str, arguments: dict) -> dict:
        self._poll()
        schemas = {s['function']['name']: s['function']['parameters'] for s in self.schemas()}
        if name not in schemas or not isinstance(arguments, dict):
            raise ToolError('Unknown tool or invalid arguments')
        schema = schemas[name]
        if set(arguments) - set(schema['properties']) or set(schema['required']) - set(arguments):
            raise ToolError('Unexpected or missing arguments')
        for key, value in arguments.items():
            kind = schema['properties'][key]['type']
            if kind == 'string' and not isinstance(value, str):
                raise ToolError(f'{key} must be a string')
            if kind == 'integer' and (type(value) is not int or value < 1):
                raise ToolError(f'{key} must be a positive integer')
        try:
            return getattr(self, '_' + name)(**arguments)
        except (OSError, UnicodeError, ValueError) as exc:
            raise ToolError(f'Operation failed: {type(exc).__name__}') from exc

    @staticmethod
    def _check_stat(info):
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ToolError('Symlinks and reparse points are forbidden')
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ToolError('Special files are forbidden')
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            raise ToolError('Hardlinked files are forbidden')

    def _path(self, value: str, missing: bool = False) -> Path:
        if (not isinstance(value, str) or not value or '\\' in value or ':' in value
                or PureWindowsPath(value).drive or Path(value).is_absolute()
                or any(ord(c) < 32 for c in value)):
            raise ToolError('Use a relative workspace path')
        parts = Path(value).parts
        if '..' in parts or any(p.endswith((' ', '.')) for p in parts):
            raise ToolError('Parent traversal and ambiguous paths are forbidden')
        current = Path(self.root.anchor)
        for part in self.root.parts[1:]:
            current /= part
            self._check_stat(current.lstat())
        if not self.root.is_dir():
            raise ToolError('Workspace root must be a directory')
        self._check_protected(self.root)
        for index, part in enumerate(parts):
            lower = part.casefold()
            if lower in [n.casefold() for n in self.security['denied_names']] or any(
                    fnmatch.fnmatchcase(lower, g.casefold()) for g in self.security['denied_globs']):
                raise ToolError('Protected name')
            if lower.split('.')[0] in {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)),
                                      *(f'lpt{i}' for i in range(1, 10))}:
                raise ToolError('Reserved device name')
            current /= part
            self._check_protected(current)
            try:
                self._check_stat(current.lstat())
            except FileNotFoundError:
                if missing and index == len(parts) - 1:
                    return current
                raise ToolError('Path does not exist') from None
        return current

    def _check_protected(self, path: Path):
        candidate = tuple(unicodedata.normalize('NFC', part).casefold() for part in path.parts)
        for protected in self.protected_paths:
            blocked = tuple(unicodedata.normalize('NFC', part).casefold() for part in protected.parts)
            if candidate[:len(blocked)] == blocked:
                raise ToolError('Protected path or subtree')
            try:
                protected_info = protected.stat()
            except FileNotFoundError:
                continue  # Lexical protection still applies to missing targets.
            # Check only this path and its ancestors, never scan the filesystem.
            # Compare against the protected object itself, not its shared parents.
            for ancestor in (path, *path.parents):
                try:
                    same = os.path.samestat(ancestor.stat(), protected_info)
                except FileNotFoundError:
                    continue
                if same:
                    raise ToolError('Protected path or subtree')

    def _read_bytes(self, path: str) -> bytes:
        target = self._path(path)
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        fd = os.open(target, flags)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            self._check_stat(info)
            if not stat.S_ISREG(info.st_mode):
                raise ToolError('Not a regular file')
            cap = self.security['max_file_bytes']
            if info.st_size > cap:
                raise ToolError('File exceeds size limit')
            data = stream.read(cap + 1)
        if len(data) > cap:
            raise ToolError('File exceeds size limit')
        text = data.decode('utf-8')
        if any(ord(c) < 32 and c not in '\t\n\r' for c in text):
            raise ToolError('Binary content is forbidden')
        return data

    @staticmethod
    def _check_executable(value: str):
        executable = Path(value).name.casefold()
        if executable.endswith(('.cmd', '.bat')) or executable.removesuffix('.exe') in {
                'sh', 'bash', 'dash', 'zsh', 'ksh', 'fish', 'csh', 'tcsh', 'cmd', 'powershell', 'pwsh'}:
            raise ToolError('Shells and batch files are forbidden')

    def _run_command(self, name: str) -> dict:
        if not self.security['enable_commands'] or self.security['read_only'] or name not in self.commands:
            raise ToolError('Command is not enabled or configured')
        recipe = self.commands[name]
        if not isinstance(recipe, dict):
            raise ToolError('Invalid command recipe')
        configured = recipe.get('argv')
        if not isinstance(configured, list) or not configured or any(
                not isinstance(arg, str) or '\x00' in arg for arg in configured):
            raise ToolError('Command argv must be a nonempty string list')
        argv = [arg.replace('{python}', sys.executable) for arg in configured]
        self._check_executable(argv[0])
        search_path = os.defpath
        if os.name == 'nt':
            search_path = str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32')
        resolved = shutil.which(argv[0], path=search_path)
        if not resolved:
            raise ToolError('Command executable not found; configure an absolute path')
        argv[0] = os.path.abspath(resolved)
        self._check_executable(argv[0])
        timeout = recipe.get('timeout_seconds', 60)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ToolError('Command timeout must be positive and finite')
        self._path('.')
        detail = json.dumps({'name': name, 'argv': argv, 'cwd': str(self.root)}, ensure_ascii=False)
        if not self.approve('run_command', detail):
            raise ToolError('User cancelled the command')
        self._path('.')
        with tempfile.TemporaryDirectory(prefix='david-command-') as home:
            env = {'PATH': search_path, 'HOME': home, 'USERPROFILE': home,
                   'TMPDIR': home, 'TMP': home, 'TEMP': home,
                   'LANG': 'C.UTF-8', 'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1'}
            if os.name == 'nt':
                env['SystemRoot'] = os.environ.get('SystemRoot', r'C:\Windows')
                env['WINDIR'] = env['SystemRoot']
            return self._capture_command(name, argv, env, timeout)

    @staticmethod
    def _kill_tree(process, env):
        if os.name == 'nt':
            taskkill = str(Path(env['SystemRoot']) / 'System32' / 'taskkill.exe')
            try:
                subprocess.run([taskkill, '/PID', str(process.pid), '/T', '/F'],
                               env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, shell=False, timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            if process.poll() is None:
                process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _capture_command(self, name, argv, env, timeout):
        self._poll()
        process = subprocess.Popen(argv, cwd=self.root, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   shell=False, start_new_session=os.name != 'nt',
                                   creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
        assert process.stdout is not None
        stream = process.stdout
        captured = bytearray()
        state = {'truncated': False}
        cap = self.security['command_output_bytes']
        def drain():
            try:
                while True:
                    chunk = stream.read(8192)
                    if not chunk:
                        return
                    remaining = cap - len(captured)
                    captured.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        state['truncated'] = True
            finally:
                stream.close()
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        timed_out = False
        try:
            while process.poll() is None or reader.is_alive():
                self._poll()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                if process.poll() is None:
                    try:
                        process.wait(timeout=min(0.05, remaining))
                    except subprocess.TimeoutExpired:
                        pass
                else:
                    reader.join(min(0.05, remaining))
            if timed_out:
                self._kill_tree(process, env)
                process.wait(timeout=5)
                reader.join(5)
        except BaseException:
            self._kill_tree(process, env)
            process.wait(timeout=5)
            reader.join(5)
            raise
        output = captured.decode('utf-8', errors='replace')
        char_cap = self.security['max_output_chars']
        return {'name': name, 'exit_code': process.returncode, 'timed_out': timed_out,
                'undoable': False,
                'undo_warning': 'Subprocess changes are not tracked or reverted by managed undo.',
                'output': output[:char_cap],
                'truncated': state['truncated'] or len(output) > char_cap}

    def _snapshot(self, path: str, expected: str | None):
        target = self._path(path, missing=True)
        if not target.exists():
            if expected is not None:
                raise ToolError('Expected file no longer exists')
            return target, None
        if expected is None or len(expected) != 64 or any(c not in '0123456789abcdef' for c in expected):
            raise ToolError('Overwriting requires the exact SHA256 returned by read_file')
        data = self._read_bytes(path)
        if hashlib.sha256(data).hexdigest() != expected:
            raise ToolError('File changed; read it again before editing')
        return target, data

    def _write_file(self, path: str, content: str, expected_sha256: str | None = None) -> dict:
        target, before = self._snapshot(path, expected_sha256)
        return self._commit_text('write_file', path, target, before, content, expected_sha256)

    def _replace_text(self, path: str, old: str, new: str, expected_sha256: str) -> dict:
        target, before = self._snapshot(path, expected_sha256)
        if before is None:
            raise ToolError('Replacement requires an existing file')
        text = before.decode('utf-8')
        if not old or text.count(old) != 1:
            raise ToolError('Old text must match exactly once')
        return self._commit_text('replace_text', path, target, before,
                                 text.replace(old, new, 1), expected_sha256)

    @staticmethod
    def _file_state(data: bytes, mode: int) -> dict:
        if os.name == 'nt':
            mode = 0o666 if mode & 0o200 else 0o444
        return {'kind': 'file', 'content': data.decode('utf-8'),
                'sha256': hashlib.sha256(data).hexdigest(), 'mode': mode}

    def _state(self, path: str) -> dict:
        target = self._path(path, missing=True)
        if not target.exists():
            return {'kind': 'absent'}
        if target.is_dir():
            return {'kind': 'directory', 'mode': stat.S_IMODE(target.stat().st_mode) & 0o777}
        data = self._read_bytes(path)
        return self._file_state(data, stat.S_IMODE(target.stat().st_mode) & 0o777)

    def _assert_state(self, path, expected):
        if self._state(path) != expected:
            raise ToolError(f'File changed; undo/edit conflict: {path}')

    @staticmethod
    def _summary(entries):
        return [{'path': e['path'],
                 'action': ('create' if e['before']['kind'] == 'absent' else
                            'delete' if e['after']['kind'] == 'absent' else 'modify'),
                 'before_sha256': e['before'].get('sha256'),
                 'after_sha256': e['after'].get('sha256')}
                for e in entries if e['before'] != e['after']]

    def begin_turn(self) -> None:
        # Preserve unfinished managed work rather than silently dropping its undo.
        self.end_turn()
        self._turn = {}

    def changes(self, include_history=False) -> list[dict]:
        if not include_history:
            return self._summary(self._turn.values())
        merged = {}
        entries = [e for record in self._undo for e in record['changes']]
        for entry in [*entries, *self._turn.values()]:
            key = self._path_key(entry['path'])
            first = merged.get(key, entry)['before']
            merged[key] = {**entry, 'before': first}
        return self._summary(merged.values())

    def end_turn(self) -> dict:
        entries = [e for e in self._turn.values() if e['before'] != e['after']]
        result = {'changes': self._summary(entries), 'undoable': bool(entries)}
        if entries:
            self._undo.append({'version': 1, 'changes': entries})
        self._turn = {}
        return result

    def _record(self, entries):
        for entry in entries:
            path = entry['path']
            first = self._turn.get(path, entry)['before']
            self._turn[path] = {**entry, 'before': first}

    def _preview(self, action, entries):
        # Never hide a target or permission change behind a truncated diff.
        headers = []
        for e in entries:
            before, after = e['before'], e['after']
            modes = [format(state['mode'], '04o') if 'mode' in state else 'absent'
                     for state in (before, after)]
            headers.append(f"{action}: {e['path']} ({before['kind']} -> {after['kind']}; "
                           f"mode {modes[0]} -> {modes[1]})\n")
        header = ''.join(headers)
        cap = self.security['max_output_chars']
        if len(header) > cap:
            raise ToolError('Change paths exceed approval preview limit; use a smaller turn')
        parts = []
        for e in entries:
            path, before, after = e['path'], e['before'], e['after']
            parts.extend(difflib.unified_diff(
                before.get('content', '').splitlines(keepends=True),
                after.get('content', '').splitlines(keepends=True),
                fromfile=path if before['kind'] != 'absent' else '/dev/null',
                tofile=path if after['kind'] != 'absent' else '/dev/null'))
        detail = header + ''.join(parts)
        return detail[:cap] + ('\n[diff preview truncated]' if len(detail) > cap else '')

    def _set_state(self, path, expected, desired):
        self._assert_state(path, expected)
        target = self._path(path, missing=True)
        if desired['kind'] == 'absent':
            if expected['kind'] == 'directory':
                target.rmdir()
            elif expected['kind'] == 'file':
                target.unlink()
            return
        if desired['kind'] == 'directory':
            # Set exact permissions before publication: mkdir applies umask, and
            # a failed chmod must not leave an unjournaled target behind.
            temporary = Path(tempfile.mkdtemp(prefix='.david-write-', dir=target.parent))
            try:
                os.chmod(temporary, desired['mode'])
                self._assert_state(path, expected)
                os.replace(temporary, target)
            finally:
                try:
                    temporary.rmdir()
                except FileNotFoundError:
                    pass
            return
        data = desired['content'].encode('utf-8')
        fd, temporary = tempfile.mkstemp(prefix='.david-write-', dir=target.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, desired['mode'])
            self._assert_state(path, expected)
            os.replace(temporary, target)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _check_directory_removals(self, entries):
        removals = {self._path_key(e['path']) for e in entries if e['after']['kind'] == 'absent'}
        for e in entries:
            if e['before']['kind'] == 'directory' and e['after']['kind'] == 'absent':
                target = self._path(e['path'])
                with os.scandir(target) as children:
                    for index, child in enumerate(children):
                        if index >= self.security['max_scan_entries']:
                            raise ToolError('Directory undo exceeds scan limit')
                        relative = (target / child.name).relative_to(self.root).as_posix()
                        if self._path_key(relative) not in removals:
                            raise ToolError(f'Unmanaged directory contents; undo conflict: {e["path"]}')

    def _planned_journal(self, entries):
        planned = dict(self._turn)
        for entry in entries:
            path = entry['path']
            first = planned[path]['before'] if path in planned else entry['before']
            planned[path] = {**entry, 'before': first}
        # Check aliases even for net-zero entries: they can still refer to another
        # spelling of a live managed path on a folding filesystem.
        keys = [self._path_key(path) for path in planned]
        if len(set(keys)) != len(keys) or len(planned) > 200:
            raise ToolError('Turn exceeds 200 paths or contains aliased paths')
        records = list(self._undo)
        current = [e for e in planned.values() if e['before'] != e['after']]
        if current:
            records.append({'version': 1, 'changes': current})
        self._validated_journal(records, check_paths=False)

    def _transaction(self, action, entries, record=True):
        if self.security['read_only']:
            raise ToolError('Workspace is read-only')
        if record:
            self._planned_journal(entries)
        for e in entries:
            self._poll()
            self._assert_state(e['path'], e['before'])
            if record and e['path'] in self._turn and self._turn[e['path']]['after'] != e['before']:
                raise ToolError('Unmanaged changes conflict with current turn; end the turn first')
        self._check_directory_removals(entries)
        if not self.approve(action, self._preview(action, entries)):
            raise ToolError('User cancelled the write')
        for e in entries:
            self._poll()
            self._assert_state(e['path'], e['before'])
        self._check_directory_removals(entries)
        applied = []
        try:
            for e in entries:
                self._poll()
                self._set_state(e['path'], e['before'], e['after'])
                applied.append(e)
        except BaseException as exc:
            remaining = []
            for e in reversed(applied):
                try:
                    self._set_state(e['path'], e['after'], e['before'])
                except (OSError, ToolError, UnicodeError):
                    remaining.append(e)
            if remaining:
                if record:
                    self._record(list(reversed(remaining)))
                raise ToolError('Partial failure; rollback failed for: ' +
                                ', '.join(e['path'] for e in remaining)) from exc
            raise
        if record:
            self._record(entries)

    def _validated_journal(self, records, check_paths=True):
        """Copy only a bounded, known format; persisted snapshots are untrusted input."""
        if not isinstance(records, list) or len(records) > 100:
            raise ToolError('Journal must contain at most 100 turns')
        copied, total_bytes, count = [], 0, 0
        for record in records:
            if (not isinstance(record, dict) or set(record) != {'version', 'changes'}
                    or type(record['version']) is not int or record['version'] != 1
                    or not isinstance(record['changes'], list) or not 1 <= len(record['changes']) <= 200):
                raise ToolError('Invalid journal record')
            entries, seen = [], set()
            for entry in record['changes']:
                count += 1
                if count > 2000:
                    raise ToolError('Journal exceeds 2000 paths')
                if not isinstance(entry, dict) or set(entry) != {'path', 'before', 'after'}:
                    raise ToolError('Invalid journal change')
                path = entry['path']
                if (not isinstance(path, str) or not 1 <= len(path) <= 4096
                        or path == '.' or Path(path).as_posix() != path):
                    raise ToolError('Invalid journal path')
                key = self._path_key(path)
                if key in seen:
                    raise ToolError('Duplicate journal path')
                seen.add(key)
                if check_paths:
                    self._path(path, missing=True)
                states = {}
                for label in ('before', 'after'):
                    state = entry[label]
                    if not isinstance(state, dict):
                        raise ToolError('Invalid journal state')
                    kind = state.get('kind')
                    fields = {'kind'}
                    if kind in ('file', 'directory'):
                        fields.add('mode')
                        if type(state.get('mode')) is not int or not 0 <= state['mode'] <= 0o777:
                            raise ToolError('Invalid journal mode')
                    if kind == 'file':
                        fields.update(('content', 'sha256'))
                        content = state.get('content')
                        if not isinstance(content, str) or len(content) > self.security['max_file_bytes']:
                            raise ToolError('Invalid journal content')
                        try:
                            data = content.encode('utf-8')
                        except UnicodeError as exc:
                            raise ToolError('Invalid journal UTF-8') from exc
                        total_bytes += len(data)
                        if (len(data) > self.security['max_file_bytes'] or total_bytes > 16 * 1024 * 1024
                                or any(ord(c) < 32 and c not in '\t\n\r' for c in content)
                                or state.get('sha256') != hashlib.sha256(data).hexdigest()):
                            raise ToolError('Invalid journal bytes, digest, or size')
                    if kind not in ('absent', 'file', 'directory') or set(state) != fields:
                        raise ToolError('Invalid journal state fields')
                    states[label] = dict(state)
                before, after = states['before'], states['after']
                if (before == after or before['kind'] == 'directory'
                        or (after['kind'] == 'directory' and before['kind'] != 'absent')):
                    raise ToolError('Invalid managed journal transition')
                entries.append({'path': path, **states})
            copied.append({'version': 1, 'changes': entries})
        return copied

    def export_journal(self) -> list:
        """Explicit opt-in export includes text snapshots; normal UI records never do."""
        records = list(self._undo)
        entries = [e for e in self._turn.values() if e['before'] != e['after']]
        if entries:
            records.append({'version': 1, 'changes': entries})
        return self._validated_journal(records)

    def import_journal(self, records: list) -> None:
        if isinstance(records, list) and not records:
            self._undo = []
            self._turn = {}
            return
        if self._undo or self._turn:
            raise ToolError('Import requires an empty session journal')
        try:
            validated = self._validated_journal(records)
        except (OSError, UnicodeError, ValueError) as exc:
            raise ToolError('Invalid journal paths') from exc
        self._undo = validated

    def undo_last(self) -> dict:
        if self.security['read_only']:
            raise ToolError('Workspace is read-only')
        self.end_turn()
        if not self._undo:
            return {'undone': False, 'changes': []}
        record = self._validated_journal([self._undo[-1]])[0]
        entries = [{'path': e['path'], 'before': e['after'], 'after': e['before']}
                   for e in reversed(record['changes'])]
        try:
            self._transaction('undo_last', entries, record=False)
        except (OSError, UnicodeError, ValueError) as exc:
            raise ToolError(f'Undo failed: {type(exc).__name__}') from exc
        self._undo.pop()
        return {'undone': True, 'changes': self._summary(entries)}

    def _existing_entry(self, path, expected):
        target, before = self._snapshot(path, expected)
        if before is None:
            raise ToolError('Operation requires an existing regular file')
        path = target.relative_to(self.root).as_posix()
        state = self._state(path)
        if state.get('sha256') != expected:
            raise ToolError('File changed before edit')
        return path, state

    def _delete_file(self, path: str, expected_sha256: str) -> dict:
        path, before = self._existing_entry(path, expected_sha256)
        self._transaction('delete_file', [{'path': path, 'before': before, 'after': {'kind': 'absent'}}])
        return {'path': path, 'deleted': True, 'sha256': before['sha256']}

    def _move_file(self, source: str, destination: str, expected_sha256: str) -> dict:
        source, before = self._existing_entry(source, expected_sha256)
        target = self._path(destination, missing=True)
        destination = target.relative_to(self.root).as_posix()
        if self._path_key(source) == self._path_key(destination) or target.exists():
            raise ToolError('Move destination already exists or aliases source')
        # Stage the destination before removing the source; rollback handles a failed unlink.
        entries = [{'path': destination, 'before': {'kind': 'absent'}, 'after': before},
                   {'path': source, 'before': before, 'after': {'kind': 'absent'}}]
        self._transaction('move_file', entries)
        return {'source': source, 'destination': destination, 'sha256': before['sha256']}

    def _make_directory(self, path: str) -> dict:
        target = self._path(path, missing=True)
        path = target.relative_to(self.root).as_posix()
        before = self._state(path)
        if before['kind'] != 'absent':
            raise ToolError('Directory destination already exists')
        self._transaction('make_directory', [{'path': path, 'before': before,
                                             'after': {'kind': 'directory', 'mode': 0o777 if os.name == 'nt' else 0o700}}])
        return {'path': path, 'created': True}

    def _text_entry(self, path, content, expected=None):
        if not isinstance(content, str):
            raise ToolError('Content must be a string')
        data = content.encode('utf-8')
        if len(data) > self.security['max_file_bytes']:
            raise ToolError('File exceeds size limit')
        if any(ord(c) < 32 and c not in '\t\n\r' for c in content):
            raise ToolError('Binary content is forbidden')
        target, before = self._snapshot(path, expected)
        path = target.relative_to(self.root).as_posix()
        initial = self._state(path)
        if initial.get('content') != (None if before is None else before.decode('utf-8')):
            raise ToolError('File changed before edit')
        return {'path': path, 'before': initial,
                'after': self._file_state(data, initial.get('mode', 0o600))}

    @staticmethod
    def _path_key(path):
        # Be conservative on case-sensitive disks, safe on case/Unicode-folding disks.
        return unicodedata.normalize('NFC', path).casefold()

    def _apply_patch(self, changes: list) -> dict:
        if not isinstance(changes, list) or not 1 <= len(changes) <= 20:
            raise ToolError('Patch requires 1 to 20 changes')
        entries, seen = [], set()
        for change in changes:
            if (not isinstance(change, dict) or not {'path', 'content'} <= change.keys()
                    or change.keys() - {'path', 'content', 'expected_sha256'}
                    or any(not isinstance(v, str) for v in change.values())):
                raise ToolError('Invalid patch change')
            entry = self._text_entry(change['path'], change['content'], change.get('expected_sha256'))
            key = self._path_key(entry['path'])
            if key in seen:
                raise ToolError('Duplicate or aliased patch path')
            seen.add(key)
            entries.append(entry)
        self._transaction('apply_patch', entries)
        return {'changes': self._summary(entries)}

    def _commit_text(self, action, path, target, before, content, expected):
        if self.security['read_only']:
            raise ToolError('Workspace is read-only')
        data = content.encode('utf-8')
        if len(data) > self.security['max_file_bytes']:
            raise ToolError('File exceeds size limit')
        if any(ord(c) < 32 and c not in '\t\n\r' for c in content):
            raise ToolError('Binary content is forbidden')
        path = target.relative_to(self.root).as_posix()
        initial = self._state(path)
        if initial.get('content') != (None if before is None else before.decode('utf-8')):
            raise ToolError('File changed before edit')
        final = self._file_state(data, initial.get('mode', 0o600))
        self._transaction(action, [{'path': path, 'before': initial, 'after': final}])
        return {'path': path, 'sha256': final['sha256'], 'bytes_written': len(data)}

    def _ignore_rules(self, directory, rules, budget):
        self._poll()
        relative = (directory / '.gitignore').relative_to(self.root).as_posix()
        try:
            (directory / '.gitignore').lstat()
        except FileNotFoundError:
            return rules
        # Fail closed if an ignore file is unsafe/unreadable, never follow a link.
        data = self._read_bytes(relative)
        lines = data.decode('utf-8').splitlines()
        budget['bytes'] += len(data)
        budget['lines'] += len(lines)
        if (budget['bytes'] > self.security['max_file_bytes']
                or budget['lines'] > self.security['max_scan_entries']):
            raise ToolError('Gitignore rules exceed discovery budget')
        return [*rules, (directory, GitIgnoreSpec.from_lines(lines))]

    @staticmethod
    def _ignored(target, is_dir, rules):
        ignored = False
        for base, spec in rules:
            name = target.relative_to(base).as_posix() + ('/' if is_dir else '')
            decision = spec.check_file(name).include
            if decision is not None:
                ignored = decision
        return ignored

    def _walk(self, path: str, state: dict):
        start = self._path(path)
        budget = {'bytes': 0, 'lines': 0}
        rules = self._ignore_rules(self.root, [], budget)
        current = self.root
        # Check every ancestor even when discovery starts inside a subtree.
        for part in start.relative_to(self.root).parts:
            self._poll()
            current /= part
            is_dir = current.is_dir()
            if self._ignored(current, is_dir, rules):
                return
            if is_dir:
                rules = self._ignore_rules(current, rules, budget)
        if start.is_file():
            yield start.relative_to(self.root).as_posix(), False
            return
        pending = [(start, rules)]
        scanned = 0
        while pending:
            self._poll()
            directory, rules = pending.pop()
            directory = self._path(directory.relative_to(self.root).as_posix())
            with os.scandir(directory) as entries:
                for entry in entries:
                    self._poll()
                    if scanned >= self.security['max_scan_entries']:
                        state['truncated'] = True
                        return
                    scanned += 1
                    relative = (directory / entry.name).relative_to(self.root).as_posix()
                    try:
                        safe = self._path(relative)
                        is_dir = stat.S_ISDIR(safe.lstat().st_mode)
                    except (ToolError, OSError):
                        continue
                    if self._ignored(safe, is_dir, rules):
                        continue
                    yield relative, is_dir
                    if is_dir:
                        pending.append((safe, self._ignore_rules(safe, rules, budget)))

    def _list_files(self, path: str = '.', max_entries: int = 200) -> dict:
        result = {'entries': [], 'truncated': False}
        budget = self.security['max_output_chars']
        for relative, is_dir in self._walk(path, result):
            cost = len(relative) + 40
            if len(result['entries']) >= max_entries or cost > budget:
                result['truncated'] = True
                break
            result['entries'].append({'path': relative, 'type': 'directory' if is_dir else 'file'})
            budget -= cost
        return result

    def _search_files(self, pattern: str, path: str = '.', max_results: int = 100,
                      glob: str | None = None) -> dict:
        if glob is not None and (not isinstance(glob, str) or not 1 <= len(glob) <= 2048):
            raise ToolError('Glob must contain 1 to 2048 characters')
        if not pattern:
            raise ToolError('Search pattern must not be empty')
        result = {'matches': [], 'truncated': False}
        budget = self.security['max_output_chars']
        for relative, is_dir in self._walk(path, result):
            if is_dir or (glob is not None and not fnmatch.fnmatchcase(relative, glob)):
                continue
            try:
                lines = self._read_bytes(relative).decode('utf-8').splitlines()
            except (ToolError, OSError, UnicodeError):
                continue
            for number, line in enumerate(lines, 1):
                if number % 128 == 1:
                    self._poll()
                if pattern not in line:
                    continue
                cost = len(relative) + len(line) + 50
                if len(result['matches']) >= max_results or cost > budget:
                    result['truncated'] = True
                    return result
                result['matches'].append({'path': relative, 'line': number, 'text': line})
                budget -= cost
        return result

    def _read_file(self, path: str, offset: int = 1, limit: int = 200) -> dict:
        data = self._read_bytes(path)
        lines = data.decode('utf-8').splitlines(keepends=True)
        text = ''.join(lines[offset-1:offset-1+limit])
        cap = self.security['max_output_chars']
        return {'path': path, 'content': text[:cap],
                'truncated': len(text) > cap or offset - 1 + limit < len(lines),
                'sha256': hashlib.sha256(data).hexdigest(), 'total_lines': len(lines)}
