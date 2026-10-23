"""Persisted display events must be validated before publishing a resumed session."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from david.controller import Controller
from david.sessions import SessionError, _descriptor_backend
from david.tui import DavidTUI


class SessionResumeValidationTests(unittest.TestCase):
    def test_native_json_answer_survives_autosave_resume_and_continuation(self):
        from test_agent import FakeClient
        example = '{"type":"tool","name":"example","arguments":{}}'
        for portable, mode in ((False, 'native'), (True, 'native'), (False, 'json'), (True, 'json')):
            answer = example if mode == 'native' else json.dumps({'type': 'final', 'content': example})
            with self.subTest(portable=portable, mode=mode), tempfile.TemporaryDirectory() as tmp:
                with patch('david.sessions._descriptor_backend',
                           return_value=not portable and _descriptor_backend()):
                    root = Path(tmp).resolve()
                    workspace = root / 'workspace'
                    workspace.mkdir()
                    config = root / 'config.json'
                    config.write_text(json.dumps({'model': {'name': 'fixture'},
                                                  'agent': {'tool_mode': mode}}))
                    events = []
                    controller = Controller(config, workspace, session_dir=root / 'sessions',
                                            on_event=events.append)
                    self.addCleanup(controller.close)
                    controller.agent.client = FakeClient({'role': 'assistant', 'content': answer})
                    for prompt in ('/save on', 'Show a JSON tool example'):
                        self.assertTrue(controller.submit(prompt))
                        self.assertTrue(controller.wait(3))
                    self.assertEqual(controller.agent.history[-1][-1]['content'], answer)
                    self.assertTrue(controller.saving, events)
                    ident = controller.session_id
                    saved = controller._store().load(ident, workspace, controller._model_key())
                    self.assertEqual(saved['tool_mode'], mode)
                    self.assertEqual(saved['history'], controller.agent.history)
                    resumed = Controller(config, workspace, session_dir=root / 'sessions',
                                         on_event=events.append)
                    self.addCleanup(resumed.close)
                    resumed.agent.client = FakeClient({'role': 'assistant', 'content': answer})
                    self.assertTrue(resumed.submit('/resume ' + ident))
                    self.assertTrue(resumed.wait(3))
                    self.assertEqual(resumed.agent.history, saved['history'])
                    self.assertFalse(resumed.saving, 'resume must retain opt-in saving')
                    for prompt in ('/save on', 'Another example'):
                        self.assertTrue(resumed.submit(prompt))
                        self.assertTrue(resumed.wait(3))
                    self.assertTrue(resumed.saving, events)
                    self.assertIn({'role': 'assistant', 'content': answer},
                                  resumed.agent.client.requests[0])
                    continued = resumed._store().load(ident, workspace, resumed._model_key())
                    self.assertEqual(continued['history'], resumed.agent.history)
                    self.assertEqual(len(continued['history']), 2)
                    self.assertFalse(any(e.get('status') == 'error' for e in events), events)

    def test_resume_rejects_payload_mode_mismatch_even_with_current_model_key(self):
        for portable in (False, True):
            for active_mode, saved_mode in (('json', 'native'), ('native', 'json')):
                with self.subTest(portable=portable, mode=active_mode), tempfile.TemporaryDirectory() as tmp:
                    with patch('david.sessions._descriptor_backend',
                               return_value=not portable and _descriptor_backend()):
                        root = Path(tmp).resolve()
                        workspace = root / 'workspace'
                        workspace.mkdir()
                        config = root / 'config.json'
                        config.write_text(json.dumps({'model': {'name': 'fixture'},
                                                      'agent': {'tool_mode': active_mode}}))
                        events = []
                        controller = Controller(config, workspace, session_dir=root / 'sessions',
                                                on_event=events.append)
                        self.addCleanup(controller.close)
                        controller.requests = ['keep active']
                        controller.saving = True
                        original_tools = controller.agent.tools
                        before = deepcopy((controller.agent.history, controller.requests,
                                           controller.transcript, controller.session_id, controller.saving))
                        ident = 'b' * 32
                        # A matching opaque fingerprint must not override the actual mode.
                        content = ('{"type":"tool","name":"example","arguments":{}}'
                                   if saved_mode == 'native' else '{"type":"final","content":"done"}')
                        controller._store().save(ident, workspace, controller._model_key(), {
                            'tool_mode': saved_mode,
                            'history': [[{'role': 'user', 'content': 'saved'},
                                         {'role': 'assistant', 'content': content}]],
                            'requests': ['saved'], 'journal': [], 'transcript': []})
                        with self.assertRaisesRegex(SessionError, 'tool mode'):
                            controller._resume(ident)
                        self.assertIs(controller.agent.tools, original_tools)
                        self.assertEqual((controller.agent.history, controller.requests,
                                          controller.transcript, controller.session_id, controller.saving), before)
                        self.assertEqual(events, [])

    def test_malformed_tool_result_cannot_replace_active_session(self):
        for portable in (False, True):
            with self.subTest(portable=portable), tempfile.TemporaryDirectory() as tmp:
                with patch('david.sessions._descriptor_backend',
                           return_value=not portable and _descriptor_backend()):
                    root = Path(tmp).resolve()
                    workspace = root / 'workspace'
                    workspace.mkdir()
                    config = root / 'config.json'
                    config.write_text(json.dumps({'model': {'name': 'fixture'}}))
                    controller = Controller(config, workspace, session_dir=root / 'sessions')
                    self.addCleanup(controller.close)
                    controller.agent.tools.approve = lambda action, detail: True
                    controller.agent.tools.begin_turn()
                    controller.agent.tools.execute('write_file', {'path': 'active.txt', 'content': 'keep'})
                    controller.agent.tools.end_turn()
                    controller.agent.history = [[{'role': 'user', 'content': 'active request'},
                                                 {'role': 'assistant', 'content': 'active answer'}]]
                    controller.requests = ['active request']
                    controller._message('active transcript')
                    controller.saving = True
                    original_tools = controller.agent.tools
                    before = deepcopy((controller.agent.history, controller.requests,
                                       controller.transcript, original_tools.export_journal(),
                                       controller.session_id, controller.saving))
                    ident = 'b' * 32
                    store = controller._store()
                    store.save(ident, workspace, controller._model_key(), {
                        'history': [], 'requests': ['saved request'], 'journal': [], 'transcript': []})
                    path = root / 'sessions' / (ident + '.json')
                    envelope = json.loads(path.read_text())
                    envelope['payload']['transcript'] = [{'type': 'tool', 'name': 'x', 'result': []}]
                    path.write_text(json.dumps(envelope))
                    tampered = path.read_bytes()
                    with create_pipe_input() as pipe:
                        ui = DavidTUI(controller, input=pipe, output=DummyOutput())
                        for event in controller.transcript:
                            ui.handle_event(event)
                        ui._drain_events(ui.application)
                        displayed = ui.transcript.text
                        events = []

                        def receive(event):
                            events.append(event)
                            ui.handle_event(event)

                        controller.on_event = receive
                        with self.assertRaisesRegex(SessionError, 'transcript'):
                            controller._resume(ident)
                        self.assertIs(controller.agent.tools, original_tools)
                        self.assertEqual((controller.agent.history, controller.requests,
                                          controller.transcript, original_tools.export_journal(),
                                          controller.session_id, controller.saving), before)
                        self.assertEqual(events, [], 'rejected resume must not publish reset or replay')
                        ui._drain_events(ui.application)
                        self.assertEqual(ui.transcript.text, displayed)
                        # Exercise the public worker/error-reporting path too:
                        # its diagnostic may append, but no saved state is published.
                        self.assertTrue(controller.submit('/resume ' + ident))
                        self.assertTrue(controller.wait(3))
                        ui._drain_events(ui.application)
                        self.assertEqual(ui.status, 'error')
                        self.assertIn('transcript', ui.detail)
                        self.assertIn('active transcript', ui.transcript.text)
                        self.assertFalse(any(e['type'] in ('reset', 'tool', 'session') for e in events))
                        self.assertIs(controller.agent.tools, original_tools)
                        self.assertEqual((controller.agent.history, controller.requests,
                                          controller.transcript[:len(before[2])],
                                          original_tools.export_journal(), controller.session_id,
                                          controller.saving), before)
                    self.assertEqual((workspace / 'active.txt').read_text(), 'keep')
                    self.assertEqual(path.read_bytes(), tampered)
