"""Explicit, user-owned JSON configuration; no project auto-discovery."""
from copy import deepcopy
import json
from pathlib import Path

DEFAULTS = {
    'mcp': {'servers': {}},
    'skills': {'registered': {}, 'enabled': []},
    'ui': {'theme': 'dark', 'input_height': 5},
    'model': {
        'base_url': 'http://127.0.0.1:11434/v1', 'name': 'CHANGE_ME',
        'api_key_env': '', 'timeout_seconds': 90, 'max_tokens': 2048,
        'temperature': 0.2, 'stream': True, 'allow_insecure_http': False,
        'ca_file': '', 'max_response_bytes': 2097152,
        # Explicit client budget, not endpoint discovery or a model capability claim.
        # Zero preserves legacy agent.max_context_chars; positive replaces it.
        'context_window_tokens': 0, 'max_request_bytes': 2097152,
    },
    'agent': {
        'tool_mode': 'native', 'max_steps': 12, 'max_context_chars': 60000,
        'system_prompt_file': '', 'context_safety_margin_tokens': 1024,
    },
    'security': {
        'read_only': False, 'enable_commands': False,
        'max_file_bytes': 262144, 'max_output_chars': 16000,
        'max_scan_entries': 2000, 'command_output_bytes': 131072,
        'denied_names': ['.git', '.ssh', '.aws', '.env', 'setting.json'],
        'denied_globs': ['.env.*', '*.pem', '*.key', '*.p12', '*.pfx', '*credentials*'],
    },
    'commands': {
        'test': {'argv': ['{python}', '-m', 'unittest', 'discover', '-s', 'tests', '-v'], 'timeout_seconds': 60},
    },
}


class ConfigError(ValueError):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError('Duplicate JSON keys are not allowed.')
        result[key] = value
    return result


def _number(value, low, high, field, integer=False):
    import math
    if (type(value) not in (int, float) or (integer and type(value) is not int)
            or not low <= value <= high or not math.isfinite(value)):
        raise ConfigError(f'{field}: expected a number in [{low}, {high}].')


def load_settings(path: Path) -> dict:
    try:
        if Path(path).stat().st_size > 131072:
            raise ConfigError('Settings exceed 128 KiB.')
        raw = json.loads(Path(path).read_text(encoding='utf-8-sig'), object_pairs_hook=_unique_object)
    except (OSError, ValueError) as exc:
        raise ConfigError('Cannot read settings: use valid UTF-8 JSON without duplicate keys.') from exc
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise ConfigError('Unknown settings section or invalid root object.')
    result = deepcopy(DEFAULTS)
    for section, values in raw.items():
        if not isinstance(values, dict):
            raise ConfigError(f'{section} must be an object.')
        if section == 'commands':
            result[section] = values
        else:
            if set(values) - set(DEFAULTS[section]):
                raise ConfigError(f'Unknown setting in {section}.')
            result[section].update(values)
    for section in ('model', 'agent', 'security', 'ui'):
        for key, default in DEFAULTS[section].items():
            value = result[section][key]
            if isinstance(default, bool) and type(value) is not bool:
                raise ConfigError(f'{section}.{key} must be true or false.')
            if isinstance(default, str) and (not isinstance(value, str) or any(ord(c) < 32 for c in value)):
                raise ConfigError(f'{section}.{key} must be a string without control characters.')
            if isinstance(default, list) and (not isinstance(value, list) or not all(isinstance(v, str) and v for v in value)):
                raise ConfigError(f'{section}.{key} must be a list of nonempty strings.')
    if not result['model']['name'].strip() or not result['model']['base_url'].strip():
        raise ConfigError('model.name and model.base_url must not be empty.')
    if result['agent']['tool_mode'] not in ('native', 'json'):
        raise ConfigError('agent.tool_mode must be native or json.')
    if result['ui']['theme'] not in ('dark', 'light', 'mono'):
        raise ConfigError('ui.theme must be dark, light, or mono.')
    for section, key, low, high, integer in (
        ('ui', 'input_height', 3, 15, True),
        ('model', 'timeout_seconds', 1, 600, False), ('model', 'max_tokens', 1, 131072, True),
        ('model', 'context_window_tokens', 0, 2000000, True),
        ('model', 'max_request_bytes', 1024, 67108864, True),
        ('agent', 'context_safety_margin_tokens', 0, 2000000, True),
        ('model', 'temperature', 0, 2, False), ('model', 'max_response_bytes', 1024, 8388608, True),
        ('agent', 'max_steps', 1, 50, True), ('agent', 'max_context_chars', 4096, 1000000, True),
        ('security', 'max_file_bytes', 1024, 2097152, True), ('security', 'max_output_chars', 256, 65536, True),
        ('security', 'max_scan_entries', 1, 20000, True), ('security', 'command_output_bytes', 1024, 2097152, True),
    ):
        _number(result[section][key], low, high, f'{section}.{key}', integer)
    window = result['model']['context_window_tokens']
    reserve = result['model']['max_tokens'] + result['agent']['context_safety_margin_tokens']
    if window and window <= reserve:
        raise ConfigError('model.context_window_tokens must exceed model.max_tokens plus '
                          'agent.context_safety_margin_tokens.')
    import re
    for name, recipe in result['commands'].items():
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name):
            raise ConfigError('Command names must be short alphanumeric identifiers.')
        if not isinstance(recipe, dict) or set(recipe) - {'argv', 'timeout_seconds'}:
            raise ConfigError('Command recipes support only argv and timeout_seconds.')
        argv = recipe.get('argv')
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a and '\x00' not in a for a in argv):
            raise ConfigError('Command argv must be a nonempty list of nonempty strings.')
        _number(recipe.get('timeout_seconds', 60), 1, 600, 'command timeout_seconds')
    _validate_extensions(result)
    return result


