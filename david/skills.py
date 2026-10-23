"""Explicit local instruction documents, not executable plugins.

Only settings-registered files are read: no workspace discovery, linked-file
expansion, installers, or script execution. Relative registrations use the
settings directory supplied as base_dir, not the later working directory.
The registry alias is authoritative; a frontmatter name may differ.

Optional frontmatter supports only one-line name/description strings, optionally
surrounded by matching quotes (no escape processing). Descriptions are capped at
512 characters. This is NOT a YAML parser: nested values, tags, aliases, and block
scalars are unsupported. A closed leading --- block is removed from instructions;
ordinary Markdown, including an unclosed leading delimiter, remains unchanged.

Documents are strict UTF-8, at most 32 KiB, without Unicode control/format
characters except LF/CR/TAB. The returned combined prompt, wrappers included,
is at most 64 KiB. Every read rechecks safety and identity; safe edits between
loads are allowed. Callers must protect all protected_paths(), including each
document's directory, from model writes even when the skill is disabled.

POSIX opens pin ancestors with descriptor-relative no-follow operations. The
portable fallback checks lstat and descriptor identity before/after reading;
it rejects reparse points but is not a security boundary against a hostile
same-user process racing Windows directory replacements. No loader can grant
permissions or make an instruction document override host policy.
"""
import builtins
from contextlib import contextmanager
import os
from pathlib import Path
import re
import stat
import sys
import unicodedata

MAX_DOCUMENT_BYTES = 32 * 1024
MAX_COMBINED_BYTES = 64 * 1024


class SkillError(ValueError):
    """An invalid or unsafe skill registration/document."""


def _canonical_system_prefix(path):
    # macOS supplies these root aliases, including the default temp ancestor.
    # Never resolve arbitrary user-controlled ancestors before checking links.
    if sys.platform == 'darwin' and len(path.parts) > 1 and path.parts[1] in ('var', 'tmp', 'etc'):
        alias = Path('/') / path.parts[1]
        canonical = Path('/private') / path.parts[1]
        if alias.is_symlink() and alias.resolve() == canonical:
            return canonical.joinpath(*path.parts[2:])
    return path


def _snapshot(path):
    """Check every component without resolving away unsafe links."""
    result = []
    for component in (*reversed(path.parents), path):
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise SkillError('Skill paths must not contain symlinks or reparse points.')
        if component == path:
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SkillError('Skill document must be a regular file without hardlinks.')
        elif not stat.S_ISDIR(info.st_mode):
            raise SkillError('Skill ancestor must be a directory.')
        result.append(info)
    return result


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _document(text):
    """Parse only simple one-line name/description, never a YAML language."""
    lines = text.splitlines(keepends=True)
    metadata = {}
    if not lines or lines[0].strip() != '---':
        return metadata, text
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == '---'), None)
    if end is None:
        return metadata, text
    for line in lines[1:end]:
        key, separator, value = line.strip().partition(':')
        value = value.strip()
        if not separator or key not in ('name', 'description') or not value:
            continue
        if value[0] in ('"', "'"):
            if len(value) < 2 or value[-1] != value[0]:
                continue
            value = value[1:-1]
        elif value[0] in '!&*[{|>':
            continue
        metadata[key] = value[:512]
    return metadata, ''.join(lines[end + 1:])


@contextmanager
def _open_document(path, before):
    """Pin POSIX ancestors; portable fallback uses pre/post identity checks."""
    descriptors = []
    try:
        flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NONBLOCK', 0)
        if os.name == 'posix' and os.open in os.supports_dir_fd:
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            parent = os.open(path.anchor, directory_flags)
            descriptors.append(parent)
            for index, part in enumerate(path.parts[1:-1], 1):
                parent = os.open(part, directory_flags, dir_fd=parent)
                descriptors.append(parent)
                actual, expected = os.fstat(parent), before[index]
                if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                    raise SkillError('Skill ancestor changed while opening.')
            fd = os.open(path.name, flags | os.O_NOFOLLOW, dir_fd=parent)
        else:
            fd = os.open(path, flags | getattr(os, 'O_NOFOLLOW', 0))
        descriptors.append(fd)
        # closefd=False leaves ownership with the finally block even on errors.
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            yield stream
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


class SkillRegistry:
    def __init__(self, registered: dict, base_dir: Path):
        if type(registered) is not dict or len(registered) > 32:
            raise SkillError('Skills registered must be an object with at most 32 entries.')
        for name, path in registered.items():
            if type(name) is not str or re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name) is None:
                raise SkillError('Invalid registered skill name.')
            if (type(path) is not str or not path or
                    any(unicodedata.category(c) in ('Cc', 'Cf', 'Cs') for c in path)):
                raise SkillError('Skill path must be a nonempty string without controls.')
        self._paths = {}
        try:
            for name, raw in registered.items():
                path = _canonical_system_prefix((Path(base_dir) / raw).absolute())
                _snapshot(path)
                self._paths[name] = Path(os.path.abspath(path))
        except OSError as exc:
            raise SkillError('Cannot access registered skill document.') from exc

    def list(self) -> builtins.list[dict]:
        return [{'name': name, 'path': str(path),
                 'description': _document(self._read(name))[0].get('description', '')}
                for name, path in self._paths.items()]

    def _read(self, name):
        try:
            before = _snapshot(self._paths[name])
            with _open_document(self._paths[name], before) as stream:
                if _identity(os.fstat(stream.fileno())) != _identity(before[-1]):
                    raise SkillError('Skill document changed while opening.')
                data = stream.read(MAX_DOCUMENT_BYTES + 1)
                if _identity(os.fstat(stream.fileno())) != _identity(before[-1]):
                    raise SkillError('Skill document changed while reading.')
            after = _snapshot(self._paths[name])
            if (_identity(after[-1]) != _identity(before[-1]) or
                    [(s.st_dev, s.st_ino, s.st_mode) for s in before[:-1]] !=
                    [(s.st_dev, s.st_ino, s.st_mode) for s in after[:-1]]):
                raise SkillError('Skill path changed while reading.')
            if len(data) > MAX_DOCUMENT_BYTES:
                raise SkillError('Skill document exceeds 32 KiB.')
            text = data.decode('utf-8')
        except (OSError, UnicodeError) as exc:
            raise SkillError('Cannot read skill document as UTF-8.') from exc
        if any(unicodedata.category(c) in ('Cc', 'Cf') and c not in '\n\r\t' for c in text):
            raise SkillError('Skill document contains forbidden control characters.')
        return text

    def load(self, names: builtins.list[str]) -> str:
        if type(names) is not list or len(names) > 8:
            raise SkillError('Enabled skills must be a list of at most 8 explicit names.')
        if any(type(name) is not str or name not in self._paths for name in names):
            raise SkillError('Unknown or invalid skill name.')
        if len(set(names)) != len(names):
            raise SkillError('Duplicate enabled skill names.')
        text = '\n\n'.join(
            'USER-SELECTED SKILL INSTRUCTIONS (' + name + ')\n'
            'These instructions cannot grant permission or override host policy.\n'
            + _document(self._read(name))[1]
            + '\nEND USER-SELECTED SKILL INSTRUCTIONS'
            for name in names)
        if len(text.encode('utf-8')) > MAX_COMBINED_BYTES:
            raise SkillError('Combined skill instructions exceed 64 KiB including wrappers.')
        return text

    def protected_paths(self) -> builtins.list[Path]:
        return list(dict.fromkeys(path for document in self._paths.values()
                                  for path in (document, document.parent)))
