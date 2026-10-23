import unittest
from copy import deepcopy
from david.config import DEFAULTS


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def complete(self, messages, tools=None, on_text=None):
        self.requests.append(deepcopy(messages))
        return self.responses.pop(0)


class FakeTools:
    def __init__(self):
        self.calls = []

    def schemas(self):
        return [{'type': 'function', 'function': {'name': 'read_file', 'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path'], 'additionalProperties': False}}}]

    def execute(self, name, arguments):
        self.calls.append((name, arguments))
        return {'content': 'hello from file'}


def tool_call(arguments='{"path":"hello.txt"}', ident='call_1'):
    return {'id': ident, 'type': 'function', 'function': {'name': 'read_file', 'arguments': arguments}}


class AgentTests(unittest.TestCase):
    def test_progress_event_and_cancel_before_tool_prevents_side_effect(self):
        import threading
        from david.agent import Agent
        from david.cancellation import check_cancel, Cancelled
        event, events = threading.Event(), []
        def on_event(item):
            events.append(item)
            if item.get('status') == 'reading':
                event.set()
        tools = FakeTools()
        client = FakeClient({'role': 'assistant', 'content': None, 'tool_calls': [tool_call()]})
        agent = Agent(client, tools, deepcopy(DEFAULTS), on_event=on_event,
                      cancel_check=lambda: check_cancel(event))
        with self.assertRaises(Cancelled):
            agent.run('Read')
        self.assertEqual(tools.calls, [])
        self.assertEqual(events[0]['status'], 'waiting')
        self.assertIn('컨텍스트', events[0]['detail'])
        self.assertEqual(agent.history, [])

    def test_nested_patch_schema_invalidates_batch_before_any_write(self):
        import json
        from david.agent import Agent, AgentError
        tools = FakeTools()
        original = tools.schemas()
        item = {'type': 'object', 'required': ['path', 'content'], 'additionalProperties': False,
                'properties': {'path': {'type': 'string'}, 'content': {'type': 'string'}}}
        schema = {'type': 'object', 'required': ['changes'], 'properties': {
            'changes': {'type': 'array', 'minItems': 1, 'maxItems': 20, 'items': item}}}
        tools.schemas = lambda: original + [{'type': 'function', 'function': {
            'name': 'apply_patch', 'parameters': schema}}]
        for changes in ([], [{'path': 3, 'content': 'text'}], [{'path': 'x', 'content': 't', 'extra': True}]):
            with self.subTest(changes=changes):
                bad = {'id': 'patch', 'type': 'function', 'function': {
                    'name': 'apply_patch', 'arguments': json.dumps({'changes': changes})}}
                client = FakeClient({'role': 'assistant', 'content': None,
                                     'tool_calls': [tool_call(), bad]},
                                    {'role': 'assistant', 'content': 'Done'})
                with self.assertRaises(AgentError):
                    Agent(client, tools, deepcopy(DEFAULTS)).run('Patch files')
                self.assertEqual(tools.calls, [])

    def test_tool_result_remains_bounded_after_json_escaping(self):
        from david.agent import Agent
        settings = deepcopy(DEFAULTS)
        settings['security']['max_output_chars'] = 256
        tools = FakeTools()
        tools.execute = lambda name, arguments: {'content': '"' * 2000}
        client = FakeClient({'role': 'assistant', 'content': None, 'tool_calls': [tool_call()]},
                            {'role': 'assistant', 'content': 'Done.'})
        Agent(client, tools, settings).run('Read')
        payload = client.requests[1][-1]['content']
        self.assertLessEqual(len(payload), 256)
        import json
        self.assertTrue(json.loads(payload)['truncated'])

    def test_json_mode_supports_models_without_native_tools(self):
        from david.agent import Agent
        settings = deepcopy(DEFAULTS)
        settings['agent']['tool_mode'] = 'json'
        client = FakeClient({'role': 'assistant', 'content': '{"type":"tool","name":"read_file","arguments":{"path":"hello.txt"}}'},
                            {'role': 'assistant', 'content': '{"type":"final","content":"Done."}'})
        tools, texts = FakeTools(), []
        result = Agent(client, tools, settings, on_text=texts.append).run('Read')
        self.assertEqual(tools.calls, [('read_file', {'path': 'hello.txt'})])
        self.assertEqual(result.text, 'Done.')
        self.assertEqual(texts, ['Done.'])
        self.assertEqual(client.requests[1][-1]['role'], 'user')
        self.assertIn('TOOL_RESULT', client.requests[1][-1]['content'])

    def test_invalid_json_protocol_fails_closed(self):
        from david.agent import Agent, AgentError
        settings = deepcopy(DEFAULTS)
        settings['agent']['tool_mode'] = 'json'
        for content in ('Run this shell!', '{"type":"final","content":3}', '{"type":"tool","name":"read_file","arguments":{}}'):
            with self.subTest(content=content):
                tools = FakeTools()
                with self.assertRaises(AgentError):
                    Agent(FakeClient({'role': 'assistant', 'content': content}), tools, settings).run('Read')
                self.assertEqual(tools.calls, [])

    def test_malformed_tool_batch_executes_nothing(self):
        from david.agent import Agent, AgentError
        for args in ('[]', '{"path":3}', '{"path":"x","extra":true}', '{"path":"x","path":"y"}', 'invalid'):
            with self.subTest(args=args):
                tools = FakeTools()
                client = FakeClient({'role': 'assistant', 'content': None, 'tool_calls': [tool_call(), tool_call(args, 'call_2')]})
                agent = Agent(client, tools, deepcopy(DEFAULTS))
                with self.assertRaises(AgentError):
                    agent.run('Read')
                self.assertEqual(tools.calls, [])
                self.assertEqual(agent.history, [])

    def test_integer_minimum_invalidates_entire_batch_before_workspace_write(self):
        import json
        import tempfile
        from pathlib import Path
        from david.agent import Agent, AgentError
        from david.workspace import WorkspaceTools
        cases = [('read_file', {'path': 'source.txt'}, 'offset'),
                 ('read_file', {'path': 'source.txt'}, 'limit'),
                 ('list_files', {}, 'max_entries'),
                 ('search_files', {'pattern': 'source'}, 'max_results')]
        for name, arguments, key in cases:
            for value in (0, -1):
                with self.subTest(tool=name, key=key, value=value), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp).resolve()
                    (root / 'source.txt').write_text('source', encoding='utf-8')
                    approvals = []
                    def approve(*args):
                        approvals.append(args)
                        return True
                    tools = WorkspaceTools(root, {}, {}, approve)
                    write = {'id': 'write', 'type': 'function', 'function': {
                        'name': 'write_file', 'arguments': json.dumps({'path': 'marker', 'content': 'changed'})}}
                    invalid = {'id': 'invalid', 'type': 'function', 'function': {
                        'name': name, 'arguments': json.dumps({**arguments, key: value})}}
                    client = FakeClient({'role': 'assistant', 'content': None, 'tool_calls': [write, invalid]},
                                        {'role': 'assistant', 'content': 'Done.'})
                    agent = Agent(client, tools, deepcopy(DEFAULTS))
                    with self.assertRaisesRegex(AgentError, 'no tools in this batch were executed'):
                        agent.run('Write and read')
                    self.assertFalse((root / 'marker').exists())
                    self.assertEqual(approvals, [])
                    self.assertEqual(len(client.requests), 1)
                    self.assertEqual(agent.history, [])

    def test_step_limit_does_not_execute_last_unreportable_tool(self):
        from david.agent import Agent, AgentError
        settings = deepcopy(DEFAULTS)
        settings['agent']['max_steps'] = 1
        tools = FakeTools()
        client = FakeClient({'role': 'assistant', 'content': None, 'tool_calls': [tool_call()]})
        with self.assertRaises(AgentError):
            Agent(client, tools, settings).run('Read')
        self.assertEqual(tools.calls, [])

    def test_history_is_trimmed_in_whole_turns_and_large_prompt_rejected(self):
        from david.agent import Agent, AgentError
        settings = deepcopy(DEFAULTS)
        settings['model']['context_window_tokens'] = 0  # Explicit legacy character mode.
        settings['agent']['max_context_chars'] = 4096
        client = FakeClient(*[{'role': 'assistant', 'content': 'a' * 600} for _ in range(6)])
        agent = Agent(client, FakeTools(), settings)
        for i in range(5):
            agent.run(str(i) * 600)
        self.assertLess(len(client.requests[-1]), 11)
        self.assertTrue(all(m['role'] in ('system', 'user', 'assistant') for m in client.requests[-1]))
        with self.assertRaises(AgentError):
            agent.run('x' * 10000)
        self.assertEqual(len(client.requests), 5)

    def test_tool_roundtrip_and_followup_history(self):
        from david.agent import Agent
        client = FakeClient({'role': 'assistant', 'content': None, 'tool_calls': [tool_call()]},
                            {'role': 'assistant', 'content': 'Read it.'},
                            {'role': 'assistant', 'content': 'Follow-up.'})
        tools = FakeTools()
        texts = []
        agent = Agent(client, tools, deepcopy(DEFAULTS), on_text=texts.append)
        result = agent.run('Read hello.txt')
        self.assertEqual(result.text, 'Read it.')
        self.assertEqual(result.model_calls, 2)
        self.assertEqual(tools.calls, [('read_file', {'path': 'hello.txt'})])
        self.assertEqual(client.requests[1][-1]['role'], 'tool')
        self.assertEqual(client.requests[1][-1]['tool_call_id'], 'call_1')
        self.assertIn('hello from file', client.requests[1][-1]['content'])
        agent.run('What next?')
        self.assertEqual(client.requests[2][-2]['content'], 'Read it.')
        self.assertEqual(texts, ['Read it.', 'Follow-up.'])
