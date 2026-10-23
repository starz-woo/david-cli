"""Security boundary and command runner contracts (stdlib-only)."""
import hashlib
import json
import os
import sys
import time
from unittest.mock import patch
from pathlib import Path
import tempfile
import unittest


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.approvals = []

    def tools(self, security=None, commands=None, approve=None, protected=None):
        from david.workspace import WorkspaceTools
        def allow(action, detail):
            self.approvals.append((action, detail))
            return True
        return WorkspaceTools(self.root, security or {}, commands or {},
                              approve or allow, protected_paths=protected)

    def test_workspace_parent_segments_cannot_bypass_protected_subtree(self):
        from david.workspace import WorkspaceTools, ToolError
        (self.root / 'sub').mkdir()
        (self.root / 'private').mkdir()
        (self.root / 'private' / 'agent.py').write_text('protected')
        tools = WorkspaceTools(self.root / 'sub' / '..', {}, {}, lambda *_: True,
                               protected_paths=[self.root / 'private'])
        with self.assertRaises(ToolError):
            tools.execute('read_file', {'path': 'private/agent.py'})

    def test_protected_path_normalizes_unicode_without_filesystem_support(self):
        from david.workspace import ToolError
        tools = self.tools(protected=[self.root / 'Café'])
        for path in ['cafe\u0301', 'cafe\u0301/new.txt']:
            with self.subTest(path=path), self.assertRaisesRegex(ToolError, 'Protected path'):
                tools._check_protected(self.root / path)
        tools._check_protected(self.root / 'public')

    def test_unicode_alias_of_protected_file_cannot_be_read_or_written(self):
        from david.workspace import ToolError
        target = self.root / 'café.json'
        alias = self.root / 'cafe\u0301.json'
        target.write_text('private', encoding='utf-8')
        if not alias.exists() or not alias.samefile(target):
            self.skipTest('Filesystem does not equate NFC and NFD names')
        tools = self.tools(protected=[target])
        for name, arguments in [
            ('read_file', {'path': alias.name}),
            ('write_file', {'path': alias.name, 'content': 'changed',
                            'expected_sha256': hashlib.sha256(b'private').hexdigest()}),
        ]:
            with self.subTest(tool=name), self.assertRaisesRegex(ToolError, 'Protected path'):
                tools.execute(name, arguments)
        self.assertEqual(target.read_text(), 'private')
        self.assertEqual(self.approvals, [])

    def test_unicode_alias_of_protected_directory_blocks_subtree(self):
        from david.workspace import ToolError, WorkspaceTools
        target = self.root / 'café'
        target.mkdir()
        (target / 'nested').mkdir()
        (target / 'nested' / 'code.py').write_text('private', encoding='utf-8')
        alias = self.root / 'cafe\u0301'
        if not alias.exists() or not alias.samefile(target):
            self.skipTest('Filesystem does not equate NFC and NFD names')
        tools = self.tools(protected=[target])
        for name, arguments in [
            ('read_file', {'path': alias.name + '/nested/code.py'}),
            ('write_file', {'path': alias.name + '/nested/new.py', 'content': 'changed'}),
            ('list_files', {'path': alias.name}),
        ]:
            with self.subTest(tool=name), self.assertRaisesRegex(ToolError, 'Protected path'):
                tools.execute(name, arguments)
        with self.assertRaisesRegex(ToolError, 'Protected path'):
            WorkspaceTools(alias / 'nested', {}, {}, lambda *_: True, protected_paths=[target])
        self.assertEqual(tools.execute('list_files', {})['entries'], [])
        self.assertEqual(self.approvals, [])

    def test_protected_file_identity_rejects_alias_but_not_sibling(self):
        from david.workspace import ToolError
        target = self.root / 'private.json'
        alias = self.root / 'alias.json'
        target.write_text('private', encoding='utf-8')
        os.link(target, alias)
        tools = self.tools(protected=[target])
        with self.assertRaisesRegex(ToolError, 'Protected path'):
            tools._check_protected(alias)
        tools.execute('write_file', {'path': 'public.txt', 'content': 'public'})
        self.assertEqual(tools.execute('read_file', {'path': 'public.txt'})['content'], 'public')

    def test_protected_directory_identity_rejects_alias_subtree(self):
        from david.workspace import ToolError
        target = self.root / 'private'
        (target / 'nested').mkdir(parents=True)
        alias = self.root / 'alias'
        alias.symlink_to(target, target_is_directory=True)
        tools = self.tools(protected=[target])
        # Exercise identity matching independently of the additional symlink ban.
        for path in [alias, alias / 'nested', alias / 'nested' / 'new.txt']:
            with self.subTest(path=path), self.assertRaisesRegex(ToolError, 'Protected path'):
                tools._check_protected(path)
        tools._check_protected(self.root / 'public' / 'new.txt')

    def test_workspace_read_lines_with_full_digest(self):
        data = 'one\ntwo\nthree\n'
        (self.root / 'notes.txt').write_text(data, encoding='utf-8')
        tools = self.tools()
        result = tools.execute('read_file', {'path': 'notes.txt', 'offset': 2, 'limit': 1})
        self.assertEqual(result['content'], 'two\n')
        self.assertEqual(result['sha256'], hashlib.sha256(data.encode()).hexdigest())
        self.assertEqual(result['total_lines'], 3)
        self.assertEqual(self.approvals, [])
        self.assertIn('read_file', [s['function']['name'] for s in tools.schemas()])

    def test_workspace_read_rejects_unsafe_paths_and_files(self):
        from david.workspace import ToolError
        (self.root / 'ok').write_text('safe', encoding='utf-8')
        for name in ['.env', 'APIcredentials.txt', 'private.pem', 'setting.json', 'custom.cfg']:
            (self.root / name).write_text('secret', encoding='utf-8')
        (self.root / 'binary').write_bytes(b'abc\x00def')
        (self.root / 'invalid').write_bytes(b'\xff')
        (self.root / 'large').write_bytes(b'x' * 30)
        (self.root / 'alias').symlink_to(self.root / 'ok')
        os.link(self.root / 'ok', self.root / 'hard')
        tools = self.tools({'max_file_bytes': 20}, protected=[self.root / 'custom.cfg'])
        for name in ['../escape', 'a/../ok', '/etc/passwd', 'C:\\Windows\\win.ini',
                     'C:relative', '\\\\host\\share', '.env', 'APIcredentials.txt',
                     'private.pem', 'setting.json', 'custom.cfg', 'binary', 'invalid',
                     'large', 'alias', 'hard', 'ok']:
            with self.subTest(name=name), self.assertRaises(ToolError):
                tools.execute('read_file', {'path': name})
        (self.root / 'dir').mkdir()
        (self.root / 'shortcut').symlink_to(self.root / 'dir', target_is_directory=True)
        with self.assertRaises(ToolError):
            tools.execute('read_file', {'path': 'shortcut/absent'})
        with self.assertRaises(ToolError):
            tools.execute('read_file', {'path': 'dir'})
        for args in [{'path':'binary', 'offset':0}, {'path':'binary', 'limit': True},
                     {'path':'binary', 'extra':1}, {}]:
            with self.assertRaises(ToolError):
                tools.execute('read_file', args)

    def test_workspace_listing_and_literal_search_are_bounded_and_skip_secrets(self):
        (self.root / 'sub').mkdir()
        (self.root / 'sub' / 'a.txt').write_text('a.b\naxb\na.b\n', encoding='utf-8')
        (self.root / '.git').mkdir()
        (self.root / '.git' / 'secret').write_text('a.b', encoding='utf-8')
        (self.root / '.env').write_text('a.b', encoding='utf-8')
        (self.root / 'binary').write_bytes(b'a.b\x00')
        (self.root / 'alias').symlink_to(self.root / 'sub', target_is_directory=True)
        tools = self.tools()
        listing = tools.execute('list_files', {})
        paths = [e['path'] for e in listing['entries']]
        self.assertIn('sub/a.txt', paths)
        self.assertNotIn('.env', paths)
        self.assertFalse(any(p.startswith(('.git', 'alias')) for p in paths))
        found = tools.execute('search_files', {'pattern': 'a.b'})
        self.assertEqual([m['line'] for m in found['matches']], [1, 3])
        self.assertEqual([m['path'] for m in found['matches']], ['sub/a.txt'] * 2)
        self.assertEqual(len(tools.execute('search_files', {'pattern': 'a.b', 'max_results': 1})['matches']), 1)
        limited = self.tools({'max_scan_entries': 1}).execute('list_files', {})
        self.assertLessEqual(len(limited['entries']), 1)
        self.assertTrue(limited['truncated'])
        self.assertEqual(len(tools.execute('list_files', {'max_entries': 1})['entries']), 1)
        self.assertEqual(self.approvals, [])

    def test_workspace_write_approved_atomic_and_replace_digest(self):
        from david.workspace import ToolError
        tools = self.tools()
        created = tools.execute('write_file', {'path': 'new.txt', 'content': 'old\n'})
        self.assertEqual((self.root / 'new.txt').read_text(), 'old\n')
        self.assertEqual(self.approvals[-1][0], 'write_file')
        self.assertIn('+old', self.approvals[-1][1])
        with self.assertRaises(ToolError):
            tools.execute('write_file', {'path': 'new.txt', 'content': 'bad'})
        replaced = tools.execute('replace_text', {'path': 'new.txt', 'old': 'old', 'new': 'new',
                                                 'expected_sha256': created['sha256']})
        self.assertEqual((self.root / 'new.txt').read_text(), 'new\n')
        self.assertNotEqual(replaced['sha256'], created['sha256'])
        self.assertIn('-old', self.approvals[-1][1])
        self.assertIn('+new', self.approvals[-1][1])
        tools.execute('write_file', {'path': 'new.txt', 'content': '',
                                    'expected_sha256': replaced['sha256']})
        self.assertEqual((self.root / 'new.txt').read_bytes(), b'')
        self.assertEqual([p.name for p in self.root.iterdir()], ['new.txt'])

    def test_workspace_write_denial_readonly_and_approval_races(self):
        from david.workspace import ToolError
        path = self.root / 'a.txt'
        path.write_text('before', encoding='utf-8')
        digest = hashlib.sha256(b'before').hexdigest()
        args = {'path': 'a.txt', 'content': 'after', 'expected_sha256': digest}
        with self.assertRaises(ToolError):
            self.tools(approve=lambda *_: False).execute('write_file', args)
        self.assertEqual(path.read_text(), 'before')
        with self.assertRaises(ToolError):
            self.tools({'read_only': True}).execute('write_file', args)
        self.assertEqual(self.approvals, [])
        def race(*_):
            path.write_text('someone else', encoding='utf-8')
            return True
        with self.assertRaises(ToolError):
            self.tools(approve=race).execute('write_file', args)
        self.assertEqual(path.read_text(), 'someone else')
        with self.assertRaises(ToolError):
            self.tools().execute('write_file', args)
        for name in ['.env', '../outside', 'missing/child']:
            with self.assertRaises(ToolError):
                self.tools().execute('write_file', {'path': name, 'content': 'no'})
        def create_race(*_):
            (self.root / 'new').write_text('other', encoding='utf-8')
            return True
        with self.assertRaises(ToolError):
            self.tools(approve=create_race).execute('write_file', {'path': 'new', 'content': 'ours'})
        self.assertEqual((self.root / 'new').read_text(), 'other')
        self.assertEqual(self.approvals, [])

    def test_workspace_named_command_approval_env_and_cwd(self):
        code = 'import os,json;print(json.dumps(dict(os.environ)));print(os.getcwd())'
        commands = {'inspect': {'argv': ['{python}', '-c', code], 'timeout_seconds': 5}}
        tools = self.tools({'enable_commands': True}, commands)
        with patch.dict(os.environ, {'SUPER_SECRET': 'dont-leak', 'PYTHONPATH': '/nope',
                                     'OPENAI_API_KEY': 'never-pass'}):
            result = tools.execute('run_command', {'name': 'inspect'})
        self.assertEqual(result['exit_code'], 0)
        self.assertFalse(result['timed_out'])
        env = json.loads(result['output'].splitlines()[0])
        self.assertNotIn('SUPER_SECRET', env)
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertNotIn('PYTHONPATH', env)
        self.assertNotEqual(env['HOME'], str(Path.home()))
        self.assertEqual(env['HOME'], env['USERPROFILE'])
        self.assertFalse(Path(env['HOME']).exists())
        self.assertEqual(result['output'].splitlines()[1], str(self.root))
        action, detail = self.approvals[0]
        self.assertEqual(action, 'run_command')
        self.assertIn(sys.executable, detail)
        self.assertIn(str(self.root), detail)
        self.assertIn(code, json.loads(detail)['argv'])

    def test_workspace_named_command_denied_or_disabled_cannot_execute(self):
        from david.workspace import ToolError
        commands = {'touch': {'argv': ['{python}', '-c', "open('marker','w').close()"]}}
        for tools, args in [
            (self.tools({}, commands), {'name': 'touch'}),
            (self.tools({'enable_commands': True, 'read_only': True}, commands), {'name': 'touch'}),
            (self.tools({'enable_commands': True}, commands, approve=lambda *_: False), {'name': 'touch'}),
            (self.tools({'enable_commands': True}, commands), {'name': 'not-configured'}),
            (self.tools({'enable_commands': True}, commands), {'name': 'touch', 'argv': ['anything']}),
        ]:
            with self.assertRaises(ToolError):
                tools.execute('run_command', args)
        self.assertFalse((self.root / 'marker').exists())
        self.assertEqual(self.approvals, [])
        for executable in ['bad.cmd', 'bad.BAT', 'sh', 'bash', 'cmd.exe', 'powershell.exe']:
            with self.subTest(executable=executable), self.assertRaises(ToolError):
                self.tools({'enable_commands': True}, {'bad': {'argv': [executable]}}).execute(
                    'run_command', {'name': 'bad'})

    def test_workspace_command_output_cap_drains_and_records_failure(self):
        code = 'import os,sys;os.write(1,b"x"*1000000);os.write(2,b"y"*100000);sys.exit(7)'
        tools = self.tools({'enable_commands': True, 'command_output_bytes': 123,
                            'max_output_chars': 100},
                           {'loud': {'argv': ['{python}', '-c', code], 'timeout_seconds': 5}})
        result = tools.execute('run_command', {'name': 'loud'})
        self.assertEqual(result['exit_code'], 7)
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['output']), 100)
        self.assertFalse(result['timed_out'])

    def test_workspace_command_timeout_kills_descendants(self):
        child = "import time;time.sleep(0.8);open('orphan-marker','w').close()"
        code = ('import subprocess,sys,time;'
                f'subprocess.Popen([sys.executable,"-c",{child!r}]);'
                'print("ready",flush=True);time.sleep(10)')
        tools = self.tools({'enable_commands': True},
                           {'hang': {'argv': ['{python}', '-c', code], 'timeout_seconds': 0.3}})
        start = time.monotonic()
        result = tools.execute('run_command', {'name': 'hang'})
        self.assertLess(time.monotonic() - start, 3)
        self.assertTrue(result['timed_out'])
        self.assertIn('ready', result['output'])
        time.sleep(1)
        self.assertFalse((self.root / 'orphan-marker').exists())

    def test_workspace_rejects_invalid_security_limits(self):
        from david.workspace import ToolError
        for security in [{'enable_commands': 'false'}, {'read_only': 0}, {'max_file_bytes': -1},
                         {'command_output_bytes': 0}, {'max_scan_entries': True},
                         {'max_output_chars': '100'}, {'denied_names': '.env'},
                         {'denied_globs': [False]}]:
            with self.subTest(security=security), self.assertRaises(ToolError):
                self.tools(security)

    def test_workspace_resolved_batch_command_is_rejected(self):
        from david.workspace import ToolError
        tools = self.tools({'enable_commands': True}, {'test': {'argv': ['test']}})
        with patch('david.workspace.shutil.which', return_value=str(self.root / 'test.cmd')):
            with self.assertRaises(ToolError):
                tools.execute('run_command', {'name': 'test'})
        self.assertEqual(self.approvals, [])

    def test_workspace_protected_path_normalization(self):
        from david.workspace import ToolError
        (self.root / 'sub').mkdir()
        (self.root / 'custom.conf').write_text('private', encoding='utf-8')
        tools = self.tools(protected=[self.root / 'sub' / '..' / 'custom.conf'])
        with self.assertRaises(ToolError):
            tools.execute('read_file', {'path': 'custom.conf'})

    @unittest.skipIf(os.name == 'nt', 'POSIX permission bits')
    def test_workspace_edit_preserves_executable_permissions(self):
        import stat
        target = self.root / 'script.py'
        target.write_text('old', encoding='utf-8')
        target.chmod(0o750)
        self.tools().execute('write_file', {'path': 'script.py', 'content': 'new',
                                          'expected_sha256': hashlib.sha256(b'old').hexdigest()})
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o750)

    def test_workspace_symlink_swap_during_approval_is_rejected(self):
        from david.workspace import ToolError
        target = self.root / 'edit.txt'
        other = self.root / 'other.txt'
        target.write_text('old', encoding='utf-8')
        other.write_text('untouched', encoding='utf-8')
        def swap(*_):
            target.unlink()
            target.symlink_to(other)
            return True
        with self.assertRaises(ToolError):
            self.tools(approve=swap).execute('write_file', {
                'path': 'edit.txt', 'content': 'new',
                'expected_sha256': hashlib.sha256(b'old').hexdigest()})
        self.assertEqual(other.read_text(), 'untouched')

    def test_workspace_failed_atomic_replace_leaves_original_and_no_tempfile(self):
        from david.workspace import ToolError
        target = self.root / 'edit.txt'
        target.write_text('old', encoding='utf-8')
        with patch('david.workspace.os.replace', side_effect=OSError('simulated failure')):
            with self.assertRaises(ToolError):
                self.tools().execute('write_file', {'path': 'edit.txt', 'content': 'new',
                    'expected_sha256': hashlib.sha256(b'old').hexdigest()})
        self.assertEqual(target.read_text(), 'old')
        self.assertEqual([p.name for p in self.root.iterdir()], ['edit.txt'])

    def test_workspace_reparse_point_and_special_file_checks(self):
        from david.workspace import ToolError
        from types import SimpleNamespace
        import stat
        target = self.root / 'junction'
        target.mkdir()
        tools = self.tools()
        original = Path.lstat
        def attributes(path, *args, **kwargs):
            if path == target:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_nlink=1, st_file_attributes=0x400)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'lstat', attributes), self.assertRaises(ToolError):
            tools.execute('list_files', {'path': 'junction'})
        if hasattr(os, 'mkfifo'):
            os.mkfifo(self.root / 'fifo')
            with self.assertRaises(ToolError):
                tools.execute('read_file', {'path': 'fifo'})
            self.assertNotIn('fifo', [e['path'] for e in tools.execute('list_files', {})['entries']])

    def test_workspace_protected_directory_subtree_is_never_read_listed_or_written(self):
        from david.workspace import ToolError
        package = self.root / 'david'
        (package / 'nested').mkdir(parents=True)
        (package / 'nested' / 'code.py').write_text('sensitive', encoding='utf-8')
        tools = self.tools(protected=[package])
        for name, arguments in [
            ('read_file', {'path': 'david/nested/code.py'}),
            ('list_files', {'path': 'david/nested'}),
            ('search_files', {'path': 'david', 'pattern': 'sensitive'}),
            ('write_file', {'path': 'david/new.py', 'content': 'overwrite'}),
        ]:
            with self.subTest(tool=name), self.assertRaises(ToolError):
                tools.execute(name, arguments)
        self.assertEqual(tools.execute('list_files', {})['entries'], [])
        self.assertEqual(tools.execute('search_files', {'pattern': 'sensitive'})['matches'], [])
        self.assertEqual(self.approvals, [])
        from david.workspace import WorkspaceTools
        with self.assertRaises(ToolError):
            WorkspaceTools(package / 'nested', {}, {}, lambda *_: True, protected_paths=[package])


if __name__ == '__main__':
    unittest.main()
