"""Load private server configuration and start one polling process."""
import json
import logging
import os
import re
from pathlib import Path

from telegram import BotCommand, BotCommandScopeChat
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from .bot import BotController
from .documents import generate_documents
from .sheets import SheetsSource
from .store import Store
from .workflow import Workflow


class SafeFormatter(logging.Formatter):
    def format(self, record):
        text = super().format(record)
        return re.sub(r'(?:bot)?\d{6,}:[A-Za-z0-9_-]{20,}', '[REDACTED]', text)


def main():
    os.umask(0o077)
    handler = logging.StreamHandler()
    handler.setFormatter(SafeFormatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)
    for name in ('httpx', 'httpcore', 'urllib3', 'google.auth'):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    logger = logging.getLogger('invoice_maker')
    logger.setLevel(logging.INFO)
    root = Path(__file__).resolve().parent.parent
    config = Path(os.environ.get('CONFIG_DIR', '/run/invoice-maker'))
    data = Path(os.environ.get('DATA_DIR', '/data'))
    defaults = json.loads((root / 'defaults.json').read_text())
    owner_id = int(json.loads((config / 'telegram-owner.json').read_text())['user_id'])
    token = (config / 'telegram-token').read_text().strip()
    store = Store(data / 'invoice-maker.sqlite', defaults)
    source = SheetsSource(config / 'google-service-account.json', store.settings()['spreadsheet_id'])
    workflow = Workflow(store, source, lambda payload: generate_documents(payload, root / 'templates'))
    controller = BotController(owner_id, store, source, workflow)

    async def post_init(app):
        identity = await app.bot.get_me()
        expected = os.environ.get('EXPECTED_BOT_USERNAME', '').strip().lstrip('@')
        if expected and identity.username.lower() != expected.lower():
            raise RuntimeError('Configured Telegram token belongs to another bot')
        await app.bot.set_my_commands([
            BotCommand('start', 'Главное меню'), BotCommand('settings', 'Договоры и реквизиты'),
            BotCommand('help', 'Как пользоваться'), BotCommand('cancel', 'Отменить ввод'),
        ], scope=BotCommandScopeChat(owner_id))
        logger.info('Started @%s; private access configured', identity.username)

    async def on_error(update, context):
        # Exception strings and tracebacks can contain request URLs with secrets.
        logger.error('Operation failed: %s', type(context.error).__name__)
        if update and controller.allowed(update) and update.effective_message:
            try:
                await update.effective_message.reply_text('Не удалось завершить операцию. Попробуйте ещё раз. Если файлы уже сформированы, скачайте их в архиве. Главное меню: /start')
            except Exception:
                logger.error('Unable to deliver error notification')

    app = Application.builder().token(token).concurrent_updates(False).post_init(post_init).build()
    app.add_handler(CommandHandler(['start', 'help', 'settings', 'cancel'], controller.on_text))
    app.add_handler(CallbackQueryHandler(controller.on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, controller.on_text))
    app.add_error_handler(on_error)
    app.run_polling(allowed_updates=['message', 'callback_query'], timeout=30, bootstrap_retries=3, drop_pending_updates=False)


if __name__ == '__main__':
    main()
