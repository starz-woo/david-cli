"""Managed workspace operations, journals, discovery, and cancellation contracts."""
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest.mock import patch

from david.workspace import ToolError, WorkspaceTools


def digest(data):
    return hashlib.sha256(data).hexdigest()


class WorkspaceAdvancedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.approvals = []
        self.tools = WorkspaceTools(self.root, {}, {}, self.allow)

    def allow(self, action, detail):
        self.approvals.append((action, detail))
        return True

    def write(self, path, content, expected=None):
        args = {'path': path, 'content': content}
        if expected is not None:
            args['expected_sha256'] = expected
        return self.tools.execute('write_file', args)

    def test_workspace_turn_merges_writes_and_undo_preserves_bytes_and_mode(self):
        target = self.root / 'edit.txt'
        target.write_bytes(b'old\r\n')
        target.chmod(0o750)
        self.tools.begin_turn()
        first = self.write('edit.txt', 'first\r\n', digest(b'old\r\n'))
        self.write('edit.txt', 'last\r\n', first['sha256'])
        changes = self.tools.changes()
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]['action'], 'modify')
        self.assertEqual(changes[0]['before_sha256'], digest(b'old\r\n'))
        self.assertEqual(changes[0]['after_sha256'], digest(b'last\r\n'))
        self.assertNotIn('content', json.dumps(changes))
        record = self.tools.end_turn()
        self.assertEqual(record['changes'], changes)
        json.dumps(record)
        self.assertNotIn('old\\r', json.dumps(record))
        self.tools.begin_turn()
        self.write('new.txt', 'new')
        self.tools.end_turn()
        self.assertTrue(self.tools.undo_last()['undone'])
        self.assertFalse((self.root / 'new.txt').exists())
        self.assertTrue(self.tools.undo_last()['undone'])
        self.assertEqual(target.read_bytes(), b'old\r\n')
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o750)
        self.assertEqual(self.approvals[-1][0], 'undo_last')
        self.assertIn('+old', self.approvals[-1][1])
        self.assertFalse(self.tools.undo_last()['undone'])

    @unittest.skipIf(os.name == 'nt', 'POSIX permission bits')
    def test_workspace_permission_only_undo_preview_shows_modes_before_approval(self):
        target = self.root / 'permissions'
        target.write_text('unchanged')
        target.chmod(0o600)
        current = self.tools._state('permissions')
        self.tools.import_journal([{'version': 1, 'changes': [{
            'path': 'permissions', 'before': {**current, 'mode': 0o777}, 'after': current}]}])
        def approve(action, detail):
            self.assertEqual(action, 'undo_last')
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertIn('mode 0600 -> 0777', detail.splitlines()[0])
            return self.allow(action, detail)
        self.tools.approve = approve
        self.assertTrue(self.tools.undo_last()['undone'])
        self.assertEqual(target.read_text(), 'unchanged')
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o777)

    @unittest.skipIf(os.name == 'nt', 'POSIX permission bits')
    def test_workspace_permission_headers_cannot_be_truncated(self):
        target = self.root / 'permissions'
        target.write_text('unchanged')
        target.chmod(0o600)
        current = self.tools._state('permissions')
        self.tools.import_journal([{'version': 1, 'changes': [{
            'path': 'permissions', 'before': {**current, 'mode': 0o777}, 'after': current}]}])
        def deny(action, detail):
            self.allow(action, detail)
            return False
        self.tools.approve = deny
        with self.assertRaisesRegex(ToolError, 'User cancelled'):
            self.tools.undo_last()
        header = self.approvals[-1][1]
        self.assertIn('mode 0600 -> 0777', header)
        self.tools.security['max_output_chars'] = len(header) - 1
        with self.assertRaisesRegex(ToolError, 'approval preview limit'):
            self.tools.undo_last()
        self.assertEqual(len(self.approvals), 1)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.tools.security['max_output_chars'] = len(header)
        self.tools.approve = self.allow
        self.assertTrue(self.tools.undo_last()['undone'])
        self.assertEqual(self.approvals[-1][1], header)

    def test_workspace_windows_snapshot_uses_representable_file_permissions(self):
        # Windows chmod/stat retain only the read-only flag, not POSIX owner bits.
        with patch('david.workspace.os.name', 'nt'):
            self.assertEqual(WorkspaceTools._file_state(b'x', 0o600)['mode'], 0o666)
            self.assertEqual(WorkspaceTools._file_state(b'x', 0o400)['mode'], 0o444)

    def test_workspace_truncated_preview_still_identifies_every_target(self):
        tools = WorkspaceTools(self.root, {'max_output_chars': 180}, {}, self.allow)
        tools.execute('apply_patch', {'changes': [
            {'path': 'first', 'content': 'long line\n' * 100},
            {'path': 'last', 'content': 'tail'}]})
        self.assertIn('first', self.approvals[-1][1])
        self.assertIn('last', self.approvals[-1][1])
        for header in self.approvals[-1][1].splitlines()[:2]:
            self.assertIn('mode absent -> ', header)
            self.assertIn(format(WorkspaceTools._file_state(b'', 0o600)['mode'], '04o'), header)
        self.assertIn('[diff preview truncated]', self.approvals[-1][1])
        self.assertEqual(len(self.approvals), 1)

    def test_workspace_partial_rollback_failure_reports_and_journals_remaining_change(self):
        (self.root / 'a').write_text('old')
        real_replace = os.replace
        calls = []
        def fail_apply_and_rollback(source, destination):
            calls.append(str(destination))
            if len(calls) >= 2:
                raise OSError('injected failure')
            return real_replace(source, destination)
        changes = [{'path': 'a', 'content': 'new', 'expected_sha256': digest(b'old')},
                   {'path': 'b', 'content': 'created'}]
        with patch('david.workspace.os.replace', side_effect=fail_apply_and_rollback):
            with self.assertRaisesRegex(ToolError, 'Partial failure; rollback failed for: a'):
                self.tools.execute('apply_patch', {'changes': changes})
        self.assertEqual((self.root / 'a').read_text(), 'new')
        self.assertFalse((self.root / 'b').exists())
        self.assertEqual([c['path'] for c in self.tools.changes()], ['a'])
        self.tools.undo_last()
        self.assertEqual((self.root / 'a').read_text(), 'old')

    def test_workspace_undo_rechecks_all_states_after_approval_and_refuses_links(self):
        self.write('a', 'first')
        self.write('b', 'second')
        self.tools.end_turn()
        def race(*_):
            (self.root / 'a').write_text('user')
            return True
        self.tools.approve = race
        with self.assertRaises(ToolError):
            self.tools.undo_last()
        self.assertEqual((self.root / 'a').read_text(), 'user')
        self.assertEqual((self.root / 'b').read_text(), 'second')
        (self.root / 'a').unlink()
        (self.root / 'a').symlink_to(self.root / 'b')
        self.tools.approve = self.allow
        count = len(self.approvals)
        with self.assertRaises(ToolError):
            self.tools.undo_last()
        self.assertEqual(len(self.approvals), count)
        self.assertEqual((self.root / 'b').read_text(), 'second')

    def test_workspace_cancelled_transaction_rolls_back_and_end_turn_ignores_cancel(self):
        class Cancelled(BaseException):
            pass
        self.write('before', 'kept')
        def cancel_after_first_write():
            if (self.root / 'a').exists():
                raise Cancelled()
        self.tools.cancel_check = cancel_after_first_write
        with self.assertRaises(Cancelled):
            self.tools.execute('apply_patch', {'changes': [
                {'path': 'a', 'content': 'one'}, {'path': 'b', 'content': 'two'}]})
        self.assertFalse((self.root / 'a').exists())
        self.assertFalse((self.root / 'b').exists())
        def cancelled():
            raise Cancelled()
        self.tools.cancel_check = cancelled
        self.assertEqual([c['path'] for c in self.tools.end_turn()['changes']], ['before'])
        self.assertTrue(self.tools.export_journal())

    def test_workspace_unsafe_gitignore_fails_closed_and_never_follows_links(self):
        (self.root / 'rules').write_text('*.txt\n')
        (self.root / 'file.txt').write_text('needle')
        ignore = self.root / '.gitignore'
        ignore.symlink_to(self.root / 'rules')
        with self.assertRaises(ToolError):
            self.tools.execute('list_files', {})
        ignore.unlink()
        ignore.write_text('x' * 262145)
        with self.assertRaises(ToolError):
            self.tools.execute('search_files', {'pattern': 'needle'})
        self.assertEqual(self.tools.execute('read_file', {'path': 'file.txt'})['content'], 'needle')

    def test_workspace_new_mutations_obey_readonly_denial_and_approval_races(self):
        (self.root / 'source').write_text('old')
        operations = [
            ('make_directory', {'path': 'dir'}),
            ('move_file', {'source': 'source', 'destination': 'new', 'expected_sha256': digest(b'old')}),
            ('delete_file', {'path': 'source', 'expected_sha256': digest(b'old')}),
            ('apply_patch', {'changes': [{'path': 'new', 'content': 'created'}]}),
        ]
        for name, args in operations:
            for tools in [WorkspaceTools(self.root, {'read_only': True}, {}, self.allow),
                          WorkspaceTools(self.root, {}, {}, lambda *_: False)]:
                with self.subTest(name=name), self.assertRaises(ToolError):
                    tools.execute(name, args)
                self.assertEqual((self.root / 'source').read_text(), 'old')
                self.assertFalse((self.root / 'dir').exists())
                self.assertFalse((self.root / 'new').exists())
        def race(*_):
            (self.root / 'source').write_text('user')
            return True
        self.tools.approve = race
        with self.assertRaises(ToolError):
            self.tools.execute('apply_patch', {'changes': [
                {'path': 'new', 'content': 'created'},
                {'path': 'source', 'content': 'agent', 'expected_sha256': digest(b'old')}]})
        self.assertFalse((self.root / 'new').exists())
        self.assertEqual((self.root / 'source').read_text(), 'user')

    def test_workspace_line_truncation_and_explicit_journal_reset(self):
        (self.root / 'lines').write_text('one\ntwo\nthree\n')
        partial = self.tools.execute('read_file', {'path': 'lines', 'limit': 1})
        self.assertTrue(partial['truncated'])
        self.assertEqual(partial['total_lines'], 3)
        complete = self.tools.execute('read_file', {'path': 'lines', 'limit': 10})
        self.assertFalse(complete['truncated'])
        self.write('new', 'kept')
        self.tools.end_turn()
        self.tools.import_journal([])
        self.assertEqual(self.tools.export_journal(), [])
        self.assertFalse(self.tools.undo_last()['undone'])
        self.assertEqual((self.root / 'new').read_text(), 'kept')
        self.write('partial', 'kept too')
        self.tools.import_journal([])
        self.assertEqual(self.tools.changes(), [])

    def test_workspace_journal_rejects_unrepresentable_turn_before_mutation(self):
        # Case/Unicode aliases must not produce two conflicting snapshots of one path.
        self.write('Name', 'first')
        count = len(self.approvals)
        with self.assertRaises(ToolError):
            self.write('name', 'second', digest(b'first') if (self.root / 'name').exists() else None)
        self.assertEqual(len(self.approvals), count)
        self.assertEqual((self.root / 'Name').read_text(), 'first')
        self.tools.undo_last()
        # A turn may not grow beyond the bounded persisted representation.
        self.tools.begin_turn()
        for i in range(10):
            self.tools.execute('apply_patch', {'changes': [
                {'path': f'file-{i}-{j}', 'content': ''} for j in range(20)]})
        with self.assertRaises(ToolError):
            self.write('overflow', '')
        self.assertFalse((self.root / 'overflow').exists())
        self.assertEqual(len(self.tools.changes()), 200)
        self.tools.undo_last()

    def test_workspace_cancel_polls_traversal_and_kills_running_command_tree(self):
        class Cancelled(BaseException):
            pass
        polls = []
        def cancel_scan():
            polls.append(1)
            if len(polls) >= 4:
                raise Cancelled()
        for i in range(20):
            (self.root / f'f{i}').write_text('needle')
        scanning = WorkspaceTools(self.root, {}, {}, self.allow, cancel_check=cancel_scan)
        with self.assertRaises(Cancelled):
            scanning.execute('search_files', {'pattern': 'needle'})
        self.assertEqual(len(polls), 4)
        child = "import time;time.sleep(0.8);open('orphan-marker','w').close()"
        code = ('import subprocess,sys,time;'
                f'subprocess.Popen([sys.executable,"-c",{child!r}]);'
                "open('ready','w').close();time.sleep(10)")
        def cancel_command():
            if (self.root / 'ready').exists():
                raise Cancelled()
        running = WorkspaceTools(self.root, {'enable_commands': True},
                                 {'hang': {'argv': ['{python}', '-c', code], 'timeout_seconds': 5}},
                                 self.allow, cancel_check=cancel_command)
        start = time.monotonic()
        with self.assertRaises(Cancelled):
            running.execute('run_command', {'name': 'hang'})
        self.assertLess(time.monotonic() - start, 2)
        time.sleep(1)
        self.assertFalse((self.root / 'orphan-marker').exists())

    def test_workspace_command_result_explains_managed_undo_limit(self):
        tools = WorkspaceTools(self.root, {'enable_commands': True},
                               {'create': {'argv': ['{python}', '-c', "open('external','w').write('keep')"]}},
                               self.allow)
        tools.begin_turn()
        result = tools.execute('run_command', {'name': 'create'})
        self.assertFalse(result['undoable'])
        self.assertIn('not tracked', result['undo_warning'])
        self.assertEqual(tools.end_turn()['changes'], [])
        self.assertFalse(tools.undo_last()['undone'])
        self.assertEqual((self.root / 'external').read_text(), 'keep')

    def test_workspace_discovery_honors_nested_gitignore_and_parent_exclusion(self):
        (self.root / '.gitignore').write_text('*.log\n!keep.log\nblocked/\n!blocked/no.txt\n/root.txt\n')
        for name in ['keep.log', 'drop.log', 'root.txt', 'ok.py']:
            (self.root / name).write_text('needle')
        (self.root / 'sub').mkdir()
        (self.root / 'sub/.gitignore').write_text('!drop.log\n*.py\n!keep.py\n')
        for name in ['drop.log', 'root.txt', 'drop.py', 'keep.py']:
            (self.root / 'sub' / name).write_text('needle')
        (self.root / 'blocked').mkdir()
        (self.root / 'blocked/no.txt').write_text('needle')
        (self.root / 'blocked/.gitignore').write_text('!no.txt\n')
        expected = {'keep.log', 'ok.py', 'sub/drop.log', 'sub/root.txt', 'sub/keep.py'}
        found = self.tools.execute('search_files', {'pattern': 'needle'})
        self.assertEqual({m['path'] for m in found['matches']}, expected)
        listed = {e['path'] for e in self.tools.execute('list_files', {})['entries']}
        self.assertTrue(expected <= listed)
        self.assertNotIn('blocked', listed)
        self.assertNotIn('drop.log', listed)
        for path in ['blocked', 'blocked/no.txt', 'drop.log']:
            self.assertEqual(self.tools.execute('search_files', {'pattern': 'needle', 'path': path})['matches'], [])
        self.assertEqual(self.tools.execute('read_file', {'path': 'drop.log'})['content'], 'needle')
        only_python = self.tools.execute('search_files', {'pattern': 'needle', 'glob': '*.py'})
        self.assertEqual({m['path'] for m in only_python['matches']}, {'ok.py', 'sub/keep.py'})
        # Re-read ignores for every discovery, including a subtree as the starting path.
        (self.root / 'sub/.gitignore').write_text('!drop.log\n')
        found = self.tools.execute('search_files', {'pattern': 'needle', 'path': 'sub'})
        self.assertEqual({m['path'] for m in found['matches']},
                         {'sub/drop.log', 'sub/root.txt', 'sub/drop.py', 'sub/keep.py'})

    def test_workspace_persisted_journal_is_bounded_validated_and_conflict_safe(self):
        (self.root / 'a').write_text('original')
        self.write('a', 'agent', digest(b'original'))
        self.write('b', 'created')
        self.tools.end_turn()
        records = json.loads(json.dumps(self.tools.export_journal()))
        fresh = WorkspaceTools(self.root, {}, {}, self.allow)
        fresh.import_journal(records)
        records[0]['changes'][0]['before']['content'] = 'tampered caller object'
        (self.root / 'a').write_text('user edit')
        count = len(self.approvals)
        with self.assertRaises(ToolError):
            fresh.undo_last()
        self.assertEqual(len(self.approvals), count)
        self.assertEqual((self.root / 'b').read_text(), 'created')
        (self.root / 'a').write_text('agent')
        self.assertTrue(fresh.undo_last()['undone'])
        self.assertEqual((self.root / 'a').read_text(), 'original')
        self.assertFalse((self.root / 'b').exists())
        clean = self.tools.export_journal()
        for bad in [None, {}, clean * 101,
                    [{'version': 99, 'changes': []}],
                    [{'version': 1, 'changes': [], 'extra': True}]]:
            with self.subTest(bad=str(bad)[:60]), self.assertRaises(ToolError):
                WorkspaceTools(self.root, {}, {}, self.allow).import_journal(bad)
        for path in ['../escape', '.env', '/outside', 'a/../b', '.', 'a' + chr(0) + 'b']:
            forged = json.loads(json.dumps(clean))
            forged[0]['changes'][0]['path'] = path
            with self.subTest(path=path), self.assertRaises(ToolError):
                WorkspaceTools(self.root, {}, {}, self.allow).import_journal(forged)
        for field, value in [('content', chr(0)), ('content', 'x' * 262145),
                             ('sha256', '0' * 64), ('mode', 0o4777), ('mode', True),
                             ('extra', 'bad')]:
            forged = json.loads(json.dumps(clean))
            forged[0]['changes'][0]['before'][field] = value
            if field == 'content':
                # A valid digest isolates binary/size rejection from hash validation.
                forged[0]['changes'][0]['before']['sha256'] = digest(value.encode('utf-8'))
            with self.subTest(field=field), self.assertRaises(ToolError):
                WorkspaceTools(self.root, {}, {}, self.allow).import_journal(forged)
        protected = WorkspaceTools(self.root, {}, {}, self.allow, protected_paths=[self.root / 'a'])
        with self.assertRaises(ToolError):
            protected.import_journal(clean)
        # Revalidate security at undo, not only at import.
        late = WorkspaceTools(self.root, {}, {}, self.allow)
        late.import_journal(clean)
        late.protected_paths.append(self.root / 'a')
        with self.assertRaises(ToolError):
            late.undo_last()

    def test_workspace_move_delete_are_approved_digest_guarded_and_undoable(self):
        source = self.root / 'source'
        source.write_bytes(b'original\r\n')
        source.chmod(0o750)
        sha = digest(source.read_bytes())
        self.tools.begin_turn()
        result = self.tools.execute('move_file', {'source': 'source', 'destination': 'destination',
                                                 'expected_sha256': sha})
        self.assertEqual(result['sha256'], sha)
        self.assertFalse(source.exists())
        self.assertEqual((self.root / 'destination').read_bytes(), b'original\r\n')
        self.assertEqual(len(self.approvals), 1)
        self.tools.end_turn()
        self.tools.execute('delete_file', {'path': 'destination', 'expected_sha256': sha})
        self.assertFalse((self.root / 'destination').exists())
        self.tools.undo_last()
        self.assertEqual((self.root / 'destination').read_bytes(), b'original\r\n')
        self.tools.undo_last()
        self.assertEqual(source.read_bytes(), b'original\r\n')
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o750)
        (self.root / 'exists').write_text('keep')
        (self.root / 'dir').mkdir()
        for name, args in [
            ('move_file', {'source': 'source', 'destination': 'exists', 'expected_sha256': sha}),
            ('move_file', {'source': 'source', 'destination': '.env', 'expected_sha256': sha}),
            ('move_file', {'source': 'source', 'destination': 'other', 'expected_sha256': '0' * 64}),
            ('delete_file', {'path': 'dir', 'expected_sha256': sha}),
            ('delete_file', {'path': 'source', 'expected_sha256': '0' * 64}),
        ]:
            with self.subTest(name=name, args=args), self.assertRaises(ToolError):
                self.tools.execute(name, args)
        self.assertEqual(source.read_bytes(), b'original\r\n')
        self.assertEqual((self.root / 'exists').read_text(), 'keep')

    @unittest.skipIf(os.name == 'nt', 'POSIX umask and permission bits')
    def test_workspace_directory_journal_matches_mode_under_restrictive_umask(self):
        previous = os.umask(0o222)
        try:
            self.tools.execute('make_directory', {'path': 'restricted'})
            recorded = self.tools.export_journal()[0]['changes'][0]['after']['mode']
            self.assertEqual(stat.S_IMODE((self.root / 'restricted').stat().st_mode), recorded)
            self.assertTrue(self.tools.undo_last()['undone'])
            self.assertFalse((self.root / 'restricted').exists())
        finally:
            os.umask(previous)

    @unittest.skipIf(os.name == 'nt', 'POSIX umask and permission bits')
    def test_workspace_interrupted_undo_recreates_directory_with_exact_mode(self):
        class Cancelled(BaseException):
            pass
        for error in (Cancelled, OSError):
            with self.subTest(error=error):
                self.write('file', 'kept')
                self.tools.execute('make_directory', {'path': 'directory'})
                journal = self.tools.export_journal()
                def interrupt_after_directory_removal():
                    if not (self.root / 'directory').exists():
                        raise error('injected interruption')
                self.tools.cancel_check = interrupt_after_directory_removal
                previous = os.umask(0o222)
                try:
                    with self.assertRaises(Cancelled if error is Cancelled else ToolError):
                        self.tools.undo_last()
                finally:
                    os.umask(previous)
                    self.tools.cancel_check = None
                self.assertEqual(stat.S_IMODE((self.root / 'directory').stat().st_mode), 0o700)
                self.assertEqual((self.root / 'file').read_text(), 'kept')
                self.assertEqual(self.tools.export_journal(), journal)
                self.assertTrue(self.tools.undo_last()['undone'])

    def test_workspace_directory_chmod_failure_does_not_publish_unjournaled_target(self):
        with patch('david.workspace.os.chmod', side_effect=OSError('chmod failed')):
            with self.assertRaises(ToolError):
                self.tools.execute('make_directory', {'path': 'directory'})
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(self.tools.export_journal(), [])

    def test_workspace_directory_recreation_failure_reports_failed_rollback(self):
        self.write('file', 'kept')
        self.tools.execute('make_directory', {'path': 'directory'})
        journal = self.tools.export_journal()
        def interrupt_after_directory_removal():
            if not (self.root / 'directory').exists():
                raise OSError('injected interruption')
        self.tools.cancel_check = interrupt_after_directory_removal
        try:
            with patch('david.workspace.os.chmod', side_effect=OSError('chmod failed')):
                with self.assertRaisesRegex(ToolError, 'Partial failure; rollback failed for: directory'):
                    self.tools.undo_last()
        finally:
            self.tools.cancel_check = None
        self.assertEqual({p.name for p in self.root.iterdir()}, {'file'})
        self.assertEqual(self.tools.export_journal(), journal)
        self.assertEqual((self.root / 'file').read_text(), 'kept')

    def test_workspace_mkdir_undo_removes_only_managed_children(self):
        self.tools.begin_turn()
        result = self.tools.execute('make_directory', {'path': 'new'})
        self.assertEqual(result, {'path': 'new', 'created': True})
        self.tools.execute('make_directory', {'path': 'new/nested'})
        self.write('new/nested/file', 'ours')
        user = self.root / 'new' / 'user'
        user.write_text('keep')
        count = len(self.approvals)
        with self.assertRaises(ToolError):
            self.tools.undo_last()
        self.assertEqual(len(self.approvals), count)
        self.assertEqual((self.root / 'new/nested/file').read_text(), 'ours')
        user.unlink()
        self.assertTrue(self.tools.undo_last()['undone'])
        self.assertFalse((self.root / 'new').exists())
        for path in ['.', '.env', 'missing/nested']:
            with self.subTest(path=path), self.assertRaises(ToolError):
                self.tools.execute('make_directory', {'path': path})

    def test_workspace_multi_patch_one_approval_and_all_or_nothing(self):
        (self.root / 'a').write_bytes(b'old\r\n')
        (self.root / 'a').chmod(0o750)
        changes = [{'path': 'a', 'content': 'new\r\n', 'expected_sha256': digest(b'old\r\n')},
                   {'path': 'b', 'content': 'created'}]
        result = self.tools.execute('apply_patch', {'changes': changes})
        self.assertEqual(len(result['changes']), 2)
        self.assertEqual(len(self.approvals), 1)
        self.assertIn('+created', self.approvals[0][1])
        self.assertIn('-old', self.approvals[0][1])
        self.assertEqual((self.root / 'a').read_bytes(), b'new\r\n')
        self.tools.undo_last()
        self.assertEqual((self.root / 'a').read_bytes(), b'old\r\n')
        self.assertFalse((self.root / 'b').exists())
        for invalid in [[], changes * 11, [changes[0], changes[0]],
                        [{'path': 'b', 'content': 'x'}, {'path': '.env', 'content': 'bad'}],
                        [{'path': 'b', 'content': 'x'}, {'path': 'a', 'content': 'bad'}],
                        [{'path': 'b', 'content': 'x'}, {'path': 'missing/c', 'content': 'bad'}],
                        [{'path': 'b', 'content': True}], [{'path': 'b', 'content': 'x', 'extra': 1}]]:
            count = len(self.approvals)
            with self.subTest(invalid=invalid), self.assertRaises(ToolError):
                self.tools.execute('apply_patch', {'changes': invalid})
            self.assertFalse((self.root / 'b').exists())
            self.assertEqual((self.root / 'a').read_bytes(), b'old\r\n')
            self.assertEqual(len(self.approvals), count)
        real_replace = os.replace
        def fail_second(source, destination):
            if Path(destination).name == 'b':
                raise OSError('injected second write failure')
            return real_replace(source, destination)
        with patch('david.workspace.os.replace', side_effect=fail_second), self.assertRaises(ToolError):
            self.tools.execute('apply_patch', {'changes': changes})
        self.assertEqual((self.root / 'a').read_bytes(), b'old\r\n')
        self.assertFalse((self.root / 'b').exists())
        self.assertEqual(self.tools.changes(), [])
        self.assertEqual({p.name for p in self.root.iterdir()}, {'a'})
