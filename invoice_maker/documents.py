"""Generate the two documents by editing the supplied XLSX package in place.

Only XML parts that need changes are serialized. Media, image relationships,
printer settings, and all other native template parts retain their original bytes.
"""
from copy import deepcopy
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO
from pathlib import Path
import re
import textwrap
from xml.etree import ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED

S = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
P = 'http://schemas.openxmlformats.org/package/2006/relationships'
D = 'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing'
A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
NS = {'s': S, 'r': R, 'xdr': D, 'a': A}
CENT = Decimal('0.01')
MONTHS = ('января февраля марта апреля мая июня июля августа сентября октября ноября декабря').split()


def _tag(name):
    return f'{{{S}}}{name}'


def _xml(root, original):
    # Keep namespace declarations used by mc:Ignorable and mc:Choice/@Requires,
    # which ElementTree otherwise drops because their prefixes occur in values.
    for _, (prefix, uri) in ET.iterparse(BytesIO(original), events=('start-ns',)):
        if not re.fullmatch(r'ns\d+', prefix):
            ET.register_namespace(prefix, uri)
    ET.register_namespace('', root.tag.split('}', 1)[0][1:])
    result = ET.tostring(root, encoding='unicode')
    first = result.index('>')
    source_root = re.search(r'<(?!\?)[^>]+>', original.decode('utf-8')).group()
    missing = []
    for declaration, prefix in re.findall(r'(xmlns(?::([\w]+))?="[^"]*")', source_root):
        marker = 'xmlns' + (':'+prefix if prefix else '') + '='
        if marker not in result[:first]:
            missing.append(declaration)
    if missing:
        result = result[:first] + ' ' + ' '.join(missing) + result[first:]
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'+result).encode('utf-8')


def _declension(n, forms):
    return forms[2] if 11 <= n % 100 <= 14 else forms[0] if n % 10 == 1 else forms[1] if 2 <= n % 10 <= 4 else forms[2]


