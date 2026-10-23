"""Cross-module regression contracts for the daily coding workflow."""
import tempfile
import unittest
from pathlib import Path


class WorkflowRegressionTests(unittest.TestCase):
    def test_session_change_summary_survives_end_turn_and_removes_undone_paths(self):
        from david.workspace import WorkspaceTools
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            tools = WorkspaceTools(root, {}, {}, lambda *args: True)
            tools.begin_turn()
            tools.execute('write_file', {'path': 'new.txt', 'content': 'new'})
            tools.end_turn()
            self.assertEqual(tools.changes(), [])
            self.assertEqual([e['path'] for e in tools.changes(include_history=True)], ['new.txt'])
            tools.undo_last()
            self.assertEqual(tools.changes(include_history=True), [])
