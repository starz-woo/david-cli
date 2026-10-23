"""Full-screen terminal frontend. The controller owns all model/tool work."""
import asyncio
import json

from bisect import bisect_right
from functools import lru_cache
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
from prompt_toolkit.layout.screen import Char
from prompt_toolkit.formatted_text import fragment_list_to_text, to_formatted_text
from prompt_toolkit.widgets import Frame, TextArea

from .cli import safe_text
from .file_refs import file_reference_prefix, quote_file_reference


_TRUNCATION_MARKER = '[앞부분 생략]\n'


THEMES = {
    'dark': {
        '': 'bg:#101722 #dce4ed', 'header': 'bg:#192433 #e7edf5 bold',
        'status': 'bg:#192433 #a6b9cc', 'changes': '#99c8ad', 'hint': '#8899ad',
        'frame.border': '#394b60', 'frame.label': '#e2b880 bold',
        'role.user': '#e2b880 bold', 'role.assistant': '#78cdd7 bold',
        'role.system': '#93a4b8 bold', 'role.tool': '#b3a4d9 bold',
        'rail.user': '#b18c61', 'rail.assistant': '#40838c', 'rail.system': '#394b60',
        'rail.tool': '#70638c', 'body.system': '#93a4b8', 'body.tool': '#b8c5d4',
        'heading': '#e7edf5 bold', 'code': 'bg:#182332 #b5d4e5', 'fence': '#738da5',
        'diff.add': '#94d6a8', 'diff.remove': '#f49898', 'diff.header': '#8fc7eb bold',
        'approval': '#e2b880 bold',
    },
    'light': {
        '': 'bg:#fafbfc #263241', 'header': 'bg:#edf2f7 #202932 bold',
        'status': 'bg:#edf2f7 #405770', 'changes': '#226b36', 'hint': '#596b7d',
        'frame.border': '#b2bfcc', 'frame.label': '#865515 bold',
        'role.user': '#865515 bold', 'role.assistant': '#096b78 bold',
        'role.system': '#596b7d bold', 'role.tool': '#695091 bold',
        'rail.user': '#ad834c', 'rail.assistant': '#4595a0', 'rail.system': '#b2bfcc',
        'rail.tool': '#9883b4', 'body.system': '#596b7d', 'body.tool': '#405770',
        'heading': '#202932 bold', 'code': 'bg:#eaf0f5 #284d6a', 'fence': '#657c91',
        'diff.add': '#176d30', 'diff.remove': '#a52222', 'diff.header': '#234e70 bold',
        'approval': '#865515 bold',
    },
    'mono': {
        'header': 'reverse bold', 'status': 'reverse', 'changes': 'bold', 'hint': '',
        'frame.border': '', 'frame.label': 'bold', 'role.user': 'bold',
        'role.assistant': 'bold underline', 'role.system': 'italic', 'role.tool': 'bold',
        'rail.user': '', 'rail.assistant': '', 'rail.system': '', 'rail.tool': '',
        'body.system': '', 'body.tool': '', 'heading': 'bold', 'code': 'italic',
        'fence': '', 'diff.add': 'underline', 'diff.remove': 'italic',
        'diff.header': 'bold', 'approval': 'bold underline',
    },
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


class TranscriptLexer(Lexer):
    """Styles come from trusted entry metadata, never role-looking body text."""

    def __init__(self, styles):
        self.styles = tuple(styles)

    def lex_document(self, document):
        def line(number):
            style = self.styles[number] if number < len(self.styles) else ''
            return [(style, document.lines[number])]
        return line


class TranscriptWindow(Window):
    """Scroll in rendered rows, independently of focus and the text cursor.

    A logical line can occupy many screens. Window's default cursor-based
    scrolling cannot preserve a reader's position inside that line.
    """

    def __init__(self, owner, **kwargs):
        super().__init__(**kwargs)
        self.owner = owner
        self._content = None
        self._document = Document('')
        self._entry_starts = []
        self._width = 1
        self._height = 1
        self._heights = {}

    @staticmethod
    @lru_cache(maxsize=4096)
    def _cell_width(character):
        # Match the renderer, including visible tab/control representations.
        return Char(character, '').width

    def _line_starts(self, row):
        if row not in self._heights:
            # Summed widths undercount rows when a wide glyph wraps with one
            # cell left. Match the renderer and retain character anchors.
            assert self._content is not None
            prefix = fragment_list_to_text(to_formatted_text(self.get_line_prefix(row, 0))) if self.get_line_prefix else ''
            prefix_width = sum(self._cell_width(c) for c in prefix)
            column, index, starts = prefix_width, 0, [0]
            for _, text, *_ in self._content.get_line(row):
                for character in text:
                    width = self._cell_width(character)
                    if column + width > self._width:
                        starts.append(index)
                        column = prefix_width
                    column += width
                    index += 1
            self._heights[row] = starts
        return self._heights[row]

    def _line_height(self, row):
        return len(self._line_starts(row))

    def _move(self, position, delta):
        row, offset = position
        offset += delta
        while offset < 0 and row > 0:
            row -= 1
            offset += self._line_height(row)
        while row < self._content.line_count - 1 and offset >= self._line_height(row):
            offset -= self._line_height(row)
            row += 1
        return row, max(0, min(offset, self._line_height(row) - 1))

    def _bottom(self):
        last = self._content.line_count - 1
        return self._move((last, self._line_height(last) - 1), -(self._height - 1))

    def _scroll(self, ui_content, width, height):
        anchor = None
        row = self.vertical_scroll
        if not self.owner.follow_output and self._content is not None:
            starts = self._line_starts(min(row, self._content.line_count - 1))
            anchor = starts[min(self.vertical_scroll_2, len(starts) - 1)]
            # The old rendered row belongs to an entry, not a permanent buffer
            # index. Eviction (and its notice) shifts all surviving entries.
            for entry, start, removed in reversed(self._entry_starts):
                if start <= row:
                    new_start = next((line for item, line, _ in self.owner._entry_starts
                                      if item is entry), None)
                    if new_start is None:
                        # The reader's entry expired: stay paused at the oldest
                        # retained output instead of jumping towards the tail.
                        row, anchor = 0, 0
                    else:
                        delta = entry.get('_text_offset', 0) - removed
                        if delta and row > start:
                            # Offsets count characters, including newlines and the
                            # inserted marker, not logical lines or display cells.
                            offset = (self._document.translate_row_col_to_index(row, anchor)
                                      - self._document.translate_row_col_to_index(start + 1, 0)
                                      - delta)
                            if offset < len(_TRUNCATION_MARKER):
                                row, anchor = 0, 0
                            else:
                                document = self.owner.transcript.buffer.document
                                index = document.translate_row_col_to_index(new_start + 1, 0) + offset
                                row, anchor = document.translate_index_to_position(index)
                        else:
                            row += new_start - start
                    break
        self._content = ui_content
        self._document = self.owner.transcript.buffer.document
        self._entry_starts = self.owner._entry_starts
        self._heights.clear()
        self._width, self._height = max(1, width), max(1, height)
        self.vertical_scroll = min(row, ui_content.line_count - 1)
        if anchor is not None:
            starts = self._line_starts(self.vertical_scroll)
            self.vertical_scroll_2 = max(0, bisect_right(starts, anchor) - 1)
        self.horizontal_scroll = 0
        bottom = self._bottom()
        if self.owner.follow_output:
            position = bottom
        else:
            row = min(self.vertical_scroll, ui_content.line_count - 1)
            position = min((row, min(self.vertical_scroll_2, self._line_height(row) - 1)), bottom)
        self.vertical_scroll, self.vertical_scroll_2 = position

    def scroll_rows(self, amount):
        if self._content is None:
            return
        position = self._move((self.vertical_scroll, self.vertical_scroll_2), amount)
        bottom = self._bottom()
        if amount > 0 and position >= bottom:
            self.owner.return_to_latest()
        else:
            self.owner.follow_output = False
            self.vertical_scroll, self.vertical_scroll_2 = min(position, bottom)
            # Cursor is only a selection/navigation aid, never the follow state.
            document = self.owner.transcript.buffer.document
            self.owner.transcript.buffer.cursor_position = document.translate_row_col_to_index(
                self.vertical_scroll, min(len(document.lines[self.vertical_scroll]),
                                          self._line_starts(self.vertical_scroll)[self.vertical_scroll_2]))
        self.owner.application.invalidate()

    def _scroll_up(self):
        self.scroll_rows(-3)

    def _scroll_down(self):
        self.scroll_rows(3)


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
        self._entry_starts = []
        self._line_prefixes = []
        self._trimmed = False
        self.changes = []
        self.show_tool_details = False
        self._stream_index = None
        self.follow_output = True
        self.unseen_output = False
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
        self.transcript.window = TranscriptWindow(
            self, content=self.transcript.control, wrap_lines=True,
            right_margins=self.transcript.window.right_margins,
            get_line_prefix=self._transcript_prefix, height=Dimension(min=1))
        self.completer = WorkspaceCompleter(controller)
        self.composer = TextArea(multiline=True, wrap_lines=True,
                                 completer=self.completer, complete_while_typing=False,
                                 height=Dimension(min=1, preferred=height, max=height))
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
            if self.application.layout.has_focus(self.approval_panel):
                scroll_page_up(event)
            else:
                window = self.transcript.window
                window.scroll_rows(-max(1, window._height - 1))

        @bindings.add('pagedown')
        def page_down(event):
            if self.application.layout.has_focus(self.approval_panel):
                scroll_page_down(event)
            else:
                window = self.transcript.window
                window.scroll_rows(max(1, window._height - 1))

        @bindings.add('c-end')
        def latest(event):
            self.return_to_latest()

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
                    Frame(self.approval_panel, title='Approval · yes / no · Ctrl+Y / Ctrl+N',
                          style='class:approval'),
                    filter=Condition(lambda: self.approval is not None)),
                ConditionalContainer(
                    Window(FormattedTextControl(self.changes_text), height=1, style='class:changes'),
                    Condition(lambda: bool(self.changes))),
                Frame(self.composer, title='Message · YOU / 나 · Enter send'),
                Window(FormattedTextControl(self.status_text), height=1, style='class:status'),
                Window(FormattedTextControl(
                    '  Alt+Enter newline · PgUp history · Ctrl+C stop · Ctrl+Q quit · /help'),
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
        # Show identity, not a potentially screen-wide absolute Windows path.
        workspace = str(self.controller.workspace).replace('\\', '/').rstrip('/').rsplit('/', 1)[-1]
        workspace = ' '.join(safe_text(workspace).split())
        model = ' '.join(safe_text(self.controller.model_name).split())
        session = ' '.join(safe_text(self.session_id).split())
        return f'  DAVID  /  {workspace}  ·  {model}  ·  {session}'

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
        paused = ('Paused · ' + ('new output · ' if self.unseen_output else '') + 'Ctrl+End latest | ') if not self.follow_output else ''
        detail = (self.detail + ' | ') if self.detail else ''
        storage = 'saving ON (평문)' if self.saving else '저장 OFF'
        return safe_text('  ' + paused + f'{self.status} | ' + detail + storage)

    def return_to_latest(self):
        self.follow_output = True
        self.unseen_output = False
        self.transcript.buffer.cursor_position = len(self.transcript.text)
        self.application.invalidate()

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
            self.return_to_latest()
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
        # Keep keyboard/cancellation responsive during a burst. Preserve FIFO,
        # including approvals and resets; never discard control events.
        for _ in range(128):
            try:
                event = self._events.get_nowait()
            except Empty:
                break
            changed = True
            kind = event.get('type')
            if kind == 'reset':
                self.follow_output = True
                self.unseen_output = False
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
                    self._entries[self._stream_index]['_text_offset'] = 0
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
        if not self._events.empty():
            self.application.invalidate()

    def _transcript_prefix(self, line, wrap_count):
        if line < len(self._line_prefixes):
            return [self._line_prefixes[line]]
        return [('', '  ')]

    def _render_transcript(self):
        # Bound both retained data and rendering cost on long coding sessions.
        for entry in self._entries:
            for key in ('text', 'details'):
                if len(entry.get(key, '')) > 100000:
                    if key == 'text':
                        entry['_text_offset'] = (entry.get('_text_offset', 0)
                                                 + len(entry[key]) - 99980 - len(_TRUNCATION_MARKER))
                    entry[key] = _TRUNCATION_MARKER + entry[key][-99980:]
                    self._trimmed = True
        size = sum(len(e.get('text', '')) + len(e.get('details', '')) + 100 for e in self._entries)
        while self._entries and (size > 200000 or len(self._entries) > 300):
            removed = self._entries.pop(0)
            size -= len(removed.get('text', '')) + len(removed.get('details', '')) + 100
            if self._stream_index is not None:
                self._stream_index = self._stream_index - 1 if self._stream_index else None
            self._trimmed = True
        lines, styles, prefixes, entry_starts = [], [], [], []

        def add(text, style='', prefix=('', '  ')):
            lines.append(text)
            styles.append(style)
            prefixes.append(prefix)

        if self._trimmed:
            add('[화면 기록 한도로 오래된 출력 생략 · 저장 세션과 모델 컨텍스트 한도는 별도]',
                'class:body.system')
        for entry in self._entries:
            if lines:
                add('')
            entry_starts.append((entry, len(lines), entry.get('_text_offset', 0)))
            role = entry['role']
            category = role if role in ('user', 'assistant') else 'tool' if role.startswith('tool ') else 'system'
            label = {'user': 'YOU / 나', 'assistant': 'DAVID', 'system': 'NOTICE / 안내',
                     'summary': 'TURN / 요약'}.get(role, 'TOOL · ' + role[5:] if category == 'tool' else role.upper())
            add(label, 'class:role.' + category)
            text = entry['text']
            if 'details' in entry:
                text += ('\n' + entry['details']) if self.show_tool_details else ' [Ctrl+O details]'
            in_code = False
            for body_line in text.split('\n'):
                style = 'class:body.' + category
                if category == 'assistant':
                    if body_line.lstrip().startswith(('```', '~~~')):
                        style = 'class:fence'
                        in_code = not in_code
                    elif in_code:
                        style = 'class:code'
                    elif body_line.startswith(('# ', '## ', '### ', '#### ')):
                        style = 'class:heading'
                add(body_line, style, ('class:rail.' + category, '  │ '))
        text = '\n'.join(lines)
        self._line_prefixes = prefixes
        self._entry_starts = entry_starts
        self.transcript.lexer = TranscriptLexer(styles)
        buffer = self.transcript.buffer
        if not self.follow_output and text != buffer.text:
            self.unseen_output = True
        cursor = len(text) if self.follow_output else min(buffer.cursor_position, len(text))
        buffer.set_document(Document(text, cursor), bypass_readonly=True)


def run_tui(controller, input=None, output=None) -> int:
    """Run on the calling thread; model/tool operations belong to the controller."""
    ui = DavidTUI(controller, input=input, output=output)
    return ui.application.run()
