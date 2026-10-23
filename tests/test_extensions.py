"""Extension integration contracts; fake backends isolate wiring from processes."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from david.config import ConfigError, load_settings


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / 'project'
        self.workspace.mkdir()
        self.config = self.root / 'config.json'
        self.write_config({})

    def write_config(self, extra):
        data = {'model': {'name': 'fixture'}}
        data.update(extra)
        self.config.write_text(json.dumps(data), encoding='utf-8')

    def test_generic_tools_dispatch_with_mutable_permissions_and_workspace_undo(self):
        from david.extensions import ExtensionTools
        from david.workspace import WorkspaceTools
        settings = load_settings(self.config)
        calls = []
        class Manager:
            def list_tools(self, server):
                self.cancel_check()
                if not self.approve('mcp', server):
                    raise ValueError('Denied')
                calls.append(('list', server))
                return {'tools': []}
            def call_tool(self, server, name, arguments):
                self.cancel_check()
                if not self.approve('mcp', name):
                    raise ValueError('Denied')
                calls.append((server, name, arguments))
                return {'content': [{'type': 'text', 'text': 'ok'}]}
            def close(self):
                calls.append('closed')
        base = WorkspaceTools(self.workspace, settings['security'], {}, lambda *args: False)
        manager = Manager()
        tools = ExtensionTools(base, manager, {'demo': {}})
        tools.approve = lambda *args: True
        checked = []
        tools.cancel_check = lambda: checked.append(True)
        names = {s['function']['name'] for s in tools.schemas()}
        self.assertIn('mcp_list_tools', names)
        self.assertIn('mcp_call', names)
        tools.execute('mcp_list_tools', {'server': 'demo'})
        result = tools.execute('mcp_call', {'server': 'demo', 'tool': 'echo', 'arguments_json': '{"x":1}'})
        self.assertEqual(result['mcp_result']['content'][0]['text'], 'ok')
        self.assertTrue(result['ok'])
        self.assertIn(('demo', 'echo', {'x': 1}), calls)
        self.assertTrue(checked)
        tools.begin_turn()
        tools.execute('write_file', {'path': 'new.txt', 'content': 'created'})
        tools.end_turn()
        self.assertTrue(tools.export_journal())
        tools.undo_last()
        self.assertFalse((self.workspace / 'new.txt').exists())
        self.assertIs(tools.approve, base.approve)
        self.assertIs(manager.approve, base.approve)
        tools.security['read_only'] = True
        before = list(calls)
        with self.assertRaises(ValueError):
            tools.execute('mcp_call', {'server': 'demo', 'tool': 'echo', 'arguments_json': '{}'})
        self.assertEqual(calls, before)
        tools.close()
        self.assertEqual(calls[-1], 'closed')

    def test_mcp_json_rejects_duplicates_nonfinite_depth_and_size_before_dispatch(self):
        from david.extensions import ExtensionTools
        from david.workspace import WorkspaceTools
        from unittest.mock import Mock
        manager = Mock()
        base = WorkspaceTools(self.workspace, load_settings(self.config)['security'], {}, lambda *args: True)
        tools = ExtensionTools(base, manager, {'demo': {}})
        bad = ['{"a":1,"a":2}', '{"a":NaN}', '{"a":1e999}', '[]',
               '{"a":' + '[' * 18 + '0' + ']' * 18 + '}', '{"a":"' + 'x' * 65536 + '"}']
        for raw in bad:
            with self.subTest(raw=raw[:100]):
                with self.assertRaises(ValueError):
                    tools.execute('mcp_call', {'server': 'demo', 'tool': 'echo', 'arguments_json': raw})
        manager.call_tool.assert_not_called()

    def test_build_loads_selected_skills_protects_paths_and_never_starts_mcp(self):
        from david.cli import build_session, main
        from david.extensions import ExtensionTools
        from unittest.mock import Mock
        skill = self.workspace / 'skills' / 'review.md'
        skill.parent.mkdir()
        skill.write_text('Review carefully.', encoding='utf-8')
        self.write_config({'mcp': {'servers': {'demo': {'command': ['{python}', 'server.py']}}},
                           'skills': {'registered': {'review': str(skill)}, 'enabled': []}})
        class Registry:
            def __init__(self, registered, base_dir):
                self.registered, self.base_dir = registered, base_dir
            def load(self, names):
                return '\n'.join(Path(self.registered[n]).read_text() for n in names)
            def protected_paths(self):
                return [Path(p) for p in self.registered.values()]
            def list(self):
                return [{'name': n, 'path': p, 'description': ''} for n, p in self.registered.items()]
        manager = Mock()
        factory = Mock(return_value=manager)
        with patch('subprocess.Popen', side_effect=AssertionError('must not spawn')):
            agent = build_session(self.config, self.workspace, skill_names=['review'],
                                  manager_factory=factory, registry_factory=Registry)
            with patch('sys.stdout', new=io.StringIO()):
                self.assertEqual(main(['--settings', str(self.config), '--check']), 0)
        self.assertIsInstance(agent.tools, ExtensionTools)
        self.assertIn('Review carefully.', agent.system)
        self.assertEqual(agent.skill_names, ['review'])
        with self.assertRaises(Exception):
            agent.tools.execute('read_file', {'path': 'skills/review.md'})
        self.assertEqual(load_settings(self.config)['skills']['enabled'], [])
        factory.assert_called_once()
        manager.list_tools.assert_not_called()
        manager.call_tool.assert_not_called()
        self.assertEqual(factory.call_args.kwargs['workspace'], self.workspace)
        agent.tools.close()
        manager.close.assert_called_once()

    def test_controller_skill_switch_partitions_session_preserves_undo_and_wrapper_on_resume(self):
        from david.controller import Controller
        from david.extensions import ExtensionTools
        skill = self.workspace / 'skills' / 'review.md'
        skill.parent.mkdir()
        skill.write_text('Review carefully.', encoding='utf-8')
        self.write_config({'mcp': {'servers': {'demo': {'command': ['{python}', 'server.py']}}},
                           'skills': {'registered': {'review': str(skill)}, 'enabled': []}})
        events = []
        controller = Controller(self.config, self.workspace, session_dir=self.root / 'sessions', on_event=events.append)
        self.addCleanup(controller.close)
        tools = controller.agent.tools
        tools.approve = lambda *args: True
        tools.begin_turn()
        tools.execute('write_file', {'path': 'new.txt', 'content': 'created'})
        tools.end_turn()
        journal = tools.export_journal()
        controller.agent.history = [[{'role': 'user', 'content': 'old'}, {'role': 'assistant', 'content': 'old'}]]
        controller.requests = ['old']
        controller.saving = True
        old_id, old_key = controller.session_id, controller._model_key()
        controller._command('/skill review')
        self.assertIn('Review carefully.', controller.agent.system)
        self.assertEqual(controller.agent.history, [])
        self.assertEqual(controller.requests, [])
        self.assertFalse(controller.saving)
        self.assertNotEqual(controller.session_id, old_id)
        self.assertNotEqual(controller._model_key(), old_key)
        self.assertEqual(controller.agent.tools.export_journal(), journal)
        self.assertIs(controller.agent.tools, tools)
        controller._persist()
        saved_id = controller.session_id
        controller._command('/new')
        controller._resume(saved_id)
        self.assertIsInstance(controller.agent.tools, ExtensionTools)
        self.assertIs(controller.agent.tools.manager, tools.manager)
        self.assertEqual(controller.agent.tools.export_journal(), journal)
        self.assertIs(controller.agent.tools.cancel_check, controller.agent.tools.manager.cancel_check)
        controller._command('/skill off')
        self.assertNotIn('Review carefully.', controller.agent.system)
        self.assertEqual(controller._model_key(), old_key)
        for name in ('/mcp', '/skills', '/skill'):
            self.assertIn(name, controller.commands)
        controller._command('/mcp')
        controller._command('/skills')
        text = '\n'.join(e.get('text', '') for e in events)
        self.assertIn('demo', text)
        self.assertIn('review', text)
        before = controller._model_key()
        controller.agent.settings['mcp']['servers']['demo']['env'] = {'TOKEN': 'OTHER_TOKEN'}
        self.assertNotEqual(before, controller._model_key())

    def test_cli_one_shot_and_plain_skill_commands_close_mcp_without_global_changes(self):
        from david.cli import main
        from david.agent import TurnResult
        skill = self.root / 'registered' / 'review.md'
        skill.parent.mkdir()
        skill.write_text('Review carefully.', encoding='utf-8')
        self.write_config({'skills': {'registered': {'review': 'registered/review.md'}, 'enabled': []},
                           'mcp': {'servers': {'demo': {'command': ['{python}', 'server.py']}}}})
        agents = []
        def run(agent, prompt):
            agents.append((agent, prompt, agent.system, list(agent.history)))
            agent.history.append([{'role': 'user', 'content': prompt}, {'role': 'assistant', 'content': 'ok'}])
            return TurnResult('ok', 1, 0.01)
        with patch('david.agent.Agent.run', new=run), patch('david.mcp.MCPManager.close') as close, \
                patch('sys.stdout', new=io.StringIO()):
            result = main(['--settings', str(self.config), '--workspace', str(self.workspace),
                           '--skill', 'review', '--prompt', 'inspect'])
        self.assertEqual(result, 0)
        self.assertIn('Review carefully.', agents[-1][2])
        close.assert_called_once()
        agents.clear()
        output = io.StringIO()
        with patch('david.agent.Agent.run', new=run), patch('david.mcp.MCPManager.close') as close, \
                patch('builtins.input', side_effect=['old', '/skills', '/mcp', '/skill review', 'new',
                                                    '/skill off', 'last', '/reload', '/quit']), \
                patch('sys.stdout', new=output):
            self.assertEqual(main(['--settings', str(self.config), '--workspace', str(self.workspace), '--plain']), 0)
        self.assertEqual([p for _, p, _, _ in agents], ['old', 'new', 'last'])
        self.assertNotIn('Review carefully.', agents[0][2])
        self.assertIn('Review carefully.', agents[1][2])
        self.assertNotIn('Review carefully.', agents[2][2])
        self.assertEqual(agents[1][3], [])
        self.assertEqual(agents[2][3], [])
        self.assertEqual(close.call_count, 2)
        self.assertIn('Configured MCP', output.getvalue())
        self.assertEqual(load_settings(self.config)['skills']['enabled'], [])

    def test_controller_reload_and_close_release_manager_and_honor_initial_selection(self):
        from david.controller import Controller
        skill = self.root / 'registered' / 'review.md'
        skill.parent.mkdir()
        skill.write_text('Review carefully.', encoding='utf-8')
        self.write_config({'skills': {'registered': {'review': 'registered/review.md'}, 'enabled': []},
                           'mcp': {'servers': {'demo': {'command': ['{python}', 'server.py']}}}})
        controller = Controller(self.config, self.workspace, skill_names=['review'], session_dir=self.root / 'sessions')
        self.addCleanup(controller.close)
        self.assertIn('Review carefully.', controller.agent.system)
        with patch.object(controller.agent.tools.manager, 'close') as old_close:
            controller._command('/reload')
            old_close.assert_called_once()
        self.assertIn('Review carefully.', controller.agent.system)
        with patch.object(controller.agent.tools.manager, 'close') as new_close:
            self.assertTrue(controller.close())
            new_close.assert_called_once()

    def test_real_mcp_failure_is_isolated_summarized_and_persisted(self):
        from david.controller import Controller
        from test_agent import FakeClient
        from test_integration import tool_message
        fixture = Path(__file__).parent / 'fixtures' / 'mcp_server.py'
        audit = self.root / 'audit.jsonl'
        self.write_config({'mcp': {'servers': {'demo': {
            'command': ['{python}', str(fixture), 'normal', str(audit)],
            'timeout_seconds': 2, 'max_response_bytes': 65536}}}})
        events = []
        controller = Controller(self.config, self.workspace, session_dir=self.root / 'sessions')
        self.addCleanup(controller.close)
        def on_event(event):
            events.append(event)
            if event['type'] == 'approval':
                controller.respond_approval(True)
        controller.on_event = on_event
        client = FakeClient(tool_message('mcp_call', {'server': 'demo', 'tool': 'fail',
                                  'arguments_json': '{"message":"fixture failure"}'}, 'm'),
                            {'role': 'assistant', 'content': 'Tool failed.'})
        controller.agent.client = client
        self.assertFalse(audit.exists())
        controller.submit('/mcp')
        self.assertTrue(controller.wait(3))
        self.assertFalse(audit.exists())
        controller.submit('Exercise MCP')
        self.assertTrue(controller.wait(5))
        tool = next(e['result'] for e in events if e['type'] == 'tool')
        self.assertFalse(tool['ok'])
        self.assertTrue(tool['mcp_result']['isError'])
        self.assertIn('error', tool)
        summary = next(e['text'] for e in events if e['type'] == 'summary')
        self.assertIn(tool['error'], summary)
        self.assertIn('fixture failure', json.dumps(client.requests[-1]))
        self.assertEqual({e['action'] for e in events if e['type'] == 'approval'}, {'mcp_start', 'mcp_call'})
        controller._persist()
        payload = controller._store().load(controller.session_id, controller.workspace, controller._model_key())
        restored = next(e['result'] for e in payload['transcript'] if e['type'] == 'tool')
        self.assertEqual(restored, tool)
        controller.close()
        import os
        pid = int(audit.with_suffix('.pid').read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_config_limits_match_runtime_and_large_numbers_fail_as_config_errors(self):
        for changes in ({'timeout_seconds': 121}, {'max_response_bytes': 2097153},
                        {'timeout_seconds': 10 ** 400}, {'command': ['{python}'] + ['x' * 4096] * 9}):
            self.write_config({'mcp': {'servers': {'demo': {'command': ['{python}'], **changes}}}})
            with self.subTest(changes=repr(changes)[:100]), self.assertRaises(ConfigError):
                load_settings(self.config)

    def test_full_screen_skill_commands_are_keyboard_accessible_without_connecting(self):
        import asyncio
        from david.controller import Controller
        from david.tui import DavidTUI
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        skill = self.root / 'registered' / 'review.md'
        skill.parent.mkdir()
        skill.write_text('Review carefully.', encoding='utf-8')
        self.write_config({'skills': {'registered': {'review': 'registered/review.md'}, 'enabled': []},
                           'mcp': {'servers': {'demo': {'command': ['{python}', 'server.py']}}}})
        controller = Controller(self.config, self.workspace, session_dir=self.root / 'sessions')
        self.addCleanup(controller.close)
        async def exercise():
            async def until(predicate):
                async with asyncio.timeout(3):
                    while not predicate():
                        await asyncio.sleep(.01)
            with create_pipe_input() as pipe:
                ui = DavidTUI(controller, input=pipe, output=DummyOutput())
                task = asyncio.create_task(ui.application.run_async())
                try:
                    await until(lambda: ui.application.is_running)
                    pipe.send_text('/skills\r')
                    await until(lambda: 'Registered skills' in ui.transcript.text)
                    pipe.send_text('/skill review\r')
                    await until(lambda: 'Skills: review' in ui.transcript.text)
                    self.assertIn('Review carefully.', controller.agent.system)
                    pipe.send_text('/mcp\r')
                    await until(lambda: 'Configured MCP' in ui.transcript.text)
                    self.assertIn('demo', ui.transcript.text)
                    pipe.send_text('/skill off\r')
                    await until(lambda: 'Skills: off' in ui.transcript.text)
                    self.assertNotIn('Review carefully.', controller.agent.system)
                    self.assertFalse(controller.saving)
                finally:
                    if not task.done():
                        ui.application.exit(result=0)
                    await asyncio.wait_for(task, 3)
        with patch('subprocess.Popen', side_effect=AssertionError('listing must not connect')):
            asyncio.run(exercise())

    def test_disabled_readonly_and_cancelled_wrappers_never_launch_or_dispatch(self):
        from david.cli import build_session
        from david.cancellation import Cancelled
        from david.agent import AgentError
        from test_agent import FakeClient
        from test_integration import tool_message
        for servers, read_only in (({}, False), ({'demo': {'command': ['{python}']}}, True)):
            self.write_config({'mcp': {'servers': servers}})
            agent = build_session(self.config, self.workspace, read_only)
            self.addCleanup(agent.tools.close)
            with patch('subprocess.Popen', side_effect=AssertionError('must not spawn')):
                self.assertNotIn('mcp_call', [s['function']['name'] for s in agent.tools.schemas()])
                with self.assertRaises(ValueError):
                    agent.tools.execute('mcp_call', {'server': 'demo', 'tool': 'echo', 'arguments_json': '{}'})
                agent.client = FakeClient(tool_message('mcp_call', {'server': 'demo', 'tool': 'echo',
                                                                   'arguments_json': '{}'}, 'm'))
                with self.assertRaises(AgentError):
                    agent.run('Invoke disabled MCP')
        self.write_config({'mcp': {'servers': {'demo': {'command': ['{python}']}}}})
        agent = build_session(self.config, self.workspace)
        self.addCleanup(agent.tools.close)
        def cancel():
            raise Cancelled('cancelled')
        agent.tools.cancel_check = cancel
        with patch('subprocess.Popen', side_effect=AssertionError('must not spawn')):
            with self.assertRaises(Cancelled):
                agent.tools.execute('mcp_list_tools', {'server': 'demo'})

    def test_closed_controller_cannot_restart_extension_work(self):
        from david.controller import Controller
        controller = Controller(self.config, self.workspace, session_dir=self.root / 'sessions')
        self.assertTrue(controller.close())
        self.assertFalse(controller.submit('/mcp'))

    def test_extension_configuration_defaults_and_strict_entries(self):
        settings = load_settings(self.config)
        self.assertEqual(settings['mcp'], {'servers': {}})
        self.assertEqual(settings['skills'], {'registered': {}, 'enabled': []})
        valid = {'mcp': {'servers': {'demo': {'command': ['{python}', '-u', 'server.py'],
                    'env': {'API_TOKEN': 'DEMO_TOKEN'}, 'timeout_seconds': 30,
                    'max_response_bytes': 262144}}},
                 'skills': {'registered': {'review': 'review/SKILL.md'}, 'enabled': ['review']}}
        self.write_config(valid)
        self.assertEqual(load_settings(self.config)['skills'], valid['skills'])
        invalid = [
            {'mcp': {'servers': []}},
            {'mcp': {'servers': {'bad name': {'command': ['{python}']}}}},
            {'mcp': {'servers': {'demo': {'command': ['python']}}}},
            {'mcp': {'servers': {'demo': {'command': ['/bin/tool.cmd']}}}},
            {'mcp': {'servers': {'demo': {'command': ['{python}'], 'shell': True}}}},
            {'mcp': {'servers': {'demo': {'command': ['{python}'], 'env': {'TOKEN': 'secret-value!'}}}}},
            {'mcp': {'servers': {'demo': {'command': ['{python}'], 'timeout_seconds': float('nan')}}}},
            {'mcp': {'servers': {'demo': {'command': ['{python}'], 'max_response_bytes': True}}}},
            {'skills': {'registered': {'bad name': 'x'}}},
            {'skills': {'registered': {'review': 2}}},
            {'skills': {'enabled': ['unknown']}},
            {'skills': {'registered': {'review': 'x'}, 'enabled': ['review', 'review']}},
            {'skills': {'registered': {str(i): 'x' for i in range(33)}}},
        ]
        for data in invalid:
            with self.subTest(data=data):
                self.write_config(data)
                with self.assertRaises(ConfigError):
                    load_settings(self.config)
