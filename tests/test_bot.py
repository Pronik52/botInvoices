import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

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


if __name__ == '__main__':
    unittest.main()
