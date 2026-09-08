"""Integration checks against the supplied native XLSX templates."""
import importlib.util
import unittest
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

TEMPLATES = Path(__file__).resolve().parents[1] / 'templates'
if not (TEMPLATES / 'act.xlsx').exists():
    TEMPLATES = TEMPLATES / 'examples'
NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
      'xdr': 'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing'}


def payload(count=16, mixed=False):
    rows = []
    for i in range(count):
        hours = Decimal('3') if i == 0 and count == 16 else Decimal('1')
        rate = Decimal('1000') if mixed and i % 2 else Decimal('2500')
        rows.append(dict(name=f'Работа {i + 1}', link=f'https://example.org/tasks/{i + 1}',
                         hours=str(hours), rate=str(rate), amount=str(hours * rate)))
    supplier = dict(name='Исполнитель Тестович', short_name='Исполнитель Т.', inn='001234567890',
                    kpp='', phone='+79990000000', account='00011122233344455566', bank='Тестовый банк',
                    bik='001234567', correspondent='00000000000000000001')
    customer = dict(name='ООО Заказчик', inn='0012345678', kpp='001234567', address='Тестовый адрес',
                    phone='+78880000000', account='00000000000000000002', bank='Другой банк',
                    bik='001234568', correspondent='00000000000000000003')
    return dict(number=9, date='2026-09-07', due_date='2026-09-15', month_title='Август 2026', rows=rows,
                hours=str(sum(Decimal(r['hours']) for r in rows)),
                total=str(sum(Decimal(r['amount']) for r in rows)),
                contract=dict(number='DEMO-2026-07', date='2026-07-01'),
                settings=dict(supplier=supplier, customer=customer, service_name='Поддержка продуктов 1С'))


class Book:
    def __init__(self, data):
        self.zip = ZipFile(BytesIO(data))
        self.sheet = ET.fromstring(self.zip.read('xl/worksheets/sheet1.xml'))
        self.strings = [''.join(n.itertext()) for n in ET.fromstring(self.zip.read('xl/sharedStrings.xml'))]
        self.cells = {c.get('r'): c for c in self.sheet.findall('.//s:c', NS)}

    def value(self, address):
        c = self.cells[address]
        if c.get('t') == 'inlineStr':
            return ''.join(c.find('s:is', NS).itertext())
        v = c.find('s:v', NS)
        if v is None:
            return None
        return self.strings[int(v.text)] if c.get('t') == 's' else Decimal(v.text)

    def text(self):
        return '\n'.join(str(self.value(a) or '') for a in self.cells)

    def formula(self, address):
        return self.cells[address].find('s:f', NS).text

    def media(self):
        return {n: self.zip.read(n) for n in self.zip.namelist() if n.startswith('xl/media/')}

    def merges(self):
        return {m.get('ref') for m in self.sheet.findall('s:mergeCells/s:mergeCell', NS)}


