"""Context estimates are client budgeting heuristics, not model tokenizer tests."""
from copy import deepcopy
import unittest

from david.agent import Agent, AgentError
from david.config import DEFAULTS
from test_agent import FakeClient, FakeTools


class ContextTests(unittest.TestCase):
    def test_context_estimator_accounts_for_utf8_schemas_and_framing(self):
        from david.context import context_usage
        settings = deepcopy(DEFAULTS)
        settings['model']['context_window_tokens'] = 1000000
        messages = [{'role': 'user', 'content': 'a'}]
        ascii_usage = context_usage(settings, messages, None)
        messages[0]['content'] = '한'
        korean_usage = context_usage(settings, messages, None)
        self.assertEqual(korean_usage['estimated_input_tokens'] - ascii_usage['estimated_input_tokens'], 2)
        schemas = FakeTools().schemas()
        schema_usage = context_usage(settings, messages, schemas)
        self.assertGreater(schema_usage['estimated_input_tokens'], korean_usage['estimated_input_tokens'])
        framed = context_usage(settings, messages + [{'role': 'assistant', 'content': ''}], schemas)
        self.assertGreater(framed['estimated_input_tokens'], schema_usage['estimated_input_tokens'] + 32)

    def test_output_reserve_and_schemas_can_each_prevent_model_call(self):
        from david.context import context_usage
        settings = deepcopy(DEFAULTS)
        settings['model']['context_window_tokens'] = 1000000
        settings['model']['max_tokens'] = 4096
        tools, client = FakeTools(), FakeClient()
        agent = Agent(client, tools, settings)
        prompt = 'small prompt'
        messages = [{'role': 'system', 'content': agent.system}, {'role': 'user', 'content': prompt}]
        without_schemas = context_usage(settings, messages, None)['estimated_input_tokens']
        settings['model']['context_window_tokens'] = without_schemas + 4096 + 1024
        with self.assertRaisesRegex(AgentError, 'Estimated context token budget'):
            agent.run(prompt)
        with_schemas = context_usage(settings, messages, tools.schemas())['estimated_input_tokens']
        settings['model']['context_window_tokens'] = with_schemas + 4096 + 1024
        client.responses.append({'role': 'assistant', 'content': 'fits exactly'})
        self.assertEqual(agent.run(prompt).text, 'fits exactly')
        agent.history.clear()
        settings['model']['max_tokens'] += 1
        with self.assertRaisesRegex(AgentError, 'Estimated context token budget'):
            agent.run(prompt)
        self.assertEqual(len(client.requests), 1)

    def test_token_trimming_keeps_complete_tool_turns(self):
        from david.context import context_usage
        from test_agent import tool_call
        settings = deepcopy(DEFAULTS)
        settings['model']['context_window_tokens'] = 1000000
        client = FakeClient({'role': 'assistant', 'content': 'ok'})
        tools = FakeTools()
        agent = Agent(client, tools, settings)
        old = [{'role': 'user', 'content': 'old' * 1000},
               {'role': 'assistant', 'content': None, 'tool_calls': [tool_call()]},
               {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'old result'},
               {'role': 'assistant', 'content': 'old answer'}]
        recent = deepcopy(old)
        recent[0]['content'] = 'recent'
        agent.history = [old, recent]
        expected = [{'role': 'system', 'content': agent.system}] + recent + [{'role': 'user', 'content': 'next'}]
        size = context_usage(settings, expected, tools.schemas())['estimated_input_tokens']
        settings['model']['context_window_tokens'] = size + settings['model']['max_tokens'] + 1024
        agent.run('next')
        self.assertEqual(client.requests[0], expected)
        self.assertEqual(agent.history[0], recent)

    def test_json_schema_is_budgeted_once_inside_system_prompt(self):
        from david.context import context_usage
        settings = deepcopy(DEFAULTS)
        settings['agent']['tool_mode'] = 'json'
        settings['model']['context_window_tokens'] = 1000000
        client = FakeClient({'role': 'assistant', 'content': '{"type":"final","content":"ok"}'})
        agent = Agent(client, FakeTools(), settings)
        messages = [{'role': 'system', 'content': agent.system}, {'role': 'user', 'content': 'hello'}]
        size = context_usage(settings, messages, None)['estimated_input_tokens']
        settings['model']['context_window_tokens'] = size + settings['model']['max_tokens'] + 1024
        agent.run('hello')
        self.assertEqual(agent.context_usage['estimated_input_tokens'], size)
        self.assertIn('Tool schemas:', client.requests[0][0]['content'])

    def test_legacy_missing_window_preserves_character_budget_and_label(self):
        settings = deepcopy(DEFAULTS)
        settings['model'].pop('context_window_tokens')
        settings['agent'].pop('context_safety_margin_tokens')
        client = FakeClient({'role': 'assistant', 'content': 'ok'})
        events = []
        agent = Agent(client, FakeTools(), settings, on_event=events.append)
        agent.run('한글')
        self.assertEqual(events[0]['context']['unit'], 'characters')
        self.assertIn('토큰 아님', events[0]['detail'])
        self.assertNotIn('estimated_input_tokens', events[0]['context'])
        with self.assertRaisesRegex(AgentError, 'character budget'):
            agent.run('a' * 80000)
        self.assertEqual(len(client.requests), 1)

    def test_million_window_reaches_real_loopback_client_without_changing_output_cap(self):
        import json
        import tempfile
        from pathlib import Path
        from david.config import load_settings
        from david.transport import OpenAIClient
        from test_transport import serve, response
        for prompt in ('a' * 800000, '한' * 250000):
            with self.subTest(character=prompt[0]), serve(response()) as (url, requests):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / 'setting.json'
                    path.write_text(json.dumps({'model': {'base_url': url, 'name': 'fixture-not-real-model',
                        'stream': False, 'context_window_tokens': 1000000,
                        'max_tokens': 4096, 'max_request_bytes': 16777216}}))
                    settings = load_settings(path)
                agent = Agent(OpenAIClient(settings['model']), FakeTools(), settings)
                self.assertEqual(agent.run(prompt).text, 'hello')
                self.assertEqual(requests[0]['body']['messages'][-1]['content'], prompt)
                self.assertEqual(requests[0]['body']['max_tokens'], 4096)
                self.assertNotIn('context_window_tokens', requests[0]['body'])
                self.assertLessEqual(agent.context_usage['estimated_input_tokens'],
                                     agent.context_usage['input_budget_tokens'])


    def test_oversized_single_input_is_rejected_before_model_without_erasing_history(self):
        settings = deepcopy(DEFAULTS)
        settings['model']['context_window_tokens'] = 1000000
        client = FakeClient()
        agent = Agent(client, FakeTools(), settings)
        history = [[{'role': 'user', 'content': 'keep'}, {'role': 'assistant', 'content': 'kept'}]]
        for prompt in ('a' * 2200000, '한' * 750000):
            with self.subTest(character=prompt[0]):
                agent.history = deepcopy(history)
                with self.assertRaisesRegex(AgentError, 'Estimated context token budget'):
                    agent.run(prompt)
                self.assertEqual(client.requests, [])
                self.assertEqual(agent.history, history)


    def test_transport_byte_rejection_preserves_history_after_provisional_trimming(self):
        from david.context import context_usage
        from david.transport import ModelError, OpenAIClient
        from test_transport import serve, response
        settings = deepcopy(DEFAULTS)
        settings['model'].update(context_window_tokens=11238, max_tokens=2048, max_request_bytes=6000,
                                 stream=False, api_key_env='')
        history = [[{'role': 'user', 'content': 'keep'}, {'role': 'assistant', 'content': 'kept'}]]
        prompt = 'a' * 7000
        with serve(response()) as (url, requests):
            settings['model']['base_url'] = url
            tools = FakeTools()
            agent = Agent(OpenAIClient(settings['model']), tools, settings)
            agent.history = deepcopy(history)
            messages = [{'role': 'system', 'content': agent.system}, {'role': 'user', 'content': prompt}]
            trimmed = context_usage(settings, messages, tools.schemas())
            untrimmed = context_usage(settings, messages[:1] + history[0] + messages[1:], tools.schemas())
            self.assertLessEqual(trimmed['estimated_input_tokens'], trimmed['input_budget_tokens'])
            self.assertGreater(untrimmed['estimated_input_tokens'], untrimmed['input_budget_tokens'])
            with self.assertRaisesRegex(ModelError, 'request size limit.*max_request_bytes'):
                agent.run(prompt)
            self.assertEqual(requests, [])
            self.assertEqual(tools.calls, [])
            self.assertEqual(agent.history, history)
            self.assertEqual(agent.run('next').text, 'hello')
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]['body']['messages'][1:-1], history[0])
            self.assertEqual(agent.history, history + [[{'role': 'user', 'content': 'next'},
                                                       {'role': 'assistant', 'content': 'hello'}]])

    def test_million_window_uses_estimates_not_legacy_character_ceiling(self):
        for prompt in ('a' * 800000, '한' * 250000):
            with self.subTest(character=prompt[0]):
                settings = deepcopy(DEFAULTS)
                settings['model']['context_window_tokens'] = 1000000
                client = FakeClient({'role': 'assistant', 'content': 'ok'})
                events = []
                agent = Agent(client, FakeTools(), settings, on_event=events.append)
                self.assertEqual(agent.run(prompt).text, 'ok')
                self.assertEqual(client.requests[0][-1]['content'], prompt)
                usage = events[0]['context']
                self.assertEqual(usage['context_window_tokens'], 1000000)
                self.assertEqual(usage['input_budget_tokens'],
                                 1000000 - settings['model']['max_tokens'] - 1024)
                self.assertEqual(usage['reserved_output_tokens'], settings['model']['max_tokens'])
                self.assertEqual(usage['safety_margin_tokens'], 1024)
                self.assertGreater(usage['estimated_input_tokens'], len(prompt.encode('utf-8')))
                self.assertLessEqual(usage['estimated_input_tokens'], usage['input_budget_tokens'])
                self.assertIn('UTF-8', usage['method'])
                self.assertIn('ESTIMATE', events[0]['detail'])
                self.assertNotIn('토큰 아님', events[0]['detail'])
