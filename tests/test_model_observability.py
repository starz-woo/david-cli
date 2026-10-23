"""Observed progress and server usage through the production loopback transport."""
import json
import unittest

from test_transport import event, response, serve
from david.transport import OpenAIClient


def usage_event(usage):
    return ('data: ' + json.dumps({'choices': [], 'usage': usage}) + '\n\n').encode()


class ObservationConfigTests(unittest.TestCase):
    def test_stream_usage_request_is_explicit_and_deadline_overrides_are_validated(self):
        import tempfile
        from pathlib import Path
        from david.config import ConfigError, load_settings
        from david.transport import ModelError
        knobs = {'connect_timeout_seconds': 10, 'idle_timeout_seconds': 120,
                 'total_timeout_seconds': 1800, 'stream_include_usage': True}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.json'
            path.write_text(json.dumps({'model': {'name': 'fixture', **knobs}}))
            settings = load_settings(path)
            self.assertEqual({key: settings['model'][key] for key in knobs}, knobs)
            for field in knobs:
                for value in ('invalid', None, -1, float('inf'), 100000, 10**400):
                    path.write_text(json.dumps({'model': {'name': 'fixture', field: value}}))
                    with self.subTest(field=field, value=value), self.assertRaises(ConfigError):
                        load_settings(path)
                    with self.assertRaises(ModelError):
                        OpenAIClient({'base_url': 'http://127.0.0.1/v1', 'name': 'fixture', field: value})
        body = event({'content': 'ok'}, 'stop') + b'data: [DONE]\n\n'
        for stream, enabled in ((True, True), (True, False), (False, True)):
            with self.subTest(stream=stream, enabled=enabled), serve(body if stream else response()) as (url, requests):
                OpenAIClient({'base_url': url, 'name': 'fixture', 'stream': stream,
                              'stream_include_usage': enabled}).complete([])
                if stream and enabled:
                    self.assertEqual(requests[0]['body']['stream_options'], {'include_usage': True})
                else:
                    self.assertNotIn('stream_options', requests[0]['body'])


class DeadlineTests(unittest.TestCase):
    def test_connect_deadline_bounds_real_stalled_tls_and_header_idle_is_distinct(self):
        import socket
        import time
        from david.transport import ModelError
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            client = OpenAIClient({'base_url': f'https://127.0.0.1:{listener.getsockname()[1]}/v1',
                                   'name': 'fixture', 'timeout_seconds': 3,
                                   'connect_timeout_seconds': 0.2})
            started = time.monotonic()
            with self.assertRaisesRegex(ModelError, 'connect deadline'):
                client.complete([])
            self.assertLess(time.monotonic() - started, 1.2)
        with serve(b'', slow_headers=True, delay=0.05) as (url, _):
            client = OpenAIClient({'base_url': url, 'name': 'fixture', 'timeout_seconds': 3,
                                   'idle_timeout_seconds': 0.2})
            with self.assertRaisesRegex(ModelError, 'idle deadline'):
                client.complete([])

    def test_reasoning_activity_extends_idle_but_never_total_deadline(self):
        from david.transport import ModelError
        # Leave scheduling slack for loaded CI/parallel UI runs while keeping
        # the stream longer than BOTH the legacy total and the new idle limit.
        pieces = [event({'reasoning_content': 'private'})] * 24
        pieces += [event({'content': 'done'}, 'stop'), b'data: [DONE]\n\n']
        with serve(b'', pieces=pieces, delay=0.05) as (url, requests):
            config = {'base_url': url, 'name': 'fixture', 'timeout_seconds': 0.15,
                      'connect_timeout_seconds': 1, 'idle_timeout_seconds': 0.8,
                      'total_timeout_seconds': 10}
            client = OpenAIClient(config)
            self.assertEqual(client.complete([])['content'], 'done')
            self.assertEqual(client.last_progress['reasoning_chunks'], 24)
            self.assertEqual(len(requests), 1)
            config['total_timeout_seconds'] = 0.3
            with self.assertRaisesRegex(ModelError, 'request deadline'):
                OpenAIClient(config).complete([])

    def test_stalled_reasoning_stream_uses_idle_deadline_and_records_partial_usage(self):
        from copy import deepcopy
        import time
        from david.agent import Agent
        from david.config import DEFAULTS
        from david.transport import ModelError
        from test_agent import FakeTools
        with serve(b'', pieces=[event({'reasoning': 'private'}), b': keepalive\n\n'], delay=0.5) as (url, requests):
            config = {'base_url': url, 'name': 'fixture', 'timeout_seconds': 2,
                      'idle_timeout_seconds': 0.12}
            agent = Agent(OpenAIClient(config), FakeTools(), deepcopy(DEFAULTS))
            started = time.monotonic()
            with self.assertRaisesRegex(ModelError, 'idle deadline'):
                agent.run('Long task')
            self.assertLess(time.monotonic() - started, 0.6)
            snapshot = agent.usage_snapshot()['turn']
            self.assertTrue(snapshot['partial'])
            self.assertEqual(snapshot['incomplete_requests'], 1)
            self.assertEqual(snapshot['requests'], 1)
            self.assertIsNone(snapshot['tokens']['total_tokens'])
            self.assertEqual(len(requests), 1)


