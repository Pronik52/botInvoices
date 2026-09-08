"""Preview and issuance decisions, separate from Telegram transport."""
import secrets
import threading
import time
from datetime import datetime, date
from zoneinfo import ZoneInfo

from .domain import default_due_date, fingerprint, totals


class PreviewChanged(ValueError):
    def __init__(self, sheet_id):
        super().__init__('Данные или дата изменились. Проверьте обновлённый итог и подтвердите ещё раз.')
        self.sheet_id = sheet_id


def is_owner(user_id, chat_id, chat_type, owner_id):
    return user_id == owner_id and chat_id == owner_id and chat_type == 'private'


class Workflow:
    def __init__(self, store, source, renderer, today=None):
        self.store, self.source, self.renderer = store, source, renderer
        self.today = today or (lambda zone: datetime.now(ZoneInfo(zone)).date().isoformat())
        self.pending = {}
        self.lock = threading.RLock()

    def clear(self):
        with self.lock:
            self.pending.clear()

    def preview(self, sheet_id):
        with self.lock:
            source = self.source.fetch_sheet(int(sheet_id))
            settings = self.store.settings()
            contract = self.store.default_contract()
            if not contract:
                raise ValueError('Добавьте договор в настройках.')
            issued = self.today(source['timezone'])
            hours, total = totals(source['rows'])
            key = f'{self.source.spreadsheet_id}:{int(sheet_id)}'
            old = self.store.latest_for_sheet(key)
            payload = {'sheet_key': key, 'month_title': source['title'], 'date': issued,
                       'due_date': default_due_date(issued, settings['due_day']), 'rows': source['rows'],
                       'hours': hours, 'total': total, 'contract': contract, 'settings': settings}
            token = secrets.token_hex(8)
            self.pending.clear()
            preview = {'token': token, 'sheet_id': int(sheet_id), 'payload': payload,
                       'number': old['number'] if old else settings['next_number'],
                       'existing_id': old['id'] if old else None,
                       'source_hash': fingerprint(source), 'settings_hash': fingerprint([settings, contract]),
                       'expires': time.monotonic() + 900}
            self.pending[token] = preview
            return preview

    def get_preview(self, token):
        preview = self.pending.get(token)
        if not preview or preview['expires'] < time.monotonic():
            self.pending.pop(token, None)
            raise ValueError('Эта кнопка устарела. Выберите месяц заново.')
        return preview

    def change_due(self, token, value):
        with self.lock:
            preview = self.get_preview(token)
            due = date.fromisoformat(value)
            if due < date.fromisoformat(preview['payload']['date']):
                raise ValueError('Срок оплаты не может быть раньше даты документов.')
            preview['payload']['due_date'] = due.isoformat()
            return preview

    def confirm(self, token):
        with self.lock:
            preview = self.get_preview(token)
            source = self.source.fetch_sheet(preview['sheet_id'])
            current_settings = [self.store.settings(), self.store.default_contract()]
            if (fingerprint(source) != preview['source_hash'] or
                    fingerprint(current_settings) != preview['settings_hash'] or
                    self.today(source['timezone']) != preview['payload']['date']):
                self.pending.pop(token)
                raise PreviewChanged(preview['sheet_id'])
            if preview['payload']['due_date'] < preview['payload']['date']:
                raise ValueError('Указанный срок оплаты уже прошёл. Нажмите «Изменить срок оплаты».')
            record = self.store.issue(preview['payload'], self.renderer)
            self.pending.pop(token, None)
            return record
