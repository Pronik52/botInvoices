"""Private Russian-language Telegram dialogs."""
import asyncio
import re
from datetime import datetime
from io import BytesIO

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .domain import clean, money
from .workflow import PreviewChanged, is_owner

FIELDS = {
    'name': 'Наименование / ФИО', 'short_name': 'Подпись: короткое ФИО', 'inn': 'ИНН',
    'kpp': 'КПП', 'phone': 'Телефон', 'address': 'Адрес', 'account': 'Расчётный счёт',
    'bank': 'Банк', 'bik': 'БИК', 'correspondent': 'Корреспондентский счёт',
}
HOME = [[('Создать акт и счёт', 'sheets')], [('Архив документов', 'archive:0')], [('Настройки', 'settings')]]
CANCEL = [[('Отмена', 'home')]]


def date_text(value):
    return datetime.strptime(value, '%Y-%m-%d').strftime('%d.%m.%Y')


def input_date(value):
    for fmt in ('%d.%m.%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError('Введите дату в формате ДД.ММ.ГГГГ, например 10.10.2026.')


class BotController:
    def __init__(self, owner_id, store, source, workflow):
        self.owner_id, self.store, self.source, self.workflow = owner_id, store, source, workflow
        self.dialog = {}

    def allowed(self, update):
        user, chat = update.effective_user, update.effective_chat
        return is_owner(user.id if user else None, chat.id if chat else None, chat.type if chat else None, self.owner_id)

    async def say(self, update, text, buttons=None):
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in buttons]) if buttons else None
        await update.effective_message.reply_text(text, reply_markup=markup)

    async def home(self, update):
        self.dialog.clear()
        self.workflow.clear()
        await self.say(update, 'Выберите месяц — подготовлю акт и счёт в Excel по Google-таблице.\nПеред созданием покажу сумму, договор и даты.', HOME)

    async def show_sheets(self, update, page=0):
        self.dialog.clear()
        self.workflow.clear()
        sheets = await asyncio.to_thread(self.source.list_sheets)
        page = min(max(0, page), max(0, (len(sheets)-1)//20))
        buttons = [[(s['title'][:60], f"select:{s['sheetId']}")] for s in sheets[page*20:(page+1)*20]]
        navigation = []
        if page:
            navigation.append(('Назад', f'sheets:{page-1}'))
        if len(sheets) > (page+1)*20:
            navigation.append(('Далее', f'sheets:{page+1}'))
        if navigation:
            buttons.append(navigation)
        buttons += [[('Обновить список', 'sheets'), ('Главное меню', 'home')]]
        await self.say(update, 'Выберите лист Google-таблицы:' if sheets else 'В таблице нет доступных листов.', buttons)

    async def show_preview(self, update, preview):
        payload = preview['payload']
        contract = payload['contract']
        text = (f"{payload['month_title']}\nРабот: {len(payload['rows'])}\nЧасов: {payload['hours']}\n"
                f"Сумма: {money(payload['total'])} ₽\n\nАкт и счёт №{preview['number']}\n"
                f"Дата документов: {date_text(payload['date'])}\nОплатить до: {date_text(payload['due_date'])}\n"
                f"Договор: {contract['number']} от {date_text(contract['date'])}")
        if preview['existing_id']:
            text += '\n\nЗа этот месяц уже есть документы. Можно скачать их или обновить с текущей датой, сохранив номер.'
        if payload['due_date'] < payload['date']:
            text += '\n\nСрок оплаты уже прошёл — измените его перед созданием.'
        token = preview['token']
        buttons = [[('Обновить документы' if preview['existing_id'] else 'Создать два Excel-файла', f'confirm:{token}')],
                   [('Изменить срок оплаты', f'due:{token}')]]
        if preview['existing_id']:
            buttons.append([('Скачать ранее созданные', f"download:{preview['existing_id']}")])
        buttons += [[('Выбрать другой месяц', 'sheets')], [('Настройки', 'settings')]]
        await self.say(update, text, buttons)

    async def send_documents(self, update, record):
        if not record:
            raise ValueError('Документы не найдены в архиве.')
        payload = record['payload']
        title = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', payload['month_title'])[:60]
        for key, label in [('act', 'Акт'), ('invoice', 'Счёт')]:
            await update.effective_message.reply_document(
                document=BytesIO(record[key]), filename=f"{label} {record['number']} {title}.xlsx",
                caption=f"{label} №{record['number']} от {date_text(payload['date'])} · {money(payload['total'])} ₽")
        await self.say(update, 'Документы сохранены в архиве. Повторное скачивание не меняет номер и дату.', HOME)

    async def settings_menu(self, update):
        self.dialog.clear()
        self.workflow.clear()
        settings, contract = self.store.settings(), self.store.default_contract()
        current = f"{contract['number']} от {date_text(contract['date'])}" if contract else 'не выбран'
        await self.say(update, f"Настройки\n\nДоговор: {current}\nСледующий номер: {settings['next_number']}\n"
                       f"Оплата по умолчанию: до {settings['due_day']}-го числа\nУслуга в счёте: {settings['service_name']}",
                       [[('Договоры', 'contracts:0')], [('Исполнитель', 'party:supplier'), ('Компания', 'party:customer')],
                        [('Следующий номер', 'set:next_number'), ('День оплаты', 'set:due_day')],
                        [('Услуга в счёте', 'set:service_name')], [('Главное меню', 'home')]])

    async def party_menu(self, update, party):
        if party not in ('supplier', 'customer'):
            raise ValueError('Неизвестный раздел настроек.')
        values = self.store.settings()[party]
        text = ('Исполнитель' if party == 'supplier' else 'Компания') + '\n\n'
        text += '\n'.join(f'{FIELDS[key]}: {value or "—"}' for key, value in values.items())
        buttons = [[(FIELDS[key], f'field:{party}:{key}')] for key in values]
        buttons.append([('Назад', 'settings')])
        await self.say(update, text[:4000], buttons)

    async def contracts_menu(self, update, page=0):
        self.dialog.clear()
        self.workflow.clear()
        contracts, current = self.store.contracts(), self.store.default_contract()
        buttons = [[(('✓ ' if current and c['id'] == current['id'] else '') + f"{c['number']} от {date_text(c['date'])}", f"contract:{c['id']}")] for c in contracts[page*10:(page+1)*10]]
        navigation = []
        if page:
            navigation.append(('Назад', f'contracts:{page-1}'))
        if len(contracts) > (page+1)*10:
            navigation.append(('Далее', f'contracts:{page+1}'))
        if navigation:
            buttons.append(navigation)
        buttons += [[('Добавить договор', 'add_contract')], [('Автоматически: последний по дате', 'auto_contract')], [('Настройки', 'settings')]]
        await self.say(update, 'Договоры\n\nНажмите договор, чтобы сделать его основным. Ручной выбор сохраняется при добавлении новых договоров.', buttons)

    async def archive_menu(self, update, page=0):
        self.dialog.clear()
        self.workflow.clear()
        records = self.store.archive(limit=11, offset=page*10)
        buttons = [[(f"№{r['number']} · {r['payload']['month_title']} · {date_text(r['payload']['date'])}", f"download:{r['id']}")] for r in records[:10]]
        navigation = []
        if page:
            navigation.append(('Назад', f'archive:{page-1}'))
        if len(records) > 10:
            navigation.append(('Далее', f'archive:{page+1}'))
        if navigation:
            buttons.append(navigation)
        buttons.append([('Главное меню', 'home')])
        await self.say(update, 'Архив: последние версии документов.' if records else 'Архив пока пуст. Создайте документы за нужный месяц.', buttons)

    async def on_text(self, update, context):
        if not self.allowed(update):
            return
        text = (update.effective_message.text or '').strip()
        try:
            if text.split(' ', 1)[0] in ('/start', '/help', '/cancel'):
                await self.home(update)
            elif text == '/settings':
                await self.settings_menu(update)
            elif self.dialog:
                await self.handle_dialog(update, text)
            else:
                await self.home(update)
        except ValueError as error:
            await self.say(update, str(error), CANCEL)

    async def handle_dialog(self, update, text):
        if len(text) > 1500:
            raise ValueError('Слишком длинное значение. Ограничение — 1500 символов.')
        mode = self.dialog['mode']
        if mode == 'due':
            preview = self.workflow.change_due(self.dialog['token'], input_date(text))
            self.dialog.clear()
            await self.show_preview(update, preview)
        elif mode == 'contract_number':
            if not text or len(text) > 100:
                raise ValueError('Введите номер договора, до 100 символов.')
            self.dialog = {'mode': 'contract_date', 'number': clean(text)}
            await self.say(update, 'Введите дату договора в формате ДД.ММ.ГГГГ.', CANCEL)
        elif mode == 'contract_date':
            value = input_date(text)
            try:
                self.store.add_contract(self.dialog['number'], value)
            except ValueError:
                raise ValueError('Договор с этим номером и датой уже добавлен.') from None
            await self.contracts_menu(update)
        elif mode == 'scalar':
            key = self.dialog['key']
            value = clean(text)
            if key in ('next_number', 'due_day'):
                if not text.isascii() or not text.isdigit() or int(text) < 1:
                    raise ValueError('Введите положительное целое число.')
                value = int(text)
                if key == 'due_day' and value > 31:
                    raise ValueError('День оплаты должен быть от 1 до 31.')
            elif not value:
                raise ValueError('Значение не может быть пустым.')
            self.store.set_setting(key, value)
            await self.settings_menu(update)
        elif mode == 'party':
            party, key = self.dialog['party'], self.dialog['key']
            value = '' if text == '-' else clean(text)
            required = {'name', 'inn', 'account', 'bank', 'bik', 'correspondent'} if party == 'supplier' else {'name', 'inn'}
            if key in required and not value:
                raise ValueError('Это поле обязательно для документов.')
            values = self.store.settings()[party]
            values[key] = value
            self.store.set_setting(party, values)
            self.dialog.clear()
            await self.party_menu(update, party)

    async def on_callback(self, update, context):
        query = update.callback_query
        if not self.allowed(update):
            await query.answer('Нет доступа.', show_alert=True)
            return
        await query.answer()
        command, _, argument = (query.data or '').partition(':')
        try:
            if command == 'home':
                await self.home(update)
            elif command == 'sheets':
                await self.show_sheets(update, max(0, int(argument or 0)))
            elif command == 'select':
                self.dialog.clear()
                await self.say(update, 'Читаю данные выбранного месяца…')
                await self.show_preview(update, await asyncio.to_thread(self.workflow.preview, int(argument)))
            elif command == 'confirm':
                self.dialog.clear()
                await self.say(update, 'Проверяю актуальность данных и формирую документы…')
                await self.send_documents(update, await asyncio.to_thread(self.workflow.confirm, argument))
            elif command == 'due':
                self.workflow.get_preview(argument)
                self.dialog = {'mode': 'due', 'token': argument}
                await self.say(update, 'Введите срок оплаты в формате ДД.ММ.ГГГГ.', CANCEL)
            elif command == 'download':
                self.dialog.clear()
                await self.send_documents(update, self.store.get_version(int(argument)))
            elif command == 'settings':
                await self.settings_menu(update)
            elif command == 'party':
                self.dialog.clear()
                self.workflow.clear()
                await self.party_menu(update, argument)
            elif command == 'field':
                self.workflow.clear()
                party, key = argument.split(':', 1)
                values = self.store.settings()[party] if party in ('supplier', 'customer') else {}
                if key not in values:
                    raise ValueError('Неизвестное поле.')
                self.dialog = {'mode': 'party', 'party': party, 'key': key}
                await self.say(update, f'{FIELDS[key]}\nСейчас: {values[key] or "—"}\n\nВведите новое значение. Для необязательного пустого поля отправьте «-».', CANCEL)
            elif command == 'set':
                self.workflow.clear()
                prompts = {'next_number': 'Введите следующий порядковый номер акта и счёта.', 'due_day': 'Введите день оплаты по умолчанию, от 1 до 31.', 'service_name': 'Введите наименование общей услуги для счёта.'}
                if argument not in prompts:
                    raise ValueError('Неизвестная настройка.')
                self.dialog = {'mode': 'scalar', 'key': argument}
                await self.say(update, prompts[argument], CANCEL)
            elif command == 'contracts':
                await self.contracts_menu(update, max(0, int(argument or 0)))
            elif command == 'contract':
                self.store.set_default_contract(int(argument))
                await self.contracts_menu(update)
            elif command == 'auto_contract':
                self.store.set_default_contract(None)
                await self.contracts_menu(update)
            elif command == 'add_contract':
                self.workflow.clear()
                self.dialog = {'mode': 'contract_number'}
                await self.say(update, 'Введите номер нового договора.', CANCEL)
            elif command == 'archive':
                await self.archive_menu(update, max(0, int(argument or 0)))
            else:
                await self.say(update, 'Эта кнопка больше не поддерживается.', HOME)
        except PreviewChanged as error:
            await self.say(update, str(error))
            await self.show_preview(update, await asyncio.to_thread(self.workflow.preview, error.sheet_id))
        except ValueError as error:
            await self.say(update, str(error), [[('Выбрать месяц', 'sheets')], [('Настройки', 'settings')]])
