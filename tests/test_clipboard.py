"""Native clipboard command boundaries (no real clipboard changes in tests)."""
import subprocess
import unittest
from unittest.mock import patch


class ClipboardTests(unittest.TestCase):
    def test_windows_sends_unicode_as_stdin_never_as_powershell_code(self):
        from david.clipboard import copy_text
        text = '한글\n$(Remove-Item C:\\*)\n"; exit 9; #'
        with patch('david.clipboard.sys.platform', 'win32'), \
             patch('david.clipboard.os.environ', {'SystemRoot': r'C:\Windows'}), \
             patch('david.clipboard.os.path.isfile', return_value=True), \
             patch('david.clipboard.subprocess.run') as run:
            copy_text(text)
        args, kwargs = run.call_args
        self.assertEqual(args[0][0], r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe')
        self.assertIn('-NoProfile', args[0])
        self.assertIn('[Console]::In.ReadToEnd()', args[0][-1])
        self.assertNotIn(text, ' '.join(args[0]))
        self.assertEqual(kwargs['input'], text.encode('utf-8'))
        self.assertFalse(kwargs.get('shell', False))
        self.assertEqual(kwargs['timeout'], 5)

    def test_macos_uses_only_fixed_system_pbcopy(self):
        from david.clipboard import copy_text
        with patch('david.clipboard.sys.platform', 'darwin'), \
             patch('david.clipboard.os.path.isfile', return_value=True), \
             patch('david.clipboard.subprocess.run') as run:
            copy_text('plain text')
        self.assertEqual(run.call_args.args[0], ['/usr/bin/pbcopy'])

    def test_missing_or_failed_helper_reports_failure_without_escape_fallback(self):
        from david.clipboard import copy_text, ClipboardError
        with patch('david.clipboard.sys.platform', 'linux'), \
             patch('david.clipboard.shutil.which', return_value=None), \
             patch('david.clipboard.subprocess.run') as run:
            with self.assertRaises(ClipboardError):
                copy_text('not copied')
            run.assert_not_called()
        with patch('david.clipboard.sys.platform', 'darwin'), \
             patch('david.clipboard.os.path.isfile', return_value=True), \
             patch('david.clipboard.subprocess.run', side_effect=subprocess.TimeoutExpired('pbcopy', 5)):
            with self.assertRaises(ClipboardError):
                copy_text('not copied')

    def test_linux_search_does_not_inherit_project_path(self):
        from david.clipboard import copy_text
        with patch('david.clipboard.sys.platform', 'linux'), \
             patch('david.clipboard.shutil.which', return_value='/usr/bin/wl-copy') as which, \
             patch('david.clipboard.subprocess.run') as run:
            copy_text('body')
        self.assertEqual(which.call_args.args[0], 'wl-copy')
        self.assertNotIn('.', which.call_args.kwargs['path'].split(':'))
        self.assertEqual(run.call_args.args[0], ['/usr/bin/wl-copy'])
