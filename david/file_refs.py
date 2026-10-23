"""Shared @file syntax; workspace tools remain the path-security authority.

Double-quoted paths are JSON strings. Legacy single-quoted paths are literal.
References must end at whitespace or end of input: never accept a valid prefix
of a malformed token. No path normalization or shell unescaping is performed.
"""
import json
import re

_START = r'(?<![\w@])@'
_START_RE = re.compile(_START)
_COMPLETION_RE = re.compile(_START + r'("(?:[^"\\\n]|\\.)*|[^\s"]*)$')
_RESERVED = '\"\'`'


def quote_file_reference(path):
    """Encode an exact path, including apostrophes, for completion insertion."""
    quoted = any(c.isspace() or c in _RESERVED + '\\' for c in path)
    return '@' + (json.dumps(path, ensure_ascii=False) if quoted else path)


def file_reference_prefix(before):
    """Return (replacement length, decoded prefix) for an unfinished reference."""
    match = _COMPLETION_RE.search(before)
    if match is None:
        return None
    token = match.group(1)
    if token.startswith('"'):
        try:
            prefix = json.loads(token + '"')
        except ValueError:
            return None
    else:
        prefix = token
    return len(match.group()), prefix


def parse_file_references(text):
    """Validate all reference tokens before any caller reads an attachment."""
    paths = []
    end = 0
    for match in _START_RE.finditer(text):
        if match.start() < end:
            continue
        start = match.end()
        if start == len(text) or text[start].isspace():
            continue  # A standalone @ is not a filename.
        try:
            if text[start] == '"':
                path, end = json.JSONDecoder().raw_decode(text, start)
            elif text[start] == "'":
                end = text.index("'", start + 1) + 1
                path = text[start + 1:end - 1]
                if '\n' in path or '\r' in path:
                    raise ValueError
            else:
                end = start
                while end < len(text) and not text[end].isspace():
                    end += 1
                path = text[start:end]
                if any(c in _RESERVED for c in path):
                    raise ValueError
            if not path or (end < len(text) and not text[end].isspace()):
                raise ValueError
        except ValueError:
            raise ValueError('잘못된 @파일 참조입니다. 파일명은 JSON 큰따옴표로 감싸세요.') from None
        paths.append(path)
    return list(dict.fromkeys(paths))
