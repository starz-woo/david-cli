"""MCP integration tests: every protocol path uses a real local subprocess."""
import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest


FIXTURE = Path(__file__).parent / 'fixtures' / 'mcp_server.py'


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.audit = self.root / 'audit.jsonl'
        self.approvals = []

    def manager(self, mode='normal', **options):
        module = importlib.import_module('david.mcp')
        config = {'command': ['{python}', str(FIXTURE), mode, str(self.audit)],
                  'env': {}, 'timeout_seconds': 1, 'max_response_bytes': 65536}
        config.update(options.pop('config', {}))
        manager = module.MCPManager({'fixture': config}, self.root,
                                    options.pop('approve', self.approve),
                                    options.pop('cancel_check', lambda: None), **options)
        self.addCleanup(manager.close)
        return manager

    def approve(self, action, detail):
        self.approvals.append((action, detail))
        return True

    def messages(self):
        return [json.loads(line) for line in self.audit.read_text().splitlines()]

    def test_MCP_approvals_gate_launch_and_every_call(self):
        from david.workspace import ToolError
        manager = self.manager(approve=lambda *_: False)
        with self.assertRaises(ToolError):
            manager.list_tools('fixture')
        self.assertFalse(self.audit.with_suffix('.pid').exists())
        manager.approve = self.approve
        arguments = {'message': 'hello\\n한글', 'nested': {'full': 'value'}}
        result = manager.call_tool('fixture', 'fail', arguments)
        self.assertTrue(result['isError'])
        self.assertEqual(json.loads(result['content'][0]['text']), arguments)
        self.assertEqual({a for a, _ in self.approvals}, {'mcp_start', 'mcp_call'})
        start = next(d for a, d in self.approvals if a == 'mcp_start')
        self.assertIn(str(self.root), start)
        self.assertIn(str(FIXTURE), start)
        self.assertIn('not sandbox', start)
        detail = next(d for a, d in self.approvals if a == 'mcp_call')
        self.assertIn('fixture', detail)
        self.assertIn('fail', detail)
        self.assertIn('value', detail)
        manager.call_tool('fixture', 'echo', {})
        self.assertEqual(sum(a == 'mcp_start' for a, _ in self.approvals), 1)
        self.assertEqual(sum(a == 'mcp_call' for a, _ in self.approvals), 2)
        before = self.messages()
        manager.approve = lambda *_: False
        with self.assertRaises(ToolError):
            manager.call_tool('fixture', 'echo', {})
        self.assertEqual(self.messages(), before)

    def test_MCP_denied_call_retains_only_prior_approved_connection(self):
        from david.mcp import MCPError
        manager = self.manager()
        manager.list_tools('fixture')
        connection = manager._connections['fixture']
        before = self.messages()
        self.assertEqual([action for action, _ in self.approvals], ['mcp_start'])

        def deny(action, detail):
            self.approvals.append((action, detail))
            return False

        manager.approve = deny
        with self.assertRaisesRegex(MCPError, 'call approval denied'):
            manager.call_tool('fixture', 'echo', {'denied': True})
        self.assertEqual(self.messages(), before)
        self.assertEqual([action for action, _ in self.approvals], ['mcp_start', 'mcp_call'])
        self.assertIs(manager._connections['fixture'], connection)
        self.assertIsNone(connection.process.poll())
        manager.approve = self.approve
        result = manager.call_tool('fixture', 'echo', {'approved': True})
        self.assertEqual(json.loads(result['content'][0]['text']), {'approved': True})
        self.assertEqual([action for action, _ in self.approvals],
                         ['mcp_start', 'mcp_call', 'mcp_call'])
        self.assertEqual([m['method'] for m in self.messages()[len(before):]], ['tools/call'])
        manager.close()
        self.assertFalse(manager._connections)
        self.assertIsNotNone(connection.process.poll())

    def test_MCP_denied_call_on_fresh_manager_never_launches(self):
        from david.mcp import MCPError

        def deny(action, detail):
            self.approvals.append((action, detail))
            return False

        manager = self.manager(approve=deny)
        with self.assertRaisesRegex(MCPError, 'call approval denied'):
            manager.call_tool('fixture', 'echo', {})
        self.assertEqual([action for action, _ in self.approvals], ['mcp_call'])
        self.assertFalse(manager._connections)
        self.assertFalse(self.audit.exists())
        self.assertFalse(self.audit.with_suffix('.pid').exists())

    def test_MCP_read_only_prohibits_launch_and_existing_connection_calls(self):
        from david.mcp import MCPError
        manager = self.manager(read_only=True)
        for operation in (lambda: manager.list_tools('fixture'),
                          lambda: manager.call_tool('fixture', 'echo', {})):
            with self.assertRaises(MCPError):
                operation()
        self.assertEqual(self.approvals, [])
        self.assertFalse(self.audit.with_suffix('.pid').exists())
        manager.read_only = False
        manager.list_tools('fixture')
        before = self.messages()
        manager.read_only = True
        with self.assertRaises(MCPError):
            manager.call_tool('fixture', 'echo', {})
        self.assertEqual(self.messages(), before)

    def test_MCP_timeout_kills_invalidates_and_never_retries_call(self):
        from david.mcp import MCPError
        import time
        manager = self.manager('slow_call')
        before = time.monotonic()
        with self.assertRaisesRegex(MCPError, 'timeout'):
            manager.call_tool('fixture', 'echo', {})
        self.assertLess(time.monotonic() - before, 2)
        pid = int(self.audit.with_suffix('.pid').read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertFalse(manager._connections)
        self.assertEqual(sum(m['method'] == 'tools/call' for m in self.messages()), 1)

    def test_MCP_cancellation_propagates_and_cleans_up(self):
        from david.cancellation import Cancelled
        import time
        manager = self.manager('slow_call')
        manager.list_tools('fixture')
        pid = int(self.audit.with_suffix('.pid').read_text())
        before = time.monotonic()

        def cancel():
            if time.monotonic() - before > 0.15:
                raise Cancelled('cancel')

        manager.cancel_check = cancel
        with self.assertRaises(Cancelled):
            manager.call_tool('fixture', 'echo', {})
        self.assertLess(time.monotonic() - before, 1)
        self.assertFalse(manager._connections)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        previous = self.messages()
        with self.assertRaises(Cancelled):
            manager.list_tools('fixture')
        self.assertEqual(self.messages(), previous)

    def test_MCP_timeout_bounds_blocked_stdin_write(self):
        from david.mcp import MCPError
        import time
        manager = self.manager('blocked_write', config={'max_response_bytes': 2097152})
        before = time.monotonic()
        with self.assertRaises(MCPError):
            manager.call_tool('fixture', 'echo', {'large': 'x' * 200000})
        self.assertLess(time.monotonic() - before, 2)
        self.assertFalse(manager._connections)

    def test_MCP_malformed_protocol_fails_closed(self):
        from david.mcp import MCPError
        for mode in ('bad_json', 'duplicate_key', 'wrong_id', 'bool_id', 'missing_id',
                     'both_result_error', 'non_object', 'nan', 'bad_version', 'null_result'):
            with self.subTest(mode=mode):
                manager = self.manager(mode)
                with self.assertRaises(MCPError):
                    manager.list_tools('fixture')
                self.assertFalse(manager._connections)
                pid = int(self.audit.with_suffix('.pid').read_text())
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)

    def test_MCP_response_bytes_bound_complete_and_unterminated_lines(self):
        from david.mcp import MCPError
        import time
        for mode in ('oversized_line', 'unterminated_line'):
            with self.subTest(mode=mode):
                manager = self.manager(mode, config={'max_response_bytes': 1024})
                before = time.monotonic()
                with self.assertRaisesRegex(MCPError, 'limit'):
                    manager.list_tools('fixture')
                self.assertLess(time.monotonic() - before, 0.9)
                self.assertFalse(manager._connections)

    def test_MCP_stderr_is_discarded_without_blocking_or_exposure(self):
        manager = self.manager('stderr_flood')
        result = manager.list_tools('fixture')
        self.assertEqual(result['tools'][0]['name'], 'echo')
        self.assertNotIn('secret stderr', repr(result) + repr(self.approvals))

    def test_MCP_server_requests_reject_unsupported_and_respond_to_ping(self):
        manager = self.manager('server_requests')
        result = manager.list_tools('fixture')
        self.assertEqual(result['tools'][0]['name'], 'echo')
        replies = [m for m in self.messages() if 'method' not in m]
        self.assertEqual(replies, [{'jsonrpc': '2.0', 'id': 'p1', 'result': {}},
                                  {'jsonrpc': '2.0', 'id': 's1', 'error': {
                                      'code': -32601, 'message': 'Method not supported'}}])

    def test_MCP_notification_count_and_aggregate_bytes_are_bounded(self):
        from david.mcp import MCPError
        for mode, limit in [('notification_count', 65536), ('notification_bytes', 1024)]:
            with self.subTest(mode=mode):
                manager = self.manager(mode, config={'max_response_bytes': limit})
                with self.assertRaisesRegex(MCPError, 'limit'):
                    manager.list_tools('fixture')
                self.assertFalse(manager._connections)

    def test_MCP_idle_stdout_queue_is_bounded_and_stops_flooder(self):
        import time
        manager = self.manager('idle_flood')
        manager.list_tools('fixture')
        connection = manager._connections['fixture']
        time.sleep(0.3)
        self.assertLessEqual(connection.incoming.qsize(), 64)
        self.assertIsNotNone(connection.process.poll())
        from david.mcp import MCPError
        with self.assertRaises(MCPError):
            manager.list_tools('fixture')
        self.assertFalse(manager._connections)

    def test_MCP_environment_only_explicit_mappings_and_workspace_cwd(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {'MCP_TEST_SECRET': 'sensitive-test-value',
                                     'MCP_AMBIENT_SECRET': 'never-in-child'}):
            manager = self.manager('environment', config={'env': {'CHILD_TOKEN': 'MCP_TEST_SECRET'}})
            result = manager.call_tool('fixture', 'echo', {})
        data = json.loads(result['content'][0]['text'])
        self.assertEqual(Path(data['cwd']).resolve(), self.root.resolve())
        self.assertEqual(data['env']['CHILD_TOKEN'], 'sensitive-test-value')
        self.assertNotIn('MCP_AMBIENT_SECRET', data['env'])
        self.assertNotIn('MCP_TEST_SECRET', data['env'])
        self.assertNotIn('sensitive-test-value', repr(self.approvals))

    def test_MCP_defensive_configuration_rejects_unsafe_launches(self):
        from david.mcp import MCPError, MCPManager
        bad_configs = [
            {'command': 'python'}, {'command': []}, {'command': ['python']},
            {'command': ['/no/such/executable']}, {'command': ['{python}', 'bad\x00arg']},
            {'command': [str(self.root / 'unsafe.CmD')]},
            {'timeout_seconds': 0}, {'timeout_seconds': 121}, {'timeout_seconds': True},
            {'timeout_seconds': float('nan')}, {'max_response_bytes': 1023},
            {'max_response_bytes': 2097153}, {'max_response_bytes': True},
            {'env': {'INVALID=NAME': 'SOURCE'}}, {'env': {'CHILD': 5}},
            {'env': ['SOURCE']}, {'url': 'https://example.invalid'},
        ]
        for config in bad_configs:
            with self.subTest(config=config), self.assertRaises(MCPError):
                self.manager(config=config)
        for servers in ([], {'fixture': None}, {'bad\x00name': {}}):
            with self.subTest(servers=servers), self.assertRaises(MCPError):
                MCPManager(servers, self.root, self.approve, lambda: None)
        manager = MCPManager({}, self.root, self.approve, lambda: None)
        self.addCleanup(manager.close)
        with self.assertRaises(MCPError):
            manager.list_tools('unknown')
        self.assertFalse(self.audit.with_suffix('.pid').exists())

    def test_MCP_resolved_batch_executable_is_rejected(self):
        from david.mcp import MCPError
        batch = self.root / 'unsafe.cmd'
        batch.write_text('#!/bin/sh\nexit 0\n')
        batch.chmod(0o700)
        link = self.root / 'safe-name'
        link.symlink_to(batch)
        with self.assertRaises(MCPError):
            self.manager(config={'command': [str(link)]})

    def test_MCP_missing_environment_source_fails_without_exposing_values(self):
        from david.mcp import MCPError
        manager = self.manager(config={'env': {'CHILD': 'MCP_ABSENT_SOURCE_XYZZY'}})
        with self.assertRaises(MCPError):
            manager.list_tools('fixture')
        self.assertFalse(self.audit.with_suffix('.pid').exists())

    def test_MCP_initialization_negotiates_only_supported_tools_versions(self):
        from david.mcp import MCPError
        for mode in ('version:2099-01-01', 'no_tools'):
            with self.subTest(mode=mode):
                manager = self.manager(mode)
                before = len(self.messages()) if self.audit.exists() else 0
                with self.assertRaises(MCPError):
                    manager.list_tools('fixture')
                self.assertFalse(manager._connections)
                self.assertEqual([m['method'] for m in self.messages()[before:]], ['initialize'])
        for version in ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05'):
            with self.subTest(version=version):
                manager = self.manager('version:' + version)
                self.assertEqual(manager.list_tools('fixture')['tools'][0]['name'], 'echo')
                manager.close()

    def test_MCP_initialize_requires_minimal_server_info_shape(self):
        from david.mcp import MCPError
        for kind in ('missing', 'null', 'array', 'string', 'missing_name',
                     'missing_version', 'bad_name', 'bad_version'):
            for operation in ('list', 'call'):
                with self.subTest(kind=kind, operation=operation):
                    manager = self.manager('server_info:' + kind)
                    before = len(self.messages()) if self.audit.exists() else 0
                    with self.assertRaisesRegex(MCPError, 'serverInfo'):
                        if operation == 'list':
                            manager.list_tools('fixture')
                        else:
                            manager.call_tool('fixture', 'echo', {})
                    self.assertFalse(manager._connections)
                    self.assertEqual([m['method'] for m in self.messages()[before:]],
                                     ['initialize'])
                    pid = int(self.audit.with_suffix('.pid').read_text())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)

    def test_MCP_initialize_allows_extra_server_info_metadata(self):
        manager = self.manager('server_info:extended')
        self.assertEqual(manager.list_tools('fixture')['tools'][0]['name'], 'echo')

    def test_MCP_list_follows_pagination_and_preserves_metadata(self):
        manager = self.manager('pages:normal')
        result = manager.list_tools('fixture')
        self.assertEqual([t['name'] for t in result['tools']], ['tool0', 'tool1'])
        self.assertNotIn('nextCursor', result)
        lists = [m for m in self.messages() if m['method'] == 'tools/list']
        self.assertEqual([m['params'] for m in lists], [{}, {'cursor': '1'}])

    def test_MCP_pagination_rejects_loops_excess_bytes_and_malformed_tools(self):
        from david.mcp import MCPError
        for mode in ('loop', 'endless', 'bytes', 'duplicate', 'bad_schema'):
            with self.subTest(mode=mode):
                manager = self.manager('pages:' + mode,
                                       config={'max_response_bytes': 1024 if mode == 'bytes' else 65536})
                with self.assertRaises(MCPError):
                    manager.list_tools('fixture')
                self.assertFalse(manager._connections)

    def test_MCP_call_rejects_invalid_or_excessive_arguments_before_approval(self):
        from david.mcp import MCPError
        manager = self.manager()
        nested = {}
        for _ in range(40):
            nested = {'nested': nested}
        for arguments in ([], {1: 'nonstring key'}, {'nan': float('nan')},
                          {'large': 'x' * 300000}, {'object': object()}, nested):
            with self.subTest(kind=type(arguments).__name__), self.assertRaises(MCPError):
                manager.call_tool('fixture', 'echo', arguments)
        for name in ('', 'x' * 129, None):
            with self.assertRaises(MCPError):
                manager.call_tool('fixture', name, {})
        with self.assertRaises(MCPError):
            manager.call_tool('unknown', 'echo', {})
        self.assertEqual(self.approvals, [])
        self.assertFalse(self.audit.with_suffix('.pid').exists())

    def test_MCP_call_depth_boundary_is_checked_before_any_side_effect(self):
        from david.mcp import MCPError
        manager = self.manager()
        arguments = {'v': 0}
        for _ in range(30):
            arguments = {'a': arguments}
        with self.assertRaisesRegex(MCPError, 'complexity limit'):
            manager.call_tool('fixture', 'echo', arguments)
        self.assertEqual(self.approvals, [])
        self.assertFalse(self.audit.exists())
        self.assertFalse(self.audit.with_suffix('.pid').exists())
        self.assertFalse(manager._connections)
        # Removing one level puts the complete wire envelope at depth 32.
        accepted = arguments['a']
        result = manager.call_tool('fixture', 'echo', accepted)
        self.assertEqual(json.loads(result['content'][0]['text']), accepted)
        call = self.messages()[-1]
        self.assertEqual(call['method'], 'tools/call')
        self.assertEqual(call['params']['arguments'], accepted)

    def test_MCP_call_node_boundary_includes_wire_keys_and_id(self):
        from david.mcp import MCPError
        manager = self.manager()
        # The envelope (including the empty values list) uses 15 JSON nodes.
        arguments = {'values': [None] * 9986}
        with self.assertRaisesRegex(MCPError, 'complexity limit'):
            manager.call_tool('fixture', 'echo', arguments)
        self.assertEqual(self.approvals, [])
        self.assertFalse(self.audit.exists())
        self.assertFalse(self.audit.with_suffix('.pid').exists())
        self.assertFalse(manager._connections)
        arguments['values'].pop()
        result = manager.call_tool('fixture', 'echo', arguments)
        self.assertEqual(json.loads(result['content'][0]['text']), arguments)

    def test_MCP_call_wire_byte_preflight_includes_prospective_id_length(self):
        from david.mcp import MCPError
        for existing in (False, True):
            with self.subTest(existing=existing):
                manager = self.manager(config={'max_response_bytes': 1024})
                if existing:
                    manager.list_tools('fixture')
                connection = manager._connections.get('fixture')
                before = self.messages() if self.audit.exists() else []
                approvals = list(self.approvals)
                # Exercise decimal growth, including initialize's ID on launch.
                manager._next_id = 10 ** 4200 - (1 if existing else 2)
                wire = {'jsonrpc': '2.0', 'id': 10 ** 4200,
                        'method': 'tools/call', 'params': {
                            'name': 'echo', 'arguments': {'padding': ''}}}
                size = len(json.dumps(wire, separators=(',', ':')))
                wire['params']['arguments']['padding'] = 'x' * (1024 + 4096 + 1 - size)
                with self.assertRaisesRegex(MCPError, 'byte limit'):
                    manager.call_tool('fixture', 'echo', wire['params']['arguments'])
                self.assertEqual(self.approvals, approvals)
                self.assertEqual(self.messages() if self.audit.exists() else [], before)
                if existing:
                    self.assertIs(manager._connections['fixture'], connection)
                    self.assertIsNone(connection.process.poll())
                else:
                    self.assertFalse(self.audit.with_suffix('.pid').exists())
                    self.assertFalse(manager._connections)
                manager.close()

    def test_MCP_call_id_decimal_growth_matches_wire_after_initialize(self):
        manager = self.manager()
        manager._next_id = 98
        manager.call_tool('fixture', 'echo', {})
        self.assertEqual([m['id'] for m in self.messages() if 'id' in m], [99, 100])
        manager._next_id = 999
        manager.call_tool('fixture', 'echo', {})
        self.assertEqual(self.messages()[-1]['id'], 1000)

    def test_MCP_call_sends_exact_approved_argument_snapshot(self):
        arguments = {'original': 'approved', 'unicode': '한글'}

        def approve(action, detail):
            if action == 'mcp_call':
                self.assertEqual(json.loads(detail)['arguments'], arguments)
                arguments['original'] = 'not-approved'
            return True

        manager = self.manager(approve=approve)
        result = manager.call_tool('fixture', 'echo', arguments)
        self.assertEqual(json.loads(result['content'][0]['text'])['original'], 'approved')

    def test_MCP_duplicate_server_request_ids_fail_closed(self):
        from david.mcp import MCPError
        manager = self.manager('server_duplicate')
        with self.assertRaises(MCPError):
            manager.list_tools('fixture')
        self.assertFalse(manager._connections)

    def test_MCP_duplicate_response_invalidates_before_another_call(self):
        from david.mcp import MCPError
        import time
        manager = self.manager('duplicate_response')
        try:
            manager.list_tools('fixture')
        except MCPError:
            self.assertFalse(manager._connections)
            return  # Both frames arrived before the first operation completed.
        time.sleep(0.1)
        with self.assertRaises(MCPError):
            manager.call_tool('fixture', 'echo', {})
        self.assertFalse(manager._connections)
        self.assertFalse(any(m.get('method') == 'tools/call' for m in self.messages()))

    def test_MCP_call_preserves_bounded_content_without_fetching_resources(self):
        manager = self.manager('result:normal')
        result = manager.call_tool('fixture', 'echo', {})
        self.assertIs(result['isError'], True)
        self.assertEqual(result['structuredContent'], {'value': 'retained'})
        self.assertEqual([c['type'] for c in result['content']], ['text', 'image', 'resource_link'])
        self.assertEqual(result['content'][2]['uri'], 'file:///never-fetch')
        self.assertEqual([m['method'] for m in self.messages()],
                         ['initialize', 'notifications/initialized', 'tools/call'])

    def test_MCP_call_rejects_malformed_or_oversize_results(self):
        from david.mcp import MCPError
        for mode in ('bad_content', 'bad_error', 'bad_text', 'bad_image', 'large_text', 'large_image'):
            with self.subTest(mode=mode):
                manager = self.manager('result:' + mode, config={'max_response_bytes': 1024})
                with self.assertRaises(MCPError):
                    manager.call_tool('fixture', 'echo', {})
                self.assertFalse(manager._connections)

    def test_MCP_exit_during_initialization_or_call_cleans_up(self):
        from david.mcp import MCPError
        for mode in ('exit_initialize', 'exit_call'):
            with self.subTest(mode=mode):
                manager = self.manager(mode)
                with self.assertRaises(MCPError):
                    manager.call_tool('fixture', 'echo', {})
                self.assertFalse(manager._connections)
                pid = int(self.audit.with_suffix('.pid').read_text())
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)

    @unittest.skipIf(os.name == 'nt', 'POSIX process group regression')
    def test_MCP_close_terminates_descendant_pipe_holders(self):
        manager = self.manager('child_pipe')
        manager.list_tools('fixture')
        connection = manager._connections['fixture']
        manager.close()
        self.assertFalse(connection.reader.is_alive())
        self.assertIsNotNone(connection.process.poll())
        child_pid = int(self.audit.with_suffix('.childpid').read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)

    def test_MCP_read_only_rechecked_after_approval(self):
        from david.mcp import MCPError
        manager = self.manager()

        def approve(action, detail):
            manager.read_only = True
            return True

        manager.approve = approve
        with self.assertRaises(MCPError):
            manager.list_tools('fixture')
        self.assertFalse(self.audit.with_suffix('.pid').exists())

    def test_MCP_human_launch_approval_does_not_consume_server_timeout(self):
        import time

        def approve(action, detail):
            if action == 'mcp_start':
                time.sleep(1.05)
            return True

        manager = self.manager(approve=approve)
        self.assertEqual(manager.list_tools('fixture')['tools'][0]['name'], 'echo')

    @unittest.skipIf(os.name == 'nt', 'POSIX executable format failure')
    def test_MCP_spawn_failure_is_a_safe_tool_error(self):
        from david.mcp import MCPError
        executable = self.root / 'not-an-executable'
        executable.write_text('not an executable format')
        executable.chmod(0o700)
        manager = self.manager(config={'command': [str(executable)]})
        with self.assertRaises(MCPError):
            manager.list_tools('fixture')
        self.assertFalse(manager._connections)

    def test_MCP_server_request_ids_have_a_bounded_lifetime_footprint(self):
        from david.mcp import MCPError
        manager = self.manager('server_long_id')
        with self.assertRaises(MCPError):
            manager.list_tools('fixture')
        self.assertFalse(manager._connections)

    @unittest.skipIf(os.name == 'nt', 'POSIX symlink approval race fixture')
    def test_MCP_executable_is_revalidated_after_launch_approval(self):
        from david.mcp import MCPError
        import sys
        marker = self.root / 'must-not-execute'
        batch = self.root / 'unsafe.cmd'
        batch.write_text('#!' + sys.executable + '\nfrom pathlib import Path\nPath(' +
                         repr(str(marker)) + ').write_text("executed")\n')
        batch.chmod(0o700)
        link = self.root / 'launcher'
        link.symlink_to(sys.executable)

        def approve(action, detail):
            link.unlink()
            link.symlink_to(batch)
            return True

        manager = self.manager(config={'command': [str(link)]}, approve=approve)
        with self.assertRaises(MCPError):
            manager.list_tools('fixture')
        self.assertFalse(marker.exists())

    def test_MCP_cancelling_call_approval_closes_existing_connection(self):
        from david.cancellation import Cancelled
        manager = self.manager()
        manager.list_tools('fixture')
        connection = manager._connections['fixture']

        def cancel_approval(action, detail):
            raise Cancelled('approval cancelled')

        manager.approve = cancel_approval
        with self.assertRaises(Cancelled):
            manager.call_tool('fixture', 'echo', {})
        self.assertFalse(manager._connections)
        self.assertIsNotNone(connection.process.poll())
        self.assertFalse(any(m.get('method') == 'tools/call' for m in self.messages()))

    def test_MCP_lazy_handshake_list_preserves_schema(self):
        manager = self.manager()
        self.assertFalse(self.audit.exists())
        result = manager.list_tools('fixture')
        self.assertEqual(result['tools'][0]['name'], 'echo')
        self.assertEqual(result['tools'][0]['inputSchema'], {'type': 'object'})
        self.assertNotIn('instructions', result)
        self.assertEqual([m['method'] for m in self.messages()],
                         ['initialize', 'notifications/initialized', 'tools/list'])
        manager.list_tools('fixture')
        self.assertEqual(sum(m['method'] == 'initialize' for m in self.messages()), 1)
        manager.close()
        manager.close()
