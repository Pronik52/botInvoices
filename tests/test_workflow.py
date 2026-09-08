import copy
import tempfile
import unittest
from pathlib import Path

try:
    from invoice_maker.workflow import Workflow, PreviewChanged, is_owner
except ImportError:
    Workflow = PreviewChanged = is_owner = None


class FakeSheets:
    spreadsheet_id = 'sheet'

    def __init__(self):
        self.data = {'title': 'Август 26', 'timezone': 'Europe/Moscow', 'rows': [{'name': 'Работа', 'link': '', 'hours': '18', 'rate': '2500', 'amount': '45000.00'}]}

    def fetch_sheet(self, sheet_id):
        return copy.deepcopy(self.data)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(Workflow, 'Confirmation workflow is missing')
        from invoice_maker.store import Store
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'test.sqlite', {'next_number': 9, 'due_day': 10, 'initial_contract': {'number': '3', 'date': '2026-07-01'}})
        self.source = FakeSheets()
        self.day = '2026-09-03'
        self.workflow = Workflow(self.store, self.source, lambda payload: (b'act', b'invoice'), today=lambda zone: self.day)

    def test_confirmation_assigns_once_and_preserves_archive_number(self):
        preview = self.workflow.preview(123)
        self.assertEqual(preview['payload']['total'], '45000.00')
        self.assertEqual(preview['payload']['due_date'], '2026-09-10')
        self.assertEqual(self.store.settings()['next_number'], 9)
        record = self.workflow.confirm(preview['token'])
        self.assertEqual(record['number'], 9)
        with self.assertRaises(ValueError):
            self.workflow.confirm(preview['token'])
        self.assertEqual(self.store.settings()['next_number'], 10)
        self.day = '2026-09-04'
        new = self.workflow.confirm(self.workflow.preview(123)['token'])
        self.assertEqual(new['number'], 9)
        self.assertEqual(self.store.get_version(record['id'])['payload']['date'], '2026-09-03')

    def test_source_changes_require_new_confirmation(self):
        preview = self.workflow.preview(123)
        self.source.data['rows'][0]['name'] = 'Исправленная работа'
        with self.assertRaises(PreviewChanged):
            self.workflow.confirm(preview['token'])
        self.assertEqual(self.store.settings()['next_number'], 9)

    def test_midnight_changes_require_new_confirmation(self):
        preview = self.workflow.preview(123)
        self.day = '2026-09-04'
        with self.assertRaises(PreviewChanged):
            self.workflow.confirm(preview['token'])

    def test_expired_keyboard_cannot_issue(self):
        preview = self.workflow.preview(123)
        self.workflow.pending[preview['token']]['expires'] = 0
        with self.assertRaises(ValueError):
            self.workflow.confirm(preview['token'])
        self.assertEqual(self.store.settings()['next_number'], 9)

    def test_overdue_default_requires_edit(self):
        self.day = '2026-09-11'
        preview = self.workflow.preview(123)
        with self.assertRaisesRegex(ValueError, 'срок'):
            self.workflow.confirm(preview['token'])
        self.workflow.change_due(preview['token'], '2026-09-20')
        record = self.workflow.confirm(preview['token'])
        self.assertEqual(record['payload']['due_date'], '2026-09-20')

    def test_owner_policy_rejects_foreign_user_and_group(self):
        self.assertTrue(is_owner(12, 12, 'private', 12))
        self.assertFalse(is_owner(13, 13, 'private', 12))
        self.assertFalse(is_owner(12, -100, 'group', 12))
        self.assertFalse(is_owner(None, None, None, 12))


if __name__ == '__main__':
    unittest.main()
