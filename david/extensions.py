"""Explicit extension wiring; no process is started during construction."""
import json


class ExtensionTools:
    """Compose workspace tools and the user-approved MCP process manager.

    Workspace journaling/search remains owned by WorkspaceTools. MCP executes
    arbitrary trusted code, is NOT sandboxed, and is never covered by undo.
    """
    def __init__(self, workspace_tools, manager, servers):
        self.workspace_tools = workspace_tools
        self.manager = manager
        self.servers = servers
        self.approve = workspace_tools.approve
        self.cancel_check = workspace_tools.cancel_check

    def __getattr__(self, name):
        return getattr(self.workspace_tools, name)

    @property
    def approve(self):
        return self.workspace_tools.approve

    @approve.setter
    def approve(self, callback):
        self.workspace_tools.approve = callback
        if self.manager is not None:
            self.manager.approve = callback

    @property
    def cancel_check(self):
        return self.workspace_tools.cancel_check

    @cancel_check.setter
    def cancel_check(self, callback):
        callback = callback or (lambda: None)
        self.workspace_tools.cancel_check = callback
        if self.manager is not None:
            self.manager.cancel_check = callback

    def schemas(self):
        schemas = self.workspace_tools.schemas()
        if not self.servers or self.security['read_only']:
            return schemas
        server = {'type': 'string', 'enum': list(self.servers), 'minLength': 1, 'maxLength': 64}
        for name, description, properties in (
            ('mcp_list_tools', 'List MCP tools. Starting a server requires approval; MCP is NOT sandboxed.',
             {'server': server}),
            ('mcp_call', 'Call an MCP tool with approval. arguments_json is an object encoded as JSON. '
             'Only generic arguments are locally validated, NOT the full MCP tool schema. MCP changes cannot be undone.',
             {'server': server, 'tool': {'type': 'string', 'minLength': 1, 'maxLength': 128},
              'arguments_json': {'type': 'string', 'minLength': 2, 'maxLength': 65536}}),
        ):
            schemas.append({'type': 'function', 'function': {'name': name, 'description': description,
                'parameters': {'type': 'object', 'properties': properties, 'required': list(properties),
                               'additionalProperties': False}}})
        return schemas

    def execute(self, name, arguments):
        if name not in ('mcp_list_tools', 'mcp_call'):
            return self.workspace_tools.execute(name, arguments)
        self.cancel_check()
        if self.security['read_only'] or not self.servers or self.manager is None:
            raise ValueError('MCP is disabled in read-only mode or has no registered servers.')
        self.manager.read_only = self.security['read_only']
        from .agent import validate_value
        schema = next(s['function']['parameters'] for s in self.schemas() if s['function']['name'] == name)
        validate_value(arguments, schema)
        if name == 'mcp_list_tools':
            result = self.manager.list_tools(arguments['server'])
        else:
            result = self.manager.call_tool(arguments['server'], arguments['tool'],
                                            parse_mcp_arguments(arguments['arguments_json']))
        # Keep arbitrary MCP fields away from internal renderer/storage metadata.
        wrapped = {'mcp_result': result, 'ok': not result.get('isError', False)}
        if not wrapped['ok']:
            wrapped['error'] = 'MCP tool reported an error; see mcp_result for server details.'
        return wrapped

    def close(self):
        if self.manager is not None:
            self.manager.close()


def select_skills(agent, names):
    """User-only activation: validate/load first, then discard prior context."""
    from .agent import Agent
    validate_skill_selection(names, agent.settings['skills']['registered'])
    text = agent.skill_registry.load(names)
    extra = agent.base_extra_prompt + ('\n' + text if text else '')
    system = Agent(agent.client, agent.tools, agent.settings, extra_prompt=extra).system
    agent.system = system
    agent.skill_names = list(names)
    agent.settings['skills']['enabled'] = list(names)
    agent.history.clear()


def extension_listing(agent, command):
    if command == '/mcp':
        names = ', '.join(agent.settings['mcp']['servers']) or '(none)'
        return ('Configured MCP servers (listing only; no connection attempted): ' + names
                + '\nModel tools: mcp_list_tools / mcp_call. Launch and calls require approval. '
                  'MCP executes arbitrary code, is NOT OS-sandboxed, and is disabled in read-only mode.')
    items = agent.skill_registry.list()
    active = set(agent.skill_names)
    return ('Registered skills (text instructions only):\n'
            + ('\n'.join(('* ' if item['name'] in active else '  ') + item['name']
                         + ' — ' + str(item['path']) + (' — ' + item['description'] if item['description'] else '')
                         for item in items) or '(none)')
            + '\n/skill NAME selects one registered skill; /skill off disables skills. '
              'Selection clears conversation; managed workspace undo remains.')


def validate_skill_selection(names, registered):
    if (not isinstance(names, list) or len(names) > 8
            or not all(isinstance(name, str) and name in registered for name in names)
            or len(set(names)) != len(names)):
        raise ValueError('Select at most 8 unique registered skill names.')


def parse_mcp_arguments(raw):
    """Bounded strict JSON object, not validation of arbitrary MCP JSON Schema."""
    import math
    from .config import _unique_object
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > 65536:
        raise ValueError('MCP arguments exceed 64 KiB.')
    def reject_constant(value):
        raise ValueError('Non-finite MCP argument.')
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=reject_constant)
    except RecursionError as exc:
        raise ValueError('MCP arguments are too deeply nested.') from exc
    if not isinstance(value, dict):
        raise ValueError('MCP arguments must be a JSON object.')
    remaining = 4096
    def check(item, depth=0):
        nonlocal remaining
        remaining -= 1
        if depth > 16 or remaining < 0:
            raise ValueError('MCP argument nesting or item limit exceeded.')
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError('Non-finite MCP argument.')
        if isinstance(item, str):
            item.encode('utf-8')
        if isinstance(item, dict):
            for key, child in item.items():
                key.encode('utf-8')
                check(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                check(child, depth + 1)
    check(value)
    return value
