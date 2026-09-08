import unittest
from decimal import Decimal

try:
    from invoice_maker.domain import parse_rows, totals, default_due_date, fingerprint
except ImportError:
    parse_rows = totals = default_due_date = fingerprint = None

HEADER = ['Ссылка на задачу', 'Наименование', 'Количество часов', 'Стоимость часа', 'Сумма', 'Комментарий']


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(parse_rows, 'Data parsing is not implemented')

    def test_fractional_hours_and_template_blanks(self):
        rows = parse_rows([HEADER, ['https://example.test', '  Работа  с\r пробелами ', '1,25', 2500, 3125], ['', '', '', 2500, 0]])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['name'], 'Работа с пробелами')
        self.assertEqual(rows[0]['amount'], '3125.00')
        self.assertEqual(totals(rows), ('1.25', '3125.00'))

    def test_missing_source_amount_computes_and_rounds_line(self):
        rows = parse_rows([HEADER, ['', 'Работа', 0.333, '100,01']])
        self.assertEqual(rows[0]['amount'], '33.30')

    def test_rejects_nonempty_incomplete_rows_and_formula_errors(self):
        for row in [['', 'Работа', '', 2500], ['', '', 2, 2500], ['', 'Работа', 2, '#REF!'], ['', 'Работа', 2, -1], ['', 'Работа', True, 2500], ['', 'Работа', 'NaN', 2500], ['', 'Работа', 1, 2500, '#VALUE!']]:
            with self.subTest(row=row), self.assertRaisesRegex(ValueError, 'Строка 2'):
                parse_rows([HEADER, row])

    def test_mismatched_amount_stops_generation(self):
        with self.assertRaisesRegex(ValueError, 'Строка 2'):
            parse_rows([HEADER, ['', 'Работа', 2, 2500, 4900]])

    def test_literal_formula_name_is_preserved(self):
        rows = parse_rows([HEADER, ['', '=HYPERLINK("bad")', 1, 2500, 2500]])
        self.assertEqual(rows[0]['name'], '=HYPERLINK("bad")')

    def test_empty_month_and_unexpected_headers_rejected(self):
        for values in [[], [HEADER], [HEADER, ['', '', '', 2500, 0]], [['Wrong']]]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                parse_rows(values)

    def test_due_date_clips_to_short_month(self):
        self.assertEqual(default_due_date('2026-02-03', 31), '2026-02-28')
        self.assertEqual(default_due_date('2026-10-03', 10), '2026-10-10')

    def test_source_change_alters_fingerprint(self):
        rows = parse_rows([HEADER, ['', 'Работа', 1, 2500, 2500]])
        before = fingerprint(rows)
        rows[0]['hours'] = '2'
        self.assertNotEqual(before, fingerprint(rows))

    def test_comment_only_row_is_not_silently_dropped(self):
        with self.assertRaisesRegex(ValueError, 'Строка 2'):
            parse_rows([HEADER, ['', '', '', 2500, 0, 'Нужно включить в акт']])

    def test_comment_change_invalidates_source_snapshot(self):
        before = parse_rows([HEADER, ['', 'Работа', 1, 2500, 2500, 'Первое примечание']])
        after = parse_rows([HEADER, ['', 'Работа', 1, 2500, 2500, 'Другое примечание']])
        self.assertNotEqual(fingerprint(before), fingerprint(after))


if __name__ == '__main__':
    unittest.main()
