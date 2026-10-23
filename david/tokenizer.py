"""Opt-in local prompt tokenization. No downloads, Transformers or remote code.

Only an explicit tokenizer.json plus a user-verified matching chat template is
supported. Counts describe that local rendered prompt, not hidden server input.
The deliberately small template subset is documented in docs/local-tokenizer.md.
"""
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import stat

from .config import ConfigError, TOKENIZER_PATHS, _unique_object

MAX_INPUT_BYTES = 67108864
MAX_RENDER_BYTES = 67108864
MAX_TEMPLATE_BYTES = 131072
MAX_TOKENIZER_BYTES = 268435456


def _signature(path):
    """Reject links/special files before reading any user-selected asset."""
    p = Path(path)
    if not p.is_absolute():
        raise ConfigError('Local tokenizer paths must be resolved relative to the settings file.')
    try:
        for part in (p, *p.parents):
            if part.is_symlink():
                raise ConfigError('Local tokenizer assets must not use symlinks.')
        info = p.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError('Local tokenizer assets must be regular files.')
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    except OSError as exc:
        raise ConfigError('Cannot access configured local tokenizer asset.') from exc


def _read(path, limit):
    try:
        if _signature(path)[2] > limit:
            raise ConfigError('Local tokenizer asset exceeds its size limit.')
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ConfigError('Local tokenizer asset must be a regular file.')
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ConfigError('Local tokenizer asset exceeds its size limit.')
        return data
    except OSError as exc:
        raise ConfigError('Cannot read configured local tokenizer asset.') from exc


def get_counter(model):
    paths = tuple(model.get(key, '') for key in TOKENIZER_PATHS)
    if not any(paths):
        return None
    if not paths[0] or not (paths[1] or paths[2]):
        raise ConfigError('Local tokenizer requires tokenizer_file and tokenizer_config_file or chat_template_file.')
    if not model.get('context_window_tokens', 0):
        raise ConfigError('Local tokenizer requires a positive context_window_tokens budget.')
    signatures = tuple(_signature(p) if p else None for p in paths)
    return _cached_counter(paths, signatures)


@lru_cache(maxsize=4)
def _cached_counter(paths, signatures):
    return LocalCounter(paths)


class LocalCounter:
    def __init__(self, paths):
        try:
            from tokenizers import Tokenizer
            from jinja2 import StrictUndefined, nodes
            from jinja2.sandbox import ImmutableSandboxedEnvironment
        except ImportError as exc:
            raise ConfigError('Local tokenizer dependencies missing: install david-coding-agent[tokenizer].') from exc
        tokenizer_path, config_path, template_path = paths
        tokenizer_data = _read(tokenizer_path, MAX_TOKENIZER_BYTES)
        config_data = _read(config_path, MAX_TEMPLATE_BYTES) if config_path else b'{}'
        try:
            config = json.loads(config_data, object_pairs_hook=_unique_object)
            if not isinstance(config, dict):
                raise ValueError()
            template_data = _read(template_path, MAX_TEMPLATE_BYTES) if template_path else b''
            template = template_data.decode('utf-8') if template_path else config.get('chat_template')
            if not isinstance(template, str) or not template or len(template.encode('utf-8')) > MAX_TEMPLATE_BYTES:
                raise ValueError()
            self.tokenizer = Tokenizer.from_str(tokenizer_data.decode('utf-8'))
            # Chat templates include special tokens themselves; never truncate or pad accounting.
            self.tokenizer.no_truncation()
            self.tokenizer.no_padding()
        except Exception as exc:
            raise ConfigError('Invalid tokenizer JSON/config or missing single-string chat_template.') from exc
        self.fingerprint = hashlib.sha256(tokenizer_data + b'\0' + config_data + b'\0' + template.encode('utf-8')).hexdigest()
        self.special = {}
        for key in ('bos_token', 'eos_token', 'unk_token', 'pad_token', 'sep_token', 'cls_token', 'mask_token'):
            if key in config:
                value = config[key]
                if isinstance(value, dict):
                    value = value.get('content')
                if not isinstance(value, str) or len(value.encode('utf-8')) > 4096:
                    raise ConfigError('Local tokenizer special tokens must be bounded strings.')
                self.special[key] = value
        env = ImmutableSandboxedEnvironment(undefined=StrictUndefined, autoescape=False,
                                             trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)
        env.globals.clear()
        env.filters = {'tojson': lambda value: json.dumps(value, ensure_ascii=False), 'trim': lambda value: value.strip()}
        env.tests.clear()
        try:
            tree = env.parse(template)
            allowed = (nodes.Template, nodes.Output, nodes.TemplateData, nodes.For, nodes.If,
                       nodes.Name, nodes.Const, nodes.Getitem, nodes.Filter, nodes.Compare,
                       nodes.Operand, nodes.And, nodes.Or, nodes.Not)
            all_nodes = [tree, *tree.find_all(nodes.Node)]
            if len(all_nodes) > 4096 or any(not isinstance(n, allowed) for n in all_nodes):
                raise ValueError('unsupported construct')
            for node in all_nodes:
                if isinstance(node, nodes.For) and (node.recursive or node.test is not None
                        or not isinstance(node.iter, nodes.Name) or node.iter.name not in ('messages', 'tools')
                        or list(node.find_all(nodes.For))):
                    raise ValueError('only non-nested messages/tools loops supported')
                if isinstance(node, nodes.Filter) and (node.name not in env.filters or node.args or node.kwargs
                                                     or node.dyn_args or node.dyn_kwargs):
                    raise ValueError('unsupported filter')
            names = {n.name for n in tree.find_all(nodes.Name)}
            if not {'messages', 'tools', 'add_generation_prompt'} <= names:
                raise ValueError('template must explicitly handle messages, tools, add_generation_prompt')
            self.template = env.from_string(template)
        except Exception as exc:
            raise ConfigError('Unsupported local chat template; see docs/local-tokenizer.md for the supported subset.') from exc

    def count(self, messages, tools):
        try:
            serialized = json.dumps({'messages': messages, 'tools': tools}, ensure_ascii=False)
            if len(serialized.encode('utf-8')) > MAX_INPUT_BYTES or len(messages) > 4096 or len(tools or []) > 4096:
                raise ConfigError('Local tokenizer input exceeds safety limit.')
            chunks, size = [], 0
            for chunk in self.template.generate(messages=messages, tools=tools,
                                                add_generation_prompt=True, **self.special):
                size += len(chunk.encode('utf-8'))
                if size > MAX_RENDER_BYTES:
                    raise ConfigError('Local tokenizer rendered prompt exceeds safety limit.')
                chunks.append(chunk)
            return len(self.tokenizer.encode(''.join(chunks), add_special_tokens=False).ids)
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError('Local tokenizer/template failed; no estimate fallback or server request. Check the matching template.') from exc