def _validate_extensions(settings):
    """Validate registration only; never import or launch an MCP process here."""
    import re
    from pathlib import PureWindowsPath
    identifier = re.compile(r'[A-Za-z0-9_-]{1,64}')
    env_name = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,127}')
    servers = settings['mcp']['servers']
    if not isinstance(servers, dict) or len(servers) > 32:
        raise ConfigError('mcp.servers must be an object with at most 32 servers.')
    for name, server in servers.items():
        if not identifier.fullmatch(name):
            raise ConfigError('MCP server names must be short alphanumeric identifiers.')
        if not isinstance(server, dict) or set(server) - {'command', 'env', 'timeout_seconds', 'max_response_bytes'}:
            raise ConfigError('Unknown MCP server fields.')
        command = server.get('command')
        if (not isinstance(command, list) or not 1 <= len(command) <= 64
                or not all(isinstance(a, str) and a and len(a) <= 4096
                           and not any(ord(c) < 32 for c in a) for a in command)
                or sum(map(len, command)) > 32768):
            raise ConfigError('MCP command must be a bounded argv list without control characters.')
        executable = command[0]
        if executable != '{python}' and not (Path(executable).is_absolute() or PureWindowsPath(executable).is_absolute()):
            raise ConfigError('MCP executable must be absolute or {python}. No shell expansion.')
        if executable.lower().endswith(('.bat', '.cmd')):
            raise ConfigError('MCP batch/shell command files are not supported.')
        env = server.get('env', {})
        if (not isinstance(env, dict) or len(env) > 32 or not all(
                isinstance(k, str) and env_name.fullmatch(k) and isinstance(v, str) and env_name.fullmatch(v)
                for k, v in env.items())):
            raise ConfigError('MCP env maps child variable names to source environment variable names, never values.')
        _number(server.get('timeout_seconds', 30), 1, 120, 'MCP timeout_seconds')
        _number(server.get('max_response_bytes', 262144), 1024, 2097152, 'MCP max_response_bytes', True)
        server.setdefault('env', {})
        server.setdefault('timeout_seconds', 30)
        server.setdefault('max_response_bytes', 262144)
    registered = settings['skills']['registered']
    enabled = settings['skills']['enabled']
    if (not isinstance(registered, dict) or len(registered) > 32 or not all(
            identifier.fullmatch(k) and isinstance(v, str) and v and len(v) <= 4096
            and not any(ord(c) < 32 for c in v) for k, v in registered.items())):
        raise ConfigError('skills.registered maps at most 32 short identifiers to explicit paths.')
    if (not isinstance(enabled, list) or len(enabled) > 8 or not all(
            isinstance(n, str) and n in registered for n in enabled) or len(set(enabled)) != len(enabled)):
        raise ConfigError('skills.enabled must select at most 8 unique registered names.')