def _words(amount):
    amount = amount.quantize(CENT, rounding=ROUND_HALF_UP)
    number = int(amount)
    if number < 0 or number >= 10**15:
        raise ValueError('Сумма вне поддерживаемого диапазона для записи прописью')
    units = ('', 'один', 'два', 'три', 'четыре', 'пять', 'шесть', 'семь', 'восемь', 'девять')
    teens = ('десять одиннадцать двенадцать тринадцать четырнадцать пятнадцать шестнадцать семнадцать восемнадцать девятнадцать').split()
    tens = ('', '', 'двадцать', 'тридцать', 'сорок', 'пятьдесят', 'шестьдесят', 'семьдесят', 'восемьдесят', 'девяносто')
    hundreds = ('', 'сто', 'двести', 'триста', 'четыреста', 'пятьсот', 'шестьсот', 'семьсот', 'восемьсот', 'девятьсот')
    scales = (None, ('тысяча', 'тысячи', 'тысяч'), ('миллион', 'миллиона', 'миллионов'),
              ('миллиард', 'миллиарда', 'миллиардов'), ('триллион', 'триллиона', 'триллионов'))
    parts = []
    for power in range(4, -1, -1):
        group = number // 1000**power % 1000
        if not group:
            continue
        words = [hundreds[group // 100]]
        tail = group % 100
        if 10 <= tail <= 19:
            words.append(teens[tail - 10])
        else:
            words.append(tens[tail // 10])
            words.append(('одна' if tail % 10 == 1 else 'две') if power == 1 and tail % 10 in (1, 2) else units[tail % 10])
        if power:
            words.append(_declension(group, scales[power]))
        parts.extend(w for w in words if w)
    if not parts:
        parts.append('ноль')
    kopecks = int((amount - number) * 100)
    parts.extend((_declension(number, ('рубль', 'рубля', 'рублей')), f'{kopecks:02d}',
                  _declension(kopecks, ('копейка', 'копейки', 'копеек'))))
    return ' '.join(parts).capitalize()


def _date(value, long=False):
    d = date.fromisoformat(value)
    return f'{d.day:02d} {MONTHS[d.month-1]} {d.year} г.' if long else d.strftime('%d.%m.%Y')


def _money(value):
    return f'{value:.2f}'.replace('.', ',')


def _details(party, bank=False):
    result = [party['name']]
    for key, label in (('inn', 'ИНН '), ('kpp', 'КПП '), ('address', ''), ('phone', 'тел.: ')):
        if party.get(key):
            result.append(label + party[key])
    if bank:
        for key, label in (('account', 'р/с '), ('bank', 'банк '), ('bik', 'БИК '), ('correspondent', 'к/с ')):
            if party.get(key):
                result.append(label + party[key])
    return ', '.join(result)


class _Template:
    def __init__(self, path):
        with ZipFile(path) as archive:
            self.parts = {name: archive.read(name) for name in archive.namelist()}
        self.sheet = ET.fromstring(self.parts['xl/worksheets/sheet1.xml'])
        self.data = self.sheet.find('s:sheetData', NS)
        self.styles = ET.fromstring(self.parts['xl/styles.xml'])
        self.xfs = self.styles.find('s:cellXfs', NS)
        self.style_cache = {}
        self.old_heights = self._heights()
        self.delta = 0
        self.start = 0
        self.end = 0
        self._index()

    def _heights(self):
        default = float(self.sheet.find('s:sheetFormatPr', NS).get('defaultRowHeight', '15'))
        heights = {int(row.get('r')): float(row.get('ht', default)) for row in self.data}
        return default, heights

    def _index(self):
        self.rows = {int(row.get('r')): row for row in self.data}
        self.cells = {cell.get('r'): cell for row in self.data for cell in row}

    def replace_rows(self, start, end, count):
        self.start, self.end, self.delta = start, end, count - (end-start+1)
        prototype = deepcopy(self.rows[start])
        new_rows = []
        for old in list(self.data):
            rownum = int(old.get('r'))
            if start <= rownum <= end:
                continue
            if rownum > end:
                old.set('r', str(rownum+self.delta))
                for cell in old:
                    cell.set('r', self.shift(cell.get('r')))
                    formula = cell.find('s:f', NS)
                    if formula is not None and formula.text:
                        formula.text = self.shift(formula.text)
            new_rows.append(old)
        for rownum in range(start, start+count):
            row = deepcopy(prototype)
            row.set('r', str(rownum))
            for cell in row:
                cell.set('r', re.sub(r'\d+$', str(rownum), cell.get('r')))
                # The values and formulas are populated from validated payload.
                for child in list(cell):
                    cell.remove(child)
                cell.attrib.pop('t', None)
            new_rows.append(row)
        self.data[:] = sorted(new_rows, key=lambda r: int(r.get('r')))
        merges = self.sheet.find('s:mergeCells', NS)
        for merge in list(merges):
            nums = [int(n) for n in re.findall(r'\d+', merge.get('ref'))]
            if min(nums) >= start and max(nums) <= end:
                merges.remove(merge)
                for i in range(count):
                    ref = re.sub(r'\d+', lambda m: str(int(m.group())-start+start+i), merge.get('ref'))
                    ET.SubElement(merges, _tag('mergeCell'), ref=ref)
            else:
                merge.set('ref', self.shift(merge.get('ref')))
        merges.set('count', str(len(merges)))
        self._index()

    def shift(self, value):
        return re.sub(r'(\$?[A-Z]{1,3}\$?)(\d+)',
                      lambda m: m[1] + str(int(m[2])+self.delta if int(m[2]) > self.end else int(m[2])), value)

    def set(self, address, value, formula=None, style=None):
        cell = self.cells.get(address)
        if cell is None:
            rownum = int(re.search(r'\d+$', address).group())
            row = self.rows[rownum]
            cell = ET.SubElement(row, _tag('c'), r=address, s=str(style or 2))
            row[:] = sorted(row, key=lambda c: (len(re.sub(r'\d+', '', c.get('r'))), c.get('r')))
            self.cells[address] = cell
        if style is not None:
            cell.set('s', str(style))
        for child in list(cell):
            cell.remove(child)
        if isinstance(value, (int, Decimal)):
            cell.attrib.pop('t', None)
            if formula:
                ET.SubElement(cell, _tag('f')).text = formula
            ET.SubElement(cell, _tag('v')).text = str(value)
        else:
            value = str(value)
            if len(value) > 32767 or re.search('[\x00-\x08\x0b\x0c\x0e-\x1f]', value):
                raise ValueError('Текст содержит недопустимые для Excel символы или слишком длинный')
            cell.set('t', 'inlineStr')
            text = ET.SubElement(ET.SubElement(cell, _tag('is')), _tag('t'))
            text.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
            text.text = value
        return cell

    def fit(self, address, value, chars, minimum=15, line_height=14):
        cell = self.cells[address]
        key = cell.get('s', '0')
        if key not in self.style_cache:
            style = deepcopy(self.xfs[int(key)])
            alignment = style.find('s:alignment', NS)
            if alignment is None:
                alignment = ET.SubElement(style, _tag('alignment'))
            alignment.set('wrapText', '1')
            alignment.attrib.pop('shrinkToFit', None)
            style.set('applyAlignment', '1')
            self.style_cache[key] = str(len(self.xfs))
            self.xfs.append(style)
            self.xfs.set('count', str(len(self.xfs)))
        cell.set('s', self.style_cache[key])
        lines = sum(max(1, len(textwrap.wrap(part, width=chars, break_long_words=True))) for part in str(value).split('\n'))
        height = max(minimum, lines*line_height+4)
        if height > 409:
            raise ValueError('Текст слишком длинный для одной строки Excel; сократите описание или ссылку')
        row = self.rows[int(re.search(r'\d+$', address).group())]
        row.set('ht', str(max(float(row.get('ht', '0')), height)))
        row.set('customHeight', '1')

    def merge(self, ref):
        merges = self.sheet.find('s:mergeCells', NS)
        if not any(m.get('ref') == ref for m in merges):
            ET.SubElement(merges, _tag('mergeCell'), ref=ref)
            merges.set('count', str(len(merges)))

    def hyperlinks(self, rows):
        relpath = 'xl/worksheets/_rels/sheet1.xml.rels'
        relations = ET.fromstring(self.parts[relpath])
        for rel in list(relations):
            if rel.get('Type').endswith('/hyperlink'):
                relations.remove(rel)
        old = self.sheet.find('s:hyperlinks', NS)
        if old is not None:
            self.sheet.remove(old)
        links = ET.Element(_tag('hyperlinks'))
        for index, row in enumerate(rows, 14):
            target = row.get('link', '')
            # Preserve arbitrary source text, but only web URLs become clickable.
            if not re.match(r'^https?://', target, re.I):
                continue
            rid = f'workLink{index}'
            ET.SubElement(links, _tag('hyperlink'), {'ref': f'D{index}', '{'+R+'}id': rid})
            ET.SubElement(relations, '{'+P+'}Relationship', Id=rid, Type=R+'/hyperlink', Target=target, TargetMode='External')
        if len(links):
            merges = self.sheet.find('s:mergeCells', NS)
            self.sheet.insert(list(self.sheet).index(merges)+1, links)
        self.parts[relpath] = _xml(relations, self.parts[relpath])

    def finish(self, name, last_col, last_row):
        self.sheet.find('s:dimension', NS).set('ref', f'A1:{last_col}{last_row}')
        setup = self.sheet.find('s:pageSetup', NS)
        setup.set('fitToWidth', '1')
        setup.set('fitToHeight', '0')
        setup.attrib.pop('scale', None)
        self.sheet.find('s:sheetPr/s:pageSetUpPr', NS).set('fitToPage', '1')
        workbook = ET.fromstring(self.parts['xl/workbook.xml'])
        names = workbook.find('s:definedNames', NS)
        if names is None:
            names = ET.Element(_tag('definedNames'))
            workbook.insert(list(workbook).index(workbook.find('s:calcPr', NS)), names)
        area = names.find("s:definedName[@name='_xlnm.Print_Area']", NS)
        if area is None:
            area = ET.SubElement(names, _tag('definedName'), name='_xlnm.Print_Area', localSheetId='0')
        area.text = f"'{name}'!$A$1:${last_col}${last_row}"
        workbook.find('s:calcPr', NS).set('fullCalcOnLoad', '1')
        default_old, old_heights = self.old_heights
        default_new, new_heights = self._heights()
        for path in list(self.parts):
            if not re.fullmatch(r'xl/drawings/drawing\d+\.xml', path):
                continue
            drawing = ET.fromstring(self.parts[path])
            for anchor in drawing:
                origin = anchor.find('xdr:from/xdr:row', NS)
                if origin is None:
                    continue
                original_row = int(origin.text)
                shifted_row = original_row+self.delta if original_row+1 > self.end else original_row
                for marker in ('from', 'to'):
                    node = anchor.find(f'xdr:{marker}/xdr:row', NS)
                    if node is not None and int(node.text)+1 > self.end:
                        node.text = str(int(node.text)+self.delta)
                # One-cell anchors track row-height changes automatically. Keep the
                # shape transform consistent as well for non-Excel consumers.
                old_y = sum(old_heights.get(i, default_old) for i in range(1, original_row+1))
                new_y = sum(new_heights.get(i, default_new) for i in range(1, shifted_row+1))
                for offset in anchor.findall('.//a:xfrm/a:off', NS):
                    offset.set('y', str(int(offset.get('y'))+round((new_y-old_y)*12700)))
            self.parts[path] = _xml(drawing, self.parts[path])
        for path, root in (('xl/worksheets/sheet1.xml', self.sheet), ('xl/styles.xml', self.styles), ('xl/workbook.xml', workbook)):
            self.parts[path] = _xml(root, self.parts[path])
        # A template's formula dependency chain refers to its original row count.
        # Let Excel reconstruct it from the actual formulas on opening this copy.
        self.parts.pop('xl/calcChain.xml', None)
        for path, attribute, value in (
            ('xl/_rels/workbook.xml.rels', 'Type', R+'/calcChain'),
            ('[Content_Types].xml', 'PartName', '/xl/calcChain.xml'),
        ):
            root = ET.fromstring(self.parts[path])
            for node in list(root):
                if node.get(attribute) == value:
                    root.remove(node)
            self.parts[path] = _xml(root, self.parts[path])
        result = BytesIO()
        with ZipFile(result, 'w', ZIP_DEFLATED) as archive:
            for path, content in self.parts.items():
                archive.writestr(path, content)
        return result.getvalue()


def generate_documents(payload: dict, templates_dir: Path) -> tuple[bytes, bytes]:
    """Return (act_xlsx, invoice_xlsx) from a validated, immutable issue payload."""
    rows = payload['rows']
    if not rows or len(rows) > 100000:
        raise ValueError('Нужно от 1 до 100000 строк работ')
    supplier, customer = payload['settings']['supplier'], payload['settings']['customer']
    total = sum((Decimal(row['amount']) for row in rows), Decimal(0)).quantize(CENT, rounding=ROUND_HALF_UP)
    hours = sum((Decimal(row['hours']) for row in rows), Decimal(0))
    if total != Decimal(payload['total']) or hours != Decimal(payload['hours']):
        raise ValueError('Итоги не совпадают со строками работ')
    contract = f"Договор {payload['contract']['number']} от {_date(payload['contract']['date'])}"
    title = f"№ {payload['number']} от {_date(payload['date'], long=True)}"
    words = _words(total)
    act = _Template(Path(templates_dir)/'act.xlsx')
    act.replace_rows(14, 29, len(rows))
    act.set('B4', 'Акт '+title)
    for address, value, chars in (('C6', _details(supplier, bank=True), 145), ('C8', _details(customer, bank=True), 145), ('C10', contract, 130)):
        act.set(address, value)
        act.fit(address, value, chars, line_height=13)
    act.merge('C10:H10')
    for index, item in enumerate(rows, 14):
        for col, value in (('B', index-13), ('C', item['name']), ('D', item.get('link', '')),
                           ('E', Decimal(item['hours'])), ('F', 'ч'), ('G', Decimal(item['rate']))):
            act.set(f'{col}{index}', value)
        amount = (Decimal(item['hours'])*Decimal(item['rate'])).quantize(CENT, rounding=ROUND_HALF_UP)
        if amount != Decimal(item['amount']):
            raise ValueError(f'Сумма работы {index-13} не совпадает с часами и ставкой')
        act.set(f'H{index}', amount, f'ROUND(E{index}*G{index},2)')
        act.fit(f'C{index}', item['name'], 85)
        act.fit(f'D{index}', item.get('link', ''), 60)
    offset = act.delta
    act.set(f'H{31+offset}', total, f'SUM(H14:H{13+len(rows)})')
    act.set(f'B{34+offset}', f'Всего оказано услуг на сумму {_money(total)} руб.')
    act.set(f'B{35+offset}', words)
    act.fit(f'B{35+offset}', words, 165)
    act.set(f'B{42+offset}', supplier['name'])
    act.fit(f'B{42+offset}', supplier['name'], 145)
    act.set(f'B{44+offset}', supplier.get('short_name') or supplier['name'])
    act.hyperlinks(rows)

    groups = {}
    for row in rows:
        rate = Decimal(row['rate'])
        group = groups.setdefault(rate, {'hours': Decimal(0), 'amount': Decimal(0), 'amounts': []})
        group['hours'] += Decimal(row['hours'])
        group['amount'] += Decimal(row['amount'])
        group['amounts'].append(row['amount'])
    invoice = _Template(Path(templates_dir)/'invoice.xlsx')
    invoice.replace_rows(23, 23, len(groups))
    invoice.set('B11', 'Счет на оплату '+title)
    for address, key in (('J3', 'bank'), ('AO3', 'bik'), ('AO4', 'correspondent'), ('L6', 'inn'), ('Y6', 'kpp'), ('AO6', 'account'), ('J7', 'name')):
        invoice.set(address, supplier.get(key, ''))
    invoice.fit('J3', supplier['bank'], 55, line_height=12)
    invoice.fit('J7', supplier['name'], 55, line_height=12)
    for address, party in (('G15', supplier), ('G18', customer)):
        text = _details(party)
        invoice.set(address, text)
        invoice.fit(address, text, 120, line_height=13)
    invoice.merge('B20:F20')
    invoice.merge('G20:BE20')
    invoice.set('B20', 'Основание:', style=2)
    invoice.set('G20', contract, style=6)
    invoice.fit('G20', contract, 120, minimum=19, line_height=13)
    for index, (rate, group) in enumerate(groups.items(), 23):
        for col, value in (('B', index-22), ('D', payload['settings']['service_name']), ('AJ', group['hours']), ('AP', 'ч.'), ('AS', rate)):
            invoice.set(f'{col}{index}', value)
        formula = f'ROUND(AJ{index}*AS{index},2)'
        if (group['hours']*rate).quantize(CENT, rounding=ROUND_HALF_UP) != group['amount']:
            # Per-work rounding can differ from rounding all hours together.
            # Invoice must agree exactly with the accepted act in that case.
            formula = '+'.join(group['amounts'])
        invoice.set(f'AX{index}', group['amount'], formula)
        invoice.fit(f'D{index}', payload['settings']['service_name'], 90, minimum=24.75, line_height=12)
    offset = invoice.delta
    invoice.set(f'AY{25+offset}', total, f'SUM(AX23:AX{22+len(groups)})')
    invoice.set(f'AY{27+offset}', total, f'AY{25+offset}')
    invoice.set(f'B{28+offset}', f'Всего наименований {len(groups)}, на сумму {_money(total)} руб.')
    invoice.set(f'B{29+offset}', words)
    invoice.fit(f'B{29+offset}', words, 140)
    invoice.set(f'B{31+offset}', f"Оплатить не позднее {_date(payload['due_date'])}г.")
    invoice.set(f'N{38+offset}', supplier['name'])
    invoice.fit(f'N{38+offset}', supplier['name'], 110)
    return act.finish('Акт', 'H', 44+act.delta), invoice.finish('Счет', 'BF', 40+invoice.delta)
