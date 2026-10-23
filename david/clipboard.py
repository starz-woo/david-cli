"""Opt-in native clipboard writes. Never interpret copied text as a command."""
import ntpath
import os
import shutil
import subprocess
import sys


class ClipboardError(RuntimeError):
    """The native clipboard could not be updated."""


# A literal program, independent of response content. UTF-8 stdin preserves
# Unicode and newlines without quoting/interpolating model text into PowerShell.
_WINDOWS_COPY = (
    "$ErrorActionPreference = 'Stop'; "
    "[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false); "
    "Set-Clipboard -Value ([Console]::In.ReadToEnd())"
)


def copy_text(text):
    """Copy plain text only when a user explicitly invokes a copy action.

    Only known OS helpers are used; do not search the workspace or arbitrary
    PATH entries. Missing helpers fail honestly, never falling back to OSC 52.
    """
    if sys.platform == 'win32':
        root = os.environ.get('SystemRoot', r'C:\Windows')
        if not ntpath.isabs(root):
            raise ClipboardError('Windows system directory is unavailable')
        binary = ntpath.join(root, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
        if not os.path.isfile(binary):
            raise ClipboardError('Windows PowerShell clipboard helper is unavailable')
        command = [binary, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', _WINDOWS_COPY]
    elif sys.platform == 'darwin':
        if not os.path.isfile('/usr/bin/pbcopy'):
            raise ClipboardError('pbcopy is unavailable')
        command = ['/usr/bin/pbcopy']
    else:
        search_path = '/usr/bin:/bin:/usr/local/bin'
        binary = shutil.which('wl-copy', path=search_path)
        if binary:
            command = [binary]
        else:
            binary = shutil.which('xclip', path=search_path)
            if not binary:
                raise ClipboardError('No native clipboard helper (wl-copy or xclip) is available')
            command = [binary, '-selection', 'clipboard']
    try:
        subprocess.run(command, input=text.encode('utf-8'), stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=True, timeout=5, shell=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ClipboardError('Native clipboard helper failed or timed out') from exc