class AgentUsageTests(unittest.TestCase):
    def test_cancelled_stream_keeps_reported_subtotals_and_missing_usage_is_unknown(self):
        from copy import deepcopy
        import threading
        from david.agent import Agent
        from david.config import DEFAULTS
        from david.cancellation import Cancelled, check_cancel
        from test_agent import FakeClient, FakeTools
        cancel = threading.Event()
        reported = {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12}
        body = usage_event(reported) + event({'reasoning_content': 'private'})
        with serve(body) as (url, requests):
            def observe(item):
                if item.get('status') == 'thinking':
                    cancel.set()
            agent = Agent(OpenAIClient({'base_url': url, 'name': 'fixture'}, cancel_event=cancel),
                          FakeTools(), deepcopy(DEFAULTS), on_event=observe,
                          cancel_check=lambda: check_cancel(cancel))
            with self.assertRaises(Cancelled):
                agent.run('Long request')
            usage = agent.usage_snapshot()['turn']
            self.assertEqual(usage['tokens']['total_tokens'], 12)
            self.assertTrue(usage['partial'])
            self.assertEqual(usage['incomplete_requests'], 1)
            self.assertEqual(len(requests), 1)
        cancel.clear()
        # Old clients without the optional callback still work, with unknown
        # usage rather than a stale prior response or fabricated zero.
        agent.client = FakeClient({'role': 'assistant', 'content': 'ok'})
        agent.run('Next')
        usage = agent.usage_snapshot()
        self.assertIsNone(usage['request_usage'])
        self.assertIsNone(usage['turn']['tokens']['total_tokens'])
        self.assertEqual(usage['session']['tokens']['total_tokens'], 12)
        self.assertEqual(usage['session']['reported_requests']['total_tokens'], 1)
        self.assertEqual(usage['session']['requests'], 2)
        self.assertTrue(usage['session']['partial'])

    def test_client_internal_typeerror_is_never_retried(self):
        from copy import deepcopy
        from david.agent import Agent
        from david.config import DEFAULTS
        from test_agent import FakeTools
        class BrokenClient:
            calls = 0
            def complete(self, messages, tools=None, on_text=None, *, on_progress=None):
                self.calls += 1
                raise TypeError('internal client failure after request')
        client = BrokenClient()
        agent = Agent(client, FakeTools(), deepcopy(DEFAULTS))
        with self.assertRaisesRegex(TypeError, 'internal client'):
            agent.run('Do not retry')
        self.assertEqual(client.calls, 1)
        self.assertTrue(agent.usage_snapshot()['turn']['partial'])

    def test_turn_and_live_session_count_requests_not_tools_or_usage_chunks(self):
        from copy import deepcopy
        from contextlib import ExitStack
        from david.agent import Agent
        from david.config import DEFAULTS
        from test_agent import FakeTools, tool_call
        tools = FakeTools()
        first = (event({'reasoning_content': 'private'})
                 + event({'tool_calls': [dict(tool_call(ident='a'), index=0),
                                         dict(tool_call(ident='b'), index=1)]}, 'tool_calls')
                 + usage_event({'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12})
                 + usage_event({'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12})
                 + b'data: [DONE]\n\n')
        second = (event({'content': 'Done'}, 'stop')
                  + usage_event({'prompt_tokens': 20, 'completion_tokens': 3, 'total_tokens': 23})
                  + b'data: [DONE]\n\n')
        # Each request still traverses the unmodified production complete/SSE path.
        with ExitStack() as stack:
            endpoints = [stack.enter_context(serve(body)) for body in (first, second, second)]
            class SequentialClient:
                def complete(self, messages, tools=None, on_text=None, *, on_progress=None):
                    url, _ = endpoints.pop(0)
                    return OpenAIClient({'base_url': url, 'name': 'fixture'}).complete(
                        messages, tools, on_text, on_progress=on_progress)
            events = []
            agent = Agent(SequentialClient(), tools, deepcopy(DEFAULTS), on_event=events.append)
            agent.run('Read twice')
            snapshot = agent.usage_snapshot()
            self.assertEqual(snapshot['turn']['requests'], 2)
            self.assertEqual(snapshot['turn']['tokens']['total_tokens'], 35)
            self.assertEqual(snapshot['turn']['tokens']['prompt_tokens'], 30)
            self.assertIsNone(snapshot['turn']['tokens']['reasoning_tokens'])
            self.assertFalse(snapshot['turn']['partial'])
            self.assertEqual(len(tools.calls), 2)
            self.assertEqual(snapshot['request_usage']['prompt_tokens'], 20)
            self.assertEqual(snapshot['context']['unit'], 'estimated_tokens')
            self.assertTrue(any(e.get('status') == 'thinking' and 'progress' in e for e in events))
            self.assertNotIn('private', json.dumps(events))
            agent.run('Next')
            snapshot = agent.usage_snapshot()
            self.assertEqual(snapshot['turn']['requests'], 1)
            self.assertEqual(snapshot['session']['requests'], 3)
            self.assertEqual(snapshot['session']['tokens']['total_tokens'], 58)
            self.assertEqual(snapshot['session']['scope'], 'live-only')


class UsageTests(unittest.TestCase):
    def test_invalid_usage_is_bounded_safe_and_never_delivered(self):
        from david.transport import ModelError
        invalid = [[], 'private-secret', {'prompt_tokens': True}, {'total_tokens': -1},
                   {'completion_tokens': 1.5}, {'total_tokens': 10**13},
                   {'prompt_tokens_details': []},
                   {'completion_tokens_details': {'reasoning_tokens': 'private-secret'}},
                   {'prompt_tokens': 4, 'completion_tokens': 3, 'total_tokens': 2},
                   {'completion_tokens': 1, 'completion_tokens_details': {'reasoning_tokens': 2}},
                   {'prompt_tokens': 1, 'prompt_tokens_details': {'cached_tokens': 2}}]
        for usage in invalid:
            for stream in (False, True):
                obj = json.loads(response())
                obj['usage'] = usage
                body = (event({'content': 'hello'}, 'stop') + usage_event(usage)
                        + b'data: [DONE]\n\n') if stream else json.dumps(obj).encode()
                with self.subTest(usage=usage, stream=stream), serve(body) as (url, _):
                    client = OpenAIClient({'base_url': url, 'name': 'fixture', 'stream': stream})
                    progress = []
                    with self.assertRaises(ModelError) as caught:
                        client.complete([], on_progress=progress.append)
                    self.assertNotIn('private-secret', str(caught.exception))
                    self.assertFalse(any(item.get('usage') for item in progress))
                    self.assertIsNone(client.last_usage)

    def test_server_usage_kept_separate_from_message_and_stream_snapshots_not_added(self):
        reported = {'prompt_tokens': 21, 'completion_tokens': 9, 'total_tokens': 30,
                    'prompt_tokens_details': {'cached_tokens': 10},
                    'completion_tokens_details': {'reasoning_tokens': 7}}
        expected = {'prompt_tokens': 21, 'completion_tokens': 9, 'total_tokens': 30,
                    'cached_tokens': 10, 'reasoning_tokens': 7}
        nonstream = json.loads(response())
        nonstream['usage'] = reported
        cases = [(False, json.dumps(nonstream).encode()),
                 (True, usage_event(reported) + event({'content': 'hello'}, 'stop')
                  + usage_event(reported) + b'data: [DONE]\n\n')]
        for stream, body in cases:
            with self.subTest(stream=stream), serve(body) as (url, _):
                client = OpenAIClient({'base_url': url, 'name': 'fixture', 'stream': stream})
                progress = []
                self.assertEqual(client.complete([], on_progress=progress.append),
                                 {'role': 'assistant', 'content': 'hello'})
                self.assertEqual(client.last_usage, expected)
                self.assertTrue(any(item.get('usage') == expected for item in progress))
        with serve(response()) as (url, _):
            client = OpenAIClient({'base_url': url, 'name': 'fixture', 'stream': False})
            client.last_usage = expected
            client.complete([])
            self.assertIsNone(client.last_usage)
        with serve(event({}, 'stop') + usage_event({'total_tokens': 5}) + b'data: [DONE]\n\n') as (url, _):
            client = OpenAIClient({'base_url': url, 'name': 'fixture'})
            client.complete([])
            self.assertIsNone(client.last_usage['prompt_tokens'])
            self.assertIsNone(client.last_usage['reasoning_tokens'])
            self.assertEqual(client.last_usage['total_tokens'], 5)


class ProgressTests(unittest.TestCase):
    def test_reasoning_is_observed_without_exposing_it_or_guessing_from_text(self):
        body = (event({'role': 'assistant'})
                + event({'reasoning_content': 'private reasoning'})
                + event({'reasoning': 'more private reasoning'})
                + event({'content': 'I am thinking about the answer.'})
                + event({}, 'stop') + b'data: [DONE]\n\n')
        with serve(body) as (url, requests):
            client = OpenAIClient({'base_url': url, 'name': 'fixture'})
            progress, texts = [], []
            result = client.complete([], on_text=texts.append, on_progress=progress.append)
        phases = [item['phase'] for item in progress]
        self.assertEqual(phases[:2], ['connecting', 'waiting'])
        self.assertIn('thinking', phases)
        self.assertIn('generating', phases)
        self.assertEqual(result, {'role': 'assistant', 'content': texts[0]})
        self.assertEqual(client.last_progress['reasoning_chunks'], 2)
        self.assertGreater(client.last_progress['last_activity_seconds'], 0)
        self.assertGreaterEqual(client.last_progress['elapsed_seconds'],
                                client.last_progress['last_activity_seconds'])
        self.assertNotIn('private reasoning', json.dumps(progress))
        self.assertEqual(len(requests), 1)
        with serve(event({'content': 'thinking...'}, 'stop') + b'data: [DONE]\n\n') as (url, _):
            progress.clear()
            OpenAIClient({'base_url': url, 'name': 'fixture'}).complete([], on_progress=progress.append)
        self.assertNotIn('thinking', [item['phase'] for item in progress])
