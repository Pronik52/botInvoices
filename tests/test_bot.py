import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from telegram.error import TimedOut

try:
    from invoice_maker.bot import BotController
except ImportError:
    BotController = None


def update(user=12, text='/start', callback=None, chat_type='private'):
    message = SimpleNamespace(text=text, reply_text=AsyncMock(), reply_document=AsyncMock())
    return SimpleNamespace(effective_user=SimpleNamespace(id=user), effective_chat=SimpleNamespace(id=user, type=chat_type), effective_message=message,
                           callback_query=SimpleNamespace(data=callback, answer=AsyncMock()) if callback else None)


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertIsNotNone(BotController, 'Telegram controller is missing')

    async def test_unknown_messages_never_reach_private_state(self):
        controller = BotController(12, None, None, None)
        foreign = update(user=99)
        await controller.on_text(foreign, None)
        foreign.effective_message.reply_text.assert_not_called()
        group = update(chat_type='group')
        await controller.on_text(group, None)
        group.effective_message.reply_text.assert_not_called()

    async def test_unknown_callback_never_accesses_documents(self):
        controller = BotController(12, None, None, None)
        foreign = update(user=99, callback='download:1')
        await controller.on_callback(foreign, None)
        foreign.effective_message.reply_document.assert_not_called()

    async def test_sheet_menu_reflects_live_ids_and_names(self):
        class Source:
            def list_sheets(self):
                return [{'sheetId': 777, 'title': 'Новый месяц'}, {'sheetId': 0, 'title': 'Январь 26'}]
        class Workflow:
            def clear(self):
                pass
        controller = BotController(12, None, Source(), Workflow())
        target = update(callback='sheets')
        await controller.on_callback(target, None)
        call = target.effective_message.reply_text.call_args
        buttons = call.kwargs['reply_markup'].inline_keyboard
        self.assertEqual(buttons[0][0].text, 'Новый месяц')
        self.assertEqual(buttons[0][0].callback_data, 'select:777')
        self.assertEqual(buttons[1][0].callback_data, 'select:0')

    async def test_months_beyond_first_page_remain_accessible(self):
        class Source:
            def list_sheets(self):
                return [{'sheetId': i, 'title': f'Месяц {i}'} for i in range(100)]
        class Workflow:
            def clear(self):
                pass
        controller = BotController(12, None, Source(), Workflow())
        target = update(callback='sheets:4')
        await controller.on_callback(target, None)
        buttons = target.effective_message.reply_text.call_args.kwargs['reply_markup'].inline_keyboard
        values = [button.callback_data for row in buttons for button in row]
        self.assertIn('select:99', values)
        self.assertIn('sheets:3', values)

    async def test_timeout_sending_act_still_attempts_invoice_and_offers_individual_downloads(self):
        record = {'id': 8, 'number': 10,
                  'payload': {'month_title': 'Октябрь 26', 'date': '2026-10-30', 'total': '45000.00'},
                  'act': b'act-file', 'invoice': b'invoice-file'}
        target = update(callback='download:8')
        target.effective_message.reply_document.side_effect = [TimedOut(), None]
        controller = BotController(12, None, None, None)
        await controller.send_documents(target, record)
        calls = target.effective_message.reply_document.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].kwargs['document'].getvalue(), b'act-file')
        self.assertEqual(calls[1].kwargs['document'].getvalue(), b'invoice-file')
        self.assertGreaterEqual(calls[1].kwargs['write_timeout'], 60)
        message = target.effective_message.reply_text.call_args
        self.assertIn('проверьте', message.args[0].lower())
        callbacks = [button.callback_data for row in message.kwargs['reply_markup'].inline_keyboard for button in row]
        self.assertIn('download:8:act', callbacks)
        self.assertIn('download:8:invoice', callbacks)

    async def test_archive_can_send_only_invoice_without_reissuing_documents(self):
        record = {'id': 8, 'number': 10,
                  'payload': {'month_title': 'Октябрь 26', 'date': '2026-10-30', 'total': '45000.00'},
                  'act': b'act-file', 'invoice': b'invoice-file'}
        class Store:
            def archive(self, limit, offset):
                return [dict(id=record['id'], number=record['number'], payload=record['payload'])]
            def get_version(self, version_id):
                return record if version_id == 8 else None
        class Workflow:
            def clear(self):
                pass
        controller = BotController(12, Store(), None, Workflow())
        menu = update(callback='archive:0')
        await controller.on_callback(menu, None)
        callbacks = [button.callback_data for row in menu.effective_message.reply_text.call_args.kwargs['reply_markup'].inline_keyboard for button in row]
        self.assertIn('download:8:invoice', callbacks)
        target = update(callback='download:8:invoice')
        await controller.on_callback(target, None)
        calls = target.effective_message.reply_document.call_args_list
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].kwargs['document'].getvalue(), b'invoice-file')
        self.assertIn('Счёт 10 Октябрь 26.xlsx', calls[0].kwargs['filename'])


if __name__ == '__main__':
    unittest.main()
