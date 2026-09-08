"""Read spreadsheet business data without silently dropping incomplete work."""
import calendar
import hashlib
import json
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

HEADERS = ['Ссылка на задачу', 'Наименование', 'Количество часов', 'Стоимость часа', 'Сумма']
CENT = Decimal('0.01')


def clean(value):
    return ' '.join(str(value or '').split())


def number(value):
    if isinstance(value, bool):
        raise ValueError('ожидается число')
    try:
        result = Decimal(str(value).replace('\u00a0', '').replace(' ', '').replace(',', '.'))
        if not result.is_finite() or abs(result) > Decimal('1000000000000'):
            raise ValueError('число вне допустимого диапазона')
        return result
    except (InvalidOperation, TypeError):
        raise ValueError('ожидается число') from None


def plain(value):
    return format(Decimal(value), 'f').rstrip('0').rstrip('.') if '.' in format(Decimal(value), 'f') else str(value)


def money(value):
    return f'{Decimal(value):,.2f}'.replace(',', ' ').replace('.', ',')


def parse_rows(values):
    if not values:
        raise ValueError('Лист пуст. Добавьте работы в Google-таблицу.')
    header = [clean(v).lower() for v in values[0]]
    try:
        indexes = [header.index(h.lower()) for h in HEADERS]
        if len(set(header)) != len(header):
            raise ValueError()
    except ValueError:
        raise ValueError('Не найдены ожидаемые заголовки: ссылка, наименование, количество часов, стоимость часа, сумма.') from None
    comment_index = header.index('комментарий') if 'комментарий' in header else None
    result, errors = [], []
    for row_number, row in enumerate(values[1:], 2):
        link, name, hours, rate, amount = [row[i] if i < len(row) else '' for i in indexes]
        comment = clean(row[comment_index]) if comment_index is not None and comment_index < len(row) else ''
        if not clean(link) and not clean(name) and not comment and hours in ('', None, 0) and amount in ('', None, 0):
            continue
        try:
            if not clean(name):
                raise ValueError('не заполнено наименование работы')
            qty, price = number(hours), number(rate)
            if qty <= 0 or price <= 0:
                raise ValueError('часы и ставка должны быть больше нуля')
            computed = (qty * price).quantize(CENT, rounding=ROUND_HALF_UP)
            if amount not in ('', None) and number(amount).quantize(CENT, rounding=ROUND_HALF_UP) != computed:
                raise ValueError('сумма не совпадает с произведением часов и ставки')
            result.append({'source_row': row_number, 'link': clean(link), 'name': clean(name), 'comment': comment, 'hours': plain(qty), 'rate': plain(price), 'amount': str(computed)})
        except (ValueError, InvalidOperation) as error:
            errors.append(f'Строка {row_number}: {error}.')
    if errors:
        raise ValueError('\n'.join(errors[:6]) + (f'\nИ ещё ошибок: {len(errors)-6}.' if len(errors) > 6 else ''))
    if not result:
        raise ValueError('В этом месяце пока нет заполненных оплачиваемых работ.')
    return result


def totals(rows):
    return plain(sum((Decimal(r['hours']) for r in rows), Decimal(0))), str(sum((Decimal(r['amount']) for r in rows), Decimal(0)).quantize(CENT))


def default_due_date(issued_date, day):
    issued = date.fromisoformat(issued_date)
    return issued.replace(day=min(int(day), calendar.monthrange(issued.year, issued.month)[1])).isoformat()


def fingerprint(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
