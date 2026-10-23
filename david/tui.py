"""Full-screen terminal frontend. The controller owns all model/tool work."""
import asyncio
import json

from copy import deepcopy
from queue import Empty, SimpleQueue
from threading import Lock

from prompt_toolkit.application import Application
from prompt_toolkit.completion import Completer, Completion, CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition, has_focus
from prompt_toolkit.keys import Keys
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.scroll import scroll_page_down, scroll_page_up
from prompt_toolkit.layout import (ConditionalContainer, DynamicContainer, Float,
                                   FloatContainer, HSplit, Layout, Window)
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, TextArea

from .cli import safe_text
from .file_refs import file_reference_prefix, quote_file_reference


THEMES = {
    'dark': {'header': 'bg:#263445 #ffffff bold', 'status': 'bg:#263445 #b9d7ed',
             'changes': '#92c99b', 'hint': '#888888', 'frame.border': '#526575',
             'frame.label': '#b9d7ed bold', 'diff.add': '#8fdf9b', 'diff.remove': '#ff9494',
             'diff.header': '#9fcef5 bold'},
    'light': {'': 'bg:#ffffff #202932', 'header': 'bg:#edf2f7 #202932 bold',
              'status': 'bg:#edf2f7 #234e70', 'changes': '#226b36', 'hint': '#555555',
              'frame.border': '#80909e', 'frame.label': '#234e70 bold', 'diff.add': '#176d30',
              'diff.remove': '#a52222', 'diff.header': '#234e70 bold'},
    'mono': {'header': 'reverse bold', 'status': 'reverse', 'changes': 'bold',
             'hint': '', 'frame.border': '', 'frame.label': 'bold', 'diff.add': 'underline',
             'diff.remove': 'italic', 'diff.header': 'bold'},
}


class DiffLexer(Lexer):
    def lex_document(self, document):
        def line(number):
            text = document.lines[number]
            style = ('class:diff.header' if text.startswith(('+++', '---', '@@')) else
                     'class:diff.add' if text.startswith('+') else
                     'class:diff.remove' if text.startswith('-') else '')
            return [(style, text)]
        return line


class WorkspaceCompleter(Completer):
    """Never walk the filesystem: the controller supplies already-allowed paths."""

    def __init__(self, controller):
        self.controller = controller

    def get_completions(self, document, complete_event):
        before = document.text_before_cursor
        reference = file_reference_prefix(before)
        if reference is not None:
            length, prefix = reference
            for path in self.controller.files:
                if safe_text(path) != path or any(c in path for c in '\n\t'):
                    continue
                if path.startswith(prefix):
                    yield Completion(quote_file_reference(path), start_position=-length, display=path)
        elif before.startswith('/') and not any(c.isspace() for c in before):
            for command in self.controller.commands:
                if command.startswith(before) and safe_text(command) == command:
                    yield Completion(command, start_position=-len(before))


