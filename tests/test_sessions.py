import json
import os
from pathlib import Path
import tempfile
import unittest


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.directory = self.root / 'private' / 'sessions'
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.ident = 'a' * 32
        self.key = 'fingerprint-not-a-secret'
        self.payload = {'history': [[{'role': 'user', 'content': 'Hello'},
                                     {'role': 'assistant', 'content': '안녕'}]],
                        'requests': ['Hello'], 'journal': [],
                        'transcript': [{'kind': 'assistant', 'text': '안녕'}],
                        'title': 'Greeting'}

    def store(self):
        from david.sessions import SessionStore
        return SessionStore(self.directory)

    def assert_transcript_rejected(self, event):
        from david.sessions import SessionError
        store = self.store()
        store.save(self.ident, self.workspace, self.key, self.payload)
        path = self.directory / (self.ident + '.json')
        before = path.read_bytes()
        with self.assertRaisesRegex(SessionError, 'transcript'):
            store.save(self.ident, self.workspace, self.key,
                       dict(self.payload, transcript=[event]))
        self.assertEqual(path.read_bytes(), before, 'failed save must preserve the good file')
        envelope = json.loads(before)
        envelope['payload']['transcript'] = [event]
        path.write_text(json.dumps(envelope), encoding='utf-8')
        tampered = path.read_bytes()
        with self.assertRaisesRegex(SessionError, 'transcript'):
            store.load(self.ident, self.workspace, self.key)
        self.assertEqual(store.list(self.workspace, self.key), [])
        self.assertEqual(path.read_bytes(), tampered, 'discovery must not rewrite corrupt files')

    def test_tool_results_must_be_objects_before_save_load_or_listing(self):
        for result in ([], None, 'text', 42, False):
            with self.subTest(result=result):
                self.assert_transcript_rejected({'type': 'tool', 'name': 'x', 'result': result})

    def test_transcript_display_event_shapes_are_validated(self):
        malformed = [{}, {'type': []}, {'type': None}, {'type': 'unknown'},
                     {'type': 'status', 'status': 'done'},
                     {'type': 'text'}, {'type': 'text', 'text': []},
                     {'type': 'summary', 'text': None},
                     {'type': 'message', 'text': 'missing role'},
                     {'type': 'message', 'role': [], 'text': 'hello'},
                     {'type': 'message', 'role': 'assistant', 'text': {}},
                     {'type': 'tool', 'result': {}},
                     {'type': 'tool', 'name': [], 'result': {}},
                     {'type': 'tool', 'name': '', 'result': {}},
                     {'type': 'tool', 'name': 'x'},
                     {'kind': 'assistant', 'text': []}, {'kind': [], 'text': 'hello'}]
        for event in malformed:
            with self.subTest(event=event):
                self.assert_transcript_rejected(event)

    def test_tool_result_consumed_fields_have_expected_scalar_types(self):
        malformed = {'error': [[], {}, None, False],
                     'ok': [[], 'false', None, 0],
                     'timed_out': [{}, 'false', None, 0],
                     'exit_code': [[], '0', None, False, 0.0],
                     'name': [[], None, 1], 'output': [[], None, 1],
                     'truncated': [[], 'false', None, 0]}
        for field, values in malformed.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assert_transcript_rejected({
                        'type': 'tool', 'name': 'run_command', 'result': {field: value}})

    def test_mcp_extension_data_round_trips_without_command_field_rules(self):
        event = {'type': 'tool', 'name': 'mcp_call', 'result': {
            'content': [{'type': 'text', 'text': 'ok'}], 'isError': False,
            'structuredContent': {'rows': [1]}, 'output': {'rows': [1]},
            'name': {'display': 'remote'}, 'truncated': {'reason': None}}}
        payload = dict(self.payload, transcript=[event])
        store = self.store()
        store.save(self.ident, self.workspace, self.key, payload)
        self.assertEqual(store.load(self.ident, self.workspace, self.key), payload)
        self.assertEqual([item['id'] for item in store.list(self.workspace, self.key)], [self.ident])

    def test_current_and_legacy_display_events_round_trip_unchanged(self):
        events = [{'kind': 'assistant', 'text': 'historical answer'},
                  {'type': 'message', 'role': 'user', 'text': 'hello'},
                  {'type': 'message', 'role': 'assistant', 'text': 'answer'},
                  {'type': 'message', 'role': 'system', 'text': ''},
                  {'type': 'text', 'text': ''},
                  {'type': 'summary', 'text': 'complete'},
                  {'type': 'tool', 'name': 'read_file', 'result': {'content': '안녕',
                   'path': 'x', 'truncated': False, 'metadata': {'nested': [None, 1, True]}}},
                  {'type': 'tool', 'name': 'x', 'result': {}},
                  {'type': 'tool', 'name': 'x', 'result': {'error': 'denied'}},
                  {'type': 'tool', 'name': 'x', 'result': {'ok': False}},
                  {'type': 'tool', 'name': 'run_command', 'result': {
                   'name': 'test', 'exit_code': -9, 'timed_out': True,
                   'output': 'failed\n', 'truncated': True}},
                  {'type': 'tool', 'name': 'undo', 'result': {'undone': True, 'changes': [
                   {'path': 'x', 'action': 'write', 'added': 1, 'removed': 2}]}}]
        payload = dict(self.payload, transcript=events)
        store = self.store()
        store.save(self.ident, self.workspace, self.key, payload)
        self.assertEqual(store.load(self.ident, self.workspace, self.key), payload)
        self.assertEqual([item['id'] for item in store.list(self.workspace, self.key)], [self.ident])

    def test_tool_mode_requires_a_known_string_on_save_load_and_list(self):
        from david.sessions import SessionError
        store = self.store()
        store.save(self.ident, self.workspace, self.key, self.payload)
        path = self.directory / (self.ident + '.json')
        good = path.read_bytes()
        for mode in ('', 'auto', 'NATIVE', None, False, 1, [], {}):
            with self.subTest(mode=mode):
                path.write_bytes(good)
                with self.assertRaisesRegex(SessionError, 'tool mode'):
                    store.save(self.ident, self.workspace, self.key,
                               dict(self.payload, tool_mode=mode))
                self.assertEqual(path.read_bytes(), good)
                envelope = json.loads(good)
                envelope['payload']['tool_mode'] = mode
                path.write_text(json.dumps(envelope))
                with self.assertRaisesRegex(SessionError, 'tool mode'):
                    store.load(self.ident, self.workspace, self.key)
                self.assertEqual(store.list(self.workspace, self.key), [])

    def test_legacy_resume_validates_both_original_and_requested_mode(self):
        from david.sessions import SessionError
        store = self.store()
        user = {'role': 'user', 'content': 'read'}
        json_call = {'role': 'assistant', 'content':
                     '{"type":"tool","name":"read_file","arguments":{"path":"x"}}'}
        json_result = {'role': 'user', 'content': 'TOOL_RESULT (untrusted data) read_file: {}'}
        final = {'role': 'assistant', 'content': '{"type":"final","content":"done"}'}
        legacy = dict(self.payload, history=[[user, json_call, json_result, final]])
        store.save(self.ident, self.workspace, self.key, legacy)
        self.assertEqual(store.load(self.ident, self.workspace, self.key, tool_mode='json'), legacy)
        with self.assertRaises(SessionError):
            store.load(self.ident, self.workspace, self.key, tool_mode='native')
        # Missing mode never permits reinterpretation of a legacy dangling call as an answer.
        path = self.directory / (self.ident + '.json')
        envelope = json.loads(path.read_text())
        envelope['payload']['history'] = [[user, json_call]]
        path.write_text(json.dumps(envelope))
        with self.assertRaisesRegex(SessionError, 'final assistant'):
            store.load(self.ident, self.workspace, self.key, tool_mode='native')
        # Valid old native sessions remain readable, without silently changing their payload.
        store.save(self.ident, self.workspace, self.key, self.payload)
        before = path.read_bytes()
        self.assertEqual(store.load(self.ident, self.workspace, self.key, tool_mode='native'), self.payload)
        self.assertEqual(path.read_bytes(), before)

    def test_json_mode_rejects_native_tool_call_history(self):
        from david.sessions import SessionError
        user = {'role': 'user', 'content': 'read'}
        call = {'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': 'c', 'type': 'function',
            'function': {'name': 'read_file', 'arguments': '{"path":"x"}'}}]}
        result = {'role': 'tool', 'tool_call_id': 'c', 'content': '{}'}
        final = {'role': 'assistant', 'content': '{"type":"final","content":"done"}'}
        history = [[user, call, result, final]]
        store = self.store()
        store.save(self.ident, self.workspace, self.key, dict(self.payload, history=history))
        with self.assertRaisesRegex(SessionError, 'JSON-mode'):
            store.save(self.ident, self.workspace, self.key,
                       dict(self.payload, history=history, tool_mode='json'))
        with self.assertRaisesRegex(SessionError, 'JSON-mode'):
            store.load(self.ident, self.workspace, self.key, tool_mode='json')

    def test_explicit_modes_preserve_answers_and_reject_dangling_calls(self):
        from david.sessions import SessionError
        store = self.store()
        user = {'role': 'user', 'content': 'example'}
        examples = ['{"type":"tool","name":"example","arguments":{}}',
                    '{"type":"tool","name":[],"arguments":null}',
                    '{"type":"final","content":"done"}', '{"result":42}']
        for content in examples:
            with self.subTest(content=content):
                payload = dict(self.payload, tool_mode='native', history=[[
                    user, {'role': 'assistant', 'content': content}]])
                store.save(self.ident, self.workspace, self.key, payload)
                self.assertEqual(store.load(self.ident, self.workspace, self.key,
                                            tool_mode='native'), payload)
                self.assertEqual([item['id'] for item in store.list(self.workspace, self.key)], [self.ident])
        call = {'role': 'assistant', 'content': examples[0]}
        result = {'role': 'user', 'content': 'TOOL_RESULT (untrusted data) example: {}'}
        final = {'role': 'assistant', 'content': examples[2]}
        for turn in ([user, final], [user, call, result, final]):
            payload = dict(self.payload, tool_mode='json', history=[turn])
            store.save(self.ident, self.workspace, self.key, payload)
            self.assertEqual(store.load(self.ident, self.workspace, self.key, tool_mode='json'), payload)
        path = self.directory / (self.ident + '.json')
        good = path.read_bytes()
        for turn in ([user, call], [user, call, final], [user, call, result]):
            with self.subTest(turn=turn):
                path.write_bytes(good)
                with self.assertRaises(SessionError):
                    store.save(self.ident, self.workspace, self.key,
                               dict(self.payload, tool_mode='json', history=[turn]))
                self.assertEqual(path.read_bytes(), good)
                envelope = json.loads(good)
                envelope['payload']['history'] = [turn]
                path.write_text(json.dumps(envelope))
                with self.assertRaises(SessionError):
                    store.load(self.ident, self.workspace, self.key, tool_mode='json')
                self.assertEqual(store.list(self.workspace, self.key), [])
        path.write_bytes(good)
        for invalid_mode in ('auto', '', False, 1, [], {}):
            with self.subTest(mode=invalid_mode), self.assertRaisesRegex(SessionError, 'tool mode'):
                store.load(self.ident, self.workspace, self.key, tool_mode=invalid_mode)
        for workspace, key in ((self.root, self.key), (self.workspace, 'other')):
            with self.assertRaisesRegex(SessionError, 'workspace or model'):
                store.load(self.ident, workspace, key, tool_mode='json')

    def test_depth_boundary_never_saves_an_unloadable_session(self):
        from david.sessions import SessionError, MAX_DEPTH
        store = self.store()
        for depth in range(MAX_DEPTH - 4, MAX_DEPTH + 2):
            nested = []
            for _ in range(depth):
                nested = [nested]
            payload = dict(self.payload, journal=nested)
            try:
                store.save(self.ident, self.workspace, self.key, payload)
            except SessionError:
                continue
            with self.subTest(depth=depth):
                self.assertEqual(store.load(self.ident, self.workspace, self.key), payload)

    def test_duplicate_nested_keys_in_otherwise_valid_envelope_are_rejected(self):
        from david.sessions import SessionError
        store = self.store()
        store.save(self.ident, self.workspace, self.key, self.payload)
        path = self.directory / (self.ident + '.json')
        raw = path.read_text()
        raw = raw.replace('"title":"Greeting"', '"title":"first","title":"Greeting"')
        path.write_text(raw)
        with self.assertRaisesRegex(SessionError, 'Duplicate'):
            store.load(self.ident, self.workspace, self.key)
        self.assertEqual(store.list(self.workspace, self.key), [])

    def test_corrupt_envelopes_fail_explicitly_and_are_skipped_by_list(self):
        from david.sessions import SessionError
        store = self.store()
        store.save(self.ident, self.workspace, self.key, self.payload)
        path = self.directory / (self.ident + '.json')
        good = path.read_bytes()
        obj = json.loads(good)
        variants = [b'{', b'[]', b'null', b'\xff', b'{"version":1,"version":1}',
                    json.dumps(dict(obj, version=True)).encode(),
                    json.dumps(dict(obj, version=2)).encode(),
                    json.dumps(dict(obj, secret='no')).encode(),
                    json.dumps(dict(obj, updated_at='yesterday')).encode(),
                    json.dumps(dict(obj, updated_at='2026-01-01T00:00:00')).encode(),
                    json.dumps(dict(obj, workspace='relative')).encode(),
                    json.dumps(dict(obj, payload=dict(self.payload, history=[[]]))).encode(),
                    json.dumps(dict(obj, payload=dict(self.payload, title='\ud800'))).encode(),
                    json.dumps(dict(obj, payload=dict(self.payload, journal=[float('nan')]))).encode(),
                    b'[' * 2000 + b']' * 2000]
        for raw in variants:
            path.write_bytes(raw)
            with self.subTest(raw=raw[:60]), self.assertRaises(SessionError):
                store.load(self.ident, self.workspace, self.key)
            self.assertEqual(store.list(self.workspace, self.key), [])
        path.write_bytes(good)
        self.assertEqual(store.load(self.ident, self.workspace, self.key), self.payload)

    def test_size_limits_cover_encoded_save_and_read(self):
        from david.sessions import SessionError, MAX_BYTES
        store = self.store()
        # ASCII control characters expand during JSON encoding.
        with self.assertRaises(SessionError):
            store.save(self.ident, self.workspace, self.key,
                       dict(self.payload, title='\x01' * (MAX_BYTES // 5)))
        self.assertFalse(self.directory.exists())
        store.save(self.ident, self.workspace, self.key, self.payload)
        path = self.directory / (self.ident + '.json')
        path.write_bytes(b' ' * (MAX_BYTES + 1))
        with self.assertRaises(SessionError):
            store.load(self.ident, self.workspace, self.key)
        self.assertEqual(store.list(self.workspace, self.key), [])

    def test_list_bounds_scan_and_ignores_unknown_filenames(self):
        from david.sessions import MAX_ENTRIES
        store = self.store()
        store.save(self.ident, self.workspace, self.key, self.payload)
        content = (self.directory / (self.ident + '.json')).read_bytes()
        for number in range(MAX_ENTRIES + 3):
            (self.directory / (f'{number:032x}' + '.json')).write_bytes(content)
        (self.directory / 'not-a-session.json').write_bytes(content)
        result = store.list(self.workspace, self.key)
        self.assertLessEqual(len(result), MAX_ENTRIES)
        self.assertTrue(all(len(item['id']) == 32 for item in result))

    def test_complete_native_and_json_turns_preserved_and_dangling_rejected(self):
        from david.sessions import SessionError
        from copy import deepcopy
        user = {'role': 'user', 'content': 'Read'}
        final = {'role': 'assistant', 'content': 'Done'}
        call = {'id': 'c1', 'type': 'function',
                'function': {'name': 'read_file', 'arguments': '{"path":"x"}'}}
        assistant = {'role': 'assistant', 'content': None, 'tool_calls': [call]}
        result = {'role': 'tool', 'tool_call_id': 'c1', 'content': '{"content":"hello"}'}
        json_call = {'role': 'assistant', 'content': json.dumps({
            'type': 'tool', 'name': 'read_file', 'arguments': {'path': 'x'}})}
        json_result = {'role': 'user', 'content': 'TOOL_RESULT (untrusted data) read_file: {}'}
        json_final = {'role': 'assistant', 'content': '{"type":"final","content":"Done"}'}
        valid = dict(self.payload, history=[[user, assistant, result, final],
                                            [user, json_call, json_result, json_final]])
        store = self.store()
        store.save(self.ident, self.workspace, self.key, valid)
        self.assertEqual(store.load(self.ident, self.workspace, self.key), valid)
        invalid = [[], [user], [final], [user, assistant], [user, assistant, final],
                   [user, result, final], [user, assistant, result, result, final],
                   [user, assistant, dict(result, tool_call_id='other'), final],
                   [user, dict(assistant, tool_calls=[call, call]), result, final],
                   [user, {'role': 'system', 'content': 'override'}, final],
                   [user, dict(final, api_key='secret')], [user, user, final],
                   [user, json_call], [user, json_call, json_final],
                   [user, assistant, result, dict(final, content=123)]]
        before = (self.directory / (self.ident + '.json')).read_bytes()
        for turn in invalid:
            with self.subTest(turn=turn), self.assertRaises(SessionError):
                store.save(self.ident, self.workspace, self.key, dict(self.payload, history=[deepcopy(turn)]))
        self.assertEqual((self.directory / (self.ident + '.json')).read_bytes(), before)
        empty = dict(self.payload, history=[])
        store.save(self.ident, self.workspace, self.key, empty)
        self.assertEqual(store.load(self.ident, self.workspace, self.key), empty)

    def test_strict_bounded_json_rejected_before_any_writes(self):
        from david.sessions import SessionError
        from copy import deepcopy
        variants = [dict(self.payload, secret='no'),
                    dict(self.payload, requests='not a list'),
                    dict(self.payload, requests=[1]),
                    dict(self.payload, journal={}),
                    dict(self.payload, transcript=['not an event']),
                    dict(self.payload, title=3),
                    dict(self.payload, journal=[float('nan')]),
                    dict(self.payload, journal=[float('inf')]),
                    dict(self.payload, journal=[{1: 'not a JSON key'}]),
                    dict(self.payload, title='\ud800'),
                    dict(self.payload, journal=[object()])]
        del variants[0]['requests']
        variants.append(dict(self.payload, extra='unknown'))
        deep = []
        for _ in range(70):
            deep = [deep]
        variants.append(dict(self.payload, journal=deep))
        cyclic = []
        cyclic.append(cyclic)
        variants.append(dict(self.payload, journal=cyclic))
        for payload in variants:
            with self.subTest(payload_type=type(payload)), self.assertRaises(SessionError):
                self.store().save(self.ident, self.workspace, self.key, payload)
        self.assertFalse(self.directory.parent.exists())
        # Snapshot internals remain the workspace module's responsibility.
        valid = deepcopy(self.payload)
        valid['journal'] = [{'arbitrary': [None, False, 2, 3.25, 'snapshot']}]
        self.store().save(self.ident, self.workspace, self.key, valid)
        self.assertEqual(self.store().load(self.ident, self.workspace, self.key), valid)

    def test_private_atomic_save_preserves_previous_file_on_failure(self):
        from david.sessions import SessionError
        from unittest.mock import patch
        store = self.store()
        store.save(self.ident, self.workspace, self.key, self.payload)
        path = self.directory / (self.ident + '.json')
        if os.name != 'nt':  # Windows privacy uses inherited ACLs, not these bits.
            self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        before = path.read_bytes()
        with patch('david.sessions.os.replace', side_effect=OSError('disk failure')):
            with self.assertRaises(SessionError):
                store.save(self.ident, self.workspace, self.key, dict(self.payload, title='new'))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual([p.name for p in self.directory.iterdir()], [path.name])

    def test_links_at_file_directory_and_ancestor_are_rejected(self):
        from david.sessions import SessionError, SessionStore
        store = self.store()
        store.save(self.ident, self.workspace, self.key, self.payload)
        path = self.directory / (self.ident + '.json')
        original = path.read_bytes()
        target = self.root / 'target.json'
        path.rename(target)
        for link in (lambda: path.symlink_to(target), lambda: os.link(target, path)):
            try:
                link()
            except OSError as exc:
                if os.name == 'nt' and getattr(exc, 'winerror', None) == 1314:
                    self.skipTest('Windows symlink privilege unavailable; not exercised')
                raise
            with self.assertRaises(SessionError):
                store.load(self.ident, self.workspace, self.key)
            with self.assertRaises(SessionError):
                store.save(self.ident, self.workspace, self.key, self.payload)
            self.assertEqual(store.list(self.workspace, self.key), [])
            self.assertEqual(target.read_bytes(), original)
            path.unlink()
        alias = self.root / 'alias'
        alias.symlink_to(self.directory.parent, target_is_directory=True)
        for directory in (alias / 'sessions', alias / 'missing', alias / '..' / 'new'):
            unsafe = SessionStore(directory)
            for action in (lambda: unsafe.list(self.workspace, self.key),
                           lambda: unsafe.load(self.ident, self.workspace, self.key),
                           lambda: unsafe.save(self.ident, self.workspace, self.key, self.payload)):
                with self.subTest(directory=directory), self.assertRaises(SessionError):
                    action()
        self.assertFalse((self.directory.parent / 'missing').exists())

    def test_identity_isolation_and_id_traversal_rejected(self):
        from david.sessions import SessionError
        store = self.store()
        for ident in ('../escape', 'A' * 32, 'a' * 31, '', 42):
            with self.subTest(ident=ident), self.assertRaises(SessionError):
                store.save(ident, self.workspace, self.key, self.payload)
        self.assertFalse(self.directory.exists())
        store.save(self.ident, self.workspace, self.key, self.payload)
        for workspace, key in ((self.root, self.key), (self.workspace, 'other')):
            with self.subTest(workspace=workspace, key=key):
                with self.assertRaises(SessionError):
                    store.load(self.ident, workspace, key)
                self.assertEqual(store.list(workspace, key), [])
        with self.assertRaises(SessionError):
            store.load('../escape', self.workspace, self.key)

    def test_round_trip_explicit_save_and_read_only_discovery(self):
        from david.sessions import SessionError
        store = self.store()
        self.assertFalse(self.directory.parent.exists())
        self.assertEqual(store.list(self.workspace, self.key), [])
        with self.assertRaises(SessionError):
            store.load(self.ident, self.workspace, self.key)
        self.assertFalse(self.directory.parent.exists())
        store.save(self.ident, self.workspace, self.key, self.payload)
        self.assertEqual(store.load(self.ident, self.workspace, self.key), self.payload)
        entries = store.list(self.workspace, self.key)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['id'], self.ident)
        self.assertEqual(entries[0]['title'], 'Greeting')
        self.assertEqual(set(entries[0]), {'id', 'title', 'updated_at'})
        envelope = json.loads((self.directory / (self.ident + '.json')).read_text())
        self.assertEqual(envelope['version'], 1)
        self.assertEqual(envelope['workspace'], str(self.workspace))
        self.assertTrue(envelope['updated_at'].endswith('+00:00'))


class PortableSessionStoreTests(SessionStoreTests):
    """Exercise real path-based I/O even on hosts with POSIX dir_fd support."""

    def setUp(self):
        super().setUp()
        from unittest.mock import patch
        self.enterContext(patch('david.sessions.os.supports_dir_fd', set()))

    def test_windows_capabilities_round_trip_without_posix_operations(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from david.sessions import _descriptor_backend
        # Emulate API availability, not a native Windows certification run.
        windows_os = SimpleNamespace(**vars(os))
        windows_os.name = 'nt'
        windows_os.supports_dir_fd = set()
        for name in ('O_NOFOLLOW', 'O_DIRECTORY', 'O_NONBLOCK', 'fchmod', 'getuid'):
            if hasattr(windows_os, name):
                delattr(windows_os, name)
        real_open = os.open

        def path_open(path, flags, mode=0o777, **kwargs):
            self.assertFalse(kwargs, 'portable backend must not use dir_fd')
            return real_open(path, flags, mode)

        windows_os.open = path_open
        with patch('david.sessions.os', windows_os):
            self.assertFalse(_descriptor_backend())
            self.test_round_trip_explicit_save_and_read_only_discovery()

    def test_reparse_attributes_reject_files_and_each_ancestor(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from david.sessions import SessionError
        store = self.store()
        store.save(self.ident, self.workspace, self.key, self.payload)
        path = self.directory / (self.ident + '.json')
        original = path.read_bytes()
        lstat = Path.lstat
        for unsafe in (path, self.directory, self.directory.parent):
            def marked(candidate, *args, **kwargs):
                info = lstat(candidate, *args, **kwargs)
                if candidate == unsafe:
                    return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
                return info
            with self.subTest(unsafe=unsafe), patch.object(Path, 'lstat', marked):
                with self.assertRaises(SessionError):
                    store.load(self.ident, self.workspace, self.key)
                with self.assertRaises(SessionError):
                    store.save(self.ident, self.workspace, self.key, self.payload)
                if unsafe == path:
                    self.assertEqual(store.list(self.workspace, self.key), [])
                else:
                    with self.assertRaises(SessionError):
                        store.list(self.workspace, self.key)
            self.assertEqual(path.read_bytes(), original)

    def test_target_replacement_before_commit_is_not_overwritten(self):
        from unittest.mock import patch
        from david.sessions import SessionError
        store = self.store()
        store.save(self.ident, self.workspace, self.key, self.payload)
        path = self.directory / (self.ident + '.json')
        replacement = self.root / 'replacement'
        replacement.write_bytes(b'concurrent replacement')
        sync = os.fsync

        def race(fd):
            sync(fd)
            os.replace(replacement, path)

        with patch('david.sessions.os.fsync', side_effect=race):
            with self.assertRaisesRegex(SessionError, 'changed'):
                store.save(self.ident, self.workspace, self.key, self.payload)
        self.assertEqual(path.read_bytes(), b'concurrent replacement')
        self.assertEqual(list(self.directory.iterdir()), [path])

    @unittest.skipIf(os.name == 'nt', 'open-directory rename injection exercised on POSIX only')
    def test_directory_replacement_before_commit_fails_closed(self):
        from unittest.mock import patch
        from david.sessions import SessionError
        store = self.store()
        store.save(self.ident, self.workspace, self.key, self.payload)
        old = self.directory.with_name('old')
        sync = os.fsync

        def race(fd):
            sync(fd)
            self.directory.rename(old)
            self.directory.mkdir()

        with patch('david.sessions.os.fsync', side_effect=race):
            with self.assertRaisesRegex(SessionError, 'changed'):
                store.save(self.ident, self.workspace, self.key, self.payload)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertTrue((old / (self.ident + '.json')).exists())
        # An unverifiable path is deliberately left alone, including our moved temp.

    def test_read_open_identity_mismatch_is_rejected(self):
        from unittest.mock import patch
        from david.sessions import SessionError
        store = self.store()
        store.save(self.ident, self.workspace, self.key, self.payload)
        path = self.directory / (self.ident + '.json')
        replacement = self.root / 'replacement'
        replacement.write_bytes(path.read_bytes())
        real_open = os.open

        def race(candidate, flags, *args, **kwargs):
            if Path(candidate) == path:
                os.replace(replacement, path)
            return real_open(candidate, flags, *args, **kwargs)

        with patch('david.sessions.os.open', side_effect=race):
            with self.assertRaisesRegex(SessionError, 'changed'):
                store.load(self.ident, self.workspace, self.key)

    def test_exclusive_temp_collision_is_never_deleted(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from david.sessions import SessionError
        store = self.store()
        store.save(self.ident, self.workspace, self.key, self.payload)
        collision = self.directory / ('.' + 'b' * 32 + '.tmp')
        collision.write_bytes(b'not ours')
        with patch('david.sessions.uuid.uuid4', return_value=SimpleNamespace(hex='b' * 32)):
            with self.assertRaises(SessionError):
                store.save(self.ident, self.workspace, self.key, self.payload)
        self.assertEqual(collision.read_bytes(), b'not ours')
        self.assertEqual(store.load(self.ident, self.workspace, self.key), self.payload)

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'FIFO creation unavailable')
    def test_special_file_is_rejected_without_opening(self):
        from david.sessions import SessionError
        store = self.store()
        self.directory.mkdir(parents=True)
        os.mkfifo(self.directory / (self.ident + '.json'))
        with self.assertRaises(SessionError):
            store.load(self.ident, self.workspace, self.key)
        with self.assertRaises(SessionError):
            store.save(self.ident, self.workspace, self.key, self.payload)
        self.assertEqual(store.list(self.workspace, self.key), [])