class DocumentsTests(unittest.TestCase):
    def generate(self, data):
        self.assertIsNotNone(importlib.util.find_spec('invoice_maker.documents'), 'Excel generation module is missing')
        from invoice_maker.documents import generate_documents
        return tuple(Book(x) for x in generate_documents(data, TEMPLATES))

    def test_actual_sample_totals_dates_and_contract(self):
        act, invoice = self.generate(payload())
        self.assertEqual(act.value('H31'), Decimal('45000'))
        self.assertEqual(invoice.value('AY27'), Decimal('45000'))
        self.assertEqual(invoice.value('AJ23'), Decimal('18'))
        self.assertEqual(act.value('B4'), 'Акт № 9 от 07 сентября 2026 г.')
        self.assertEqual(invoice.value('B11'), 'Счет на оплату № 9 от 07 сентября 2026 г.')
        for book in (act, invoice):
            self.assertIn('Сорок пять тысяч рублей 00 копеек', book.text())
            self.assertIn('Договор DEMO-2026-07 от 01.07.2026', book.text())
            self.assertIn('Исполнитель Тестович', book.text())
            self.assertIn('001234567890', book.text())
        self.assertIn('15.09.2026', invoice.text())
        self.assertIn('Налог на профессиональный доход', invoice.text())
        self.assertEqual(invoice.value('AO6'), '00011122233344455566')

    def test_dynamic_act_rows_totals_merges_media_and_print_settings(self):
        for count in (1, 4, 16, 40):
            with self.subTest(count=count):
                data = payload(count)
                act, invoice = self.generate(data)
                delta = count - 16
                self.assertEqual(act.value(f'B{13+count}'), count)
                self.assertEqual(act.value(f'H{31+delta}'), Decimal(data['total']))
                self.assertEqual(act.formula(f'H{31+delta}'), f'SUM(H14:H{13+count})')
                self.assertEqual(act.formula(f'H{13+count}'), f'ROUND(E{13+count}*G{13+count},2)')
                self.assertIn(f'B{44+delta}:D{44+delta}', act.merges())
                self.assertEqual(act.value(f'B{44+delta}'), 'Исполнитель Т.')
                drawing = ET.fromstring(act.zip.read('xl/drawings/drawing1.xml'))
                self.assertEqual(int(drawing.find('.//xdr:from/xdr:row', NS).text), 40+delta)
                links = act.sheet.findall('s:hyperlinks/s:hyperlink', NS)
                self.assertEqual(len(links), count)
                self.assertEqual(links[-1].get('ref'), f'D{13+count}')
                for name, book in (('act', act), ('invoice', invoice)):
                    self.assertEqual(book.media(), Book((TEMPLATES/f'{name}.xlsx').read_bytes()).media())
                    setup = book.sheet.find('s:pageSetup', NS)
                    self.assertEqual(setup.get('fitToWidth'), '1')
                    self.assertEqual(setup.get('fitToHeight'), '0')
                    workbook = ET.fromstring(book.zip.read('xl/workbook.xml'))
                    area = workbook.find("s:definedNames/s:definedName[@name='_xlnm.Print_Area']", NS)
                    self.assertIsNotNone(area)
                self.assertIn(f'$H${44+delta}', ET.fromstring(act.zip.read('xl/workbook.xml')).find("s:definedNames/s:definedName[@name='_xlnm.Print_Area']", NS).text)

    def test_mixed_rates_get_separate_numeric_invoice_groups(self):
        data = payload(4, True)
        _, invoice = self.generate(data)
        self.assertEqual(invoice.value('AJ23'), Decimal('2'))
        self.assertEqual(invoice.value('AS23'), Decimal('2500'))
        self.assertEqual(invoice.value('AX23'), Decimal('5000'))
        self.assertEqual(invoice.value('AJ24'), Decimal('2'))
        self.assertEqual(invoice.value('AS24'), Decimal('1000'))
        self.assertEqual(invoice.value('AX24'), Decimal('2000'))
        self.assertEqual(invoice.value('AY28'), Decimal('7000'))
        self.assertEqual(invoice.formula('AY26'), 'SUM(AX23:AX24)')
        self.assertIn('D24:AI24', invoice.merges())
        self.assertIn('N39:BE39', invoice.merges())
        drawing = ET.fromstring(invoice.zip.read('xl/drawings/drawing1.xml'))
        self.assertEqual(drawing.find('.//xdr:from/xdr:row', NS).text, '35')

    def test_long_text_grows_rows_and_formula_looking_text_is_literal(self):
        data = payload(4)
        data['rows'][0]['name'] = '=HYPERLINK("https://example.org", "name")'
        data['rows'][1]['name'] = 'Длинное описание выполненной доработки системы ' * 20
        data['rows'][1]['link'] = 'https://example.org/' + 'long-path/' * 40
        act, _ = self.generate(data)
        self.assertEqual(act.value('C14'), data['rows'][0]['name'])
        self.assertIsNone(act.cells['C14'].find('s:f', NS))
        self.assertEqual(act.value('C15'), data['rows'][1]['name'])
        row = act.sheet.find("s:sheetData/s:row[@r='15']", NS)
        self.assertGreater(float(row.get('ht')), 90)
        style = ET.fromstring(act.zip.read('xl/styles.xml')).find('s:cellXfs', NS)[int(act.cells['D15'].get('s'))]
        self.assertEqual(style.find('s:alignment', NS).get('wrapText'), '1')

    def test_kopecks_rounding_and_invalid_empty_payload(self):
        data = payload(1)
        data['rows'][0].update(hours='1.01', rate='21.01', amount='21.22')
        data.update(hours='1.01', total='21.22')
        act, invoice = self.generate(data)
        for book in (act, invoice):
            self.assertIn('Двадцать один рубль 22 копейки', book.text())
        from invoice_maker.documents import generate_documents
        data['rows'] = []
        with self.assertRaises(ValueError):
            generate_documents(data, TEMPLATES)

    def test_changed_row_count_does_not_keep_stale_calculation_chain(self):
        act, invoice = self.generate(payload(1))
        for book in (act, invoice):
            self.assertNotIn('xl/calcChain.xml', book.zip.namelist())
            rels = ET.fromstring(book.zip.read('xl/_rels/workbook.xml.rels'))
            self.assertFalse(any(n.get('Type', '').endswith('/calcChain') for n in rels))
            content_types = ET.fromstring(book.zip.read('[Content_Types].xml'))
            self.assertFalse(any(n.get('PartName') == '/xl/calcChain.xml' for n in content_types))


if __name__ == '__main__':
    unittest.main()