class DavidTUI:
    """An injectable prompt_toolkit application for the controller protocol."""

    def __init__(self, controller, input=None, output=None):
        self.controller = controller
        preferences = getattr(controller, 'ui', {})
        if not isinstance(preferences, dict):
            preferences = {}
        theme = preferences.get('theme', 'dark')
        self.theme = theme if isinstance(theme, str) and theme in THEMES else 'dark'
        height = preferences.get('input_height', 5)
        if type(height) is not int or not 1 <= height <= 20:
            height = 5
        self._events = SimpleQueue()
        self._event_lock = Lock()
        self._approval_epoch = 0
        self._shown_approval_epoch = None
        self._entries = []
        self._trimmed = False
        self.changes = []
        self.show_tool_details = False
        self._stream_index = None
        self.approval = None
        self.approval_panel = TextArea(read_only=True, scrollbar=True, wrap_lines=True, lexer=DiffLexer(),
                                       height=Dimension(min=1, preferred=7, max=12))
        self.status = 'idle'
        self.detail = ''
        self._closing = False
        self._stopping = False
        self._submitting = False
        self._composer_revision = 0
        self._cancel_requested = False
        self.session_id = controller.session_id
        self.saving = controller.saving
        self.transcript = TextArea(read_only=True, scrollbar=True, wrap_lines=True)
        self.completer = WorkspaceCompleter(controller)
        self.composer = TextArea(multiline=True, wrap_lines=True,
                                 completer=self.completer, complete_while_typing=False,
                                 height=Dimension(min=1, preferred=height, max=max(height, 8)))
        bindings = KeyBindings()
        editing = has_focus(self.composer)
        self.composer.buffer.on_text_changed += self._sanitize_composer

        @bindings.add('enter', filter=editing)
        @bindings.add('c-s', filter=editing)
        def submit(event):
            self.submit()

        @bindings.add('escape', 'enter', filter=editing)
        @bindings.add('c-j', filter=editing)
        def newline(event):
            self.composer.buffer.insert_text('\n')

        @bindings.add(Keys.BracketedPaste, filter=editing)
        def paste(event):
            text = event.data.replace('\r\n', '\n').replace('\r', '\n')
            self.composer.buffer.insert_text(safe_text(text))

        @bindings.add('tab')
        def tab(event):
            buffer = self.composer.buffer
            if editing() and self.approval is None:
                if buffer.complete_state:
                    buffer.complete_next()
                    return
                completions = list(self.completer.get_completions(buffer.document, CompleteEvent()))
                if len(completions) == 1:
                    buffer.apply_completion(completions[0])
                    return
                if completions:
                    buffer.start_completion(select_first=True)
                    return
            self.application.layout.focus_next()

        @bindings.add('s-tab')
        def previous_focus(event):
            self.application.layout.focus_previous()

        @bindings.add('c-o')
        def toggle_tools(event):
            self.show_tool_details = not self.show_tool_details
            self._render_transcript()

        @bindings.add('pageup')
        def page_up(event):
            if not self.application.layout.has_focus(self.approval_panel):
                self.application.layout.focus(self.transcript)
            scroll_page_up(event)

        @bindings.add('pagedown')
        def page_down(event):
            if not self.application.layout.has_focus(self.approval_panel):
                self.application.layout.focus(self.transcript)
            buffer = self.application.current_buffer
            previous = buffer.cursor_position
            scroll_page_down(event)
            if buffer.cursor_position <= previous:
                info = self.application.layout.current_window.render_info
                buffer.cursor_down(count=max(1, info.window_height - 1) if info else 1)

        @bindings.add('c-c')
        def cancel(event):
            self.cancel()

        @bindings.add('c-q')
        def quit(event):
            self.request_close()

        @bindings.add('c-y', filter=Condition(lambda: self.approval is not None))
        def approve(event):
            self._decide(True)

        @bindings.add('c-n', filter=Condition(lambda: self.approval is not None))
        def deny(event):
            self._decide(False)

        normal = HSplit([
                Window(FormattedTextControl(self.header_text), height=1, style='class:header'),
                self.transcript,
                ConditionalContainer(
                    Frame(self.approval_panel, title='Approval · yes / no · Ctrl+Y / Ctrl+N'),
                    filter=Condition(lambda: self.approval is not None)),
                Window(FormattedTextControl(self.changes_text), height=1, style='class:changes'),
                Frame(self.composer, title='Message · Enter send'),
                Window(FormattedTextControl(self.status_text), height=1, style='class:status'),
                Window(FormattedTextControl(
                    'Enter send · Alt+Enter/Ctrl+J newline · Ctrl+S send · Ctrl+C cancel · Ctrl+Q quit · '
                    'Tab complete/focus · Shift+Tab back · PgUp/PgDn scroll · Ctrl+O tools'),
                    height=1, style='class:hint'),
            ])
        compact = HSplit([
            ConditionalContainer(Window(FormattedTextControl(self.header_text), height=1,
                                        style='class:header'),
                                 Condition(lambda: self._size().rows >= 6)),
            ConditionalContainer(self.transcript,
                                 Condition(lambda: self._size().rows >= 4 and self.approval is None)),
            ConditionalContainer(self.approval_panel, Condition(lambda: self.approval is not None
                                                                and self._size().rows >= 3)),
            self.composer,
            ConditionalContainer(Window(FormattedTextControl(self.status_text), height=1),
                                 Condition(lambda: self._size().rows >= 2)),
        ])
        root = FloatContainer(
            DynamicContainer(lambda: compact if self._size().rows < 16 or self._size().columns < 48 else normal),
            floats=[Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=7))],
        )
        self.application = Application(
            layout=Layout(root, focused_element=self.composer),
            key_bindings=bindings, full_screen=True, mouse_support=True,
            input=input, output=output, before_render=self._drain_events,
            style=Style.from_dict(THEMES[self.theme]),
        )
        controller.on_event = self.handle_event
        self.handle_event({'type': 'message', 'role': 'system', 'text':
            '코드·프롬프트·도구 출력은 설정된 서버로 전송됩니다. 명령 실행은 OS 샌드박스가 아닙니다.\n'
            '대화 저장은 기본 OFF입니다. /save on은 코드·undo 원본을 평문 저장합니다. /help로 명령을 확인하세요.'})

    def _size(self):
        if not hasattr(self, 'application'):
            # Layout walks dynamic containers before Application has been assigned.
            from prompt_toolkit.data_structures import Size
            return Size(rows=24, columns=80)
        return self.application.output.get_size()

    def header_text(self):
        return safe_text(f'David | {self.controller.model_name} | {self.controller.workspace}'
                         f' | session {self.session_id}')

    def changes_text(self):
        if not self.changes:
            return 'Changes: none'
        items = []
        for change in self.changes:
            counts = ''
            if 'added' in change:
                counts += f" +{change['added']}"
            if 'removed' in change:
                counts += f" -{change['removed']}"
            action = change.get('action', change.get('status', ''))
            items.append(f"{change.get('path', '?')} {action}{counts}")
        return safe_text('Changes: ' + ' | '.join(items))

    def status_text(self):
        return safe_text(f'{self.status} | {self.detail}' + (' | saving ON (평문)' if self.saving else ' | 저장 OFF'))

    def cancel(self):
        if self.controller.busy or self._submitting or self.approval is not None:
            self._cancel_requested = True
            self.approval = None
            self.status = 'stopping'
            self.detail = 'Cancellation requested; waiting for active work'
            self.controller.cancel()
        else:
            self.composer.text = ''

    def request_close(self):
        if self._closing:
            return
        self._closing = True
        self._stopping = True
        self.approval = None
        self.status = 'stopping'
        self.detail = 'Cancelling and waiting for active work; no tools are killed'
        self.controller.cancel()
        self.application.create_background_task(self._close())

    async def _close(self):
        try:
            while self._submitting:
                await asyncio.sleep(.02)
            # A submit in flight may have launched work after the first cancel.
            self.controller.cancel()
            while True:
                stopped = await asyncio.to_thread(self.controller.close, timeout=0)
                if stopped:
                    self.application.exit(result=0)
                    return
                self.status = 'stopping'
                self.detail = 'Still stopping; waiting safely (Ctrl+Q requested shutdown)'
                self.application.invalidate()
                await asyncio.sleep(.2)
        except Exception as exc:
            self.status = 'error'
            self.detail = safe_text(exc) + '; Ctrl+Q retries shutdown'
        finally:
            self._closing = False
            self.application.invalidate()

    def submit(self):
        text = safe_text(self.composer.text)
        if self.approval is not None:
            if text in ('yes', 'no'):
                self._decide(text == 'yes')
            else:
                self.detail = 'Type exactly yes or no, or use Ctrl+Y / Ctrl+N'
            return
        if text in ('/quit', '/exit'):
            self.request_close()
            return
        if not self._stopping and not self._submitting and text.strip():
            self._cancel_requested = False
            self._submitting = True
            self.application.create_background_task(self._submit(text, self._composer_revision))

    async def _submit(self, text, revision):
        try:
            accepted = await asyncio.to_thread(self.controller.submit, text)
            if accepted and self._cancel_requested:
                self.controller.cancel()
            if accepted and revision == self._composer_revision and self.approval is None:
                self.composer.text = ''
        except (Exception, KeyboardInterrupt) as exc:
            self.status = 'cancelled' if isinstance(exc, KeyboardInterrupt) else 'error'
            self.detail = safe_text(exc)
        finally:
            self._submitting = False
            self.application.invalidate()

    def _decide(self, decision):
        with self._event_lock:
            current = self._shown_approval_epoch == self._approval_epoch
        if self.approval is None or not current:
            return
        self.approval = None
        self.composer.text = ''
        self.status = 'running'
        self.detail = 'Approved' if decision else 'Denied'
        self.controller.respond_approval(decision)
        self.application.layout.focus(self.composer)

    def _sanitize_composer(self, buffer):
        self._composer_revision += 1
        text = safe_text(buffer.text)
        if text != buffer.text:
            cursor = len(safe_text(buffer.document.text_before_cursor))
            buffer.document = Document(text, cursor)

    def handle_event(self, event):
        """Safe for controller worker threads; widgets mutate only during render."""
        event = deepcopy(event)
        with self._event_lock:
            if event.get('type') in ('approval', 'reset') or (
                event.get('type') == 'status' and event.get('status') in ('cancelled', 'error', 'done', 'idle')
            ):
                self._approval_epoch += 1
            event['_approval_epoch'] = self._approval_epoch
            self._events.put(event)
        self.application.invalidate()

    def _drain_events(self, application):
        changed = False
        while True:
            try:
                event = self._events.get_nowait()
            except Empty:
                break
            changed = True
            kind = event.get('type')
            if kind == 'reset':
                self._entries.clear()
                self._trimmed = False
                self.changes = []
                self._stream_index = None
                self.approval = None
                self.approval_panel.buffer.set_document(Document(''), bypass_readonly=True)
                self.composer.text = ''
                self.show_tool_details = False
                self.status = 'idle'
                self.detail = ''
                self.application.layout.focus(self.composer)
            elif kind == 'text':
                if self._stream_index is None:
                    self._stream_index = len(self._entries)
                    self._entries.append({'role': 'assistant', 'text': ''})
                self._entries[self._stream_index]['text'] += safe_text(event.get('text', ''))
            elif kind == 'message':
                role = safe_text(event.get('role', 'message'))
                text = safe_text(event.get('text', ''))
                if role == 'assistant' and self._stream_index is not None:
                    self._entries[self._stream_index]['text'] = text
                else:
                    self._entries.append({'role': role, 'text': text})
                self._stream_index = None
            elif kind == 'tool':
                name = safe_text(event.get('name', 'tool'))
                result = event.get('result', {})
                failed = result.get('error') or result.get('ok') is False or result.get('timed_out') or result.get('exit_code', 0) != 0
                outcome = 'error' if failed else 'done'
                details = '\n'.join(
                    f'{key}:\n{value}' if isinstance(value, str) and '\n' in value
                    else f'{key}: {value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)}'
                    for key, value in result.items())
                self._entries.append({'role': 'tool ' + name, 'text': outcome,
                                      'details': safe_text(details)})
                self._stream_index = None
            elif kind == 'changes':
                self.changes = event.get('changes', [])
            elif kind == 'summary':
                self._entries.append({'role': 'summary', 'text': safe_text(event.get('text', ''))})
                # Some controllers deliver the final assistant message after metrics.
                # Keep its streaming slot until that message or the next turn arrives.
            elif kind == 'approval' and not self._stopping and not self._cancel_requested:
                self.approval = event
                self._shown_approval_epoch = event['_approval_epoch']
                self.status = 'approval'
                self.detail = 'Review the action before deciding'
                text = safe_text(str(event.get('action', '')) + '\n' + str(event.get('detail', '')))
                self.approval_panel.buffer.set_document(Document(text, 0), bypass_readonly=True)
                self.composer.text = ''
                self.application.layout.focus(self.composer)
            elif kind == 'status' and not self._stopping:
                self.status = safe_text(event.get('status', 'idle'))
                self.detail = safe_text(event.get('detail', ''))
                if self.status in ('cancelled', 'error', 'done', 'idle'):
                    self.approval = None
            elif kind == 'session':
                self.session_id = safe_text(event.get('id', ''))
                self.saving = bool(event.get('saving', False))
            elif kind == 'input' and self.approval is None:
                text = safe_text(event.get('text', ''))
                self.composer.buffer.document = Document(text, len(text))
        if changed:
            self._render_transcript()

    def _render_transcript(self):
        # Bound both retained data and rendering cost on long coding sessions.
        for entry in self._entries:
            for key in ('text', 'details'):
                if len(entry.get(key, '')) > 100000:
                    entry[key] = '[앞부분 생략]\n' + entry[key][-99980:]
                    self._trimmed = True
        size = sum(len(e.get('text', '')) + len(e.get('details', '')) + 100 for e in self._entries)
        while self._entries and (size > 200000 or len(self._entries) > 300):
            removed = self._entries.pop(0)
            size -= len(removed.get('text', '')) + len(removed.get('details', '')) + 100
            if self._stream_index is not None:
                self._stream_index = self._stream_index - 1 if self._stream_index else None
            self._trimmed = True
        parts = []
        if self._trimmed:
            parts.append('[화면 기록 한도로 오래된 출력 생략 · 저장 세션과 모델 컨텍스트 한도는 별도]')
        for entry in self._entries:
            text = entry['text']
            if 'details' in entry:
                text += ('\n' + entry['details']) if self.show_tool_details else ' [Ctrl+O details]'
            parts.append(f"{entry['role']}> {text}")
        text = '\n\n'.join(parts)
        buffer = self.transcript.buffer
        follow = buffer.cursor_position == len(buffer.text)
        cursor = len(text) if follow else min(buffer.cursor_position, len(text))
        buffer.set_document(Document(text, cursor), bypass_readonly=True)


def run_tui(controller, input=None, output=None) -> int:
    """Run on the calling thread; model/tool operations belong to the controller."""
    ui = DavidTUI(controller, input=input, output=output)
    return ui.application.run()
